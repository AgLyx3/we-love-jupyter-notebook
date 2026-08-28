import time
import threading

import pytest

from backend.app.kernel_execution.kernel_session import KernelResult


class ApiKernel:
    # Mirrors KernelSession: kernel_status() reports which interpreter runs
    # the cells (#52), so a double without these is an AttributeError on
    # every status read.
    interpreter = "/fake/.venv/bin/python"
    interpreter_source = "kernelspec"
    kernel_session_id = "api-kernel"
    status = "idle"
    busy_attempt_id = None
    startup_timeout = 1
    cell_timeout = 1
    recovery_timeout = 1

    def execute(self, source, attempt_id):
        return KernelResult([{"output_type": "stream", "name": "stdout", "text": "ok\n"}], 1, False)
    def interrupt(self): pass
    def restart(self):
        self.kernel_session_id = "api-kernel-2"
        self.status = "idle"
        return self.kernel_session_id
    def shutdown(self): pass
    def execution_correlation(self):
        return self.kernel_session_id, self.busy_attempt_id
    def interrupt_correlated(self, kernel_session_id, attempt_id):
        matched = (
            kernel_session_id == self.kernel_session_id
            and attempt_id == self.busy_attempt_id
        )
        if matched:
            self.interrupt()
        return matched
    def restart_correlated(self, kernel_session_id, attempt_id, on_matched=None):
        if kernel_session_id != self.kernel_session_id or attempt_id != self.busy_attempt_id:
            return False
        if on_matched is not None:
            on_matched()
        self.restart()
        return True


def test_manual_execution_api_returns_pollable_operation(client, notebook_payload):
    uploaded = client.post("/notebooks/upload", files={"file": ("example.ipynb", notebook_payload(), "application/json")}).json()
    client.app.state.kernel_execution_service.kernel = ApiKernel()
    response = client.post("/execution/cells/editable/run", json={
        "sessionId": uploaded["sessionId"], "expectedDocumentRevision": uploaded["revision"]
    })
    assert response.status_code == 202
    operation_id = response.json()["operationId"]
    for _ in range(100):
        current = client.get(f"/execution/{operation_id}").json()
        if current["state"] == "completed":
            break
        time.sleep(.01)
    assert current["attempts"][0]["cellId"] == "editable"
    assert current["currentDocumentRevision"] == uploaded["revision"] + 1


def test_session_status_discovers_active_operation(client, notebook_payload):
    uploaded = client.post(
        "/notebooks/upload",
        files={"file": ("example.ipynb", notebook_payload(), "application/json")},
    ).json()
    entered = threading.Event()
    release = threading.Event()

    class BlockingKernel(ApiKernel):
        def execute(self, source, attempt_id):
            self.busy_attempt_id = attempt_id
            entered.set()
            assert release.wait(2)
            self.busy_attempt_id = None
            return KernelResult([], 1, False)

    client.app.state.kernel_execution_service.kernel = BlockingKernel()
    created = client.post("/execution/cells/editable/run", json={
        "sessionId": uploaded["sessionId"],
        "expectedDocumentRevision": uploaded["revision"],
    }).json()
    assert entered.wait(2)
    status = client.get("/session/status").json()
    assert status["sessionId"] == uploaded["sessionId"]
    assert status["documentRevision"] == uploaded["revision"]
    assert status["activeTurn"] is None
    assert status["activeExecution"]["operationId"] == created["operationId"]
    assert status["activeExecution"]["state"] == "running"
    release.set()


def test_kernel_restart_rejects_stale_session(client, notebook_payload):
    uploaded = client.post("/notebooks/upload", files={"file": ("example.ipynb", notebook_payload(), "application/json")}).json()
    client.app.state.kernel_execution_service.kernel = ApiKernel()
    body = {"sessionId": uploaded["sessionId"], "expectedDocumentRevision": uploaded["revision"]}
    assert client.post("/kernel/wrong/restart", json=body).status_code == 409
    restarted = client.post("/kernel/api-kernel/restart", json=body)
    assert restarted.status_code == 200
    assert restarted.json()["kernelSessionId"] == "api-kernel-2"


