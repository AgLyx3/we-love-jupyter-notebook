from __future__ import annotations

from dataclasses import dataclass, field

from ..agent_workspace.models import (
    ReturnedStructureEntry, TrustedWorkspaceManifest, WorkspaceBoundaryError,
)


CELL_TYPES = {"code", "markdown", "raw"}


@dataclass(frozen=True)
class PlannedCell:
    """A cell in the next notebook state. ``origin_id`` is the existing cell id
    the content/outputs come from, or ``None`` for a newly added cell."""

    origin_id: str | None
    cell_type: str
    source: str


@dataclass(frozen=True)
class StructuralOp:
    """A derived, user-facing structural change (advisory badge data)."""

    op: str  # add | delete | edit | retype | move
    cell_id: str | None
    detail: dict = field(default_factory=dict)


@dataclass(frozen=True)
class StructuralPlan:
    next_cells: tuple[PlannedCell, ...]
    ops: tuple[StructuralOp, ...]

    @property
    def is_noop(self) -> bool:
        return len(self.ops) == 0


def _lis_positions(seq: list[int]) -> set[int]:
    """Positions on a deterministic longest strictly-increasing subsequence.

    O(n^2) DP with a leftmost tie-break so the result is stable regardless of
    input identity — used to distinguish genuinely moved cells (off the LIS)
    from cells that merely shifted because others were inserted/deleted.
    """
    n = len(seq)
    if n == 0:
        return set()
    length = [1] * n
    parent = [-1] * n
    for i in range(n):
        for j in range(i):
            if seq[j] < seq[i] and length[j] + 1 > length[i]:
                length[i] = length[j] + 1
                parent[i] = j
    best = 0
    for i in range(1, n):
        if length[i] > length[best]:
            best = i
    positions: set[int] = set()
    k = best
    while k != -1:
        positions.add(k)
        k = parent[k]
    return positions


def derive_structural_plan(
    *, manifest: TrustedWorkspaceManifest, entries: list[ReturnedStructureEntry],
) -> StructuralPlan:
    """Diff the agent-returned structure against the frozen original and derive
    the next ordered cell list plus advisory ops. Raises WorkspaceBoundaryError
    (retryable as a format correction) on any structural-validity violation."""
    violations: list[str] = []
    if not entries:
        violations.append("structure.json: a turn must leave at least one cell")

    frozen = {cell.cell_id: cell for cell in manifest.cells}
    frozen_index = {cell.cell_id: cell.index for cell in manifest.cells}
    seen_ids: set[str] = set()
    for position, entry in enumerate(entries):
        if entry.cell_type not in CELL_TYPES:
            violations.append(
                f"structure.json[{position}]: invalid cellType {entry.cell_type!r}"
            )
        if entry.is_add:
            continue
        if entry.cell_id is None:
            violations.append(f"structure.json[{position}]: existing entry is missing cellId")
            continue
        if entry.cell_id not in frozen:
            violations.append(f"structure.json[{position}]: unknown cellId {entry.cell_id}")
            continue
        if entry.cell_id in seen_ids:
            violations.append(f"structure.json[{position}]: duplicate cellId {entry.cell_id}")
            continue
        seen_ids.add(entry.cell_id)
    if violations:
        raise WorkspaceBoundaryError(sorted(set(violations)))

    surviving = [entry.cell_id for entry in entries if not entry.is_add]
    stable = _lis_positions([frozen_index[cid] for cid in surviving])
    moved_ids = {
        surviving[i] for i in range(len(surviving)) if i not in stable
    }

    next_cells: list[PlannedCell] = []
    ops: list[StructuralOp] = []
    for entry in entries:
        if entry.is_add:
            next_cells.append(PlannedCell(None, entry.cell_type, entry.content))
            ops.append(
                StructuralOp("add", None, {"toIndex": len(next_cells) - 1,
                                           "cellType": entry.cell_type})
            )
            continue
        cell = frozen[entry.cell_id]
        next_cells.append(PlannedCell(entry.cell_id, entry.cell_type, entry.content))
        to_index = len(next_cells) - 1
        if entry.cell_type != cell.cell_type:
            ops.append(
                StructuralOp("retype", entry.cell_id,
                             {"from": cell.cell_type, "to": entry.cell_type})
            )
        if entry.content != cell.original_source:
            ops.append(StructuralOp("edit", entry.cell_id, {}))
        if entry.cell_id in moved_ids:
            ops.append(
                StructuralOp("move", entry.cell_id,
                             {"fromIndex": cell.index, "toIndex": to_index})
            )
    for cell in manifest.cells:
        if cell.cell_id not in seen_ids:
            ops.append(StructuralOp("delete", cell.cell_id, {"fromIndex": cell.index}))
    return StructuralPlan(tuple(next_cells), tuple(ops))
