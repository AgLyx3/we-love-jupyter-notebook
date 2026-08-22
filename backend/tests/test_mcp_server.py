"""The MCP tool surface: what it refuses, what it explains, and what it waits for."""

from __future__ import annotations

import asyncio
import json

import pytest

from backend.app.mcp.polling import await_execution
from backend.app.mcp.server import (
    EditorSession,
    ToolFailure,
    _explain,
    build_server,
    resolve_requested_path,
)


# --- the revision contract ---------------------------------------------------


def test_a_write_before_any_read_is_refused_with_the_reason():
    """Not defaulted to "latest": an edit has to be checked against a version
    the caller actually looked at."""
    session = EditorSession()
    with pytest.raises(ToolFailure, match="notebook_open or notebook_read"):
        session.mutation()


def test_a_write_carries_the_revision_from_the_last_read():
    session = EditorSession()
    session.observe({"sessionId": "s1", "revision": 4})
    assert session.mutation() == {"sessionId": "s1", "expectedDocumentRevision": 4}


def test_a_later_read_moves_the_revision_forward():
    session = EditorSession()
    session.observe({"sessionId": "s1", "revision": 4})
    session.observe({"sessionId": "s1", "revision": 9})
    assert session.mutation()["expectedDocumentRevision"] == 9


def test_session_status_uses_its_own_revision_field():
    """`/session/status` says documentRevision where a snapshot says revision."""
    session = EditorSession()
    session.observe({"sessionId": "s1", "documentRevision": 7})
    assert session.mutation()["expectedDocumentRevision"] == 7


def test_a_payload_without_a_revision_does_not_clear_what_is_known():
    session = EditorSession()
    session.observe({"sessionId": "s1", "revision": 2})
    session.observe({"sessionId": "s1"})
    assert session.mutation()["expectedDocumentRevision"] == 2


# --- errors phrased as next steps --------------------------------------------


def test_a_conflict_says_the_edit_was_not_made():
    """The single most important thing to get right in this mapping.

    A caller told only "409" may reasonably try again; it has to know the write
    did not happen *and* that re-reading is the way forward, or it will either
    retry blindly or assume success.
    """
    message = _explain(
        409,
        {"error": {"code": "revision_conflict", "message": "Revision moved"}},
        what="Editing the cell",
    )
    assert "changed since you last read it" in message
    assert "has not been made" in message
    assert "notebook_read" in message


def test_no_notebook_open_says_which_tool_to_call():
    message = _explain(
        404, {"error": {"code": "notebook_not_loaded", "message": "No notebook"}},
        what="Reading",
    )
    assert "notebook_open" in message


def test_a_busy_notebook_points_at_status_rather_than_a_retry():
    message = _explain(
        409, {"error": {"code": "mutation_conflict", "message": "turn in progress"}},
        what="Editing",
    )
    assert "notebook_status" in message


def test_a_rejected_path_explains_the_workspace_boundary():
    message = _explain(
        400, {"error": {"code": "notebook_path_invalid", "message": "Bad path"}},
        what="Opening",
    )
    assert "workspace" in message


def test_every_mapped_code_is_one_the_app_actually_raises():
    """Guessing a code name is silent: the branch simply never fires and the
    caller gets the generic fallback. This caught exactly that."""
    import re
    from pathlib import Path

    source = Path("backend/app/mcp/server.py").read_text(encoding="utf-8")
    mapped = set(re.findall(r'code == "([a-z_]+)"', source))
    for group in re.findall(r"code in \{([^}]+)\}", source):
        mapped |= set(re.findall(r'"([a-z_]+)"', group))

    defined: set[str] = set()
    for path in Path("backend/app").rglob("*.py"):
        defined |= set(
            re.findall(r'^\s+code = "([a-z_]+)"', path.read_text(encoding="utf-8"), re.M)
        )
    assert mapped, "no codes mapped at all"
    assert mapped <= defined, f"mapped codes no longer raised: {sorted(mapped - defined)}"


