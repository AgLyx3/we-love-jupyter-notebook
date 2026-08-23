"""The tools against a real editor process, a real kernel, and the real gate.

Everything else about the MCP layer is tested against fakes. This drives the
whole thing the way a client would: the tools start the editor themselves, talk
to it over HTTP, and a person's approval is delivered through the same endpoint
the browser tab uses.

Slow by nature — it boots uvicorn and a Python kernel — so it is one test per
behaviour that could only be wrong in the real arrangement.
"""

from __future__ import annotations

import asyncio
import json
import time

import httpx
import pytest
from mcp.server.mcpserver.exceptions import ToolError

from backend.app.mcp.server import build_server

SAFE = "values = [2, 4, 6]\ntotal = sum(values)\nprint(f'Total: {total}')\n"
RISKY = "import subprocess\nprint(subprocess.run(['echo','ran'],capture_output=True,text=True).stdout)\n"


def notebook(*sources: str) -> bytes:
    return json.dumps(
        {
            "cells": [
                {
                    "cell_type": "code",
                    "id": f"cell{index}",
                    "metadata": {},
                    "source": [source],
                    "execution_count": None,
                    "outputs": [],
                }
                for index, source in enumerate(sources)
            ],
            "metadata": {
                "kernelspec": {"display_name": "Python 3", "language": "python", "name": "python3"},
                "language_info": {"name": "python", "version": "3"},
            },
            "nbformat": 4,
            "nbformat_minor": 5,
        }
    ).encode()


def call(server, name, **arguments):
    """Invoke a tool the way a client does, and hand back its payload."""
    result = asyncio.run(server.call_tool(name, arguments))
    if getattr(result, "structuredContent", None):
        return result.structuredContent
    return json.loads(result.content[0].text)


@pytest.fixture
def workspace(tmp_path):
    root = tmp_path / "project"
    root.mkdir()
    (root / "work.ipynb").write_bytes(notebook(SAFE, RISKY))
    (tmp_path / "outside.ipynb").write_bytes(notebook(SAFE))
    return root, tmp_path / "outside.ipynb"


@pytest.fixture
def editor(workspace):
    root, _ = workspace
    server, tools = build_server(workspace_root=str(root), open_browser=False)
    try:
        yield server, tools
    finally:
        tools.close()


def test_the_tools_drive_a_real_notebook_end_to_end(editor, workspace):
    server, tools = editor
    root, _ = workspace

    opened = call(server, "notebook_open", path=str(root / "work.ipynb"))
    assert opened["filename"] == "work.ipynb"
    assert opened["cellCount"] == 2

    read = call(server, "notebook_read")
    assert [cell["cellId"] for cell in read["cells"]] == ["cell0", "cell1"]

    edited = call(
        server, "notebook_set_cell_source",
        cell_id="cell0", source="values = [10, 20]\ntotal = sum(values)\nprint(f'Total: {total}')\n",
    )
    assert edited["dirty"] is True

    ran = call(server, "notebook_run_cell", cell_id="cell0")
    assert ran["state"] == "completed"
    printed = "".join(
        output.get("text", "")
        for cell in ran["cells"] for output in cell.get("outputs", [])
    )
    assert "Total: 30" in printed

    saved = call(server, "notebook_save")
    assert saved["dirty"] is False
    assert b"[10, 20]" in (root / "work.ipynb").read_bytes()


def test_a_risky_cell_waits_for_a_person_and_runs_once_approved(editor, workspace):
    """The behaviour the whole browser tab exists for.

    The run is started from a tool, so it is model-initiated and gated. It must
    not run until a decision arrives through the endpoint the tab posts to.
    """
    server, tools = editor
    root, _ = workspace
    call(server, "notebook_open", path=str(root / "work.ipynb"))

    started: dict = {}

    def run_in_background():
        started["result"] = call(server, "notebook_run_cell", cell_id="cell1")

    import threading

    worker = threading.Thread(target=run_in_background, daemon=True)
    worker.start()

    # Wait for the pause to appear, then confirm nothing has run.
    deadline = time.monotonic() + 60
    awaiting = None
    while time.monotonic() < deadline:
        status = call(server, "notebook_status")
        execution = status.get("activeExecution") or {}
        if execution.get("awaitingApproval"):
            awaiting = execution
            break
        time.sleep(0.2)

    assert awaiting is not None, "the risky cell never paused"
    assert awaiting["kind"] == "mcp"
    assert awaiting["awaitingApproval"]["cellId"] == "cell1"
    assert "Starts another process" in awaiting["awaitingApproval"]["reasons"]

    # Approve it exactly as the browser tab does.
    operation = httpx.get(f"{tools.editor.api_url}/execution/{awaiting['operationId']}").json()
    attempt = operation["attempts"][-1]
    approved = httpx.post(
        f"{tools.editor.api_url}/execution/{attempt['executionAttemptId']}/approve",
        json={
            "sessionId": operation["sessionId"],
            "expectedDocumentRevision": operation["currentDocumentRevision"],
            "turnId": None,
            "cellId": "cell1",
        },
    )
    assert approved.status_code == 200

    worker.join(timeout=60)
    result = started["result"]
    assert result["state"] == "completed"
    assert "ran" in "".join(
        output.get("text", "")
        for cell in result["cells"] for output in cell.get("outputs", [])
    )


