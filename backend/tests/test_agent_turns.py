import json
import os
from pathlib import Path
import sys
import time
from threading import Event

import pytest
from fastapi.testclient import TestClient

from backend.app.agent_turns.service import (
    AgentTurn, AgentTurnService, AgentTurnServiceShuttingDown,
    MAX_TURN_HISTORY_BYTES, RevertConflict, UndoConflict,
)
from backend.app.api.agent_turn_routes import (
    MAX_TURN_SUMMARY_BYTES, serialize_turn_summary,
)
from backend.app.boundary_validation.validator import CandidateCellSourceChange
from backend.app.agent_workspace.adapters import FakeAgentAdapter, FakeAttempt
from backend.app.agent_workspace.models import (
    AdapterResult, AgentAdapterError, WorkspaceCleanupError,
)
from backend.app.agent_workspace.workspace_builder import AgentWorkspaceBuilder
from backend.app.agent_workspace.runner import ProcessRunner
from backend.app.notebook_document.models import MutationConflict, RevisionConflict
from backend.app.notebook_document.service import NotebookDocumentService
from backend.app.main import create_app
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


def test_session_status_persists_bounded_turn_history_with_frozen_scope(
    notebook_payload,
):
    app = create_app(agent_adapter=FakeAgentAdapter())
    with TestClient(app) as api:
        uploaded = api.post(
            "/notebooks/upload",
            files={"file": ("sample.ipynb", notebook_payload(), "application/json")},
        ).json()
        preconditions = {
            "sessionId": uploaded["sessionId"],
            "expectedDocumentRevision": uploaded["revision"],
        }
        api.post("/turn-scope/editable-cells", json={
            **preconditions, "cellId": "editable",
        })
        api.post("/turn-scope/context-cells", json={
            **preconditions, "cellId": "intro",
        })
        created = api.post("/agent-turns", json={
            **preconditions, "prompt": "remember this turn",
        }).json()
        for _ in range(100):
            current = api.get(f"/agent-turns/{created['turnId']}").json()
            if current["state"] == "completed":
                break
            time.sleep(.01)
        assert current["historyTruncated"] is False
        status = api.get("/session/status").json()
        assert status["activeTurn"] is None
        assert len(status["turnHistory"]) == 1
        stored = status["turnHistory"][0]
        assert stored["prompt"] == "remember this turn"
        assert stored["editableCellIds"] == ["editable"]
        assert stored["contextCellIds"] == ["intro"]
        assert stored["state"] == "completed"
        assert stored["finalOutput"] == ""
        assert stored["changes"] == []
        assert stored["undoEligible"] is False


def test_completed_turn_memory_is_bounded(notebook_payload):
    documents = NotebookDocumentService()
    snapshot = documents.import_notebook(notebook_payload())
    scopes = TurnScopeService(documents)
    turns = AgentTurnService(
        documents=documents, scopes=scopes, adapter=FakeAgentAdapter(), timeout=1,
    )
    for index in range(55):
        scopes.add("editable", editable=True)
        turns.start(
            prompt=f"turn {index}", session_id=snapshot.session_id,
            expected_revision=snapshot.revision, background=False,
        )
    history = turns.history_for_session(snapshot.session_id, limit=100)
    assert len(history) == 50
    assert history[0].prompt == "turn 54"
    assert history[-1].prompt == "turn 5"


def test_large_turn_history_keeps_only_latest_undo_checkpoint(notebook_payload):
    documents = NotebookDocumentService()
    snapshot = documents.import_notebook(notebook_payload())
    scopes = TurnScopeService(documents)
    sources = [f"value = {index}\n#" + (str(index) * 700_000) for index in range(3)]
    turns = AgentTurnService(
        documents=documents, scopes=scopes,
        adapter=FakeAgentAdapter([
            FakeAttempt(edits={"editable/cell_editable.py": source})
            for source in sources
        ]), timeout=2,
    )
    created = []
    for index in range(3):
        current = documents.get_snapshot()
        scopes.add("editable", editable=True)
        created.append(turns.start(
            prompt=f"large {index}", session_id=current.session_id,
            expected_revision=current.revision, background=False,
        ))
    history = turns.history_for_session(snapshot.session_id, limit=100)
    assert len(history) < 3
    assert sum(turns._history_size(item) for item in history) <= MAX_TURN_HISTORY_BYTES
    assert sum(item.checkpoint is not None for item in history) == 1
    latest = turns.get(created[-1].turn_id)
    assert turns.is_undo_eligible(latest)
    restored = turns.undo(
        latest.turn_id, session_id=latest.session_id,
        expected_revision=latest.applied_revision,
    )
    assert _source(restored).startswith("value = 1\n")
    assert turns.get(latest.turn_id).checkpoint is None
    assert len(turns.history_for_session(snapshot.session_id, limit=100)) < 3


