import json
from datetime import datetime, timezone

import pytest

from backend.app.notebook_document.models import (
    MutationConflict, NotebookImportError, RevisionConflict,
)
from backend.app.notebook_document.service import NotebookDocumentService
from backend.app.turn_scope.models import FrozenTurnScope, StaleTurnScope
from backend.app.turn_scope.service import (
    MAX_TERMINAL_SCOPE_RECORD_BYTES, TurnScopeService,
)


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


def test_scope_removes_individual_cells(notebook_payload):
    documents, snapshot = _loaded(notebook_payload)
    scopes = TurnScopeService(documents)
    scopes.add("editable", editable=True, session_id=snapshot.session_id, revision=snapshot.revision)
    scopes.add("intro", editable=False, session_id=snapshot.session_id, revision=snapshot.revision)
    remaining = scopes.remove("editable", session_id=snapshot.session_id, revision=snapshot.revision)
    assert remaining.editable_cell_ids == ()
    assert remaining.context_cell_ids == ("intro",)
    # Removing a cell that is not scoped is a harmless no-op.
    assert scopes.remove("editable").context_cell_ids == ("intro",)
    emptied = scopes.remove("intro", session_id=snapshot.session_id, revision=snapshot.revision)
    assert emptied.editable_cell_ids == () and emptied.context_cell_ids == ()
    assert emptied.session_id is None and emptied.notebook_revision is None


def test_scope_remove_rejects_stale_revision(notebook_payload):
    documents, snapshot = _loaded(notebook_payload)
    scopes = TurnScopeService(documents)
    scopes.add("editable", editable=True, session_id=snapshot.session_id, revision=snapshot.revision)
    with pytest.raises(RevisionConflict):
        scopes.remove("editable", session_id=snapshot.session_id, revision=99)


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


def test_terminal_scope_history_is_count_and_byte_bounded(notebook_payload):
    documents, snapshot = _loaded(notebook_payload)
    scopes = TurnScopeService(documents)
    for index in range(120):
        scopes.expire(FrozenTurnScope(
            turn_id=f"turn-{index}", session_id=snapshot.session_id,
            notebook_revision=snapshot.revision,
            editable_cell_ids=("editable",), context_cell_ids=(),
            prompt=str(index) * 20_000, frozen_at=datetime.now(timezone.utc),
        ), "completed")
    assert len(scopes.history) < 30
    assert scopes.history[-1].scope.turn_id == "turn-119"
    newest = scopes.history[-1]
    size = (
        len(newest.scope.prompt.encode())
        + sum(len(value.encode()) for value in newest.scope.editable_cell_ids)
        + sum(len(value.encode()) for value in newest.scope.context_cell_ids)
        + len(newest.scope.turn_id.encode())
        + len(newest.scope.session_id.encode())
        + len(newest.outcome.encode())
        + 256
    )
    assert size <= MAX_TERMINAL_SCOPE_RECORD_BYTES


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


def test_close_clears_turn_scope(notebook_payload):
    documents, snapshot = _loaded(notebook_payload)
    scopes = TurnScopeService(documents)
    scopes.add("editable", editable=True)

    documents.close_notebook(
        expected_session_id=snapshot.session_id,
        expected_revision=snapshot.revision,
    )

    selection = scopes.current()
    assert selection.editable_cell_ids == ()
    assert selection.context_cell_ids == ()
    assert selection.session_id is None


def test_close_purges_terminal_scope_history(notebook_payload):
    documents, snapshot = _loaded(notebook_payload)
    scopes = TurnScopeService(documents)
    scopes.expire(FrozenTurnScope(
        turn_id="old-turn", session_id=snapshot.session_id,
        notebook_revision=snapshot.revision,
        editable_cell_ids=("editable",), context_cell_ids=("intro",),
        prompt="old scoped prompt", frozen_at=datetime.now(timezone.utc),
    ), "completed")
    assert scopes.history

    documents.close_notebook(
        expected_session_id=snapshot.session_id,
        expected_revision=snapshot.revision,
    )

    assert scopes.history == ()


def test_replacement_purges_terminal_scope_history(notebook_payload):
    documents, snapshot = _loaded(notebook_payload)
    scopes = TurnScopeService(documents)
    scopes.expire(FrozenTurnScope(
        turn_id="old-turn", session_id=snapshot.session_id,
        notebook_revision=snapshot.revision,
        editable_cell_ids=("editable",), context_cell_ids=("intro",),
        prompt="old scoped prompt", frozen_at=datetime.now(timezone.utc),
    ), "completed")

    replacement = documents.import_notebook(
        notebook_payload(cell_ids=("new-intro", "new-editable")),
        expected_session_id=snapshot.session_id,
        expected_revision=snapshot.revision,
    )

    assert replacement.session_id != snapshot.session_id
    assert scopes.history == ()


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
