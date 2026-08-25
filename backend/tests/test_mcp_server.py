"""The MCP tool surface: what it refuses, what it explains, and what it waits for."""

from __future__ import annotations

import asyncio
import json

import pytest
from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from backend.app.mcp.polling import await_execution
from backend.app.mcp.server import (
    MAX_SUGGESTED_NOTEBOOKS,
    EditorSession,
    ToolFailure,
    _explain,
    _added_cell,
    available_notebooks,
    build_server,
    resolve_requested_path,
)
from backend.app.mcp.supervisor import EditorStartupError


# --- the revision contract ---------------------------------------------------


def test_a_write_before_any_read_is_refused_with_the_reason():
    """Not defaulted to "latest": an edit has to be checked against a version
    the caller actually looked at."""
    session = EditorSession()
    with pytest.raises(ToolFailure, match="open or read"):
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
    assert "read" in message


def test_no_notebook_open_says_which_tool_to_call():
    message = _explain(
        404, {"error": {"code": "notebook_not_loaded", "message": "No notebook"}},
        what="Reading",
    )
    assert "open" in message


def test_a_busy_notebook_points_at_status_rather_than_a_retry():
    message = _explain(
        409, {"error": {"code": "mutation_conflict", "message": "turn in progress"}},
        what="Editing",
    )
    assert "status" in message


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
    assert "read" in message


def test_an_unmapped_error_still_carries_its_message():
    message = _explain(500, {"error": {"code": "boom", "message": "kaboom"}}, what="Saving")
    assert "kaboom" in message and "Saving" in message


def test_a_body_that_is_not_json_does_not_break_the_explanation():
    assert "503" in _explain(503, None, what="Reading")


@pytest.mark.parametrize(
    "failure",
    [ToolFailure("phrased as a next step"), EditorStartupError("phrased as a next step")],
    ids=["tool-failure", "startup-failure"],
)
def test_a_failure_the_caller_should_read_survives_the_sdk(failure):
    """Every sentence above is wasted if the SDK replaces it on the way out.

    The SDK forwards the message of a `ToolError` and drops the message of
    anything else, so the two failure types raised out of these tools have to
    be that one. When they were plain `RuntimeError`s the caller saw only
    "Error executing tool <name>" and had nothing to act on.
    """
    server = MCPServer(name="probe", version="0")

    @server.tool(name="probe", description="Raises the failure under test.")
    def probe() -> str:
        raise failure

    with pytest.raises(ToolError, match="phrased as a next step"):
        asyncio.run(server.call_tool("probe", {}))


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


def failed_run(**extra):
    return {
        "operationId": "op1", "sessionId": "s1", "state": "failed",
        "currentDocumentRevision": 4,
        "error": {"message": "Cell execution failed"},
        "attempts": [
            {"cellId": "first", "state": "completed", "outputs": []},
            {"cellId": "second", "state": "failed", "outputs": []},
        ],
        **extra,
    }


def finished(operation, **kwargs):
    return await_execution(
        FakeTools([]), operation, timeout=10, interval=0, sleep=lambda _s: None, **kwargs
    )


def test_a_failed_run_names_the_cell_that_failed():
    """"Cell execution failed" does not say which one. The result carries an
    attempt per cell, so naming it costs nothing and saves a read."""
    note = finished(failed_run())["note"]
    assert "'second'" in note
    assert "traceback" in note, "and points at where the detail actually is"


def test_the_generic_message_is_not_repeated_after_naming_the_cell():
    """"Cell 'second' failed: Cell execution failed" says one thing twice."""
    assert finished(failed_run())["note"].lower().count("failed") == 1


def test_a_specific_failure_message_is_kept():
    """Dropping the generic message must not drop the informative ones — a
    kernel death or an output limit says something naming the cell does not."""
    note = finished(
        failed_run(error={"message": "The kernel ran out of output buffer"})
    )["note"]
    assert "output buffer" in note
    assert "'second'" in note


def test_a_failed_run_all_says_the_later_cells_did_not_run():
    """The gap this fixes. A run that stops partway returns only the cells it
    attempted, so the ones after the failure are absent rather than marked —
    from the outside, indistinguishable from a shorter notebook."""
    note = finished(failed_run(), whole_notebook=True)["note"]
    assert "did not run" in note
    assert [cell["cellId"] for cell in finished(failed_run())["cells"]] == ["first", "second"]


def test_a_single_failing_cell_does_not_claim_anything_about_later_cells():
    """run_cell ran one cell on purpose. Saying "later cells did not
    run" there would invent a truncation that never happened."""
    assert "did not run" not in finished(failed_run())["note"]