def test_turn_history_summary_has_hard_serialized_cap():
    turn = AgentTurn(
        turn_id="turn-large", session_id="session-large", base_revision=1,
        prompt="p" * 500_000, state="completed", final_output="o" * 500_000,
        editable_cell_ids=tuple(f"cell-{index}" for index in range(500)),
        changes=tuple(
            CandidateCellSourceChange(
                cell_id=f"cell-{index}", previous_source="a" * 20_000,
                next_source="b" * 20_000,
            )
            for index in range(200)
        ),
    )
    summary = serialize_turn_summary(turn, undo_eligible=True)
    assert len(json.dumps(summary, separators=(",", ":")).encode()) <= MAX_TURN_SUMMARY_BYTES
    assert summary["turnId"] == "turn-large"
    assert summary["sessionId"] == "session-large"
    assert summary["state"] == "completed"
    assert summary["undoEligible"] is True
    assert summary["historyTruncated"] is True


def test_turn_history_summary_marks_removed_error_details_as_truncated():
    turn = AgentTurn(
        turn_id="turn-error", session_id="session-error", base_revision=1,
        prompt="short", state="failed",
        error={"code": "failed", "message": "short", "details": {"stage": "execution"}},
    )

    summary = serialize_turn_summary(turn)

    assert summary["error"]["details"] == {}
    assert summary["historyTruncated"] is True


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


def test_cleanup_failure_does_not_mask_primary_agent_failure(
    notebook_payload, caplog,
):
    class CleanupFailingBuilder(AgentWorkspaceBuilder):
        def __init__(self):
            super().__init__(cleanup_delay=0)
            self.created = []

        def build(self, *args, **kwargs):
            workspace = super().build(*args, **kwargs)
            self.created.append(workspace)
            return workspace

        def destroy(self, workspace):
            raise WorkspaceCleanupError(
                workspace.root, 3, OSError("injected cleanup failure")
            )

    documents = NotebookDocumentService()
    snapshot = documents.import_notebook(notebook_payload())
    scopes = TurnScopeService(documents)
    scopes.add("editable", editable=True)
    builder = CleanupFailingBuilder()
    turns = AgentTurnService(
        documents=documents, scopes=scopes,
        adapter=FakeAgentAdapter([FakeAttempt(error=AgentAdapterError("primary failure"))]),
        builder=builder,
    )

    turn = turns.start(
        prompt="fail", session_id=snapshot.session_id,
        expected_revision=snapshot.revision, background=False,
    )

    assert turn.state == "failed"
    assert turn.error["code"] == "agent_adapter_error"
    assert turn.error["message"] == "primary failure"
    assert "Failed to remove agent workspace" in caplog.text
    for workspace in builder.created:
        AgentWorkspaceBuilder(cleanup_delay=0).destroy(workspace)


def test_active_turn_blocks_other_mutations_and_can_be_cancelled(notebook_payload):
    documents, _scopes, turns, snapshot = _services(
        notebook_payload, [FakeAttempt(delay=0.5, edits={"editable/cell_editable.py": "late\n"})]
    )
    turn = turns.start(prompt="slow", session_id=snapshot.session_id, expected_revision=snapshot.revision)
    with pytest.raises(MutationConflict):
        documents.update_cell_source(
            cell_id="editable", source="manual\n", expected_revision=snapshot.revision, owner="manual"
        )
    turns.cancel(
        turn.turn_id, session_id=snapshot.session_id,
        expected_revision=snapshot.revision,
    )
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
    assert turns.get(turn.turn_id).state == "cleaning_up"
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
    assert turns.get(turn.turn_id).state == "completed"
    scopes.add("intro", editable=False)
    assert scopes.current().context_cell_ids == ("intro",)
    assert scopes.history[-1].scope.turn_id == turn.turn_id