def test_a_notebook_outside_the_workspace_is_refused(editor, workspace):
    """Phase 4's boundary, reached through the tool rather than the API."""
    server, _ = editor
    _, outside = workspace
    with pytest.raises(ToolError, match="workspace"):
        call(server, "notebook_open", path=str(outside))


def test_an_edit_against_a_stale_revision_is_refused_not_retried(editor, workspace):
    """Someone edits in the tab; the tool's next write must not overwrite it.

    This is the contract the first draft of the plan got wrong by proposing a
    silent retry. The edit has to fail, and say so, with the other change intact.
    """
    server, tools = editor
    root, _ = workspace
    opened = call(server, "notebook_open", path=str(root / "work.ipynb"))

    # A change made in the tab, which the tool has not seen.
    typed_in_the_tab = httpx.post(
        f"{tools.editor.api_url}/cells/cell0/source",
        json={
            "sessionId": opened["sessionId"],
            "expectedDocumentRevision": opened["revision"],
            "source": "# a person typed this\n",
        },
    )
    assert typed_in_the_tab.status_code == 200

    with pytest.raises(ToolError) as raised:
        call(server, "notebook_set_cell_source", cell_id="cell0", source="# the model's edit\n")
    message = str(raised.value)
    assert "changed since you last read it" in message
    assert "has not been made" in message

    current = httpx.get(f"{tools.editor.api_url}/notebooks/current").json()
    source = next(c["source"] for c in current["cells"] if c["cellId"] == "cell0")
    assert source == "# a person typed this\n", "the person's edit was overwritten"

    # Re-reading is what unblocks it, which is what the error told the caller.
    call(server, "notebook_read")
    call(server, "notebook_set_cell_source", cell_id="cell0", source="# the model's edit\n")


def test_reading_a_plot_notebook_does_not_return_image_bytes(editor, workspace):
    """The §5.4 measurement, enforced against a real kernel rather than a fixture."""
    server, _ = editor
    root, _ = workspace
    plot = root / "plot.ipynb"
    # `%matplotlib inline` is what registers the PNG formatter — without it a
    # Figure renders only as text/plain and there is no image to elide. The
    # repo's own plot fixtures do the same.
    plot.write_bytes(
        notebook(
            "%matplotlib inline\nimport matplotlib.pyplot as plt\n",
            "fig, ax = plt.subplots()\nax.plot([1, 2, 3])\nplt.show()\n",
        )
    )
    call(server, "notebook_open", path=str(plot))
    ran = call(server, "notebook_run_all")
    assert ran["state"] == "completed"

    read = call(server, "notebook_read")
    serialized = json.dumps(read)
    assert "iVBORw0KGgo" not in serialized, "a base64 PNG reached the tool result"
    images = [
        image
        for cell in read["cells"]
        for output in cell.get("outputs", [])
        for image in output.get("images", [])
    ]
    assert images and images[0]["mimeType"] == "image/png"
    assert images[0]["bytes"] > 1000
    assert len(serialized) < 20_000


def test_a_cell_can_be_added_and_run_through_the_tools(editor, workspace):
    """The gap the eval found: "add a cell that does X" had no tool at all.

    Exercised against a real editor because the delete path sends a body on a
    DELETE, which is legal but not universally handled — worth proving over
    real HTTP rather than a fake.
    """
    server, _ = editor
    root, _ = workspace
    opened = call(server, "notebook_open", path=str(root / "work.ipynb"))
    assert opened["cellCount"] == 2

    added = call(
        server, "notebook_insert_cell",
        source="print('added by a tool')\n", cell_type="code",
    )
    assert added["cellCount"] == 3
    assert added["cellId"]
    assert "not run" in added["note"]

    # It exists but has produced nothing until it is run.
    read = call(server, "notebook_read")
    new_cell = next(c for c in read["cells"] if c["cellId"] == added["cellId"])
    assert new_cell.get("outputs") in (None, [])

    ran = call(server, "notebook_run_cell", cell_id=added["cellId"])
    assert ran["state"] == "completed"
    assert "added by a tool" in "".join(
        output.get("text", "")
        for cell in ran["cells"] for output in cell.get("outputs", [])
    )

    removed = call(server, "notebook_delete_cell", cell_id=added["cellId"])
    assert removed["cellCount"] == 2
    assert added["cellId"] not in [
        cell["cellId"] for cell in call(server, "notebook_read")["cells"]
    ]


