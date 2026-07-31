"""HTTP contract for the per-operation review endpoints.

The service behaviour is covered in test_turn_operation_review.py. This file
covers what only the route layer decides: field aliases, the deliberately
asymmetric preconditions (accept carries no expected revision, reject does),
and the status codes clients branch on. Those were previously verifiable only
by hand.
"""

from __future__ import annotations

import json
import time

import pytest
from fastapi.testclient import TestClient

from backend.app.agent_turns.operations import SourceHunk, TurnOperation
from backend.app.agent_turns.service import AgentTurn
from backend.app.agent_workspace.adapters import FakeAgentAdapter, FakeAttempt
from backend.app.api.agent_turn_routes import (
    MAX_TURN_SUMMARY_BYTES, serialize_turn_summary,
)
from backend.app.main import create_app


THREE_LINE = "a = 1\nb = 2\nc = 3\n"
BOTH_EDITED = "a = 99\nb = 2\nc = 99\n"


def await_terminal(api: TestClient, turn_id: str) -> dict:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        turn = api.get(f"/agent-turns/{turn_id}").json()
        if turn["state"] in {"completed", "failed", "cancelled", "validation_incomplete"}:
            return turn
        time.sleep(0.01)
    raise AssertionError(f"turn {turn_id} never reached a terminal state")


@pytest.fixture
def reviewed_turn(notebook_payload):
    """An applied turn whose single cell carries two independent hunks."""
    app = create_app(agent_adapter=FakeAgentAdapter([
        FakeAttempt(edits={"editable/cell_editable.py": BOTH_EDITED}),
    ]))
    app.state.agent_turn_service.executions = None
    with TestClient(app) as api:
        uploaded = api.post(
            "/notebooks/upload",
            files={"file": ("sample.ipynb", notebook_payload(), "application/json")},
        ).json()
        seeded = api.post(
            "/cells/editable/source",
            json={
                "sessionId": uploaded["sessionId"],
                "expectedDocumentRevision": uploaded["revision"],
                "source": THREE_LINE,
            },
        ).json()
        preconditions = {
            "sessionId": uploaded["sessionId"],
            "expectedDocumentRevision": seeded["revision"],
        }
        api.post(
            "/turn-scope/editable-cells",
            json={**preconditions, "cellId": "editable"},
        )
        created = api.post(
            "/agent-turns", json={**preconditions, "prompt": "edit"},
        ).json()
        turn = await_terminal(api, created["turnId"])
        assert turn["state"] == "completed", turn.get("error")
        assert len(turn["operations"]) == 2, turn["operations"]
        yield api, uploaded["sessionId"], turn


class TestSerializedLedger:
    def test_operations_carry_kind_state_and_ranges_but_no_text(
        self, reviewed_turn,
    ):
        _api, _session, turn = reviewed_turn
        first = turn["operations"][0]
        assert first["kind"] == "source_hunk"
        assert first["state"] == "pending"
        assert len(first["previousRange"]) == 2
        assert len(first["nextRange"]) == 2
        # Ranges only: hunk text is reconstructible from `changes`, and
        # repeating it would blow the turn-summary budget.
        assert "text" not in first and "source" not in first

    def test_session_status_reports_the_same_ledger(self, reviewed_turn):
        api, _session, turn = reviewed_turn
        status = api.get("/session/status").json()
        summary = next(
            item for item in status["turnHistory"]
            if item["turnId"] == turn["turnId"]
        )
        assert len(summary["operations"]) == 2


class TestSummaryBounds:
    def test_a_huge_ledger_is_truncated_in_the_summary(self):
        """The summary is size-capped, so the ledger must be bounded like its
        siblings rather than serialized whole and only then discovered too big."""
        operations = tuple(
            TurnOperation(
                operation_id=f"t:c:{index}", cell_id="c", ordinal=index,
                hunk=SourceHunk(index, index, index + 1, index, index + 1),
            )
            for index in range(1000)
        )
        turn = AgentTurn(
            turn_id="t", session_id="s", base_revision=1, prompt="p",
            operations=operations, applied_revision=2,
        )
        summary = serialize_turn_summary(turn)
        assert len(summary["operations"]) == 256
        assert len(json.dumps(summary).encode()) <= MAX_TURN_SUMMARY_BYTES