def test_api_publishes_terminal_only_after_cleanup_and_lease_release(
    notebook_payload,
):
    entered_expire = Event()
    allow_expire = Event()

    class BarrierScopeService(TurnScopeService):
        def expire(self, scope, outcome):
            entered_expire.set()
            assert allow_expire.wait(2)
            super().expire(scope, outcome)

    documents = NotebookDocumentService()
    scopes = BarrierScopeService(documents)
    turns = AgentTurnService(
        documents=documents, scopes=scopes, adapter=FakeAgentAdapter()
    )
    app = create_app()
    app.state.notebook_service = documents
    app.state.turn_scope_service = scopes
    app.state.agent_turn_service = turns
    with TestClient(app) as api:
        uploaded = api.post(
            "/notebooks/upload",
            files={"file": ("sample.ipynb", notebook_payload(), "application/json")},
        ).json()
        preconditions = {
            "sessionId": uploaded["sessionId"],
            "expectedDocumentRevision": uploaded["revision"],
        }
        api.post(
            "/turn-scope/editable-cells",
            json={**preconditions, "cellId": "editable"},
        )
        created = api.post(
            "/agent-turns", json={**preconditions, "prompt": "inspect"}
        ).json()
        assert entered_expire.wait(2)
        assert api.get(f"/agent-turns/{created['turnId']}").json()["state"] == "cleaning_up"
        blocked = api.post(
            "/turn-scope/context-cells",
            json={**preconditions, "cellId": "intro"},
        )
        assert blocked.status_code == 409
        allow_expire.set()
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            state = api.get(f"/agent-turns/{created['turnId']}").json()["state"]
            if state == "completed":
                break
            time.sleep(0.01)
        assert state == "completed"
        assert api.get("/turn-scope").json()["editableCellIds"] == []
        accepted = api.post(
            "/turn-scope/context-cells",
            json={**preconditions, "cellId": "intro"},
        )
        assert accepted.status_code == 200
    assert scopes.history[-1].scope.turn_id == created["turnId"]


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
    turns.cancel(
        turn.turn_id, session_id=snapshot.session_id,
        expected_revision=snapshot.revision,
    )
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
    turns.cancel(
        turn.turn_id, session_id=snapshot.session_id,
        expected_revision=snapshot.revision,
    )
    allow_apply.set()
    deadline = time.monotonic() + 2
    while turns.get(turn.turn_id).state not in {"cancelled", "completed", "failed"} and time.monotonic() < deadline:
        time.sleep(0.01)
    finished = turns.get(turn.turn_id)
    assert finished.state == "cancelled"
    assert finished.applied_revision == snapshot.revision + 1
    assert _source(documents.get_snapshot()) == "value = 2\n"


def test_api_cancel_accepts_base_revision_after_commit_before_publication(
    notebook_payload,
):
    committed = Event()
    allow_publication = Event()

    class PostCommitBarrierDocumentService(NotebookDocumentService):
        def apply_source_changes_under_lease(self, **kwargs):
            result = super().apply_source_changes_under_lease(**kwargs)
            committed.set()
            assert allow_publication.wait(2)
            return result

    documents = PostCommitBarrierDocumentService()
    scopes = TurnScopeService(documents)
    turns = AgentTurnService(
        documents=documents,
        scopes=scopes,
        adapter=FakeAgentAdapter([
            FakeAttempt(edits={"editable/cell_editable.py": "value = 2\n"})
        ]),
    )
    app = create_app()
    app.state.notebook_service = documents
    app.state.turn_scope_service = scopes
    app.state.agent_turn_service = turns
    with TestClient(app) as api:
        uploaded = api.post(
            "/notebooks/upload",
            files={"file": ("sample.ipynb", notebook_payload(), "application/json")},
        ).json()
        preconditions = {
            "sessionId": uploaded["sessionId"],
            "expectedDocumentRevision": uploaded["revision"],
        }
        assert api.post(
            "/turn-scope/editable-cells",
            json={**preconditions, "cellId": "editable"},
        ).status_code == 200
        created = api.post(
            "/agent-turns", json={**preconditions, "prompt": "change"}
        ).json()
        assert committed.wait(2)
        assert documents.get_snapshot().revision == uploaded["revision"] + 1
        stale = api.post(
            f"/agent-turns/{created['turnId']}/cancel",
            json={
                **preconditions,
                "expectedDocumentRevision": uploaded["revision"] + 99,
            },
        )
        assert stale.status_code == 409
        assert stale.json()["error"]["code"] == "revision_conflict"
        cancellation = api.post(
            f"/agent-turns/{created['turnId']}/cancel", json=preconditions,
        )
        assert cancellation.status_code == 200
        allow_publication.set()
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            finished = api.get(f"/agent-turns/{created['turnId']}").json()
            if finished["state"] in {"cancelled", "completed", "failed"}:
                break
            time.sleep(0.01)
        assert finished["state"] == "cancelled"
        assert finished["appliedRevision"] == uploaded["revision"] + 1
        current = api.get("/notebooks/current").json()
        edited = next(cell for cell in current["cells"] if cell["cellId"] == "editable")
        assert edited["source"] == "value = 2\n"
        retry = api.post(
            f"/agent-turns/{created['turnId']}/cancel", json=preconditions,
        )
        assert retry.status_code == 200
        assert retry.json()["state"] == "cancelled"
        conflicting = api.post(
            f"/agent-turns/{created['turnId']}/cancel",
            json={
                **preconditions,
                "expectedDocumentRevision": uploaded["revision"] + 1,
            },
        )
        assert conflicting.status_code == 409


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


