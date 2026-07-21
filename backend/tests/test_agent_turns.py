import time
from threading import Event

import pytest

from backend.app.agent_turns.service import (
    AgentTurnService, RevertConflict, UndoConflict,
)
from backend.app.agent_workspace.adapters import FakeAgentAdapter, FakeAttempt
from backend.app.notebook_document.models import MutationConflict, RevisionConflict
from backend.app.notebook_document.service import NotebookDocumentService
from backend.app.turn_scope.service import TurnScopeService


def _services(notebook_payload, attempts):
    documents = NotebookDocumentService()
    snapshot = documents.import_notebook(notebook_payload())
    scopes = TurnScopeService(documents)
    scopes.add("editable", editable=True)
    turns = AgentTurnService(
        documents=documents, scopes=scopes,
        adapter=FakeAgentAdapter(attempts), timeout=1,
    )
    return documents, scopes, turns, snapshot


def _source(snapshot, cell_id="editable"):
    cell = next(item for item in snapshot.notebook["cells"] if item["id"] == cell_id)
    value = cell["source"]
    return "".join(value) if isinstance(value, list) else value


def test_applies_scoped_changes_atomically_and_records_output(notebook_payload):
    documents, scopes, turns, snapshot = _services(
        notebook_payload, [FakeAttempt(edits={"editable/cell_editable.py": "value = 2\n"}, final_output="done")]
    )
    turn = turns.start(prompt="change", session_id=snapshot.session_id, expected_revision=snapshot.revision, background=False)
    assert turn.state == "completed"
    assert turn.final_output == "done"
    assert turn.applied_revision == snapshot.revision + 1
    assert _source(documents.get_snapshot()) == "value = 2\n"
    assert documents.coordinator.active_lease is None
    assert scopes.current().editable_cell_ids == ()


def test_noop_turn_does_not_increment_revision(notebook_payload):
    documents, _scopes, turns, snapshot = _services(notebook_payload, [FakeAttempt(final_output="no changes")])
    turn = turns.start(prompt="inspect", session_id=snapshot.session_id, expected_revision=snapshot.revision, background=False)
    assert turn.state == "completed"
    assert turn.changes == ()
    assert documents.get_snapshot().revision == snapshot.revision


def test_turn_rejects_stale_revision_and_releases_lease(notebook_payload):
    documents, _scopes, turns, snapshot = _services(notebook_payload, [FakeAttempt()])
    with pytest.raises(RevisionConflict):
        turns.start(
            prompt="stale", session_id=snapshot.session_id,
            expected_revision=snapshot.revision + 1, background=False,
        )
    assert documents.coordinator.active_lease is None


def test_boundary_violation_retries_in_fresh_workspace(notebook_payload):
    attempts = [
        FakeAttempt(creates={"outside.txt": "bad"}),
        FakeAttempt(edits={"editable/cell_editable.py": "value = 3\n"}),
    ]
    documents, _scopes, turns, snapshot = _services(notebook_payload, attempts)
    turn = turns.start(prompt="change", session_id=snapshot.session_id, expected_revision=snapshot.revision, background=False)
    assert turn.state == "completed"
    assert turn.attempts == 2
    assert _source(documents.get_snapshot()) == "value = 3\n"


def test_three_boundary_violations_fail_without_partial_apply(notebook_payload):
    attempts = [
        FakeAttempt(edits={"editable/cell_editable.py": "value = 9\n"}, creates={"bad.txt": "bad"})
    ]
    documents, _scopes, turns, snapshot = _services(notebook_payload, attempts)
    turn = turns.start(prompt="change", session_id=snapshot.session_id, expected_revision=snapshot.revision, background=False)
    assert turn.state == "failed"
    assert turn.attempts == 3
    assert turn.error["code"] == "workspace_boundary_violation"
    assert _source(documents.get_snapshot()) == "value = 1\n"


