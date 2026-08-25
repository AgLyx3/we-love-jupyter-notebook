from __future__ import annotations

import difflib
import hashlib
import json
import logging
import os
import re
import shutil
import tempfile
import time
from pathlib import Path

from ..agent_turns.operations import split_lines
from ..notebook_document.models import NotebookSnapshot
from ..turn_scope.models import FrozenTurnScope
from .models import (
    AgentWorkspace, ContextCellManifest, EditableCellManifest, MemoryEntry,
    MemoryOperation, StructuralCellManifest, TrustedWorkspaceManifest,
    WorkspaceCleanupError, WorkspaceManifest,
)


logger = logging.getLogger(__name__)

MEMORY_BUDGET_BYTES = 16 * 1024
MEMORY_DIFF_LINES = 60
MEMORY_REPLY_CHARS = 500
# A plan is the entire content of a plan-mode turn; 500 chars mangles it.
MEMORY_PLAN_REPLY_CHARS = 1500
MEMORY_PROMPT_CHARS = 1000
# Prompts are composed on the frontend (selectionEdit.ts). Both composers use a
# fixed delimiter before the quoted payload, so the lead splits off exactly —
# no heuristic truncation, and an attached traceback never replays into a later
# turn.
_ATTACHMENT_DELIMITER = "\nReferenced selections:\n"
_INLINE_EDIT_DELIMITER = "\nApply this change to the selected region"
_ATTACHMENT_HEADING = re.compile(r"^Cell \d+ \(.*\):$", re.MULTILINE)


def _source(cell: dict) -> str:
    value = cell.get("source", "")
    return "".join(value) if isinstance(value, list) else value


def _hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _count(quantity: int, noun: str) -> str:
    return f"{quantity} {noun}" if quantity == 1 else f"{quantity} {noun}s"


def _capped(text: str) -> tuple[str, str]:
    """Bound a lead to MEMORY_PROMPT_CHARS, with a note when it was cut.

    Applied to every branch, not just the no-delimiter one. Nothing limits a
    prompt's length at the API, and a composed prompt splits into a lead of
    whatever the user typed plus a payload — so a long typed instruction with a
    selection attached escaped the cap entirely. An oversized lead now costs the
    turn it belongs to: the feed budget truncates the newest block, and a lead
    big enough leaves nothing of that turn but the omission notice.
    """
    text = text.strip()
    if len(text) > MEMORY_PROMPT_CHARS:
        return text[:MEMORY_PROMPT_CHARS].rstrip(), " (truncated)"
    return text, ""


def _prompt_lead(prompt: str) -> tuple[str, str]:
    """Split a composed prompt into the user's own words and a payload note."""
    head, delimiter, tail = prompt.partition(_ATTACHMENT_DELIMITER)
    if delimiter:
        attached = len(_ATTACHMENT_HEADING.findall(tail)) or 1
        lead, cut = _capped(head)
        return lead, f"{cut} (+{_count(attached, 'referenced selection')})"
    head, delimiter, _ = prompt.partition(_INLINE_EDIT_DELIMITER)
    if delimiter:
        lead, cut = _capped(head)
        return lead, f"{cut} (+1 selected region)"
    return _capped(prompt)


def _diff_body(operation: MemoryOperation) -> list[str]:
    """The diff rows only, with unified_diff's own headers removed.

    The two file headers are dropped by position, not by prefix. A removed line
    is rendered "-" + itself, so a source line of "---" arrives as "----" and a
    prefix filter eats it — as does "--" for a SQL comment or YAML front matter,
    and "++" on the added side. For an UNDONE operation this diff is the only
    surviving copy of the content, so a silently dropped row is content loss,
    and it reads as "nothing changed here" rather than as an error.

    "@@" stays a prefix test: a content line only ever reaches here with a
    leading "-", "+" or " ", so it cannot collide with a hunk header.

    Split with ``split("\n")``, matching ``operations.split_lines``, so the rows
    describe the same lines the ledger reviewed. ``str.splitlines`` disagrees
    with it twice over: it drops the empty final element, so a hunk that only
    adds or removes a trailing newline renders as no rows at all — silent
    content loss on an UNDONE operation, which is the one place the diff is the
    only surviving copy — and it also splits on \x0b, \x0c and \u2028, which
    would emit rows matching no line of the cell.
    """
    rows = difflib.unified_diff(
        split_lines(operation.previous_source),
        split_lines(operation.next_source),
        lineterm="", n=2,
    )
    return [line for line in list(rows)[2:] if not line.startswith("@@")]