def test_restart_required_rejects_new_execution_with_structured_conflict(
    client, notebook_payload,
):
    uploaded = client.post(
        "/notebooks/upload",
        files={"file": ("example.ipynb", notebook_payload(), "application/json")},
    ).json()
    kernel = ApiKernel()
    kernel.status = "restart_required"
    client.app.state.kernel_execution_service.kernel = kernel
    response = client.post("/execution/cells/editable/run", json={
        "sessionId": uploaded["sessionId"],
        "expectedDocumentRevision": uploaded["revision"],
    })
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "kernel_restart_required"


def test_execution_validation_errors_are_structured(client, notebook_payload):
    uploaded = client.post("/notebooks/upload", files={"file": ("example.ipynb", notebook_payload(), "application/json")}).json()
    response = client.post("/execution/run-all", json={"sessionId": uploaded["sessionId"]})
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "invalid_request"


def test_session_event_journal_publishes_notebook_and_execution_state(client, notebook_payload):
    uploaded = client.post("/notebooks/upload", files={"file": ("example.ipynb", notebook_payload(), "application/json")}).json()
    client.app.state.kernel_execution_service.kernel = ApiKernel()
    response = client.post("/execution/cells/editable/run", json={
        "sessionId": uploaded["sessionId"], "expectedDocumentRevision": uploaded["revision"]
    })
    operation_id = response.json()["operationId"]
    for _ in range(100):
        if client.get(f"/execution/{operation_id}").json()["state"] == "completed":
            break
        time.sleep(.01)
    event_types = [event.event_type for event in client.app.state.session_event_service.list()]
    assert "execution.updated" in event_types
    assert event_types.count("notebook.updated") >= 2
    execution_events = [
        event for event in client.app.state.session_event_service.list()
        if event.event_type == "execution.updated"
    ]
    assert all(
        "outputs" not in attempt
        for event in execution_events for attempt in event.data["attempts"]
    )


def test_event_stream_native_reconnect_honors_last_event_id_header(
    client, notebook_payload, monkeypatch,
):
    uploaded = client.post(
        "/notebooks/upload",
        files={"file": ("example.ipynb", notebook_payload(), "application/json")},
    ).json()
    service = client.app.state.session_event_service
    for index in range(1, 9):
        service.publish("test.updated", {
            "sessionId": uploaded["sessionId"], "index": index,
        })

    captured = []

    async def finite_stream(*, session_id, after, is_disconnected):
        captured.append(after)
        for event in service.list(after=after, session_id=session_id):
            yield event
        yield None

    monkeypatch.setattr(service, "stream", finite_stream)
    initial = client.get(
        f"/events?sessionId={uploaded['sessionId']}&after=0",
    )
    assert initial.status_code == 200
    assert "id: 7\n" in initial.text
    response = client.get(
        f"/events?sessionId={uploaded['sessionId']}&after=0",
        headers={"Last-Event-ID": "7"},
    )
    assert response.status_code == 200
    assert captured == [0, 7]
    assert "id: 8\n" in response.text
    assert "id: 7\n" not in response.text
    assert ": keep-alive\n\n" in response.text


def test_event_stream_ignores_malformed_last_event_id(
    client, notebook_payload, monkeypatch,
):
    uploaded = client.post(
        "/notebooks/upload",
        files={"file": ("example.ipynb", notebook_payload(), "application/json")},
    ).json()
    service = client.app.state.session_event_service
    captured = []

    async def finite_stream(*, session_id, after, is_disconnected):
        captured.append(after)
        yield None

    monkeypatch.setattr(service, "stream", finite_stream)
    response = client.get(
        f"/events?sessionId={uploaded['sessionId']}&after=5",
        headers={"Last-Event-ID": "not-a-cursor"},
    )
    assert response.status_code == 200
    assert captured == [5]
    assert response.text == ": keep-alive\n\n"