def test_an_inserted_cell_carries_its_provenance_to_the_editor(editor, workspace):
    """What the browser tab renders as "review before running"."""
    server, tools = editor
    root, _ = workspace
    call(server, "notebook_open", path=str(root / "work.ipynb"))
    added = call(server, "notebook_insert_cell", source="x = 1\n")

    current = httpx.get(f"{tools.editor.api_url}/notebooks/current").json()
    cell = next(c for c in current["cells"] if c["cellId"] == added["cellId"])
    assert cell["metadata"]["agent_authored"] is True


def test_deleting_the_last_remaining_cell_is_refused(editor, workspace, tmp_path):
    server, _ = editor
    root, _ = workspace
    only = root / "single.ipynb"
    only.write_bytes(notebook(SAFE))
    call(server, "notebook_open", path=str(only))
    read = call(server, "notebook_read")
    with pytest.raises(ToolError, match="at least one cell"):
        call(server, "notebook_delete_cell", cell_id=read["cells"][0]["cellId"])


def test_run_all_stops_at_a_failing_cell_and_leaves_the_rest_unrun(editor, workspace):
    """What a caller is told when the notebook breaks halfway.

    The error is the reason most notebooks get read at all, so it has to come
    back diagnosable: the exception type and message, not just "it failed".
    Everything after the failure genuinely does not run, and this pins that —
    a model that assumed otherwise would report results it never saw.

    Note the shape this asserts: the result lists only the cells that ran, and
    the note is "Cell execution failed" with no mention of the ones skipped.
    That is current behaviour, not an endorsement of it — the risky-cell path
    says "later cells did not run" and this path says nothing.
    """
    server, _ = editor
    root, _ = workspace
    broken = root / "broken.ipynb"
    broken.write_bytes(
        notebook("print('first ok')\n", "raise ValueError('boom')\n", "marker = 1\n")
    )
    call(server, "notebook_open", path=str(broken))

    result = call(server, "notebook_run_all")
    assert result["state"] == "failed"

    failure = next(
        output
        for cell in result["cells"]
        for output in cell.get("outputs", [])
        if output.get("type") == "error"
    )
    assert failure["errorName"] == "ValueError"
    assert failure["errorValue"] == "boom"

    ran = {cell["cellId"] for cell in result["cells"]}
    assert "cell2" not in ran, "the third cell must not have run"


def test_a_cell_deleted_through_the_tools_is_gone_from_the_document(editor, workspace):
    """Only the refusal — deleting the last cell — was covered end to end.

    A delete rewrites the whole cell list, so the invariant worth holding is
    that the survivors come through with their identity and content intact.

    The tool itself answers compactly — what was deleted, how many are left,
    the new revision — and leaves the cell list to a read, so the document is
    checked directly here.
    """
    server, tools = editor
    root, _ = workspace
    three = root / "three.ipynb"
    three.write_bytes(notebook("a = 1\n", "b = 2\n", "c = 3\n"))
    call(server, "notebook_open", path=str(three))

    after = call(server, "notebook_delete_cell", cell_id="cell1")
    assert after["deletedCellId"] == "cell1"
    assert after["cellCount"] == 2

    live = httpx.get(f"{tools.editor.api_url}/notebooks/current").json()
    assert [cell["cellId"] for cell in live["cells"]] == ["cell0", "cell2"]
    assert [cell["source"] for cell in live["cells"]] == ["a = 1\n", "c = 3\n"]


def test_a_delete_moves_the_revision_so_a_stale_edit_is_refused(editor, workspace):
    """A structural change has to advance the revision like any other write,
    or a caller holding the pre-delete revision could edit against a document
    that no longer has the cell it is aiming at."""
    server, _ = editor
    root, _ = workspace
    three = root / "revision.ipynb"
    three.write_bytes(notebook("a = 1\n", "b = 2\n", "c = 3\n"))
    opened = call(server, "notebook_open", path=str(three))

    after = call(server, "notebook_delete_cell", cell_id="cell1")
    assert after["revision"] > opened["revision"]