def test_a_missing_cell_points_at_a_fresh_read():
    message = _explain(
        404, {"error": {"code": "cell_not_found", "message": "gone"}}, what="Editing",
    )
    assert "notebook_read" in message


def test_an_unmapped_error_still_carries_its_message():
    message = _explain(500, {"error": {"code": "boom", "message": "kaboom"}}, what="Saving")
    assert "kaboom" in message and "Saving" in message


def test_a_body_that_is_not_json_does_not_break_the_explanation():
    assert "503" in _explain(503, None, what="Reading")


# --- waiting on a run --------------------------------------------------------


class FakeTools:
    """Serves a scripted sequence of execution states."""

    def __init__(self, states):
        self.states = list(states)
        self.state = EditorSession()
        self.calls = 0

    def request(self, method, path, *, json_body=None, what=""):
        self.calls += 1
        return self.states.pop(0)


def running(**extra):
    return {"operationId": "op1", "sessionId": "s1", "state": "running", **extra}


def test_a_run_is_polled_until_it_finishes():
    tools = FakeTools([
        running(),
        {
            "operationId": "op1", "sessionId": "s1", "state": "completed",
            "currentDocumentRevision": 5,
            "attempts": [
                {
                    "cellId": "a", "state": "completed",
                    "outputs": [{"output_type": "stream", "name": "stdout", "text": "ok\n"}],
                }
            ],
        },
    ])
    result = await_execution(tools, running(), timeout=10, interval=0, sleep=lambda _s: None)
    assert result["state"] == "completed"
    assert result["cells"][0]["outputs"][0]["text"] == "ok\n"
    assert result["revision"] == 5


def test_finishing_updates_the_revision_so_a_later_edit_is_not_stale():
    """A run rewrites outputs, which moves the revision. Without this the next
    edit would be refused against a revision the caller could not have seen."""
    tools = FakeTools([
        {
            "operationId": "op1", "sessionId": "s1", "state": "completed",
            "currentDocumentRevision": 12, "attempts": [],
        },
    ])
    await_execution(tools, running(), timeout=10, interval=0, sleep=lambda _s: None)
    assert tools.state.mutation()["expectedDocumentRevision"] == 12


def test_a_timeout_while_paused_says_what_it_is_waiting_for():
    """"Timed out" alone would read as a broken kernel rather than a person
    who has not looked at the tab yet."""
    paused = {
        "operationId": "op1", "sessionId": "s1", "state": "awaiting_approval",
        "attempts": [
            {
                "cellId": "risky", "state": "awaiting_approval", "decision": None,
                "risk": {"level": "confirm", "reasons": ["Starts another process"]},
            }
        ],
    }
    clock = iter([0.0, 100.0, 200.0])
    result = await_execution(
        FakeTools([paused]), paused, timeout=1,
        interval=0, now=lambda: next(clock), sleep=lambda _s: None,
    )
    assert result["state"] == "awaiting_approval"
    assert result["cellId"] == "risky"
    assert "Starts another process" in result["reasons"]
    assert "Nothing has run" in result["note"]


def test_a_skipped_cell_says_later_cells_did_not_run():
    tools = FakeTools([])
    result = await_execution(
        tools,
        {
            "operationId": "op1", "sessionId": "s1", "state": "validation_incomplete",
            "currentDocumentRevision": 3,
            "attempts": [{"cellId": "risky", "state": "skipped", "decision": "skip"}],
        },
        timeout=10, interval=0, sleep=lambda _s: None,
    )
    assert "did not run" in result["note"]
    assert result["cells"][0]["decision"] == "skip"


