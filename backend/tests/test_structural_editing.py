import pytest

from backend.app.agent_workspace.models import (
    ReturnedStructureEntry, StructuralCellManifest, TrustedWorkspaceManifest,
    WorkspaceBoundaryError,
)
from backend.app.boundary_validation.structural_validator import (
    derive_structural_plan,
)
from backend.app.notebook_document.mutation_coordinator import MutationCoordinator
from backend.app.notebook_document.models import RevisionConflict, SessionConflict
from backend.app.notebook_document.service import NotebookDocumentService


def _manifest(cells):
    """cells: list of (cell_id, cell_type, source)."""
    structural = tuple(
        StructuralCellManifest(cid, i, ctype, f"cells/cell_{cid}", src)
        for i, (cid, ctype, src) in enumerate(cells)
    )
    return TrustedWorkspaceManifest(
        "notebook.readonly.ipynb", "structure.json", structural, ()
    )


def _existing(cell_id, cell_type, content):
    return ReturnedStructureEntry(False, cell_id, cell_type, f"cells/cell_{cell_id}", content)


def _add(cell_type, content, name="new_1"):
    return ReturnedStructureEntry(True, None, cell_type, f"cells/{name}", content)


def _op_types(plan):
    return sorted(op.op for op in plan.ops)


def test_edit_only_derives_single_edit_op():
    manifest = _manifest([("a", "code", "x = 1\n")])
    plan = derive_structural_plan(manifest=manifest, entries=[_existing("a", "code", "x = 2\n")])
    assert _op_types(plan) == ["edit"]
    assert [c.origin_id for c in plan.next_cells] == ["a"]
    assert plan.next_cells[0].source == "x = 2\n"


def test_add_appends_new_cell_with_no_origin():
    manifest = _manifest([("a", "code", "x\n")])
    plan = derive_structural_plan(
        manifest=manifest, entries=[_existing("a", "code", "x\n"), _add("code", "print(x)\n")]
    )
    assert _op_types(plan) == ["add"]
    assert [c.origin_id for c in plan.next_cells] == ["a", None]
    assert plan.next_cells[1].source == "print(x)\n"


def test_delete_missing_survivor_emits_delete():
    manifest = _manifest([("a", "code", "x\n"), ("b", "code", "y\n")])
    plan = derive_structural_plan(manifest=manifest, entries=[_existing("a", "code", "x\n")])
    assert _op_types(plan) == ["delete"]
    assert plan.ops[0].cell_id == "b"


def test_retype_emits_retype_op():
    manifest = _manifest([("a", "code", "# title\n")])
    plan = derive_structural_plan(
        manifest=manifest, entries=[_existing("a", "markdown", "# title\n")]
    )
    assert _op_types(plan) == ["retype"]


def test_reorder_moves_only_off_lis_cells():
    manifest = _manifest([(x, "code", f"{x}\n") for x in ("a", "b", "c", "d")])
    entries = [_existing(x, "code", f"{x}\n") for x in ("c", "d", "a", "b")]
    plan = derive_structural_plan(manifest=manifest, entries=entries)
    moved = {op.cell_id for op in plan.ops if op.op == "move"}
    # Deterministic LIS keeps {c,d}; only a,b are genuinely moved.
    assert moved == {"a", "b"}


def test_top_insert_does_not_falsely_move_downstream_cells():
    manifest = _manifest([(x, "code", f"{x}\n") for x in ("a", "b", "c")])
    entries = [_add("code", "import os\n"), *[_existing(x, "code", f"{x}\n") for x in ("a", "b", "c")]]
    plan = derive_structural_plan(manifest=manifest, entries=entries)
    assert _op_types(plan) == ["add"]  # no spurious move ops


def test_new_string_is_not_a_sentinel_real_cell_id_new_is_preserved():
    # R1 regression: a real cell whose id is literally "new" must NOT be read as an add.
    manifest = _manifest([("new", "code", "x\n"), ("b", "code", "y\n")])
    entries = [_existing("new", "code", "x\n"), _existing("b", "code", "y\n")]
    plan = derive_structural_plan(manifest=manifest, entries=entries)
    assert plan.ops == ()  # no add, no delete, no corruption
    assert [c.origin_id for c in plan.next_cells] == ["new", "b"]


def test_zero_cell_floor_rejected():
    manifest = _manifest([("a", "code", "x\n")])
    with pytest.raises(WorkspaceBoundaryError):
        derive_structural_plan(manifest=manifest, entries=[])


def test_unknown_cell_id_rejected():
    manifest = _manifest([("a", "code", "x\n")])
    with pytest.raises(WorkspaceBoundaryError):
        derive_structural_plan(manifest=manifest, entries=[_existing("zzz", "code", "x\n")])


