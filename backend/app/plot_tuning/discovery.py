"""Find the literal-assigned variables a plot cell transitively depends on.

The scan is transitive by necessity. The motivating case is::

    n_samples = 1000          # cell 2
    df = generate(n_samples)  # cell 3
    plt.hist(df.value)        # cell 4  <- the plot cell

The plot cell never mentions ``n_samples``, so resolving only the names it reads
directly would miss precisely the knob worth turning.

Everything here is conservative on purpose. Offering a knob that does not
actually control the value it claims to is this feature's worst failure mode —
it is silent, and it teaches the user to distrust the panel. Over-rejecting is
recoverable (the user edits the literal by hand, as they do today), so every
ambiguous case is rejected *with a reason*, and the reason is surfaced rather
than the name simply vanishing from the list.
"""

from __future__ import annotations

import ast
from dataclasses import dataclass
from collections.abc import Sequence
from typing import Any

#: Cell magics whose body is still Python. We drop the magic line (keeping the
#: line count intact so source ranges stay valid) and analyse the rest. Any
#: other cell magic (``%%bash``, ``%%html``, ``%%script``) means the cell is not
#: Python at all, so we cannot know what it binds and must not guess.
PYTHON_BODY_CELL_MAGICS = frozenset({"time", "timeit", "capture", "prun", "debug"})


@dataclass(frozen=True)
class ChainCell:
    """One code cell in the dependency chain, in document order."""

    cell_id: str
    index: int
    source: str


@dataclass(frozen=True)
class Knob:
    """A literal we may offer as a control *and* rewrite in place.

    The source range is what makes both halves safe, which is why discovery and
    write-back are the same problem: a value is offerable exactly when we can
    locate it precisely enough to replace it.

    ``col_offset``/``end_col_offset`` are UTF-8 **byte** offsets into their line,
    which is what `ast` reports. `rewrite` is responsible for that conversion.
    """

    name: str
    cell_id: str
    cell_index: int
    kind: str
    value: Any
    lineno: int
    col_offset: int
    end_lineno: int
    end_col_offset: int


@dataclass(frozen=True)
class Rejection:
    """A name in the dependency closure that cannot be a knob, and why.

    ``detail`` is user-facing copy. The panel's empty state shows these instead
    of "no tunable variables found", which is the difference between a feature
    that looks broken and one that is being honest.
    """

    name: str
    reason: str
    detail: str


@dataclass(frozen=True)
class Discovery:
    knobs: tuple[Knob, ...] = ()
    rejections: tuple[Rejection, ...] = ()
    #: Set when the chain could not be analysed at all. ``knobs`` is then empty.
    blocked: str | None = None


@dataclass(frozen=True)
class _Binding:
    cell: ChainCell
    #: The literal node, when this binding assigns one; otherwise None.
    value_node: ast.expr | None
    #: Names this binding's right-hand side reads, for the transitive walk.
    reads: frozenset[str]


def strip_magics(source: str) -> str | None:
    """Make a notebook cell parseable as Python, or report that it is not.

    Returns None when the cell is a non-Python cell magic. Line magics (``%foo``)
    and shell escapes (``!cmd``) become ``pass`` so that line numbers — and
    therefore every source range in the cell — are preserved exactly.
    """
    lines = source.splitlines()
    first = next((index for index, line in enumerate(lines) if line.strip()), None)
    if first is not None and lines[first].lstrip().startswith("%%"):
        magic = lines[first].lstrip()[2:].split()[0] if lines[first].lstrip()[2:].split() else ""
        if magic not in PYTHON_BODY_CELL_MAGICS:
            return None
        lines[first] = "pass"
    cleaned = []
    for line in lines:
        stripped = line.lstrip()
        if stripped.startswith("%") or stripped.startswith("!"):
            cleaned.append(" " * (len(line) - len(stripped)) + "pass")
        else:
            cleaned.append(line)
    return "\n".join(cleaned)


def parse_cell(source: str) -> ast.Module | None:
    """Parse a notebook cell, falling back to magic-stripping only if needed.

    Order matters. Stripping first would rewrite any line that merely *starts*
    with ``%`` or ``!`` — including lines inside a triple-quoted string — so a
    cell holding::

        BANNER = \"\"\"
        %complete
        \"\"\"

    would report its value as ``'\\npass\\n'`` and show the user a string their
    file does not contain. Real Python parses on the first attempt; only genuine
    magics raise, and only those get stripped.
    """
    try:
        return ast.parse(source)
    except SyntaxError:
        pass
    cleaned = strip_magics(source)
    if cleaned is None:
        return None
    try:
        return ast.parse(cleaned)
    except SyntaxError:
        return None


