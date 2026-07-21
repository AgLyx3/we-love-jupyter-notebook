import json


def upload(client, payload, filename="sample.ipynb", **data):
    return client.post(
        "/notebooks/upload",
        files={"file": (filename, payload, "application/x-ipynb+json")},
        data=data,
    )


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