def test_a_cancelled_run_all_also_says_where_it_stopped():
    """Cancelling has the same shape as failing: the rest of the notebook is
    silently missing from the result."""
    cancelled = {
        "operationId": "op1", "sessionId": "s1", "state": "cancelled",
        "currentDocumentRevision": 4,
        "attempts": [{"cellId": "first", "state": "cancelled"}],
    }
    assert "did not run" in finished(cancelled, whole_notebook=True)["note"]
    assert "did not run" not in finished(cancelled)["note"]


def test_a_failure_with_no_failed_attempt_still_reads_sensibly():
    """A run can fail before any cell does — a dead kernel, a bad revision."""
    note = finished({
        "operationId": "op1", "sessionId": "s1", "state": "failed",
        "error": {"message": "The kernel died"}, "attempts": [],
    })["note"]
    assert note.startswith("The kernel died")


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
    reported = result["cells"][0]["outputs"][0]["images"][0]["bytes"]
    assert 0 < reported < len(png), "the decoded size, not the base64 length"


# --- the surface itself ------------------------------------------------------


@pytest.fixture
def built():
    server, tools = build_server(open_browser=False)
    yield server, tools
    tools.close()


def test_the_surface_is_the_intended_tools(built):
    server, _ = built
    names = {tool.name for tool in asyncio.run(server.list_tools())}
    assert names == {
        "open",
        "read",
        "status",
        "set_cell_source",
        "insert_cell",
        "delete_cell",
        "run_cell",
        "run_all",
        "cancel_run",
        "save",
        "show",
    }


def test_insert_says_the_cell_is_not_run_and_points_at_the_editing_tool(built):
    """Two mistakes the description has to head off: assuming a new code cell
    has already produced output, and reaching for insert to change a cell that
    is already there."""
    server, _ = built
    description = {
        tool.name: tool.description for tool in asyncio.run(server.list_tools())
    }["insert_cell"]
    assert "NOT run" in description
    assert "set_cell_source" in description


def test_delete_says_a_notebook_keeps_one_cell(built):
    server, _ = built
    description = {
        tool.name: tool.description for tool in asyncio.run(server.list_tools())
    }["delete_cell"]
    assert "at least one cell" in description


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
    assert hints["read"] is True
    assert hints["status"] is True
    assert hints["set_cell_source"] is not True
    assert hints["run_cell"] is not True


def test_every_tool_describes_itself(built):
    server, _ = built
    for tool in asyncio.run(server.list_tools()):
        assert tool.description and len(tool.description) > 40, tool.name


def test_the_run_tools_warn_that_they_can_block_on_a_person(built):
    """Published guidance: say when a tool is slow and when not to reach for it."""
    server, _ = built
    descriptions = {tool.name: tool.description for tool in asyncio.run(server.list_tools())}
    assert "approve" in descriptions["run_cell"]
    assert "approve" in descriptions["run_all"]
    assert "run_cell" in descriptions["run_all"]


def test_the_read_tool_says_images_are_not_returned(built):
    server, _ = built
    description = {t.name: t.description for t in asyncio.run(server.list_tools())}["read"]
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


# --- which cell an insert produced -------------------------------------------


def snapshot_with(*cell_ids):
    return {"cells": [{"cellId": cid, "index": i} for i, cid in enumerate(cell_ids)]}


def test_an_appended_cell_is_identified_as_the_last_one():
    """The caller needs an id back, or it cannot run what it just added."""
    added = _added_cell(snapshot_with("a", "b", "new"), None)
    assert added["cellId"] == "new"


def test_a_positioned_cell_is_identified_at_that_position():
    added = _added_cell(snapshot_with("new", "a", "b"), 0)
    assert added["cellId"] == "new"


def test_an_impossible_position_yields_nothing_rather_than_a_wrong_cell():
    """Better to return no id than confidently return someone else's."""
    assert _added_cell(snapshot_with("a"), 5) is None
    assert _added_cell({"cells": []}, None) is None


# --- first-run clarity -------------------------------------------------------


def test_the_workspace_notebooks_are_listed_for_a_caller_that_guessed(tmp_path):
    """A client with no filesystem tools has to be told what is there.

    Browsing is deliberately not in the tool surface, and this is not browsing:
    it is `.ipynb` files inside the root the editor is already confined to,
    offered only when a caller names a path that is not there.
    """
    (tmp_path / "analysis").mkdir()
    (tmp_path / "first.ipynb").write_text("{}", encoding="utf-8")
    (tmp_path / "analysis" / "second.ipynb").write_text("{}", encoding="utf-8")
    assert available_notebooks(str(tmp_path)) == ["analysis/second.ipynb", "first.ipynb"]