def _reads(node: ast.AST) -> frozenset[str]:
    return frozenset(
        child.id for child in ast.walk(node)
        if isinstance(child, ast.Name) and isinstance(child.ctx, ast.Load)
    )


def _module_level_nodes(statement: ast.stmt):
    """Walk a statement without entering function or class bodies.

    Names assigned inside a `def` are local. Treating them as module-level
    bindings would reject a perfectly good global knob just because some helper
    reused the name, and would surface a function's private locals to the user
    as things they "cannot tune".
    """
    stack = [statement]
    while stack:
        node = stack.pop()
        yield node
        if node is not statement and isinstance(
            node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef),
        ):
            continue
        stack.extend(ast.iter_child_nodes(node))


#: Methods that mutate their receiver and return nothing useful. Used only for
#: the narrow in-place-call rule below, so the list is kept to names whose
#: mutating meaning is unambiguous across the builtin containers.
IN_PLACE_METHODS = frozenset({
    "append", "extend", "insert", "update", "add", "remove", "discard",
    "clear", "sort", "setdefault", "popitem",
})


def _in_place_receiver(statement: ast.stmt) -> str | None:
    """The name a bare ``x.method(...)`` statement mutates, if it clearly does.

    ``df.fillna(FILL, inplace=True)`` changes ``df`` without producing a single
    AST store, so nothing in `_stored_names` can see it and ``FILL`` never
    becomes reachable from a plot that reads ``df``. That is everyday pandas.

    Deliberately narrow. The tempting general rule — "a bare expression
    statement is there for its side effect, so treat it as mutating" — would
    also swallow the notebook display idiom (a trailing ``df.head()`` renders a
    table) and over-connect the graph, which is how a knob that controls nothing
    gets offered. So we require an unambiguous signal: an ``inplace=True``
    keyword, or one of the container methods that exist only to mutate.
    """
    if not isinstance(statement, ast.Expr) or not isinstance(statement.value, ast.Call):
        return None
    call = statement.value
    if not isinstance(call.func, ast.Attribute):
        return None
    inplace = any(
        keyword.arg == "inplace" and isinstance(keyword.value, ast.Constant)
        and keyword.value.value is True
        for keyword in call.keywords
    )
    if not inplace and call.func.attr not in IN_PLACE_METHODS:
        return None
    base = call.func.value
    while isinstance(base, (ast.Subscript, ast.Attribute)):
        base = base.value
    return base.id if isinstance(base, ast.Name) else None


def _stored_names(statement: ast.stmt) -> frozenset[str]:
    """Every module-level name a statement binds *or mutates in place*.

    Mutation matters as much as binding. The ordinary imperative training loop::

        for _ in range(EPOCHS):
            weights[i] -= LEARNING_RATE * error

    never rebinds ``weights``, so a closure that followed assignment
    right-hand-sides alone would never learn that ``EPOCHS`` and
    ``LEARNING_RATE`` decide it. We therefore treat a subscript or attribute
    write as a dependency edge from its base name.

    Function and class bodies are not descended into — their locals are not
    module names — but the ``def``/``class`` name itself is bound.
    """
    if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
        return frozenset({statement.name})
    found: set[str] = set()
    # Module-level only: a `def` nested inside an `if` still has locals, and a
    # full `ast.walk` would promote them to module bindings.
    for child in _module_level_nodes(statement):
        if isinstance(child, ast.Name) and isinstance(child.ctx, (ast.Store, ast.Del)):
            found.add(child.id)
        elif isinstance(child, (ast.Assign, ast.AugAssign, ast.AnnAssign)):
            targets = child.targets if isinstance(child, ast.Assign) else [child.target]
            for target in targets:
                base = target
                while isinstance(base, (ast.Subscript, ast.Attribute)):
                    base = base.value
                if isinstance(base, ast.Name):
                    found.add(base.id)
        else:
            receiver = _in_place_receiver(child) if isinstance(child, ast.stmt) else None
            if receiver is not None:
                found.add(receiver)
    return frozenset(found)