def test_historical_cell_revert_survives_unrelated_manual_mutation(
    notebook_payload,
):
    documents, _scopes, turns, snapshot = _services(
        notebook_payload,
        [FakeAttempt(edits={"editable/cell_editable.py": "value = 2\n"})],
    )
    turn = turns.start(
        prompt="one", session_id=snapshot.session_id,
        expected_revision=snapshot.revision, background=False,
    )
    changed = documents.update_cell_source(
        cell_id="intro", source="# manual context\n",
        expected_revision=turn.applied_revision,
        expected_session_id=snapshot.session_id, owner="manual",
    )

    assert not turns.is_undo_eligible(turn)
    assert turns.get(turn.turn_id).checkpoint is None
    reverted = turns.revert_cell(
        turn.turn_id, "editable", session_id=snapshot.session_id,
        expected_revision=changed.revision,
    )

    assert _source(reverted) == "value = 1\n"
    with pytest.raises(UndoConflict):
        turns.undo(
            turn.turn_id, session_id=snapshot.session_id,
            expected_revision=reverted.revision,
        )


def test_historical_cell_revert_survives_later_turn_on_another_cell(
    notebook_payload,
):
    documents, scopes, turns, snapshot = _services(
        notebook_payload,
        [
            FakeAttempt(edits={"editable/cell_editable.py": "value = 2\n"}),
            FakeAttempt(edits={"editable/cell_intro.md": "# later\n"}),
        ],
    )
    first = turns.start(
        prompt="one", session_id=snapshot.session_id,
        expected_revision=snapshot.revision, background=False,
    )
    current = documents.get_snapshot()
    scopes.add("intro", editable=True)
    second = turns.start(
        prompt="two", session_id=current.session_id,
        expected_revision=current.revision, background=False,
    )

    reverted = turns.revert_cell(
        first.turn_id, "editable", session_id=snapshot.session_id,
        expected_revision=second.applied_revision,
    )

    assert _source(reverted) == "value = 1\n"


def test_api_reverts_historical_cell_after_unrelated_manual_edit(
    notebook_payload,
):
    app = create_app(agent_adapter=FakeAgentAdapter([
        FakeAttempt(edits={"editable/cell_editable.py": "value = 2\n"}),
    ]))
    app.state.agent_turn_service.executions = None
    with TestClient(app) as api:
        uploaded = api.post(
            "/notebooks/upload",
            files={"file": ("sample.ipynb", notebook_payload(), "application/json")},
        ).json()
        preconditions = {
            "sessionId": uploaded["sessionId"],
            "expectedDocumentRevision": uploaded["revision"],
        }
        api.post(
            "/turn-scope/editable-cells",
            json={**preconditions, "cellId": "editable"},
        )
        created = api.post(
            "/agent-turns", json={**preconditions, "prompt": "change"},
        ).json()
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            turn = api.get(f"/agent-turns/{created['turnId']}").json()
            if turn["state"] in {"completed", "failed", "cancelled"}:
                break
            time.sleep(0.01)
        assert turn["state"] == "completed"
        manual = api.post(
            "/cells/intro/source",
            json={
                "sessionId": uploaded["sessionId"],
                "expectedDocumentRevision": turn["appliedRevision"],
                "source": "# manual context\n",
            },
        ).json()
        assert api.get(f"/agent-turns/{created['turnId']}").json()["undoEligible"] is False

        reverted = api.post(
            f"/agent-turns/{created['turnId']}/cells/editable/revert",
            json={
                "sessionId": uploaded["sessionId"],
                "expectedDocumentRevision": manual["revision"],
            },
        )

        assert reverted.status_code == 200
        editable = next(
            cell for cell in reverted.json()["cells"]
            if cell["cellId"] == "editable"
        )
        assert editable["source"] == "value = 1\n"


def test_app_lifespan_stops_agent_turns_before_kernel_execution():
    app = create_app()
    calls = []
    app.state.agent_turn_service.shutdown = lambda: calls.append("turns")
    app.state.kernel_execution_service.shutdown = lambda: calls.append("kernel")

    with TestClient(app):
        pass

    assert calls == ["turns", "kernel"]