def test_run_outputs_are_summarised_like_any_other():
    """An image in a run result must not arrive as base64 either."""
    png = "iVBOR" + "C" * 5_000
    tools = FakeTools([])
    result = await_execution(
        tools,
        {
            "operationId": "op1", "sessionId": "s1", "state": "completed",
            "currentDocumentRevision": 2,
            "attempts": [
                {
                    "cellId": "plot", "state": "completed",
                    "outputs": [
                        {
                            "output_type": "display_data",
                            "data": {"image/png": png, "text/plain": "<Figure>"},
                            "metadata": {},
                        }
                    ],
                }
            ],
        },
        timeout=10, interval=0, sleep=lambda _s: None,
    )
    assert png[:20] not in json.dumps(result)
    assert result["cells"][0]["outputs"][0]["images"][0]["bytes"] == len(png)


# --- the surface itself ------------------------------------------------------


@pytest.fixture
def built():
    server, tools = build_server(open_browser=False)
    yield server, tools
    tools.close()


def test_the_surface_is_the_seven_intended_tools(built):
    server, _ = built
    names = {tool.name for tool in asyncio.run(server.list_tools())}
    assert names == {
        "notebook_open",
        "notebook_read",
        "notebook_status",
        "notebook_set_cell_source",
        "notebook_run_cell",
        "notebook_run_all",
        "notebook_save",
        "notebook_show",
    }


def test_nothing_exposes_agent_turns_tuning_or_the_filesystem(built):
    """Excluded deliberately — see the module docstring for why each one is."""
    server, _ = built
    names = {tool.name for tool in asyncio.run(server.list_tools())}
    for forbidden in ("turn", "agent", "tuning", "tune", "files", "search", "list_files"):
        assert not any(forbidden in name for name in names), forbidden


def test_reads_are_marked_read_only_and_writes_are_not(built):
    """So a client is free to run reads concurrently and must not assume the
    same of a run or an edit."""
    server, _ = built
    hints = {
        tool.name: getattr(tool.annotations, "read_only_hint", None)
        for tool in asyncio.run(server.list_tools())
    }
    assert hints["notebook_read"] is True
    assert hints["notebook_status"] is True
    assert hints["notebook_set_cell_source"] is not True
    assert hints["notebook_run_cell"] is not True


def test_every_tool_describes_itself(built):
    server, _ = built
    for tool in asyncio.run(server.list_tools()):
        assert tool.description and len(tool.description) > 40, tool.name


def test_the_run_tools_warn_that_they_can_block_on_a_person(built):
    """Published guidance: say when a tool is slow and when not to reach for it."""
    server, _ = built
    descriptions = {tool.name: tool.description for tool in asyncio.run(server.list_tools())}
    assert "approve" in descriptions["notebook_run_cell"]
    assert "approve" in descriptions["notebook_run_all"]
    assert "notebook_run_cell" in descriptions["notebook_run_all"]


def test_the_read_tool_says_images_are_not_returned(built):
    server, _ = built
    description = {t.name: t.description for t in asyncio.run(server.list_tools())}["notebook_read"]
    assert "Images" in description
    assert "cells" in description


# --- path resolution (found by the eval) -------------------------------------


def test_a_relative_path_is_anchored_to_the_workspace(tmp_path):
    """Observed in the eval: asked to work on "analysis.ipynb", the model passes
    exactly that. Unanchored it would resolve against whichever process saw it
    first, in whichever directory that process was started — which matched the
    workspace only by luck.
    """
    assert resolve_requested_path("analysis.ipynb", str(tmp_path)) == str(
        tmp_path / "analysis.ipynb"
    )
    assert resolve_requested_path("nested/x.ipynb", str(tmp_path)) == str(
        tmp_path / "nested" / "x.ipynb"
    )


def test_an_absolute_path_is_left_alone(tmp_path):
    """Confinement still judges it; anchoring must not rewrite it into the
    workspace and make an outside path look like an inside one."""
    outside = "/somewhere/else/notebook.ipynb"
    assert resolve_requested_path(outside, str(tmp_path)) == outside


def test_without_a_workspace_a_relative_path_still_becomes_absolute(tmp_path, monkeypatch):
    """So the editor child's inherited directory never decides what it meant."""
    monkeypatch.chdir(tmp_path)
    assert resolve_requested_path("here.ipynb", None) == str(tmp_path / "here.ipynb")
