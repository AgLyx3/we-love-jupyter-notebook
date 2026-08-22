"""The risky-cell gate, applied to runs a model asked for.

The gate is what makes the browser tab load-bearing rather than decorative when
the editor is driven by tools: a person clicking Run has already authorised it,
but nothing authorises a run a model asked for, so those stop and wait.
"""

from __future__ import annotations

import json
import time

import pytest

from backend.tests.test_execution_api import ApiKernel


RISKY_SOURCE = "import subprocess\nsubprocess.run(['echo', 'hi'])\n"
SAFE_SOURCE = "value = 1 + 1\n"


def notebook_with(source: str, cell_id: str = "target") -> bytes:
    return json.dumps(
        {
            "cells": [
                {
                    "cell_type": "code",
                    "id": cell_id,
                    "metadata": {},
                    "source": [source],
                    "execution_count": None,
                    "outputs": [],
                }
            ],
            "metadata": {},
            "nbformat": 4,
            "nbformat_minor": 5,
        }
    ).encode()


@pytest.fixture
def client(client):
    """The real kernel is irrelevant here — these tests are about the gate.

    Substituting the suite's fake keeps them fast and deterministic; a real
    kernel takes seconds to boot and would make the approval timing racy.
    """
    client.app.state.kernel_execution_service.kernel = ApiKernel()
    return client


@pytest.fixture
def risky(client):
    response = client.post(
        "/notebooks/upload",
        files={"file": ("risky.ipynb", notebook_with(RISKY_SOURCE), "application/x-ipynb+json")},
    )
    assert response.status_code == 201
    return response.json()


def run(client, snapshot, **extra):
    return client.post(
        "/execution/cells/target/run",
        json={
            "sessionId": snapshot["sessionId"],
            "expectedDocumentRevision": snapshot["revision"],
            **extra,
        },
    )


def settle(client, operation_id, until, tries=200):
    """Poll until the operation reaches one of `until`, then return it."""
    for _ in range(tries):
        operation = client.get(f"/execution/{operation_id}").json()
        if operation["state"] in until:
            return operation
        time.sleep(0.01)
    raise AssertionError(
        f"never reached {until}; last state was {operation['state']!r}"
    )


def test_a_model_initiated_risky_cell_waits_for_a_person(client, risky):
    """The finding this whole change exists for.

    Before it, this exact cell was classified `confirm` and ran anyway, because
    it arrived on the path built for a human pressing Run.
    """
    started = run(client, risky, initiator="mcp")
    assert started.status_code == 202
    assert started.json()["kind"] == "mcp"

    operation = settle(client, started.json()["operationId"], {"awaiting_approval"})
    attempt = operation["attempts"][-1]
    assert attempt["state"] == "awaiting_approval"
    assert attempt["risk"]["level"] == "confirm"
    assert attempt["decision"] is None
    assert attempt["outputs"] == []

    client.post(
        f"/execution/{attempt['executionAttemptId']}/cancel",
        json={
            "sessionId": risky["sessionId"],
            "expectedDocumentRevision": operation["currentDocumentRevision"],
            "turnId": None,
            "cellId": "target",
        },
    )


def test_a_person_pressing_run_is_still_not_re_asked(client, risky):
    """The existing asymmetry is deliberate and must survive.

    Re-prompting someone who just clicked Run would be noise, and treating the
    click as insufficient would make the editor unusable.
    """
    started = run(client, risky)
    assert started.json()["kind"] == "manual"
    operation = settle(client, started.json()["operationId"], {"completed", "failed"})
    assert operation["state"] == "completed"
    assert operation["attempts"][-1]["decision"] is None


def test_the_default_initiator_is_the_person(client, risky):
    """A body with no initiator behaves exactly as it did before this change."""
    assert run(client, risky).json()["kind"] == "manual"


def test_a_model_initiated_safe_cell_is_not_gated(client, notebook_payload):
    """Only `confirm` cells stop. A safe cell runs straight through."""
    created = client.post(
        "/notebooks/upload",
        files={"file": ("safe.ipynb", notebook_with(SAFE_SOURCE), "application/x-ipynb+json")},
    ).json()
    started = run(client, created, initiator="mcp")
    operation = settle(client, started.json()["operationId"], {"completed", "failed"})
    assert operation["state"] == "completed"
    assert operation["attempts"][-1]["risk"]["level"] == "safe"


def test_an_unknown_initiator_is_rejected(client, risky):
    """Not silently treated as a person, which would drop the gate."""
    assert run(client, risky, initiator="something-else").status_code == 422


def test_approval_lets_a_gated_run_proceed(client, risky):
    """The other half of the gate: approving must actually run the cell.

    This is where keying the approval rule on `kind` rather than on the absence
    of a parent turn would strand the run — the operation has no turn id, so a
    kind-based rule demands one that cannot exist and every decision conflicts.
    """
    started = run(client, risky, initiator="mcp")
    operation = settle(client, started.json()["operationId"], {"awaiting_approval"})
    attempt = operation["attempts"][-1]

    approved = client.post(
        f"/execution/{attempt['executionAttemptId']}/approve",
        json={
            "sessionId": risky["sessionId"],
            "expectedDocumentRevision": operation["currentDocumentRevision"],
            "turnId": None,
            "cellId": "target",
        },
    )
    assert approved.status_code == 200

    settled = settle(client, started.json()["operationId"], {"completed", "failed"})
    assert settled["state"] == "completed"
    assert settled["attempts"][-1]["decision"] == "approve"


def test_skipping_a_gated_run_leaves_the_cell_unrun(client, risky):
    started = run(client, risky, initiator="mcp")
    operation = settle(client, started.json()["operationId"], {"awaiting_approval"})
    attempt = operation["attempts"][-1]

    skipped = client.post(
        f"/execution/{attempt['executionAttemptId']}/skip",
        json={
            "sessionId": risky["sessionId"],
            "expectedDocumentRevision": operation["currentDocumentRevision"],
            "turnId": None,
            "cellId": "target",
        },
    )
    assert skipped.status_code == 200

    settled = settle(client, started.json()["operationId"], {"validation_incomplete", "failed"})
    assert settled["state"] == "validation_incomplete"
    assert settled["attempts"][-1]["state"] == "skipped"
    assert settled["attempts"][-1]["outputs"] == []


def test_a_gated_run_is_visible_in_session_status(client, risky):
    """What the browser tab polls.

    The gate is only worth anything if the person can see the pending decision,
    and the tab learns about a run it did not start through this endpoint.
    """
    started = run(client, risky, initiator="mcp")
    settle(client, started.json()["operationId"], {"awaiting_approval"})

    status = client.get("/session/status").json()
    active = status["activeExecution"]
    assert active is not None
    assert active["kind"] == "mcp"
    assert active["state"] == "awaiting_approval"
    assert active["parentTurnId"] is None
    # The browser sends `turnId: operation.parentTurnId` when deciding, so a
    # null here is what makes the tab's existing approval controls work.

    client.post(
        f"/execution/{active['attempts'][-1]['executionAttemptId']}/cancel",
        json={
            "sessionId": risky["sessionId"],
            "expectedDocumentRevision": active["currentDocumentRevision"],
            "turnId": None,
            "cellId": "target",
        },
    )
