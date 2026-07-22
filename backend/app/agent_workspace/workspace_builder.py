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
            if manifest.editable_cells:
                instructions = [scope.prompt, "",
                                "You have permission to edit the files listed below, but editing is",
                                "optional — permission is a grant, not a requirement. First answer the",
                                "request directly in your final message. Only change a listed file when the",
                                "request calls for a concrete edit, and explain any edit you make.",
                                "Do not modify files that are not listed.",
                                "Do not change notebook structure, metadata, outputs, or cell types.",
                                "Do not run shell commands.", "", "Editable files:"]
                instructions.extend(f"- {item.relative_path}" for item in manifest.editable_cells)
            else:
                instructions = [scope.prompt, "",
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
