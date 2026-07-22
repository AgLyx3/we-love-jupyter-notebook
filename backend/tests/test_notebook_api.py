import json


def upload(client, payload, filename="sample.ipynb", **data):
    return client.post(
        "/notebooks/upload",
        files={"file": (filename, payload, "application/x-ipynb+json")},
        data=data,
    )


def test_backend_readiness(client):
    response = client.get("/health/ready")
    assert response.status_code == 200
    assert response.json() == {"status": "ready"}


def test_upload_current_edit_and_download_round_trip(client, notebook_payload):
    response = upload(client, notebook_payload())
    assert response.status_code == 201
    created = response.json()
    assert created["revision"] == 0
    assert created["dirty"] is False
    assert [cell["cellId"] for cell in created["cells"]] == ["intro", "editable"]

    current = client.get("/notebooks/current")
    assert current.status_code == 200
    assert current.json()["sessionId"] == created["sessionId"]

    edited = client.post(
        "/cells/editable/source",
        json={
            "sessionId": created["sessionId"],
            "expectedDocumentRevision": 0,
            "source": "value = 42\n",
        },
    )
    assert edited.status_code == 200
    assert edited.json()["revision"] == 1
    assert edited.json()["source"] == "value = 42\n"

    downloaded = client.get("/notebooks/download")
    assert downloaded.status_code == 200
    assert "attachment;" in downloaded.headers["content-disposition"]
    document = json.loads(downloaded.content)
    assert document["cells"][1]["source"] == "value = 42\n"


def test_source_edit_returns_structured_revision_conflict(client, notebook_payload):
    created = upload(client, notebook_payload()).json()
    first = client.post(
        "/cells/editable/source",
        json={
            "sessionId": created["sessionId"],
            "expectedDocumentRevision": 0,
            "source": "first = True",
        },
    )
    assert first.status_code == 200

    stale = client.post(
        "/cells/editable/source",
        json={
            "sessionId": created["sessionId"],
            "expectedDocumentRevision": 0,
            "source": "stale = True",
        },
    )
    assert stale.status_code == 409
    assert stale.json()["error"] == {
        "code": "revision_conflict",
        "message": "Notebook revision does not match",
        "details": {"currentDocumentRevision": 1},
    }


def test_replacement_upload_requires_current_session_and_revision(client, notebook_payload):
    created = upload(client, notebook_payload(), filename="first.ipynb").json()

    missing_preconditions = upload(client, notebook_payload(), filename="second.ipynb")
    assert missing_preconditions.status_code == 409
    assert missing_preconditions.json()["error"]["code"] == "replacement_precondition_required"

    replaced = upload(
        client,
        notebook_payload(cell_ids=("new-intro", "new-code")),
        filename="second.ipynb",
        sessionId=created["sessionId"],
        expectedDocumentRevision=str(created["revision"]),
    )
    assert replaced.status_code == 201
    assert replaced.json()["filename"] == "second.ipynb"
    assert replaced.json()["sessionId"] != created["sessionId"]


def test_api_reports_missing_notebook_and_invalid_upload(client):
    current = client.get("/notebooks/current")
    assert current.status_code == 404
    assert current.json()["error"]["code"] == "notebook_not_loaded"

    invalid = upload(client, b"not-json", filename="bad.ipynb")
    assert invalid.status_code == 422
    assert invalid.json()["error"]["code"] == "invalid_notebook"


def test_close_notebook_checks_preconditions_and_allows_fresh_upload(
    client, notebook_payload,
):
    created = upload(client, notebook_payload()).json()

    wrong_session = client.request("DELETE", "/notebooks/current", json={
        "sessionId": "stale-session",
        "expectedDocumentRevision": created["revision"],
    })
    assert wrong_session.status_code == 409
    assert wrong_session.json()["error"]["code"] == "session_conflict"

    stale = client.request("DELETE", "/notebooks/current", json={
        "sessionId": created["sessionId"],
        "expectedDocumentRevision": created["revision"] + 1,
    })
    assert stale.status_code == 409
    assert client.get("/notebooks/current").status_code == 200

    closed = client.request("DELETE", "/notebooks/current", json={
        "sessionId": created["sessionId"],
        "expectedDocumentRevision": created["revision"],
    })
    assert closed.status_code == 200
    assert closed.json() == {
        "closedSessionId": created["sessionId"],
        "cleanupErrors": [],
    }
    assert client.get("/notebooks/current").status_code == 404
    assert not client.app.state.session_event_service.is_active(
        created["sessionId"]
    )

    reopened = upload(client, notebook_payload(), filename="fresh.ipynb")
    assert reopened.status_code == 201
    assert reopened.json()["sessionId"] != created["sessionId"]


def test_close_reports_cleanup_diagnostics_without_partial_failure(
    client, notebook_payload,
):
    created = upload(client, notebook_payload()).json()

    def fail_cleanup(_session_id, _revision):
        raise RuntimeError("diagnostic cleanup failure")

    client.app.state.notebook_service.register_session_replacement_listener(
        fail_cleanup
    )
    response = client.request("DELETE", "/notebooks/current", json={
        "sessionId": created["sessionId"],
        "expectedDocumentRevision": created["revision"],
    })

    assert response.status_code == 200
    assert response.json()["cleanupErrors"] == [
        "RuntimeError: diagnostic cleanup failure",
    ]
    assert client.get("/notebooks/current").status_code == 404