def test_active_turn_blocks_other_mutations_and_can_be_cancelled(notebook_payload):
    documents, _scopes, turns, snapshot = _services(
        notebook_payload, [FakeAttempt(delay=0.5, edits={"editable/cell_editable.py": "late\n"})]
    )
    turn = turns.start(prompt="slow", session_id=snapshot.session_id, expected_revision=snapshot.revision)
    with pytest.raises(MutationConflict):
        documents.update_cell_source(
            cell_id="editable", source="manual\n", expected_revision=snapshot.revision, owner="manual"
        )
    turns.cancel(turn.turn_id)
    deadline = time.monotonic() + 2
    while turns.get(turn.turn_id).state not in {"cancelled", "failed"} and time.monotonic() < deadline:
        time.sleep(0.01)
    cancelled = turns.get(turn.turn_id)
    assert cancelled.state == "cancelled"
    assert _source(documents.get_snapshot()) == "value = 1\n"
    assert documents.coordinator.active_lease is None


def test_timeout_fails_without_apply_and_releases_lease(notebook_payload):
    documents, _scopes, turns, snapshot = _services(
        notebook_payload, [FakeAttempt(delay=0.1, edits={"editable/cell_editable.py": "late\n"})]
    )
    turns.timeout = 0.01
    turn = turns.start(
        prompt="slow", session_id=snapshot.session_id,
        expected_revision=snapshot.revision, background=False,
    )
    assert turn.state == "failed"
    assert turn.error["code"] == "agent_timed_out"
    assert _source(documents.get_snapshot()) == "value = 1\n"
    assert documents.coordinator.active_lease is None


def test_scope_expiration_completes_before_lease_release(notebook_payload):
    entered_expire = Event()
    allow_expire = Event()

    class BarrierScopeService(TurnScopeService):
        def expire(self, scope, outcome):
            entered_expire.set()
            assert allow_expire.wait(2)
            super().expire(scope, outcome)

    documents = NotebookDocumentService()
    snapshot = documents.import_notebook(notebook_payload())
    scopes = BarrierScopeService(documents)
    scopes.add("editable", editable=True)
    turns = AgentTurnService(
        documents=documents, scopes=scopes, adapter=FakeAgentAdapter()
    )
    turn = turns.start(
        prompt="no-op", session_id=snapshot.session_id,
        expected_revision=snapshot.revision,
    )
    assert entered_expire.wait(2)
    with pytest.raises(MutationConflict):
        scopes.add("intro", editable=False)
    with pytest.raises(MutationConflict):
        turns.start(
            prompt="interleave", session_id=snapshot.session_id,
            expected_revision=snapshot.revision,
        )
    allow_expire.set()
    deadline = time.monotonic() + 2
    while documents.coordinator.active_lease is not None and time.monotonic() < deadline:
        time.sleep(0.01)
    assert documents.coordinator.active_lease is None
    assert scopes.history[-1].scope.turn_id == turn.turn_id


def test_cancel_immediately_before_commit_gate_prevents_apply(notebook_payload):
    validator_entered = Event()
    allow_validation = Event()

    from backend.app.boundary_validation.validator import BoundaryValidator

    class BarrierValidator(BoundaryValidator):
        def validate(self, **kwargs):
            result = super().validate(**kwargs)
            validator_entered.set()
            assert allow_validation.wait(2)
            return result

    documents, scopes, _turns, snapshot = _services(notebook_payload, [])
    turns = AgentTurnService(
        documents=documents, scopes=scopes,
        adapter=FakeAgentAdapter([
            FakeAttempt(edits={"editable/cell_editable.py": "value = 2\n"})
        ]),
        validator=BarrierValidator(),
    )
    turn = turns.start(
        prompt="change", session_id=snapshot.session_id,
        expected_revision=snapshot.revision,
    )
    assert validator_entered.wait(2)
    turns.cancel(turn.turn_id)
    allow_validation.set()
    deadline = time.monotonic() + 2
    while turns.get(turn.turn_id).state not in {"cancelled", "failed"} and time.monotonic() < deadline:
        time.sleep(0.01)
    assert turns.get(turn.turn_id).state == "cancelled"
    assert _source(documents.get_snapshot()) == "value = 1\n"


