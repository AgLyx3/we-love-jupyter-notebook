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
    WorkspaceCleanupError, WorkspaceManifest,
)


logger = logging.getLogger(__name__)


def _source(cell: dict) -> str:
    value = cell.get("source", "")
    return "".join(value) if isinstance(value, list) else value


def _hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class AgentWorkspaceBuilder:
    def __init__(self, *, cleanup_attempts: int = 3, cleanup_delay: float = 0.05) -> None:
        self.cleanup_attempts = max(1, cleanup_attempts)
        self.cleanup_delay = max(0, cleanup_delay)

    def build(
        self, snapshot: NotebookSnapshot, scope: FrozenTurnScope,
        *, correction: str | None = None,
    ) -> AgentWorkspace:
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
                                "Do not run shell commands.", "", "Editable files:"]
                instructions.extend(f"- {item.relative_path}" for item in manifest.editable_cells)
            else:
                instructions = [scope.prompt, "", *notebook_context,
                                "This is a read-only turn. Do not modify any file.",
                                "Answer in your final message.",
                                "Do not run shell commands.", ""]
            if context:
                instructions.extend(["", "Explicit context cells:"])
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