def test_checkpoint_and_dependency_directories_are_not_offered(tmp_path):
    """`.ipynb_checkpoints` is full of notebooks nobody means."""
    for noise in (".ipynb_checkpoints", "node_modules", ".git"):
        (tmp_path / noise).mkdir()
        (tmp_path / noise / "copy.ipynb").write_text("{}", encoding="utf-8")
    (tmp_path / "real.ipynb").write_text("{}", encoding="utf-8")
    assert available_notebooks(str(tmp_path)) == ["real.ipynb"]


def test_the_listing_is_bounded(tmp_path):
    for index in range(40):
        (tmp_path / f"n{index:02d}.ipynb").write_text("{}", encoding="utf-8")
    assert len(available_notebooks(str(tmp_path))) == MAX_SUGGESTED_NOTEBOOKS


def test_no_workspace_means_no_suggestions(tmp_path):
    """Unconfined, there is no bounded set to offer — and enumerating a whole
    machine is exactly what the surface refuses to do."""
    assert available_notebooks(None) == []
    assert available_notebooks(str(tmp_path / "absent")) == []


def test_open_tells_the_caller_where_a_person_can_watch(built):
    """webbrowser.open silently does nothing on a headless or remote host, so
    the URL has to come back in the answer or nobody ever learns it."""
    server, _ = built
    description = {
        tool.name: tool.description for tool in asyncio.run(server.list_tools())
    }["open"]
    assert "editorUrl" in description


# --- what the code review found ----------------------------------------------


def test_status_does_not_become_a_write_baseline():
    """`status` is read-only and shows no cell content.

    If it adopted the server's revision, this sequence would silently clobber:
    read at r5 → a person edits in the tab (r6) → status → write. The write
    would go out at r6 and succeed, overwriting a change the caller never saw,
    instead of conflicting the way a stale write must.
    """
    session = EditorSession()
    session.observe({"sessionId": "s1", "revision": 5})
    session.observe({"sessionId": "s1", "documentRevision": 6})
    # observe() itself still accepts documentRevision — it is the *caller* of
    # status that must not feed it in. Assert the tool does not.
    import inspect

    from backend.app.mcp import server as module

    source = inspect.getsource(module)
    status_body = source[source.index("def notebook_status"):source.index("def notebook_set_cell_source")]
    assert "tools.state.observe" not in status_body, (
        "status must not update the write baseline"
    )


def test_a_missing_cell_is_named_not_described():
    """The id belongs in the message; the generic text is not the id.

    This read "There is no cell 'Notebook cell was not found'" — the error's
    own message interpolated where the cell id should have been.
    """
    message = _explain(
        404,
        {"error": {"code": "cell_not_found", "message": "Notebook cell was not found",
                   "details": {"cellId": "vanished"}}},
        what="Editing",
    )
    assert "'vanished'" in message
    assert "'Notebook cell was not found'" not in message


def test_a_missing_cell_without_details_still_reads_sensibly():
    message = _explain(
        404, {"error": {"code": "cell_not_found", "message": "gone"}}, what="Editing",
    )
    assert "There is no cell in the open notebook" in message


def test_a_stuck_run_can_be_cancelled(built):
    """A run parked on approval holds the mutation lease, so every edit — the
    caller's and the person's — is refused until it resolves. Headless, with
    nobody to approve, there has to be a way out that is not a restart."""
    server, _ = built
    tools = {tool.name: tool for tool in asyncio.run(server.list_tools())}
    assert "cancel_run" in tools
    assert "holds the notebook" in tools["cancel_run"].description


def test_the_readme_lists_every_tool_that_exists(built):
    """The README's tool list had ten of the eleven — `cancel_run`, the one a
    reader most needs to know exists, because without it a run parked on
    approval holds the notebook with no way out.

    A list maintained by hand beside a surface that changes is a list that goes
    stale, so it is checked rather than trusted.
    """
    import re
    from pathlib import Path

    server, _ = built
    names = {tool.name for tool in asyncio.run(server.list_tools())}
    readme = Path(__file__).resolve().parents[2] / "README.md"
    text = readme.read_text()
    # Find the list by its contents, not by a heading or an offset: both have
    # already been renamed and renumbered by ordinary editing, and a tool-list
    # check that breaks when prose moves gets weakened to make it pass.
    paragraphs = [
        para for para in text.split("\n\n") if "`set_cell_source`" in para
    ]
    assert len(paragraphs) == 1, (
        f"expected exactly one paragraph listing the tools, found {len(paragraphs)}"
    )
    listed = set(re.findall(r"`([a-z_]+)`", paragraphs[0]))
    assert listed == names, f"README missing {names - listed}, extra {listed - names}"