def pending_risky_execution(client, uploaded):
    updated = client.post("/cells/editable/source", json={
        "sessionId": uploaded["sessionId"],
        "expectedDocumentRevision": uploaded["revision"],
        "source": "!echo guarded",
    }).json()
    service = client.app.state.kernel_execution_service
    lease = client.app.state.notebook_service.coordinator.acquire(
        operation_type="agent_turn", operation_id="turn-api",
    )
    operation = service.create_downstream(
        parent_turn_id="turn-api", session_id=uploaded["sessionId"],
        expected_revision=updated["revision"], cancel_event=threading.Event(),
    )
    worker = threading.Thread(target=lambda: service.run_downstream(
        operation.operation_id, changed_cell_ids={"editable"}, lease=lease,
    ))
    worker.start()
    for _ in range(100):
        pending = service.get(operation.operation_id)
        if pending.state == "awaiting_approval":
            break
        time.sleep(.01)
    assert pending.state == "awaiting_approval"
    serialized = client.get(f"/execution/{pending.operation_id}").json()
    assert serialized["attempts"][0]["sourcePreview"] == "!echo guarded"
    return service, lease, worker, pending, updated["revision"]


@pytest.mark.parametrize("decision", ["approve", "skip", "cancel"])
@pytest.mark.parametrize(
    "missing_field",
    ["sessionId", "expectedDocumentRevision", "turnId", "cellId"],
)
def test_risky_decision_requires_complete_correlation(
    client, notebook_payload, decision, missing_field,
):
    uploaded = client.post("/notebooks/upload", files={"file": ("example.ipynb", notebook_payload(), "application/json")}).json()
    service, lease, worker, pending, revision = pending_risky_execution(client, uploaded)
    attempt = pending.attempts[0]
    body = {
        "sessionId": uploaded["sessionId"], "expectedDocumentRevision": revision,
        "turnId": "turn-api", "cellId": "editable",
    }
    body.pop(missing_field)
    response = client.post(f"/execution/{attempt.attempt_id}/{decision}", json=body)
    assert response.status_code == 422
    service.skip(attempt.attempt_id, session_id=uploaded["sessionId"], expected_revision=revision, turn_id="turn-api", cell_id="editable")
    worker.join(2)
    client.app.state.notebook_service.coordinator.release(lease)


@pytest.mark.parametrize("decision", ["approve", "skip", "cancel"])
@pytest.mark.parametrize(
    "mismatch",
    ["sessionId", "expectedDocumentRevision", "turnId", "cellId", "attemptId"],
)
def test_risky_decision_rejects_mismatched_correlation(
    client, notebook_payload, decision, mismatch,
):
    uploaded = client.post("/notebooks/upload", files={"file": ("example.ipynb", notebook_payload(), "application/json")}).json()
    service, lease, worker, pending, revision = pending_risky_execution(client, uploaded)
    attempt = pending.attempts[0]
    body = {
        "sessionId": uploaded["sessionId"], "expectedDocumentRevision": revision,
        "turnId": "turn-api", "cellId": "editable",
    }
    path_attempt_id = attempt.attempt_id
    if mismatch == "expectedDocumentRevision":
        body[mismatch] = revision + 100
    elif mismatch == "attemptId":
        path_attempt_id = "wrong"
    else:
        body[mismatch] = "wrong"
    response = client.post(f"/execution/{path_attempt_id}/{decision}", json=body)
    assert response.status_code == 409
    service.skip(attempt.attempt_id, session_id=uploaded["sessionId"], expected_revision=revision, turn_id="turn-api", cell_id="editable")
    worker.join(2)
    client.app.state.notebook_service.coordinator.release(lease)


