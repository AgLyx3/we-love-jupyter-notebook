from __future__ import annotations

from dataclasses import dataclass

from ..agent_workspace.models import WorkspaceManifest, WorkspaceBoundaryError
from ..notebook_document.models import NotebookSnapshot, RevisionConflict
from ..turn_scope.models import FrozenTurnScope


@dataclass(frozen=True)
class CandidateCellSourceChange:
    cell_id: str
    previous_source: str
    next_source: str


class BoundaryValidator:
    def validate(
        self, *, snapshot: NotebookSnapshot, scope: FrozenTurnScope,
        manifest: WorkspaceManifest, candidates: dict[str, str],
    ) -> tuple[CandidateCellSourceChange, ...]:
        if snapshot.revision != scope.notebook_revision:
            raise RevisionConflict(snapshot.revision)
        ids = [item.cell_id for item in manifest.editable_cells]
        violations: list[str] = []
        if len(ids) != len(set(ids)) or set(ids) != set(scope.editable_cell_ids):
            violations.append("authoritative manifest does not match frozen editable scope")
        cells = {cell["id"]: cell for cell in snapshot.notebook["cells"]}
        for item in manifest.editable_cells:
            cell = cells.get(item.cell_id)
            if cell is None:
                violations.append(f"missing cell: {item.cell_id}")
                continue
            suffix = ".py" if cell["cell_type"] == "code" else ".md"
            if item.cell_type != cell["cell_type"] or not item.relative_path.endswith(suffix):
                violations.append(f"cell type/path mismatch: {item.cell_id}")
        if not set(candidates) <= set(ids):
            violations.append("candidate includes an out-of-scope cell")
        if violations:
            raise WorkspaceBoundaryError(violations)
        return tuple(
            CandidateCellSourceChange(
                cell_id=item.cell_id, previous_source=item.original_source,
                next_source=candidates[item.cell_id],
            )
            for item in manifest.editable_cells if item.cell_id in candidates
        )