def test_cancel_during_apply_preserves_sources_and_finishes_cancelled(notebook_payload):
    apply_entered = Event()
    allow_apply = Event()

    class BarrierDocumentService(NotebookDocumentService):
        def apply_source_changes_under_lease(self, **kwargs):
            apply_entered.set()
            assert allow_apply.wait(2)
            return super().apply_source_changes_under_lease(**kwargs)

    documents = BarrierDocumentService()
    snapshot = documents.import_notebook(notebook_payload())
    scopes = TurnScopeService(documents)
    scopes.add("editable", editable=True)
    turns = AgentTurnService(
        documents=documents, scopes=scopes,
        adapter=FakeAgentAdapter([
            FakeAttempt(edits={"editable/cell_editable.py": "value = 2\n"})
        ]),
    )
    turn = turns.start(
        prompt="change", session_id=snapshot.session_id,
        expected_revision=snapshot.revision,
    )
    assert apply_entered.wait(2)
    turns.cancel(turn.turn_id)
    allow_apply.set()
    deadline = time.monotonic() + 2
    while turns.get(turn.turn_id).state not in {"cancelled", "completed", "failed"} and time.monotonic() < deadline:
        time.sleep(0.01)
    finished = turns.get(turn.turn_id)
    assert finished.state == "cancelled"
    assert finished.applied_revision == snapshot.revision + 1
    assert _source(documents.get_snapshot()) == "value = 2\n"


def test_only_latest_applied_turn_can_be_whole_undone(notebook_payload):
    documents, scopes, turns, snapshot = _services(
        notebook_payload, [
            FakeAttempt(edits={"editable/cell_editable.py": "value = 2\n"}),
            FakeAttempt(edits={"editable/cell_editable.py": "value = 3\n"}),
        ]
    )
    first = turns.start(prompt="one", session_id=snapshot.session_id, expected_revision=snapshot.revision, background=False)
    current = documents.get_snapshot()
    scopes.add("editable", editable=True)
    second = turns.start(prompt="two", session_id=current.session_id, expected_revision=current.revision, background=False)
    with pytest.raises(UndoConflict):
        turns.undo(first.turn_id, session_id=current.session_id, expected_revision=second.applied_revision)
    restored = turns.undo(second.turn_id, session_id=current.session_id, expected_revision=second.applied_revision)
    assert _source(restored) == "value = 2\n"


def test_per_cell_revert_requires_applied_source_hash(notebook_payload):
    documents, _scopes, turns, snapshot = _services(
        notebook_payload, [FakeAttempt(edits={"editable/cell_editable.py": "value = 2\n"})]
    )
    turn = turns.start(prompt="one", session_id=snapshot.session_id, expected_revision=snapshot.revision, background=False)
    edited = documents.update_cell_source(
        cell_id="editable", source="manual\n", expected_revision=turn.applied_revision,
        expected_session_id=snapshot.session_id, owner="manual",
    )
    with pytest.raises(RevertConflict):
        turns.revert_cell(
            turn.turn_id, "editable", session_id=snapshot.session_id,
            expected_revision=edited.revision,
        )


def test_agent_turn_api_exposes_status_and_terminal_scope(client, notebook_payload):
    uploaded = client.post(
        "/notebooks/upload", files={"file": ("sample.ipynb", notebook_payload(), "application/json")}
    ).json()
    preconditions = {
        "sessionId": uploaded["sessionId"],
        "expectedDocumentRevision": uploaded["revision"],
    }
    assert client.post("/turn-scope/editable-cells", json={**preconditions, "cellId": "editable"}).status_code == 200
    created = client.post("/agent-turns", json={**preconditions, "prompt": "inspect"})
    assert created.status_code == 202
    turn_id = created.json()["turnId"]
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        status = client.get(f"/agent-turns/{turn_id}").json()
        if status["state"] in {"completed", "failed", "cancelled"}:
            break
        time.sleep(0.01)
    assert status["state"] == "completed"
    assert client.get("/turn-scope").json()["editableCellIds"] == []