class TestAcceptContract:
    def test_accept_all_needs_only_a_session_id(self, reviewed_turn):
        api, session, turn = reviewed_turn
        before = api.get("/notebooks/current").json()["revision"]
        response = api.post(
            f"/agent-turns/{turn['turnId']}/operations/accept-all",
            json={"sessionId": session},
        )
        assert response.status_code == 200
        assert all(
            item["state"] == "accepted" for item in response.json()["operations"]
        )
        # Accept settles review state only; the document must not move.
        assert api.get("/notebooks/current").json()["revision"] == before

    def test_accept_takes_a_batch_of_operation_ids(self, reviewed_turn):
        api, session, turn = reviewed_turn
        ids = [item["operationId"] for item in turn["operations"]]
        response = api.post(
            f"/agent-turns/{turn['turnId']}/operations/accept-all",
            json={"sessionId": session, "operationIds": ids},
        )
        assert response.status_code == 200
        assert all(
            item["state"] == "accepted" for item in response.json()["operations"]
        )

    def test_accept_one_operation_by_path(self, reviewed_turn):
        api, session, turn = reviewed_turn
        target = turn["operations"][0]["operationId"]
        response = api.post(
            f"/agent-turns/{turn['turnId']}/operations/{target}/accept",
            json={"sessionId": session},
        )
        assert response.status_code == 200
        states = {
            item["operationId"]: item["state"]
            for item in response.json()["operations"]
        }
        assert states[target] == "accepted"
        assert states[turn["operations"][1]["operationId"]] == "pending"

    def test_accept_rejects_a_foreign_session(self, reviewed_turn):
        api, _session, turn = reviewed_turn
        response = api.post(
            f"/agent-turns/{turn['turnId']}/operations/accept-all",
            json={"sessionId": "not-this-session"},
        )
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "session_conflict"

    def test_accept_requires_a_session_id(self, reviewed_turn):
        api, _session, turn = reviewed_turn
        assert api.post(
            f"/agent-turns/{turn['turnId']}/operations/accept-all", json={},
        ).status_code == 422

    def test_accept_bounds_the_operation_id_batch(self, reviewed_turn):
        api, session, turn = reviewed_turn
        response = api.post(
            f"/agent-turns/{turn['turnId']}/operations/accept-all",
            json={"sessionId": session, "operationIds": ["x"] * 513},
        )
        assert response.status_code == 422

    def test_unknown_operation_is_not_found(self, reviewed_turn):
        api, session, turn = reviewed_turn
        response = api.post(
            f"/agent-turns/{turn['turnId']}/operations/nope/accept",
            json={"sessionId": session},
        )
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "turn_operation_not_found"

    def test_unknown_turn_is_not_found(self, reviewed_turn):
        api, session, _turn = reviewed_turn
        response = api.post(
            "/agent-turns/does-not-exist/operations/accept-all",
            json={"sessionId": session},
        )
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "agent_turn_not_found"


class TestRejectContract:
    def test_reject_one_operation_returns_the_updated_notebook(
        self, reviewed_turn,
    ):
        api, session, turn = reviewed_turn
        response = api.post(
            f"/agent-turns/{turn['turnId']}/operations/"
            f"{turn['operations'][0]['operationId']}/reject",
            json={
                "sessionId": session,
                "expectedDocumentRevision": turn["appliedRevision"],
            },
        )
        assert response.status_code == 200
        cell = next(
            item for item in response.json()["cells"] if item["cellId"] == "editable"
        )
        assert cell["source"] == "a = 1\nb = 2\nc = 99\n"

    def test_reject_all_restores_the_pre_turn_source(self, reviewed_turn):
        api, session, turn = reviewed_turn
        response = api.post(
            f"/agent-turns/{turn['turnId']}/operations/reject-all",
            json={
                "sessionId": session,
                "expectedDocumentRevision": turn["appliedRevision"],
            },
        )
        assert response.status_code == 200
        cell = next(
            item for item in response.json()["cells"] if item["cellId"] == "editable"
        )
        assert cell["source"] == THREE_LINE

    def test_reject_requires_the_expected_revision(self, reviewed_turn):
        api, session, turn = reviewed_turn
        assert api.post(
            f"/agent-turns/{turn['turnId']}/operations/reject-all",
            json={"sessionId": session},
        ).status_code == 422

    def test_reject_conflicts_on_a_stale_revision(self, reviewed_turn):
        api, session, turn = reviewed_turn
        response = api.post(
            f"/agent-turns/{turn['turnId']}/operations/reject-all",
            json={
                "sessionId": session,
                "expectedDocumentRevision": turn["appliedRevision"] + 99,
            },
        )
        assert response.status_code == 409

    def test_a_manual_edit_makes_the_cell_stale_then_conflicts(
        self, reviewed_turn,
    ):
        api, session, turn = reviewed_turn
        edited = api.post(
            "/cells/editable/source",
            json={
                "sessionId": session,
                "expectedDocumentRevision": turn["appliedRevision"],
                "source": "hand written\n",
            },
        ).json()
        # Derived on read: the ledger reports stale with no reject attempted.
        refreshed = api.get(f"/agent-turns/{turn['turnId']}").json()
        assert {item["state"] for item in refreshed["operations"]} == {"stale"}

        response = api.post(
            f"/agent-turns/{turn['turnId']}/operations/"
            f"{turn['operations'][0]['operationId']}/reject",
            json={
                "sessionId": session,
                "expectedDocumentRevision": edited["revision"],
            },
        )
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "operation_conflict"


class TestUndoLineageOverHttp:
    def test_whole_turn_undo_survives_a_partial_reject(self, reviewed_turn):
        """The gap that made granular review self-defeating, over the wire."""
        api, session, turn = reviewed_turn
        rejected = api.post(
            f"/agent-turns/{turn['turnId']}/operations/"
            f"{turn['operations'][0]['operationId']}/reject",
            json={
                "sessionId": session,
                "expectedDocumentRevision": turn["appliedRevision"],
            },
        ).json()

        assert api.get(f"/agent-turns/{turn['turnId']}").json()["undoEligible"] is True
        restored = api.post(
            f"/agent-turns/{turn['turnId']}/undo",
            json={
                "sessionId": session,
                "expectedDocumentRevision": rejected["revision"],
            },
        )
        assert restored.status_code == 200
        cell = next(
            item for item in restored.json()["cells"] if item["cellId"] == "editable"
        )
        assert cell["source"] == THREE_LINE

    def test_undo_settles_the_ledger(self, reviewed_turn):
        api, session, turn = reviewed_turn
        api.post(
            f"/agent-turns/{turn['turnId']}/undo",
            json={
                "sessionId": session,
                "expectedDocumentRevision": turn["appliedRevision"],
            },
        )
        settled = api.get(f"/agent-turns/{turn['turnId']}").json()
        assert {item["state"] for item in settled["operations"]} == {"rejected"}
