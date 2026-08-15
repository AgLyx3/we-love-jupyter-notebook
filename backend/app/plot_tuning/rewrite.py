"""Replace a knob's literal in cell source, exactly where `ast` says it lives.

Two details make this less trivial than it looks:

* `ast` reports ``col_offset`` as a **UTF-8 byte** offset into the line, not a
  character index. A comment or string containing non-ASCII text earlier on the
  same line shifts every later character, so slicing the `str` directly silently
  corrupts the source. We slice the encoded line and decode back.
* Several knobs can share a line (``xlim = (0, 10)`` is one knob; ``a = 1; b = 2``
  is two). Edits are applied last-position-first so that earlier offsets stay
  valid as the text length changes.

After rewriting we re-parse and confirm the binding now holds the requested
value. That check is cheap and turns an off-by-one in the offset maths into a
loud failure here rather than a wrong plot three layers up.
"""

from __future__ import annotations

import ast
from typing import Any

from .discovery import Knob, strip_magics


class RewriteError(RuntimeError):
    """The rewrite did not produce source with the requested value in it."""


def render(kind: str, value: Any) -> str:
    """Render a knob value as the Python literal that will replace the old one."""
    if kind == "bool":
        return "True" if value else "False"
    if kind == "int":
        return repr(int(value))
    if kind == "float":
        return repr(float(value))
    if kind == "str":
        return repr(str(value))
    if kind in {"tuple", "list"}:
        items = ", ".join(repr(item) for item in value)
        # A one-element tuple needs its trailing comma or it is just parentheses.
        if kind == "tuple":
            return f"({items},)" if len(tuple(value)) == 1 else f"({items})"
        return f"[{items}]"
    raise RewriteError(f"No literal rendering for kind {kind!r}")


def _offset(lines: list[str], lineno: int, col_offset: int) -> int:
    """Absolute character index in the joined source for an `ast` position."""
    absolute = sum(len(line) + 1 for line in lines[: lineno - 1])
    # col_offset is a byte offset into this line; convert it to characters.
    line = lines[lineno - 1]
    prefix = line.encode("utf-8")[:col_offset].decode("utf-8", errors="strict")
    return absolute + len(prefix)


def rewrite_cell(source: str, edits: list[tuple[Knob, Any]]) -> str:
    """Apply every edit belonging to one cell and return the new source.

    Raises `RewriteError` if the result does not parse, or if any edited name
    does not read back as the value that was asked for.
    """
    if not edits:
        return source
    lines = source.split("\n")
    spans = []
    for knob, value in edits:
        start = _offset(lines, knob.lineno, knob.col_offset)
        end = _offset(lines, knob.end_lineno, knob.end_col_offset)
        spans.append((start, end, render(knob.kind, value)))

    spans.sort(key=lambda span: span[0])
    for earlier, later in zip(spans, spans[1:]):
        if earlier[1] > later[0]:
            raise RewriteError("Two knobs overlap in the same source range")

    updated = source
    # Last first, so the offsets of the edits still to come stay valid.
    for start, end, replacement in reversed(spans):
        updated = updated[:start] + replacement + updated[end:]

    _verify(updated, edits)
    return updated


def _verify(source: str, edits: list[tuple[Knob, Any]]) -> None:
    cleaned = strip_magics(source)
    if cleaned is None:
        raise RewriteError("Rewritten cell is no longer analysable")
    try:
        tree = ast.parse(cleaned)
    except SyntaxError as error:
        raise RewriteError(f"Rewritten cell does not parse: {error}") from error

    wanted = {knob.name: value for knob, value in edits}
    seen: dict[str, Any] = {}
    for statement in tree.body:
        if isinstance(statement, ast.Assign) and len(statement.targets) == 1 \
                and isinstance(statement.targets[0], ast.Name):
            name, value_node = statement.targets[0].id, statement.value
        elif isinstance(statement, ast.AnnAssign) and statement.value is not None \
                and isinstance(statement.target, ast.Name):
            name, value_node = statement.target.id, statement.value
        else:
            continue
        if name in wanted:
            try:
                seen[name] = ast.literal_eval(value_node)
            except ValueError as error:
                raise RewriteError(f"`{name}` is no longer a literal") from error

    for name, value in wanted.items():
        if name not in seen:
            raise RewriteError(f"`{name}` disappeared from the rewritten cell")
        actual = seen[name]
        if isinstance(value, tuple):
            actual = tuple(actual) if isinstance(actual, (list, tuple)) else actual
        # bool/int compare equal in Python, so guard the type as well as the value.
        if actual != value or isinstance(actual, bool) != isinstance(value, bool):
            raise RewriteError(f"`{name}` is {actual!r} after rewrite, expected {value!r}")


def rewrite_chain(
    sources: dict[str, str], edits: list[tuple[Knob, Any]],
) -> dict[str, str]:
    """Group edits by cell and rewrite each. Returns only the cells that changed."""
    by_cell: dict[str, list[tuple[Knob, Any]]] = {}
    for knob, value in edits:
        by_cell.setdefault(knob.cell_id, []).append((knob, value))
    return {
        cell_id: rewrite_cell(sources[cell_id], cell_edits)
        for cell_id, cell_edits in by_cell.items()
    }