def test_close_rejects_active_mutation_before_preconditions(client, notebook_payload):
    created = upload(client, notebook_payload()).json()
    coordinator = client.app.state.notebook_service.coordinator
    lease = coordinator.acquire(operation_type="agent_turn", operation_id="turn-1")
    try:
        response = client.request("DELETE", "/notebooks/current", json={
            "sessionId": "stale-session",
            "expectedDocumentRevision": 999,
        })
    finally:
        coordinator.release(lease)

    assert response.status_code == 409
    assert response.json()["error"] == {
        "code": "mutation_conflict",
        "message": "Another notebook mutation is active",
        "details": {
            "activeOperationType": "agent_turn",
            "activeOperationId": "turn-1",
            "currentDocumentRevision": created["revision"],
        },
    }
    assert client.get("/notebooks/current").status_code == 200


def test_active_lease_precedes_stale_edit_and_invalid_replacement(
    client, notebook_payload
):
    created = upload(client, notebook_payload()).json()
    coordinator = client.app.state.notebook_service.coordinator
    lease = coordinator.acquire(operation_type="agent_turn", operation_id="turn-1")

    stale_edit = client.post(
        "/cells/editable/source",
        json={
            "sessionId": "stale-session",
            "expectedDocumentRevision": 999,
            "source": "should_not_apply = True",
        },
    )
    invalid_replacement = upload(
        client,
        b"not-json",
        sessionId="stale-session",
        expectedDocumentRevision="999",
    )

    for response in (stale_edit, invalid_replacement):
        assert response.status_code == 409
        assert response.json()["error"] == {
            "code": "mutation_conflict",
            "message": "Another notebook mutation is active",
            "details": {
                "activeOperationType": "agent_turn",
                "activeOperationId": "turn-1",
                "currentDocumentRevision": created["revision"],
            },
        }
    coordinator.release(lease)


def test_request_validation_errors_use_structured_envelope(client, notebook_payload):
    upload(client, notebook_payload())

    response = client.post("/cells/editable/source", json={"source": 12})

    assert response.status_code == 422
    body = response.json()["error"]
    assert body["code"] == "invalid_request"
    assert body["message"] == "Request validation failed"
    assert isinstance(body["details"]["errors"], list)


def test_source_edit_reports_session_conflict_and_missing_cell(client, notebook_payload):
    created = upload(client, notebook_payload()).json()

    wrong_session = client.post(
        "/cells/editable/source",
        json={
            "sessionId": "wrong-session",
            "expectedDocumentRevision": created["revision"],
            "source": "value = 2",
        },
    )
    assert wrong_session.status_code == 409
    assert wrong_session.json()["error"]["code"] == "session_conflict"

    missing_cell = client.post(
        "/cells/does-not-exist/source",
        json={
            "sessionId": created["sessionId"],
            "expectedDocumentRevision": created["revision"],
            "source": "value = 2",
        },
    )
    assert missing_cell.status_code == 404
    assert missing_cell.json()["error"]["code"] == "cell_not_found"


def test_download_header_sanitizes_uploaded_filename(client, notebook_payload):
    created = upload(
        client,
        notebook_payload(),
        filename='../../unsafe"\r\nX-Evil: yes.ipynb',
    ).json()

    response = client.get("/notebooks/download")

    disposition = response.headers["content-disposition"]
    assert response.status_code == 200
    assert "\r" not in disposition and "\n" not in disposition
    assert "x-evil" not in response.headers
    assert created["filename"] in disposition


def test_normalized_cell_ids_persist_in_download(client):
    payload = json.dumps(
        {
            "cells": [
                {"cell_type": "markdown", "metadata": {}, "source": ["one"]},
                {"cell_type": "markdown", "metadata": {}, "source": ["two"]},
            ],
            "metadata": {},
            "nbformat": 4,
            "nbformat_minor": 5,
        }
    ).encode()
    created = upload(client, payload).json()

    downloaded = json.loads(client.get("/notebooks/download").content)

    response_ids = [cell["cellId"] for cell in created["cells"]]
    assert created["revision"] == 1
    assert created["dirty"] is True
    assert [cell["id"] for cell in downloaded["cells"]] == response_ids


def test_oversized_source_returns_structured_error_without_revision_change(
    client, notebook_payload
):
    created = upload(client, notebook_payload()).json()

    response = client.post(
        "/cells/editable/source",
        json={
            "sessionId": created["sessionId"],
            "expectedDocumentRevision": created["revision"],
            "source": "x" * (5 * 1024 * 1024),
        },
    )

    assert response.status_code == 413
    assert response.json()["error"]["code"] == "notebook_too_large"
    assert client.get("/notebooks/current").json()["revision"] == created["revision"]
