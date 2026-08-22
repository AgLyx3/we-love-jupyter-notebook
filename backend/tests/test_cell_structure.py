"""Adding and removing cells directly, rather than only through an agent turn.

Structural change existed but had exactly one entrance: a Trusted-mode agent
turn. So "add a cell that plots this" was not expressible by anything driving
the editor through its API — an eval run found a model saying so plainly and
declining to improvise.

These use the same structural applier that Trusted turns use, which is what
gives an added cell its provenance metadata and keeps the apply atomic.
"""

from __future__ import annotations

import json

import pytest


def notebook_bytes(*sources: str) -> bytes:
    return json.dumps(
        {
            "cells": [
                {
                    "cell_type": "code", "id": f"cell{index}", "metadata": {},
                    "source": [source], "outputs": [], "execution_count": None,
                }
                for index, source in enumerate(sources)
            ],
            "metadata": {}, "nbformat": 4, "nbformat_minor": 5,
        }
    ).encode()


@pytest.fixture
def opened(client):
    created = client.post(
        "/notebooks/upload",
        files={"file": ("n.ipynb", notebook_bytes("a = 1\n", "b = 2\n"), "application/json")},
    )
    assert created.status_code == 201
    return created.json()


def mutation(snapshot):
    return {
        "sessionId": snapshot["sessionId"],
        "expectedDocumentRevision": snapshot["revision"],
    }


def ids(snapshot):
    return [cell["cellId"] for cell in snapshot["cells"]]


# --- inserting ---------------------------------------------------------------


def test_a_cell_is_added_at_the_end_by_default(client, opened):
    response = client.post(
        "/cells", json={**mutation(opened), "source": "print(sum([a, b]))\n"}
    )
    assert response.status_code == 201
    snapshot = response.json()
    assert len(snapshot["cells"]) == 3
    assert snapshot["cells"][-1]["source"] == "print(sum([a, b]))\n"
    assert snapshot["revision"] == opened["revision"] + 1


def test_a_cell_can_be_added_at_a_position(client, opened):
    snapshot = client.post(
        "/cells", json={**mutation(opened), "source": "first\n", "cellType": "markdown", "index": 0}
    ).json()
    assert snapshot["cells"][0]["source"] == "first\n"
    assert snapshot["cells"][0]["cellType"] == "markdown"
    assert ids(snapshot)[1:] == ["cell0", "cell1"]


def test_an_added_cell_is_marked_as_agent_authored(client, opened):
    """The editor renders this as a badge reading "review before running".

    An added code cell is inert until somebody runs it, and that badge is how a
    person knows which cells arrived without them typing them.
    """
    snapshot = client.post("/cells", json={**mutation(opened), "source": "x\n"}).json()
    assert snapshot["cells"][-1]["metadata"]["agent_authored"] is True
    assert all(
        not cell["metadata"].get("agent_authored") for cell in snapshot["cells"][:-1]
    )


def test_an_added_code_cell_has_no_output_and_has_not_run(client, opened):
    snapshot = client.post("/cells", json={**mutation(opened), "source": "1/0\n"}).json()
    added = snapshot["cells"][-1]
    assert added["outputs"] == []
    assert added["executionCount"] is None


def test_existing_cells_keep_their_identity_and_outputs(client, opened, notebook_payload):
    """An insert rewrites the whole cell list, so this is the invariant that
    matters most: everything already there must come through untouched."""
    edited = client.post(
        "/cells/cell0/source", json={**mutation(opened), "source": "a = 99\n"}
    ).json()
    snapshot = client.post(
        "/cells", json={"sessionId": edited["sessionId"],
                        "expectedDocumentRevision": edited["revision"], "source": "new\n"}
    ).json()
    assert ids(snapshot)[:2] == ["cell0", "cell1"]
    assert snapshot["cells"][0]["source"] == "a = 99\n"


def test_an_index_past_the_end_is_refused_rather_than_clamped(client, opened):
    """Clamping would silently put the cell somewhere the caller did not ask
    for, which is worse than saying the index was wrong."""
    response = client.post("/cells", json={**mutation(opened), "source": "x\n", "index": 9})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "cell_index_out_of_range"


def test_a_negative_index_is_refused(client, opened):
    assert client.post(
        "/cells", json={**mutation(opened), "source": "x\n", "index": -1}
    ).status_code == 400


def test_an_unsupported_cell_type_is_refused(client, opened):
    assert client.post(
        "/cells", json={**mutation(opened), "source": "x\n", "cellType": "raw"}
    ).status_code == 422


def test_inserting_against_a_stale_revision_is_refused(client, opened):
    stale = {"sessionId": opened["sessionId"], "expectedDocumentRevision": opened["revision"]}
    client.post("/cells", json={**stale, "source": "one\n"})
    conflicted = client.post("/cells", json={**stale, "source": "two\n"})
    assert conflicted.status_code == 409
    assert len(client.get("/notebooks/current").json()["cells"]) == 3


# --- deleting ----------------------------------------------------------------


def test_a_cell_is_removed_by_id(client, opened):
    response = client.request(
        "DELETE", "/cells/cell0", json=mutation(opened)
    )
    assert response.status_code == 200
    assert ids(response.json()) == ["cell1"]


def test_deleting_an_unknown_cell_is_refused(client, opened):
    response = client.request("DELETE", "/cells/nope", json=mutation(opened))
    assert response.status_code == 404
    assert response.json()["error"]["code"] == "cell_not_found"


def test_the_last_cell_cannot_be_deleted(client, opened):
    """nbformat tolerates it, but an empty notebook has no cell to select, edit
    or run — there is no way back from it in the editor."""
    after_first = client.request("DELETE", "/cells/cell0", json=mutation(opened)).json()
    response = client.request(
        "DELETE", "/cells/cell1", json=mutation(after_first)
    )
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "last_cell"
    assert len(client.get("/notebooks/current").json()["cells"]) == 1


def test_deleting_against_a_stale_revision_is_refused(client, opened):
    stale = mutation(opened)
    client.post("/cells", json={**stale, "source": "new\n"})
    conflicted = client.request("DELETE", "/cells/cell0", json=stale)
    assert conflicted.status_code == 409
    assert "cell0" in ids(client.get("/notebooks/current").json())


def test_a_round_trip_leaves_the_notebook_as_it_was(client, opened):
    added = client.post("/cells", json={**mutation(opened), "source": "temp\n"}).json()
    new_id = ids(added)[-1]
    restored = client.request("DELETE", f"/cells/{new_id}", json=mutation(added)).json()
    assert ids(restored) == ids(opened)
    assert [cell["source"] for cell in restored["cells"]] == [
        cell["source"] for cell in opened["cells"]
    ]