def _literal(node: ast.expr) -> tuple[str, Any] | None:
    """Classify a value node as a control we know how to render and rewrite."""
    if isinstance(node, ast.Constant):
        value = node.value
        # bool before int: bool is an int subclass and wants a toggle, not a slider.
        if isinstance(value, bool):
            return "bool", value
        if isinstance(value, int):
            return "int", value
        if isinstance(value, float):
            return "float", value
        if isinstance(value, str):
            return "str", value
        return None
    if isinstance(node, (ast.Tuple, ast.List)):
        items = []
        for element in node.elts:
            if not isinstance(element, ast.Constant) or isinstance(element.value, bool):
                return None
            if not isinstance(element.value, (int, float)):
                return None
            items.append(element.value)
        if not items:
            return None
        return ("tuple" if isinstance(node, ast.Tuple) else "list"), tuple(items)
    return None


class _Disqualifier(ast.NodeVisitor):
    """Collect names bound in any way we cannot treat as a single fixed literal.

    Function and class bodies are *not* descended into for bindings: a name
    assigned inside a function is local and does not affect the module-level
    name of the same spelling. Rejecting on that would kill a perfectly good
    global knob because some helper happened to reuse the name. The one
    exception is an explicit ``global`` declaration, which does reach out.
    """

    def __init__(self) -> None:
        self.reasons: dict[str, str] = {}

    def _mark(self, name: str, reason: str) -> None:
        self.reasons.setdefault(name, reason)

    def _mark_target(self, target: ast.expr, reason: str) -> None:
        for child in ast.walk(target):
            if isinstance(child, ast.Name):
                self._mark(child.id, reason)

    def visit_AugAssign(self, node: ast.AugAssign) -> None:
        self._mark_target(node.target, "augmented")
        self.generic_visit(node)

    def visit_For(self, node: ast.For) -> None:
        self._mark_target(node.target, "loop_target")
        self.generic_visit(node)

    visit_AsyncFor = visit_For

    def visit_comprehension(self, node: ast.comprehension) -> None:
        self._mark_target(node.target, "comprehension_target")
        self.generic_visit(node)

    def visit_Delete(self, node: ast.Delete) -> None:
        # Without this, `n = 5` followed by `del n` reads as a single literal
        # assignment and would be offered as a knob for a name that no longer
        # exists by the time the plot runs.
        for target in node.targets:
            self._mark_target(target, "deleted")
        self.generic_visit(node)

    def visit_NamedExpr(self, node: ast.NamedExpr) -> None:
        self._mark_target(node.target, "walrus")
        self.generic_visit(node)

    def visit_withitem(self, node: ast.withitem) -> None:
        if node.optional_vars is not None:
            self._mark_target(node.optional_vars, "context_manager")
        self.generic_visit(node)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        for child in ast.walk(node):
            if isinstance(child, ast.Global):
                for name in child.names:
                    self._mark(name, "global_in_function")

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        for child in ast.walk(node):
            if isinstance(child, ast.Global):
                for name in child.names:
                    self._mark(name, "global_in_function")


_REJECTION_DETAIL = {
    "augmented": "`{name}` is updated with an operator like `+=`, so no single literal sets it.",
    "loop_target": "`{name}` is a loop variable, so it takes a different value each iteration.",
    "comprehension_target": "`{name}` is a comprehension variable, not a fixed value.",
    "walrus": "`{name}` is assigned inline with `:=`, which we cannot rewrite safely.",
    "context_manager": "`{name}` comes from a `with ... as` block, not a literal.",
    "global_in_function": "`{name}` is reassigned inside a function via `global`.",
    "nested": "`{name}` is assigned inside an `if`, `for`, `with` or `try` block, so its value is not fixed.",
    "unpacked": "`{name}` is assigned by unpacking several values at once, not by its own literal.",
    "mutated": "`{name}` is changed in place (for example `{name}[...] = ...`), so no single literal sets it.",
    "deleted": "`{name}` is removed with `del` before this cell runs.",
    "rebound": "`{name}` is assigned more than once before this cell, so one literal does not decide it.",
    "not_literal": "`{name}` is computed rather than set to a fixed value.",
    "unsupported_type": "`{name}` is set to a value type the panel has no control for.",
}