def test_shutdown_terminates_agent_tree_cleans_workspace_and_rejects_start(
    notebook_payload, tmp_path,
):
    pid_file = tmp_path / "descendant.pid"
    workspace_paths: list[Path] = []

    class SubprocessAdapter:
        auxiliary_paths = frozenset()

        def __init__(self):
            self.runner = ProcessRunner()

        def run(self, workspace, *, timeout, cancel_event):
            workspace_paths.append(workspace.root)
            descendant = (
                "import pathlib,subprocess,sys,time;"
                "p=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)']);"
                f"pathlib.Path({str(pid_file)!r}).write_text(str(p.pid));"
                "time.sleep(30)"
            )
            self.runner.run(
                [sys.executable, "-c", descendant], cwd=workspace.root,
                timeout=timeout, cancel_event=cancel_event, grace_period=0.05,
            )
            return AdapterResult("")

        def shutdown(self):
            self.runner.shutdown(grace_period=0.05)

    documents = NotebookDocumentService()
    snapshot = documents.import_notebook(notebook_payload())
    scopes = TurnScopeService(documents)
    scopes.add("editable", editable=True)
    turns = AgentTurnService(
        documents=documents, scopes=scopes, adapter=SubprocessAdapter(),
        timeout=30,
    )
    turn = turns.start(
        prompt="slow", session_id=snapshot.session_id,
        expected_revision=snapshot.revision,
    )
    deadline = time.monotonic() + 3
    while not pid_file.exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    assert pid_file.exists()

    started = time.monotonic()
    turns.shutdown(timeout=2)

    assert time.monotonic() - started < 2.5
    finished = turns.get(turn.turn_id)
    assert finished.state == "cancelled", finished.error
    assert documents.coordinator.active_lease is None
    assert workspace_paths and not workspace_paths[0].exists()
    descendant_pid = int(pid_file.read_text())
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        try:
            os.kill(descendant_pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.02)
    else:
        pytest.fail("agent descendant survived service shutdown")
    with pytest.raises(AgentTurnServiceShuttingDown):
        turns.start(
            prompt="late", session_id=snapshot.session_id,
            expected_revision=snapshot.revision,
        )


def test_shutdown_deadline_records_worker_failure_until_cleanup(
    notebook_payload,
):
    entered = Event()
    release = Event()

    class UncooperativeAdapter:
        auxiliary_paths = frozenset()

        def run(self, workspace, *, timeout, cancel_event):
            entered.set()
            assert release.wait(2)
            return AdapterResult("")

    documents = NotebookDocumentService()
    snapshot = documents.import_notebook(notebook_payload())
    scopes = TurnScopeService(documents)
    scopes.add("editable", editable=True)
    turns = AgentTurnService(
        documents=documents, scopes=scopes, adapter=UncooperativeAdapter(),
    )
    turn = turns.start(
        prompt="slow", session_id=snapshot.session_id,
        expected_revision=snapshot.revision,
    )
    assert entered.wait(1)

    turns.shutdown(timeout=0.01)

    timed_out = turns.get(turn.turn_id)
    assert timed_out.error["code"] == "shutdown_timeout"
    assert documents.coordinator.active_lease is not None
    release.set()
    deadline = time.monotonic() + 2
    while documents.coordinator.active_lease is not None and time.monotonic() < deadline:
        time.sleep(0.01)
    assert documents.coordinator.active_lease is None
    assert turns.get(turn.turn_id).state == "cancelled"


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


def test_replacement_api_clears_scope_but_failed_replacement_does_not(
    client, notebook_payload,
):
    uploaded = client.post(
        "/notebooks/upload",
        files={"file": ("one.ipynb", notebook_payload(), "application/json")},
    ).json()
    preconditions = {
        "sessionId": uploaded["sessionId"],
        "expectedDocumentRevision": uploaded["revision"],
    }
    client.post(
        "/turn-scope/editable-cells",
        json={**preconditions, "cellId": "editable"},
    )
    failed = client.post(
        "/notebooks/upload",
        files={"file": ("bad.ipynb", b"bad", "application/json")},
        data=preconditions,
    )
    assert failed.status_code == 422
    assert client.get("/turn-scope").json()["editableCellIds"] == ["editable"]
    replaced = client.post(
        "/notebooks/upload",
        files={"file": ("two.ipynb", notebook_payload(), "application/json")},
        data=preconditions,
    )
    assert replaced.status_code == 201
    assert replaced.json()["sessionId"] != uploaded["sessionId"]
    assert client.get("/turn-scope").json()["editableCellIds"] == []