def _describe(operation: MemoryOperation, body: list[str]) -> str:
    """Describe an edit by what the diff actually changed, not by cell size."""
    if not operation.previous_source.splitlines():
        return f"wrote {_count(len(operation.next_source.splitlines()), 'line')}"
    if not operation.next_source.splitlines():
        return f"cleared {_count(len(operation.previous_source.splitlines()), 'line')}"
    removed = sum(1 for line in body if line.startswith("-"))
    added = sum(1 for line in body if line.startswith("+"))
    if removed == added == 1:
        return "changed 1 line"
    if not removed:
        return f"added {_count(added, 'line')}"
    if not added:
        return f"removed {_count(removed, 'line')}"
    return f"replaced {_count(removed, 'line')} with {_count(added, 'line')}"


def _diff_lines(body: list[str]) -> list[str]:
    # Split the marker off the content so +/- lines align with context lines.
    lines = [f"    {line[0]} {line[1:]}" for line in body[:MEMORY_DIFF_LINES]]
    if len(body) > MEMORY_DIFF_LINES:
        dropped = len(body) - MEMORY_DIFF_LINES
        lines.append(f"    ... ({_count(dropped, 'more diff line')} omitted)")
    return lines


# kind -> (phrase under a shared cell header, standalone phrase, status lead).
# The standalone phrase takes {reference}; "delete" deliberately does not use it,
# because the cell is gone and cannot be named by its position any more.
_STRUCTURAL_PHRASES = {
    "delete": (
        "deleted this cell",
        "deleted a cell (id {cell_id})",
        "DELETED. Its content is not recorded here.",
    ),
    "move": (
        "moved this cell",
        "moved {reference} to a different position",
        "MOVED.",
    ),
    "retype": (
        "changed this cell's type",
        "changed the type of {reference}",
        "RETYPED.",
    ),
}


def _cell_reference(cell_id: str, indexed: dict) -> str:
    found = indexed.get(cell_id)
    if found is None:
        # The diff is the only surviving record of a cell that no longer exists.
        return f"a since-deleted cell (id {cell_id})"
    return f"cell {found[0]} (id {cell_id})"


def _by_cell(
    operations: tuple[MemoryOperation, ...],
) -> list[tuple[str, list[MemoryOperation]]]:
    """Group by cell, preserving first-appearance order."""
    grouped: dict[str, list[MemoryOperation]] = {}
    for operation in operations:
        grouped.setdefault(operation.cell_id, []).append(operation)
    return list(grouped.items())