def _collect(cells: tuple[ChainCell, ...]) -> tuple[
    dict[str, list[_Binding]], dict[str, str], set[str], str | None,
]:
    """Scan the chain for top-level literal bindings, disqualifiers and code names."""
    bindings: dict[str, list[_Binding]] = {}
    disqualified: dict[str, str] = {}
    #: Names bound to a function, class or lambda. They are never candidate
    #: knobs, so reporting "`sigmoid` is computed rather than set to a fixed
    #: value" would be both true and useless — it buries the rejections that
    #: describe something the user might actually have wanted to turn.
    code_names: set[str] = set()
    for cell in cells:
        tree = parse_cell(cell.source)
        if tree is None:
            if strip_magics(cell.source) is None:
                return {}, {}, set(), (
                    f"Cell {cell.index + 1} runs a non-Python cell magic, so the "
                    "panel cannot tell what it assigns."
                )
            return {}, {}, set(), f"Cell {cell.index + 1} could not be parsed as Python."

        disqualifier = _Disqualifier()
        disqualifier.visit(tree)
        for name, reason in disqualifier.reasons.items():
            disqualified.setdefault(name, reason)

        # Only a statement at the very top of the module body that assigns one
        # plain name a literal is a candidate knob. Anything nested inside a
        # compound statement is conditional or repeated, so a single literal no
        # longer decides the value.
        for statement in tree.body:
            targets: list[ast.expr] | None = None
            value: ast.expr | None = None
            if isinstance(statement, ast.Assign):
                targets, value = statement.targets, statement.value
            elif isinstance(statement, ast.AnnAssign) and statement.value is not None:
                targets, value = [statement.target], statement.value

            if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                code_names.add(statement.name)

            if targets is not None and value is not None \
                    and len(targets) == 1 and isinstance(targets[0], ast.Name):
                if isinstance(value, ast.Lambda):
                    code_names.add(targets[0].id)
                bindings.setdefault(targets[0].id, []).append(
                    _Binding(cell=cell, value_node=value, reads=_reads(value)),
                )
                continue

            # Everything else still records a dependency-only edge. Rejecting a
            # name as a *knob* must never sever the *graph*: `train, test =
            # scaled[:n], scaled[n:]` is unrewritable, but dropping it here
            # would hide every literal reachable through it.
            if targets is not None:
                for target in targets:
                    # `df["col"] = ...` and `obj.attr = ...` change a name in
                    # place. Reporting that as unpacking would be flatly untrue,
                    # and it is one of the most common lines in a real notebook.
                    if isinstance(target, (ast.Subscript, ast.Attribute)):
                        base = target
                        while isinstance(base, (ast.Subscript, ast.Attribute)):
                            base = base.value
                        if isinstance(base, ast.Name):
                            disqualified.setdefault(base.id, "mutated")
                        continue
                    for child in ast.walk(target):
                        if isinstance(child, ast.Name):
                            disqualified.setdefault(child.id, "unpacked")
            elif not isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef,
                                            ast.ClassDef)):
                for child in _module_level_nodes(statement):
                    if isinstance(child, (ast.Assign, ast.AnnAssign)):
                        nested = child.targets if isinstance(child, ast.Assign) else [child.target]
                        for target in nested:
                            for leaf in ast.walk(target):
                                if isinstance(leaf, ast.Name):
                                    disqualified.setdefault(leaf.id, "nested")

            reads = _reads(statement)
            for name in _stored_names(statement):
                bindings.setdefault(name, []).append(
                    _Binding(cell=cell, value_node=None, reads=reads),
                )
    return bindings, disqualified, code_names, None


def _live_bindings(name: str, items: Sequence[_Binding]) -> list[_Binding]:
    """Drop the bindings a later rebinding overwrote.

    Following *every* binding of a name leaks dependencies out of dead code.
    A plot cell that opens with its own ``fig, ax = plt.subplots()`` would
    inherit whatever an earlier cell's ``fig, ax = plt.subplots(figsize=FIGSIZE)``
    read, and ``FIGSIZE`` would be offered as a knob for a cell that does not use
    it — drag it, wait for the re-run, watch nothing change. That is the failure
    this module exists to avoid, so the dead binding has to go.

    A later binding kills the earlier one only when it does not read the name
    itself. ``data = clean(data, THRESH)`` accumulates rather than replaces, so
    everything the first ``data = load(N)`` depended on is still live and ``N``
    stays reachable.

    This governs the dependency walk only, not knob eligibility: a name assigned
    twice is still rejected as `rebound`, because deciding *which* literal the
    user meant to turn is a different question from which values reach the plot.
    """
    live = []
    for index, binding in enumerate(items):
        if any(name not in later.reads for later in items[index + 1:]):
            continue
        live.append(binding)
    return live


