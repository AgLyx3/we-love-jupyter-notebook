import pytest

from backend.app.notebook_document.models import MutationConflict, RevisionConflict
from backend.app.notebook_document.service import NotebookDocumentService
from backend.app.turn_scope.service import TurnScopeService


def _loaded(notebook_payload):
    documents = NotebookDocumentService()
    snapshot = documents.import_notebook(notebook_payload())
    return documents, snapshot


def test_scope_moves_cells_between_roles_and_clears(notebook_payload):
    documents, snapshot = _loaded(notebook_payload)
    scopes = TurnScopeService(documents)
    scopes.add("editable", editable=True, session_id=snapshot.session_id, revision=snapshot.revision)
    selected = scopes.add("editable", editable=False, session_id=snapshot.session_id, revision=snapshot.revision)
    assert selected.editable_cell_ids == ()
    assert selected.context_cell_ids == ("editable",)
    assert scopes.clear(session_id=snapshot.session_id, revision=snapshot.revision).context_cell_ids == ()


def test_scope_rejects_stale_revision_and_active_mutation(notebook_payload):
    documents, snapshot = _loaded(notebook_payload)
    scopes = TurnScopeService(documents)
    with pytest.raises(RevisionConflict):
        scopes.add("editable", editable=True, session_id=snapshot.session_id, revision=99)
    lease = documents.coordinator.acquire(operation_type="agent_turn", operation_id="turn")
    try:
        with pytest.raises(MutationConflict):
            scopes.add("editable", editable=True)
    finally:
        documents.coordinator.release(lease)


def test_terminal_expiration_clears_scope_and_records_history(notebook_payload):
    documents, snapshot = _loaded(notebook_payload)
    scopes = TurnScopeService(documents)
    scopes.add("editable", editable=True)
    lease = documents.coordinator.acquire(operation_type="agent_turn", operation_id="turn")
    try:
        frozen = scopes.freeze(
            turn_id="turn", session_id=snapshot.session_id, revision=snapshot.revision,
            prompt="change it", lease=lease,
        )
        scopes.expire(frozen, "failed")
    finally:
        documents.coordinator.release(lease)
    assert scopes.current().editable_cell_ids == ()
    assert scopes.history[-1].scope.editable_cell_ids == ("editable",)
    assert scopes.history[-1].outcome == "failed"