def _render_entry(entry: MemoryEntry, distance: int, indexed: dict) -> list[str]:
    lead, note = _prompt_lead(entry.prompt)
    lines = [
        f"--- {_count(distance, 'turn')} ago ---",
        f'You asked: "{lead}"{note}',
    ]
    indent = "  " if len(entry.operations) > 1 else ""
    # One header per cell, then a status line per operation under it. A cell
    # reviewed hunk by hunk produces two operations with opposing outcomes;
    # repeating "It edited cell 2" above each reads like two separate edits.
    for cell_id, operations in _by_cell(entry.operations):
        reference = _cell_reference(cell_id, indexed)
        split = len(operations) > 1
        if split:
            # "reviewed separately" is only true of parts that carry a review
            # outcome. A move or a retype carries none, so a cell that was both
            # edited and moved would get a header its own STATUS lines
            # contradict.
            reviewed_parts = all(
                operation.kind not in _STRUCTURAL_PHRASES
                for operation in operations
            )
            lines.append(
                f"It edited {reference}, and the parts were reviewed separately:"
                if reviewed_parts else f"It changed {reference}:"
            )
        for operation in operations:
            is_add = operation.kind == "add"
            # delete/move/retype carry no source pair — see MemoryOperation —
            # so they are stated, never diffed. A deleted cell also cannot be
            # named by position: it is gone, and _cell_reference would render
            # "It deleted a since-deleted cell".
            structural = _STRUCTURAL_PHRASES.get(operation.kind)
            body = [] if is_add or structural else _diff_body(operation)
            described = (
                "added this whole cell" if is_add
                else structural[0] if structural
                else _describe(operation, body)
            )
            lines.append(
                f"  - {described}." if split
                else f"It added {reference} as a new cell." if is_add
                else f"It {structural[1].format(reference=reference, cell_id=cell_id)}."
                if structural
                else f"It edited {reference}: {described}."
            )
            prefix = "    " if split else indent
            if structural:
                lines.append(
                    f"{prefix}STATUS: UNDONE by the user — this was reversed and "
                    "the notebook no longer reflects it."
                    if operation.status == "UNDONE" else
                    f"{prefix}STATUS: {structural[2]} notebook.ipynb is the "
                    "current structure; read it there."
                )
            elif is_add and operation.status == "UNDONE":
                # The ledger stores a hash of the added source, not the source,
                # so unlike an undone edit there is no diff to show. Say that
                # outright rather than leaving the agent to assume the cell is
                # still there.
                lines.append(
                    f"{prefix}STATUS: UNDONE by the user — the cell was removed. "
                    "Its content is not recorded here. Do not add it back unless "
                    "the request asks for it again."
                )
            elif operation.status == "UNDONE":
                lines.append(
                    f"{prefix}STATUS: UNDONE by the user. This code is NOT in the "
                    "notebook. Do not re-propose it."
                )
                lines.extend(_diff_lines(body))
            elif operation.status == "KEPT":
                lines.append(
                    f"{prefix}STATUS: KEPT. The result is in notebook.ipynb; read it there."
                )
            elif operation.status == "APPLIED":
                lines.append(
                    f"{prefix}STATUS: APPLIED but not yet reviewed by the user. The "
                    "result is in notebook.ipynb; read it there."
                )
            if operation.stale:
                # Said after the status, not instead of it: the outcome above is
                # still what the user chose. This only warns that the cell has
                # moved on since, so the account above no longer describes it.
                #
                # Deliberately says nothing about *who* changed it. Staleness is
                # derived by comparing the cell against the ledger, and several
                # writers trip it: a hand edit in the tab, an MCP client calling
                # set_cell_source, the plot-tuning panel writing back a tuned
                # literal. Naming the user would report a machine's edit as a
                # person's, and the agent might act on that fiction.
                lines.append(
                    f"{prefix}STALE: this cell has changed since, outside this "
                    "turn's record. notebook.ipynb is the only reliable source "
                    "for it."
                )
    if entry.turn_status == "CANCELLED" and entry.operations:
        lines.append(
            "STATUS: CANCELLED. Whether these changes are in the notebook is not"
        )
        lines.append("recorded here — read notebook.ipynb for current source.")
    elif entry.turn_status:
        lines.append(f"STATUS: {entry.turn_status}.")
    if not entry.operations:
        limit = (
            MEMORY_PLAN_REPLY_CHARS if entry.mode == "plan" else MEMORY_REPLY_CHARS
        )
        reply = entry.reply.strip()
        if len(reply) > limit:
            reply = reply[:limit].rstrip() + "..."
        lines.append(
            f'It made no changes. It replied: "{reply}"' if reply
            else "It made no changes."
        )
    return lines


def _truncate_block(block: list[str], budget: int) -> list[str]:
    """Cut a single turn's block down to `budget` bytes, oldest lines last.

    The newest turn is admitted whatever its size — a feed that drops the turn
    that just happened is worse than one that abbreviates it — so without this
    the budget bounds nothing. One turn can carry an entry per edited cell, each
    with up to MEMORY_DIFF_LINES rows of source whose line length has no bound:
    twenty undone cells of long lines render half a megabyte against a 16 KiB
    budget, and all of it lands in the next turn's INSTRUCTIONS.md.
    """
    kept: list[str] = []
    used = 0
    for line in block:
        size = len(line.encode("utf-8")) + 1
        if used + size > budget:
            kept.append("(rest of this turn omitted — too large for the feed)")
            break
        used += size
        kept.append(line)
    return kept


_MEMORY_HEADING = (
    "Conversation so far (this notebook session). These earlier turns are yours."
)
_OMITTED_NOTE = "(earlier turns omitted)"
# What _render_memory spends outside the turn blocks: the heading, the note it
# may add, and their newlines. Reserved unconditionally — over-reserving by the
# note's length on a feed that does not need it is worth a budget that holds in
# every case rather than most.
_MEMORY_WRAPPER_BYTES = len(_MEMORY_HEADING) + len(_OMITTED_NOTE) + 2