def discover(cells: tuple[ChainCell, ...], target_cell_id: str) -> Discovery:
    """Find the knobs the cell ``target_cell_id`` transitively depends on.

    ``cells`` must be the code cells up to and including the target, in document
    order. The target's own top-level literals count: ``bins = 30`` written just
    above the plot call is as tunable as one three cells back.
    """
    target = next((cell for cell in cells if cell.cell_id == target_cell_id), None)
    if target is None:
        return Discovery(blocked="The plot cell is not in the chain.")

    bindings, disqualified, code_names, blocked = _collect(cells)
    if blocked is not None:
        return Discovery(blocked=blocked)

    target_tree = parse_cell(target.source)
    if target_tree is None:
        if strip_magics(target.source) is None:
            return Discovery(blocked="The plot cell runs a non-Python cell magic.")
        return Discovery(blocked="The plot cell could not be parsed as Python.")

    # Transitive closure: start from what the plot cell reads, then follow each
    # binding's right-hand side. This is the step that reaches `n_samples`
    # through `df`.
    frontier = set(_reads(target_tree))
    closure: set[str] = set()
    while frontier:
        name = frontier.pop()
        if name in closure:
            continue
        closure.add(name)
        for binding in _live_bindings(name, bindings.get(name, ())):
            frontier |= binding.reads - closure

    knobs: list[Knob] = []
    rejections: list[Rejection] = []
    for name in sorted(closure):
        found = bindings.get(name, ())
        if name in code_names:
            # A function, class or lambda. Never a knob, never worth explaining.
            continue
        if not found and name not in disqualified:
            # Builtins, imported modules, kernel state. Not a rejection — saying
            # "`plt` cannot be tuned" would bury the real reasons in noise.
            #
            # A disqualified name is different: it *is* bound somewhere in the
            # chain, just not in a way we can rewrite, so it must be explained
            # rather than dropped. Loop targets and `with ... as` names have no
            # top-level binding at all, so testing `found` alone would silently
            # omit exactly the cases the user is most likely to go looking for.
            continue
        # Dependency-only edges (recorded so that mutation and rejected names
        # still connect the graph) are not assignments and must not be counted
        # as rebinds.
        #
        # Defensive, and deliberately so: no current input reaches it, because
        # every module-level store that is not a simple assignment is already
        # disqualified by another rule (loop_target, context_manager, mutated,
        # nested, walrus, deleted). That is an unstated invariant between two
        # separate passes, so a future `_stored_names` source added without a
        # matching disqualifier would otherwise surface as a bogus "assigned
        # more than once" message. `deleted` above was exactly that gap.
        assignments = [item for item in found if item.value_node is not None]
        if name in disqualified:
            reason = disqualified[name]
        elif len(assignments) > 1:
            reason = "rebound"
        else:
            node = assignments[0].value_node if assignments else None
            classified = _literal(node) if node is not None else None
            if classified is None:
                reason = (
                    "unsupported_type"
                    if isinstance(node, ast.Constant)
                    else "not_literal"
                )
            else:
                kind, value = classified
                binding = assignments[0]
                knobs.append(Knob(
                    name=name, cell_id=binding.cell.cell_id,
                    cell_index=binding.cell.index, kind=kind, value=value,
                    lineno=node.lineno, col_offset=node.col_offset,
                    end_lineno=node.end_lineno or node.lineno,
                    end_col_offset=node.end_col_offset or node.col_offset,
                ))
                continue
        rejections.append(Rejection(
            name=name, reason=reason,
            detail=_REJECTION_DETAIL[reason].format(name=name),
        ))

    knobs.sort(key=lambda knob: (knob.cell_index, knob.lineno, knob.col_offset))
    return Discovery(knobs=tuple(knobs), rejections=tuple(rejections))
