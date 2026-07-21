import json

import pytest

from backend.app.notebook_document.models import (
    MutationConflict, NotebookImportError, RevisionConflict,
)
from backend.app.notebook_document.service import NotebookDocumentService
from backend.app.turn_scope.service import TurnScopeService
from backend.app.turn_scope.models import StaleTurnScope


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


def test_successful_replacement_clears_scope_even_with_colliding_ids(notebook_payload):
    documents, snapshot = _loaded(notebook_payload)
    scopes = TurnScopeService(documents)
    scopes.add("editable", editable=True)
    replacement = documents.import_notebook(
        notebook_payload(), expected_session_id=snapshot.session_id,
        expected_revision=snapshot.revision,
    )
    assert replacement.session_id != snapshot.session_id
    assert scopes.current().editable_cell_ids == ()


def test_failed_replacement_preserves_current_scope(notebook_payload):
    documents, snapshot = _loaded(notebook_payload)
    scopes = TurnScopeService(documents)
    scopes.add("editable", editable=True)
    with pytest.raises(NotebookImportError):
        documents.import_notebook(
            b"not-json", expected_session_id=snapshot.session_id,
            expected_revision=snapshot.revision,
        )
    assert scopes.current().editable_cell_ids == ("editable",)


def test_scope_is_bound_to_notebook_revision(notebook_payload):
    documents, snapshot = _loaded(notebook_payload)
    scopes = TurnScopeService(documents)
    scopes.add("editable", editable=True)
    edited = documents.update_cell_source(
        cell_id="editable", source="value = 2\n",
        expected_revision=snapshot.revision, owner="manual",
    )
    lease = documents.coordinator.acquire(
        operation_type="agent_turn", operation_id="turn"
    )
    try:
        with pytest.raises(StaleTurnScope):
            scopes.freeze(
                turn_id="turn", session_id=edited.session_id,
                revision=edited.revision, prompt="change", lease=lease,
            )
    finally:
        documents.coordinator.release(lease)


def test_adding_cell_at_new_revision_discards_all_prior_selections(
    notebook_payload,
):
    payload = json.loads(notebook_payload())
    payload["cells"].append({
        "cell_type": "code",
        "id": "third",
        "metadata": {},
        "source": "other = 1\n",
        "execution_count": None,
        "outputs": [],
    })
    documents = NotebookDocumentService()
    snapshot = documents.import_notebook(json.dumps(payload).encode())
    scopes = TurnScopeService(documents)
    scopes.add("editable", editable=True)
    edited = documents.update_cell_source(
        cell_id="editable", source="value = 2\n",
        expected_revision=snapshot.revision, owner="manual",
    )
    selection = scopes.add("intro", editable=False)
    assert selection.editable_cell_ids == ()
    assert selection.context_cell_ids == ("intro",)
    scopes.add("third", editable=True)
    lease = documents.coordinator.acquire(
        operation_type="agent_turn", operation_id="turn"
    )
    try:
        frozen = scopes.freeze(
            turn_id="turn", session_id=edited.session_id,
            revision=edited.revision, prompt="change third", lease=lease,
        )
    finally:
        documents.coordinator.release(lease)
    assert frozen.editable_cell_ids == ("third",)
    assert frozen.context_cell_ids == ("intro",)
    assert "editable" not in frozen.editable_cell_ids
