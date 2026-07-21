from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
from pathlib import Path

from ..notebook_document.models import NotebookSnapshot
from ..turn_scope.models import FrozenTurnScope
from .models import AgentWorkspace, ContextCellManifest, EditableCellManifest, WorkspaceManifest


def _source(cell: dict) -> str:
    value = cell.get("source", "")
    return "".join(value) if isinstance(value, list) else value


def _hash(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


class AgentWorkspaceBuilder:
    def build(
        self, snapshot: NotebookSnapshot, scope: FrozenTurnScope,
        *, correction: str | None = None,
    ) -> AgentWorkspace:
        root = Path(tempfile.mkdtemp(prefix=f"notebook-turn-{scope.turn_id[:8]}-"))
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
        instructions = [scope.prompt, "", "Only edit the listed files under editable/.",
                        "Do not change notebook structure, metadata, outputs, or cell types.",
                        "Do not run shell commands.", ""]
        instructions.extend(f"- {item.relative_path}" for item in manifest.editable_cells)
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

    @staticmethod
    def destroy(workspace: AgentWorkspace) -> None:
        shutil.rmtree(workspace.root, ignore_errors=True)
