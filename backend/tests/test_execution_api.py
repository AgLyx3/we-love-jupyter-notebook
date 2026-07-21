import time

from backend.app.kernel_execution.kernel_session import KernelResult


class ApiKernel:
    kernel_session_id = "api-kernel"
    status = "idle"
    busy_attempt_id = None
    startup_timeout = 1
    cell_timeout = 1

    def execute(self, source, attempt_id):
        return KernelResult([{"output_type": "stream", "name": "stdout", "text": "ok\n"}], 1, False)
    def interrupt(self): pass
    def restart(self): self.kernel_session_id = "api-kernel-2"; return self.kernel_session_id
    def shutdown(self): pass


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


def test_kernel_restart_rejects_stale_session(client, notebook_payload):
    uploaded = client.post("/notebooks/upload", files={"file": ("example.ipynb", notebook_payload(), "application/json")}).json()
    client.app.state.kernel_execution_service.kernel = ApiKernel()
    body = {"sessionId": uploaded["sessionId"], "expectedDocumentRevision": uploaded["revision"]}
    assert client.post("/kernel/wrong/restart", json=body).status_code == 409
    restarted = client.post("/kernel/api-kernel/restart", json=body)
    assert restarted.status_code == 200
    assert restarted.json()["kernelSessionId"] == "api-kernel-2"


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
