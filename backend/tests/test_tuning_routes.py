"""HTTP surface for plot tuning.

The load-bearing test here is that `open` starts nothing. Decision D5 in the
design says the user is shown what a warm-up will re-fire *before* it re-fires
it, and that promise is only real if analysis and execution are genuinely
separate calls — not merely presented as separate in the UI.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from backend.app.main import create_app


def notebook_bytes(cells: list[dict]) -> bytes:
    return json.dumps({
        "cells": cells,
        "metadata": {"kernelspec": {"name": "python3", "display_name": "Python 3"}},
        "nbformat": 4, "nbformat_minor": 5,
    }).encode()


def code(cell_id: str, source: str) -> dict:
    return {
        "cell_type": "code", "id": cell_id, "metadata": {},
        "source": source.splitlines(keepends=True),
        "execution_count": None, "outputs": [],
    }


TUNABLE = [
    code("params", "BINS = 30\nCOLOR = 'red'\n"),
    code("data", "samples = make(BINS)\n"),
    code("plot", "plt.hist(samples, bins=BINS, color=COLOR)\n"),
]


@pytest.fixture
def load():
    """Open a notebook in its own app.

    A fresh app per notebook rather than re-uploading into one: replacing the
    active notebook requires replacement preconditions, and threading those
    through would test the upload route rather than the tuning routes.
    """
    clients = []

    def build(cells):
        app = create_app()
        client = TestClient(app)
        client.__enter__()
        clients.append(client)
        response = client.post(
            "/notebooks/upload",
            files={"file": ("n.ipynb", notebook_bytes(cells), "application/json")},
        )
        assert response.status_code == 201, response.text
        body = response.json()
        return client, body["sessionId"], body["revision"]

    yield build
    for client in clients:
        client.__exit__(None, None, None)


@pytest.fixture
def opened(load):
    return load(TUNABLE)


def open_panel(bundle, cell_id="plot"):
    client, session_id, revision = bundle
    return client.post("/tuning/open", json={
        "sessionId": session_id, "expectedDocumentRevision": revision,
        "cellId": cell_id,
    })


def test_open_returns_the_knobs_the_plot_depends_on(opened):
    body = open_panel(opened).json()
    assert {knob["name"] for knob in body["knobs"]} == {"BINS", "COLOR"}


def test_open_attaches_bounds_to_numeric_knobs_only(opened):
    knobs = {knob["name"]: knob for knob in open_panel(opened).json()["knobs"]}
    assert knobs["BINS"]["bounds"] == {"minimum": 6.0, "maximum": 150.0, "step": 1.0}
    # A string has no slider, so it must carry no range rather than a fake one.
    assert knobs["COLOR"]["bounds"] is None


def test_open_starts_no_kernel(opened):
    """The whole point of splitting open from warm.

    If this ever fails, the risky-cell confirmation has become decorative: the
    replay would already have happened by the time the dialog is on screen.
    """
    client, _session, _revision = opened
    open_panel(opened)
    assert client.app.state.plot_tuning_panel_service._shadow is None
    assert client.app.state.kernel_execution_service.kernel_status()["state"] \
        == "not_started"


def test_open_reports_risky_chain_cells_before_anything_runs(load):
    client, session_id, revision = load([
        code("fetch", "import requests\nraw = requests.get('http://x').json()\n"),
        code("params", "BINS = 30\n"),
        code("plot", "plt.hist(raw, bins=BINS)\n"),
    ])
    response = client.post("/tuning/open", json={
        "sessionId": session_id, "expectedDocumentRevision": revision,
        "cellId": "plot",
    })
    flagged = response.json()["risky"]
    assert [item["cellId"] for item in flagged] == ["fetch"]
    assert "network_client" in flagged[0]["categories"]
    assert client.app.state.plot_tuning_panel_service._shadow is None


def test_open_explains_rejections_rather_than_omitting_them(load):
    client, session_id, revision = load([
        code("setup", "n = 1\nn = 2\n"),
        code("plot", "plt.plot(n)\n"),
    ])
    response = client.post("/tuning/open", json={
        "sessionId": session_id, "expectedDocumentRevision": revision,
        "cellId": "plot",
    }).json()
    assert response["knobs"] == []
    rejection = next(item for item in response["rejections"] if item["name"] == "n")
    assert rejection["reason"] == "rebound"
    # The panel renders this sentence; an empty detail would strand the user on
    # "no tunable variables found".
    assert "assigned more than once" in rejection["detail"]


def test_open_refuses_a_markdown_cell(load):
    client, session_id, revision = load([
        {"cell_type": "markdown", "id": "note", "metadata": {}, "source": ["# hi\n"]},
        code("plot", "plt.plot(1)\n"),
    ])
    response = client.post("/tuning/open", json={
        "sessionId": session_id, "expectedDocumentRevision": revision,
        "cellId": "note",
    })
    assert response.status_code == 400
    assert "code cells" in response.json()["detail"]


def test_open_rejects_a_stale_revision(opened):
    client, session_id, revision = opened
    response = client.post("/tuning/open", json={
        "sessionId": session_id, "expectedDocumentRevision": revision + 5,
        "cellId": "plot",
    })
    assert response.status_code == 409


def test_open_rejects_an_unknown_cell(opened):
    client, session_id, revision = opened
    response = client.post("/tuning/open", json={
        "sessionId": session_id, "expectedDocumentRevision": revision,
        "cellId": "nope",
    })
    assert response.status_code == 400


def test_previewing_an_unknown_shadow_is_a_404(opened):
    client, _session, _revision = opened
    response = client.post("/tuning/deadbeef/preview", json={"values": {"BINS": 5}})
    assert response.status_code == 404


def test_closing_an_unknown_shadow_succeeds(opened):
    # The client calls this on unmount, which races the idle timeout and the
    # revision check; making it a 404 would surface phantom errors.
    client, _session, _revision = opened
    assert client.delete("/tuning/deadbeef").status_code == 200