def _render_memory(memory: tuple[MemoryEntry, ...], indexed: dict) -> list[str]:
    """Newest-first accumulation so eviction only ever drops the oldest turns."""
    blocks: list[list[str]] = []
    budget = MEMORY_BUDGET_BYTES - _MEMORY_WRAPPER_BYTES
    used = 0
    for distance, entry in enumerate(reversed(memory), start=1):
        block = _render_entry(entry, distance, indexed)
        # +1 for the blank line this block is separated by.
        size = len("\n".join(block).encode("utf-8")) + 1
        if blocks and used + size > budget:
            break
        if not blocks and size > budget:
            block = _truncate_block(block, budget - 1)
            size = len("\n".join(block).encode("utf-8")) + 1
        used += size
        blocks.append(block)
    lines = [_MEMORY_HEADING]
    if len(blocks) < len(memory):
        lines.append(_OMITTED_NOTE)
    for block in reversed(blocks):
        lines.append("")
        lines.extend(block)
    return lines


class AgentWorkspaceBuilder:
    def __init__(self, *, cleanup_attempts: int = 3, cleanup_delay: float = 0.05) -> None:
        self.cleanup_attempts = max(1, cleanup_attempts)
        self.cleanup_delay = max(0, cleanup_delay)

    def build(
        self, snapshot: NotebookSnapshot, scope: FrozenTurnScope,
        *, write_scope: str = "blocking", correction: str | None = None,
        memory: tuple[MemoryEntry, ...] = (),
    ) -> AgentWorkspace:
        if write_scope == "trusted":
            return self._build_trusted(
                snapshot, scope, correction=correction, memory=memory
            )
        root = Path(tempfile.mkdtemp(prefix=f"notebook-turn-{scope.turn_id[:8]}-"))
        try:
            editable_dir = root / "editable"
            editable_dir.mkdir()
            indexed = {cell["id"]: (index, cell) for index, cell in enumerate(snapshot.notebook["cells"])}
            editable = []
            for cell_id in scope.editable_cell_ids:
                index, cell = indexed[cell_id]
                suffix = ".py" if cell["cell_type"] == "code" else ".md"
                relative = f"editable/cell_{cell_id}{suffix}"
                source = _source(cell)
                (root / relative).write_text(source, encoding="utf-8")
                editable.append(EditableCellManifest(cell_id, index, cell["cell_type"], relative, source))
            context = tuple(
                ContextCellManifest(
                    cell_id, indexed[cell_id][0], indexed[cell_id][1]["cell_type"],
                    _source(indexed[cell_id][1]).splitlines()[0][:160]
                    if _source(indexed[cell_id][1]).splitlines() else "",
                )
                for cell_id in scope.context_cell_ids
            )
            manifest = WorkspaceManifest("notebook.ipynb", tuple(editable), context)
            notebook_path = root / "notebook.ipynb"
            notebook_path.write_text(json.dumps(snapshot.notebook, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
            manifest_copy = {
                "notebookPath": manifest.notebook_path,
                "editableCells": [
                    {"cellId": item.cell_id, "index": item.index, "type": item.cell_type, "path": item.relative_path}
                    for item in manifest.editable_cells
                ],
                "contextCells": [
                    {"cellId": item.cell_id, "index": item.index, "type": item.cell_type}
                    for item in manifest.context_cells
                ],
            }
            (root / "AGENT_CELL_MANIFEST.json").write_text(json.dumps(manifest_copy, indent=2) + "\n", encoding="utf-8")
            # Reasoning context every turn gets: this is a Jupyter notebook, so an
            # undefined-name error is usually an out-of-order execution problem
            # (the name is defined in another cell that has not run yet), not a
            # missing definition. Point the agent at the whole-notebook copy so it
            # diagnoses the error against the entire notebook, not just one cell.
            notebook_context = [
                "Notebook context:",
                "- This is a Jupyter notebook. Every cell runs against one shared kernel",
                "  namespace, and cells may be executed out of order or not at all, so a",
                "  name can be defined in a different cell than the one that uses it.",
                "- The whole notebook is available read-only as notebook.ipynb. Read it to",
                "  see the other cells before deciding what an error means. A code cell that",
                "  has not been run yet has \"execution_count\": null in that file.",
                "- For a NameError, \"name '...' is not defined\", or a missing import, search",
                "  the whole notebook for the cell whose source defines that name or import.",
                "  If such a cell exists but has not been run yet (execution_count null), the",
                "  real fix is to run that earlier cell. Only add a definition when the name",
                "  appears nowhere else in the notebook.",
                "- You may only edit the cells listed below; other cells are not part of this",
                "  turn and cannot be edited. If the fix belongs in a different cell, describe",
                "  it in your final message rather than redefining anything here.",
                "",
            ]
            # The conversation belongs next to the request that continues it.
            # Empty memory leaves the file byte-identical to a turn without it.
            preamble = [scope.prompt, ""]
            if memory:
                preamble.extend(_render_memory(memory, indexed))
                preamble.append("")
            if manifest.editable_cells:
                instructions = [*preamble, *notebook_context,
                                "You have permission to edit the files listed below, but editing is",
                                "optional — permission is a grant, not a requirement. First answer the",
                                "request directly in your final message. Only change a listed file when the",
                                "request calls for a concrete edit, and explain any edit you make.",
                                "Do not modify files that are not listed.",
                                "Do not change notebook structure, metadata, outputs, or cell types.",
                                "Do not run shell commands.", "", "Editable files:"]
                instructions.extend(f"- {item.relative_path}" for item in manifest.editable_cells)
            else:
                instructions = [*preamble, *notebook_context,
                                "This is a read-only turn. Do not modify any file.",
                                "Answer in your final message.",
                                "Do not run shell commands.", ""]
            if context:
                instructions.extend([
                    "",
                    "Focus cells — most relevant to this request (read-only unless also "
                    "listed as editable above):",
                ])
                instructions.extend(
                    f"- {item.cell_id} (cell {item.index}, {item.cell_type}): {item.preview}"
                    for item in context
                )
            if correction:
                instructions.extend(["", "Previous boundary violation to correct:", correction])
            (root / "INSTRUCTIONS.md").write_text("\n".join(instructions) + "\n", encoding="utf-8")
            protected = ["notebook.ipynb", "AGENT_CELL_MANIFEST.json", "INSTRUCTIONS.md"]
            baseline = {name: _hash(root / name) for name in protected}
            os.chmod(notebook_path, 0o444)
            return AgentWorkspace(root=root, manifest=manifest, baseline_hashes=baseline)
        except BaseException as error:
            try:
                self._remove_root(root)
            except WorkspaceCleanupError as cleanup_error:
                logger.exception(
                    "Failed to remove partially built agent workspace %s", root,
                )
                error.add_note(str(cleanup_error))
            raise

    def _build_trusted(
        self, snapshot: NotebookSnapshot, scope: FrozenTurnScope,
        *, correction: str | None = None,
        memory: tuple[MemoryEntry, ...] = (),
    ) -> AgentWorkspace:
        """Trusted turn: every cell is writable; structure.json is agent-owned.

        The whole notebook is materialized under ``cells/`` and the ordered
        ``structure.json`` is left agent-writable (NOT protected). Only
        ``notebook.readonly.ipynb`` and ``INSTRUCTIONS.md`` are protected. The
        returned manifest is the frozen original the backend diffs against — the
        on-disk structure file is never trusted for the original order.
        """
        root = Path(tempfile.mkdtemp(prefix=f"notebook-turn-{scope.turn_id[:8]}-"))
        try:
            cells_dir = root / "cells"
            cells_dir.mkdir()
            cells: list[StructuralCellManifest] = []
            structure_entries: list[dict] = []
            for index, cell in enumerate(snapshot.notebook["cells"]):
                cell_id = cell["id"]
                suffix = ".py" if cell["cell_type"] == "code" else ".md"
                relative = f"cells/cell_{cell_id}{suffix}"
                source = _source(cell)
                (root / relative).write_text(source, encoding="utf-8")
                cells.append(
                    StructuralCellManifest(cell_id, index, cell["cell_type"], relative, source)
                )
                structure_entries.append(
                    {"cellId": cell_id, "cellType": cell["cell_type"], "source": relative}
                )
            manifest = TrustedWorkspaceManifest(
                notebook_path="notebook.readonly.ipynb",
                structure_path="structure.json",
                cells=tuple(cells),
                # In a Trusted turn editable and context collapse into a single
                # attention hint (the whole notebook is editable regardless), so
                # both sets are forwarded as attention, editable first, deduped.
                context_cell_ids=tuple(
                    dict.fromkeys((*scope.editable_cell_ids, *scope.context_cell_ids))
                ),
            )
            # Agent-writable ordered structure. The agent edits this to
            # add/delete/reorder/retype; the backend re-derives ops from it.
            (root / "structure.json").write_text(
                json.dumps({"cells": structure_entries}, indent=2) + "\n", encoding="utf-8"
            )
            notebook_path = root / "notebook.readonly.ipynb"
            notebook_path.write_text(
                json.dumps(snapshot.notebook, ensure_ascii=False, indent=1) + "\n",
                encoding="utf-8",
            )
            instructions = self._trusted_instructions(
                scope, manifest, correction,
                memory=memory,
                indexed={
                    cell["id"]: (index, cell)
                    for index, cell in enumerate(snapshot.notebook["cells"])
                },
            )
            (root / "INSTRUCTIONS.md").write_text("\n".join(instructions) + "\n", encoding="utf-8")
            protected = ["notebook.readonly.ipynb", "INSTRUCTIONS.md"]
            baseline = {name: _hash(root / name) for name in protected}
            os.chmod(notebook_path, 0o444)
            return AgentWorkspace(root=root, manifest=manifest, baseline_hashes=baseline)
        except BaseException as error:
            try:
                self._remove_root(root)
            except WorkspaceCleanupError as cleanup_error:
                logger.exception(
                    "Failed to remove partially built trusted workspace %s", root,
                )
                error.add_note(str(cleanup_error))
            raise

    @staticmethod
    def _trusted_instructions(
        scope: FrozenTurnScope, manifest: TrustedWorkspaceManifest,
        correction: str | None, *,
        memory: tuple[MemoryEntry, ...] = (), indexed: dict | None = None,
    ) -> list[str]:
        lines = [
            scope.prompt,
            "",
        ]
        # Same placement as the Blocking path: the conversation belongs next to
        # the request that continues it. A Trusted turn is the *most* likely to
        # want the thread — it can restructure anything, so "you already tried
        # that and I undid it" is what keeps it from doing so again.
        if memory:
            lines.extend(_render_memory(memory, indexed or {}))
            lines.append("")
        lines += [
            "Trusted turn: the WHOLE notebook is editable.",
            "- Every cell has a writable source file under cells/. The read-only whole",
            "  notebook is notebook.readonly.ipynb (do not edit it).",
            "- structure.json is the ordered list of cells. Edit it to change structure:",
            "  * Edit source: change the referenced file under cells/.",
            '  * Add a cell: insert an entry {"op": "add", "cellType": "code"|"markdown"|"raw",',
            '    "source": "cells/<new-file>"} with NO cellId, and create that source file.',
            "  * Delete a cell: remove its entry from the list.",
            "  * Reorder: change entry order in the list.",
            '  * Change type: change an existing entry\'s "cellType".',
            "- Editing is optional — permission is a grant, not a requirement. Answer the",
            "  request in your final message; only change files when the request calls for it.",
            "- Do not run shell commands. Do not edit notebook.readonly.ipynb or INSTRUCTIONS.md.",
            "- Each entry's source must be a distinct file directly under cells/.",
            "",
        ]
        if manifest.context_cell_ids:
            lines.append(
                "Focus cells — most relevant to this request (you may edit any cell): "
                + ", ".join(manifest.context_cell_ids)
            )
            lines.append("")
        if correction:
            lines.extend(["Previous structure.json error to correct:", correction, ""])
        return lines

    def destroy(self, workspace: AgentWorkspace) -> None:
        self._remove_root(workspace.root)

    def _remove_root(self, root: Path) -> None:
        last_error: OSError | None = None
        for attempt in range(1, self.cleanup_attempts + 1):
            try:
                shutil.rmtree(root)
                return
            except FileNotFoundError:
                return
            except OSError as error:
                last_error = error
                if attempt < self.cleanup_attempts and self.cleanup_delay:
                    time.sleep(self.cleanup_delay)
        assert last_error is not None
        raise WorkspaceCleanupError(root, self.cleanup_attempts, last_error)