def test_running_manual_execution_accepts_idempotent_null_turn_cancel(
    client, notebook_payload,
):
    entered = threading.Event()
    release = threading.Event()

    class BlockingKernel(ApiKernel):
        def __init__(self):
            self.busy_attempt_id = None
            self.interrupts = 0

        def execute(self, source, attempt_id):
            self.busy_attempt_id = attempt_id
            entered.set()
            assert release.wait(2)
            self.busy_attempt_id = None
            return KernelResult([
                {"output_type": "stream", "name": "stdout", "text": "late\n"},
            ], 1, False)

        def interrupt(self):
            self.interrupts += 1

    uploaded = client.post(
        "/notebooks/upload",
        files={"file": ("example.ipynb", notebook_payload(), "application/json")},
    ).json()
    kernel = BlockingKernel()
    client.app.state.kernel_execution_service.kernel = kernel
    created = client.post("/execution/cells/editable/run", json={
        "sessionId": uploaded["sessionId"],
        "expectedDocumentRevision": uploaded["revision"],
    }).json()
    assert entered.wait(2)
    running = client.get(f"/execution/{created['operationId']}").json()
    attempt_id = running["currentExecutionAttemptId"]
    body = {
        "sessionId": uploaded["sessionId"],
        "expectedDocumentRevision": uploaded["revision"],
        "turnId": None,
        "cellId": "editable",
    }
    mismatched = client.post(
        f"/execution/{attempt_id}/cancel", json={**body, "turnId": "turn-x"},
    )
    assert mismatched.status_code == 409
    first = client.post(f"/execution/{attempt_id}/cancel", json=body)
    second = client.post(f"/execution/{attempt_id}/cancel", json=body)
    assert first.status_code == second.status_code == 200
    release.set()
    for _ in range(100):
        cancelled = client.get(f"/execution/{created['operationId']}").json()
        if cancelled["state"] == "cancelled":
            break
        time.sleep(.01)
    assert cancelled["state"] == "cancelled"
    current = client.get("/notebooks/current").json()
    assert current["revision"] == uploaded["revision"]
    assert current["cells"][1]["outputs"] == []
    assert kernel.interrupts == 1


def test_downstream_cancel_rejects_null_turn_id(client, notebook_payload):
    uploaded = client.post(
        "/notebooks/upload",
        files={"file": ("example.ipynb", notebook_payload(), "application/json")},
    ).json()
    service, lease, worker, pending, revision = pending_risky_execution(
        client, uploaded,
    )
    attempt = pending.attempts[0]
    response = client.post(f"/execution/{attempt.attempt_id}/cancel", json={
        "sessionId": uploaded["sessionId"],
        "expectedDocumentRevision": revision,
        "turnId": None,
        "cellId": "editable",
    })
    assert response.status_code == 409
    service.skip(
        attempt.attempt_id, session_id=uploaded["sessionId"],
        expected_revision=revision, turn_id="turn-api", cell_id="editable",
    )
    worker.join(2)
    client.app.state.notebook_service.coordinator.release(lease)


@pytest.mark.parametrize("decision", ["approve", "skip"])
def test_downstream_approve_and_skip_reject_a_null_turn(
    client, notebook_payload, decision,
):
    """A turn's paused cell still cannot be decided without that turn's id.

    This used to be refused by the request schema, as a 422. It cannot be any
    more: a model-initiated run pauses the same way and genuinely has no turn,
    so `turnId` has to be nullable for the person looking at the dialog to be
    able to approve at all. The invariant is unchanged and now enforced where
    it can tell the two apart — the operation's own parent turn — so a null
    turn against a turn-owned operation is a 409 correlation conflict instead.
    """
    uploaded = client.post(
        "/notebooks/upload",
        files={"file": ("example.ipynb", notebook_payload(), "application/json")},
    ).json()
    service, lease, worker, pending, revision = pending_risky_execution(
        client, uploaded,
    )
    attempt = pending.attempts[0]
    response = client.post(f"/execution/{attempt.attempt_id}/{decision}", json={
        "sessionId": uploaded["sessionId"],
        "expectedDocumentRevision": revision,
        "turnId": None,
        "cellId": "editable",
    })
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "execution_decision_conflict"
    service.skip(
        attempt.attempt_id, session_id=uploaded["sessionId"],
        expected_revision=revision, turn_id="turn-api", cell_id="editable",
    )
    worker.join(2)
    client.app.state.notebook_service.coordinator.release(lease)