def test_every_mutating_tool_says_the_file_is_not_written_yet(built):
    """Found by running the suite against a second model.

    Haiku inserted a cell, ran it, announced success and never called save — in
    2 of 4 runs. Nothing in the surface said it had to: `save` was described as
    "write the notebook back to its file" with no hint that it was the only
    thing that did, and the mutating tools said nothing at all. A model that
    assumes an editor autosaves is not being unreasonable; this one does not,
    headless, because autosave lives in the browser tab.
    """
    server, _ = built
    described = {tool.name: tool.description for tool in asyncio.run(server.list_tools())}
    for name in ("set_cell_source", "insert_cell", "delete_cell"):
        assert "save" in described[name], f"{name} does not mention saving"
    assert "lost if nobody saves" in described["save"]


def test_a_structural_change_reports_the_notebook_as_unsaved(built):
    """The description is advice; `dirty` is the fact, and a caller checking
    its work should not have to call status to learn it."""
    server, _ = built
    import inspect
    from backend.app.mcp import server as module

    source = inspect.getsource(module)
    for marker in ("def notebook_insert_cell", "def notebook_delete_cell"):
        body = source[source.index(marker):][:1200]
        assert '"dirty"' in body, f"{marker} does not report dirty"


# --- which interpreter runs the cells -----------------------------------------


def test_the_kernel_interpreter_defaults_to_this_environment(monkeypatch):
    """No flag, no env var: jupyter_client resolves `python3` as it always did."""
    from backend.app.kernel_execution.kernel_session import KernelSession

    monkeypatch.delenv("NOTEBOOK_KERNEL_PYTHON", raising=False)
    assert KernelSession().kernel_python is None


def test_the_kernel_interpreter_is_taken_from_the_environment(monkeypatch):
    """The supervisor hands it to the child that way, so reading it matters."""
    from backend.app.kernel_execution.kernel_session import KernelSession

    monkeypatch.setenv("NOTEBOOK_KERNEL_PYTHON", "/somewhere/.venv/bin/python")
    assert KernelSession().kernel_python == "/somewhere/.venv/bin/python"


def test_an_explicit_interpreter_beats_the_environment(monkeypatch):
    from backend.app.kernel_execution.kernel_session import KernelSession

    monkeypatch.setenv("NOTEBOOK_KERNEL_PYTHON", "/from/env/python")
    assert KernelSession(kernel_python="/explicit/python").kernel_python == "/explicit/python"


def test_the_supervisor_passes_the_interpreter_to_the_editor_process():
    """It reaches the child as an env var, beside the workspace root."""
    from backend.app.mcp.supervisor import EditorProcess

    process = EditorProcess(kernel_python="/project/.venv/bin/python")
    assert process.kernel_python == "/project/.venv/bin/python"
    # And is distinct from the interpreter that serves the editor itself.
    assert process.python != process.kernel_python


def test_a_restarted_kernel_keeps_the_interpreter_it_was_given():
    """The replacement is built field by field, so a new one is easy to drop.

    Losing it here would be near-invisible: the notebook keeps working, on the
    wrong environment, and only an import that used to resolve would say so.
    """
    import inspect

    from backend.app.kernel_execution import service

    source = inspect.getsource(service)
    constructions = source.count("KernelSession(\n")
    carried = source.count("kernel_python=old_kernel.kernel_python")
    assert carried == constructions, (
        f"{constructions} KernelSession(...) reconstructions but {carried} carry "
        "kernel_python — a restart would silently fall back to this environment"
    )


def test_a_virtualenv_interpreter_is_not_resolved_through_its_symlink(tmp_path):
    """`bin/python` in a virtualenv is a symlink to the base interpreter.

    Resolving it hands back that base interpreter, which has none of the
    virtualenv's packages — the exact environment `--kernel-python` exists to
    escape. The symptom is silent: the flag is accepted, a kernel starts, and
    the notebook's imports fail as if the flag had not been passed.
    """
    from backend.app.mcp.server import _resolved_kernel_python

    # Stands in for the base interpreter: all the probe asks of it is that
    # `-c "import ipykernel"` exits 0, which keeps this test off the machine's
    # real environments and fast.
    base = tmp_path / "base-python"
    base.write_text("#!/bin/sh\nexit 0\n")
    base.chmod(0o755)

    venv_bin = tmp_path / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    link = venv_bin / "python"
    link.symlink_to(base)

    def unexpected_failure(message):  # pragma: no cover - only on regression
        raise AssertionError(message)

    resolved = _resolved_kernel_python(str(link), unexpected_failure)
    assert resolved == str(link), (
        f"followed the symlink to {resolved} — the virtualenv's packages are "
        "not on that interpreter's path"
    )
