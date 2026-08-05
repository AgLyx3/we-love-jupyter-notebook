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


def _prompt_lead(prompt: str) -> tuple[str, str]:
    """Split a composed prompt into the user's own words and a payload note."""
    head, delimiter, tail = prompt.partition(_ATTACHMENT_DELIMITER)
    if delimiter:
        attached = len(_ATTACHMENT_HEADING.findall(tail)) or 1
        return head.strip(), f" (+{_count(attached, 'referenced selection')})"
    head, delimiter, _ = prompt.partition(_INLINE_EDIT_DELIMITER)
    if delimiter:
        return head.strip(), " (+1 selected region)"
    if len(prompt) > MEMORY_PROMPT_CHARS:
        return prompt[:MEMORY_PROMPT_CHARS].strip(), " (truncated)"
    return prompt.strip(), ""


def _diff_body(operation: MemoryOperation) -> list[str]:
    return [
        line for line in difflib.unified_diff(
            operation.previous_source.splitlines(),
            operation.next_source.splitlines(),
            lineterm="", n=2,
        )
        if not line.startswith(("---", "+++", "@@"))
    ]


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
            lines.append(f"It edited {reference}, and the parts were reviewed separately:")
        for operation in operations:
            body = _diff_body(operation)
            described = _describe(operation, body)
            lines.append(
                f"  - {described}." if split
                else f"It edited {reference}: {described}."
            )
            prefix = "    " if split else indent
            if operation.status == "UNDONE":
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


def _render_memory(memory: tuple[MemoryEntry, ...], indexed: dict) -> list[str]:
    """Newest-first accumulation so eviction only ever drops the oldest turns."""
    blocks: list[list[str]] = []
    used = 0
    for distance, entry in enumerate(reversed(memory), start=1):
        block = _render_entry(entry, distance, indexed)
        size = len("\n".join(block).encode("utf-8"))
        if blocks and used + size > MEMORY_BUDGET_BYTES:
            break
        used += size
        blocks.append(block)
    lines = [
        "Conversation so far (this notebook session). These earlier turns are yours.",
    ]
    if len(blocks) < len(memory):
        lines.append("(earlier turns omitted)")
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
