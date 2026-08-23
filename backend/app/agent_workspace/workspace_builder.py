from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import tempfile
import time
from pathlib import Path

from ..notebook_document.models import NotebookSnapshot
from ..turn_scope.models import FrozenTurnScope
from .models import (
    AgentWorkspace, ContextCellManifest, EditableCellManifest,
    StructuralCellManifest, TrustedWorkspaceManifest, WorkspaceCleanupError,
    WorkspaceManifest,
)


logger = logging.getLogger(__name__)


def _source(cell: dict) -> str:
    value = cell.get("source", "")
    return "".join(value) if isinstance(value, list) else value


def _hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _shell_rule(file_access_via_shell: bool, *, writable: bool = True) -> str:
    """The turn's shell rule, worded for how the agent actually reaches files.

    The point of the rule is that a turn runs no arbitrary commands — no
    installs, no network, no git. An agent with dedicated file tools can be told
    that flatly. An agent whose *only* file API is its shell tool (Codex) reads
    nothing at all under the flat wording, so it gets the same prohibition
    scoped to everything except file access.

    ``writable`` narrows that scope to reading alone. A read-only turn that
    still granted "read and write" here would contradict its own opening line
    one paragraph later, which is the kind of mixed instruction an agent is
    entitled to resolve the wrong way.
    """
    if file_access_via_shell:
        access = "read and write" if writable else "read"
        return (
            f"Use your shell/exec tool only to {access} files in this "
            "workspace; run no other commands."
        )
    return "Do not run shell commands."


class AgentWorkspaceBuilder:
    def __init__(self, *, cleanup_attempts: int = 3, cleanup_delay: float = 0.05) -> None:
        self.cleanup_attempts = max(1, cleanup_attempts)
        self.cleanup_delay = max(0, cleanup_delay)

    def build(
        self, snapshot: NotebookSnapshot, scope: FrozenTurnScope,
        *, write_scope: str = "blocking", correction: str | None = None,
        file_access_via_shell: bool = False,
    ) -> AgentWorkspace:
        if write_scope == "trusted":
            return self._build_trusted(
                snapshot, scope, correction=correction,
                file_access_via_shell=file_access_via_shell,
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
            if manifest.editable_cells:
                instructions = [scope.prompt, "", *notebook_context,
                                "You have permission to edit the files listed below, but editing is",
                                "optional — permission is a grant, not a requirement. First answer the",
                                "request directly in your final message. Only change a listed file when the",
                                "request calls for a concrete edit, and explain any edit you make.",
                                "Do not modify files that are not listed.",
                                "Do not change notebook structure, metadata, outputs, or cell types.",
                                _shell_rule(file_access_via_shell), "", "Editable files:"]
                instructions.extend(f"- {item.relative_path}" for item in manifest.editable_cells)
            else:
                instructions = [scope.prompt, "", *notebook_context,
                                "This is a read-only turn. Do not modify any file.",
                                "Answer in your final message.",
                                _shell_rule(file_access_via_shell, writable=False), ""]
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
        *, correction: str | None = None, file_access_via_shell: bool = False,
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
                scope, manifest, correction, file_access_via_shell,
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
        correction: str | None, file_access_via_shell: bool = False,
    ) -> list[str]:
        lines = [
            scope.prompt,
            "",
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
            f"- {_shell_rule(file_access_via_shell)} Do not edit notebook.readonly.ipynb"
            " or INSTRUCTIONS.md.",
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