def test_duplicate_cell_id_rejected():
    manifest = _manifest([("a", "code", "x\n")])
    with pytest.raises(WorkspaceBoundaryError):
        derive_structural_plan(
            manifest=manifest,
            entries=[_existing("a", "code", "x\n"), _existing("a", "code", "z\n")],
        )


def test_existing_entry_missing_cell_id_rejected():
    manifest = _manifest([("a", "code", "x\n")])
    with pytest.raises(WorkspaceBoundaryError):
        derive_structural_plan(
            manifest=manifest,
            entries=[ReturnedStructureEntry(False, None, "code", "cells/x.py", "x\n")],
        )


def test_invalid_cell_type_rejected():
    manifest = _manifest([("a", "code", "x\n")])
    with pytest.raises(WorkspaceBoundaryError):
        derive_structural_plan(manifest=manifest, entries=[_existing("a", "python", "x\n")])


def _service(notebook_payload):
    coordinator = MutationCoordinator()
    service = NotebookDocumentService(coordinator)
    snap = service.import_notebook(notebook_payload())
    lease = coordinator.acquire(operation_type="agent_turn", operation_id="t")
    return service, coordinator, lease, snap


def test_apply_structural_add_delete_edit(notebook_payload):
    service, coordinator, lease, snap = _service(notebook_payload)
    try:
        next_cells = [
            {"origin_id": "editable", "cell_type": "code", "source": "value = 2\n"},
            {"origin_id": None, "cell_type": "markdown", "source": "## added\n"},
        ]
        updated = service.apply_structural_changes_under_lease(
            next_cells=next_cells, expected_session_id=snap.session_id,
            expected_revision=snap.revision, owner="t", lease=lease,
        )
        cells = updated.notebook["cells"]
        # intro deleted (not in next_cells); order + types follow next_cells.
        assert [c["cell_type"] for c in cells] == ["code", "markdown"]
        assert cells[0]["id"] == "editable" and cells[0]["source"] == "value = 2\n"
        assert cells[1]["metadata"].get("agent_authored") is True
        assert "outputs" not in cells[1]  # markdown carries no outputs
        assert cells[1]["id"] != "editable"  # fresh id
        assert updated.revision == snap.revision + 1
    finally:
        coordinator.release(lease)


def test_apply_retype_code_to_markdown_drops_outputs(notebook_payload):
    service, coordinator, lease, snap = _service(notebook_payload)
    try:
        next_cells = [
            {"origin_id": "intro", "cell_type": "markdown", "source": "# Example notebook\n"},
            {"origin_id": "editable", "cell_type": "markdown", "source": "# now markdown\n"},
        ]
        updated = service.apply_structural_changes_under_lease(
            next_cells=next_cells, expected_session_id=snap.session_id,
            expected_revision=snap.revision, owner="t", lease=lease,
        )
        retyped = next(c for c in updated.notebook["cells"] if c["id"] == "editable")
        assert retyped["cell_type"] == "markdown"
        assert "outputs" not in retyped and "execution_count" not in retyped
    finally:
        coordinator.release(lease)


def test_apply_retype_markdown_to_code_adds_outputs(notebook_payload):
    service, coordinator, lease, snap = _service(notebook_payload)
    try:
        next_cells = [
            {"origin_id": "intro", "cell_type": "code", "source": "x = 1\n"},
            {"origin_id": "editable", "cell_type": "code", "source": "value = 1\n"},
        ]
        updated = service.apply_structural_changes_under_lease(
            next_cells=next_cells, expected_session_id=snap.session_id,
            expected_revision=snap.revision, owner="t", lease=lease,
        )
        promoted = next(c for c in updated.notebook["cells"] if c["id"] == "intro")
        assert promoted["cell_type"] == "code"
        assert promoted["execution_count"] is None and promoted["outputs"] == []
    finally:
        coordinator.release(lease)


def test_apply_structural_rejects_session_and_revision_conflicts(notebook_payload):
    service, coordinator, lease, snap = _service(notebook_payload)
    try:
        spec = [{"origin_id": "editable", "cell_type": "code", "source": "x\n"}]
        with pytest.raises(SessionConflict):
            service.apply_structural_changes_under_lease(
                next_cells=spec, expected_session_id="wrong-session",
                expected_revision=snap.revision, owner="t", lease=lease,
            )
        with pytest.raises(RevisionConflict):
            service.apply_structural_changes_under_lease(
                next_cells=spec, expected_session_id=snap.session_id,
                expected_revision=snap.revision + 5, owner="t", lease=lease,
            )
    finally:
        coordinator.release(lease)
