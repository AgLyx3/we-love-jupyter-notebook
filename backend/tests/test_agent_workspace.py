import os
import sys
import time
from threading import Event
from threading import Timer
from types import SimpleNamespace

import pytest

from backend.app.agent_workspace.models import WorkspaceBoundaryError, WorkspaceCleanupError
from backend.app.agent_workspace.adapters import (
    ClaudeAgentAdapter,
    DevelopmentFakeAgentAdapter,
    FakeAgentAdapter,
    FakeAttempt,
)
from backend.app.main import configured_agent_adapter
from backend.app.agent_workspace.models import AgentAdapterError, AgentTimedOut
from backend.app.agent_workspace.models import AgentCancelled
from backend.app.agent_workspace.runner import ProcessRunner
from backend.app.agent_workspace.workspace_auditor import WorkspaceAuditor
from backend.app.agent_workspace.workspace_builder import AgentWorkspaceBuilder
from backend.app.notebook_document.service import NotebookDocumentService
from backend.app.turn_scope.models import FrozenTurnScope, ScopeSelection


def _workspace(notebook_payload):
    documents = NotebookDocumentService()
    snapshot = documents.import_notebook(notebook_payload())
    scope = FrozenTurnScope.create(
        turn_id="turn", session_id=snapshot.session_id,
        notebook_revision=snapshot.revision,
        selection=ScopeSelection(("editable",), ("intro",)), prompt="update value",
    )
    builder = AgentWorkspaceBuilder()
    return builder, builder.build(snapshot, scope)


def test_builds_plain_source_manifest_and_protected_context(notebook_payload):
    builder, workspace = _workspace(notebook_payload)
    try:
        assert (workspace.root / "editable/cell_editable.py").read_text() == "value = 1\n"
        assert workspace.manifest.editable_cells[0].cell_id == "editable"
        assert workspace.manifest.context_cells[0].cell_id == "intro"
        assert "update value" in (workspace.root / "INSTRUCTIONS.md").read_text()
        assert workspace.baseline_hashes.keys() == {
            "notebook.ipynb", "AGENT_CELL_MANIFEST.json", "INSTRUCTIONS.md"
        }
    finally:
        builder.destroy(workspace)


@pytest.mark.parametrize("failure_stage", ["write", "hash", "chmod"])
def test_build_failure_removes_partial_workspace(
    monkeypatch, notebook_payload, tmp_path, failure_stage,
):
    documents = NotebookDocumentService()
    snapshot = documents.import_notebook(notebook_payload())
    scope = FrozenTurnScope.create(
        turn_id="turn", session_id=snapshot.session_id,
        notebook_revision=snapshot.revision,
        selection=ScopeSelection(("editable",), ()), prompt="update",
    )
    root = tmp_path / f"workspace-{failure_stage}"

    def make_workspace(*_args, **_kwargs):
        root.mkdir()
        return str(root)

    monkeypatch.setattr(
        "backend.app.agent_workspace.workspace_builder.tempfile.mkdtemp",
        make_workspace,
    )
    if failure_stage == "write":
        original_write = type(root).write_text

        def failing_write(path, *args, **kwargs):
            if path.name == "AGENT_CELL_MANIFEST.json":
                raise OSError("injected write failure")
            return original_write(path, *args, **kwargs)

        monkeypatch.setattr(type(root), "write_text", failing_write)
    elif failure_stage == "hash":
        monkeypatch.setattr(
            "backend.app.agent_workspace.workspace_builder._hash",
            lambda _path: (_ for _ in ()).throw(OSError("injected hash failure")),
        )
    else:
        monkeypatch.setattr(
            "backend.app.agent_workspace.workspace_builder.os.chmod",
            lambda *_args: (_ for _ in ()).throw(OSError("injected chmod failure")),
        )

    with pytest.raises(OSError, match=f"injected {failure_stage} failure"):
        AgentWorkspaceBuilder(cleanup_delay=0).build(snapshot, scope)

    assert not root.exists()


def test_workspace_destroy_retries_transient_rmtree_failure(
    monkeypatch, notebook_payload,
):
    builder, workspace = _workspace(notebook_payload)
    original_rmtree = __import__("shutil").rmtree
    calls = 0

    def flaky_rmtree(path):
        nonlocal calls
        calls += 1
        if calls < 3:
            raise OSError("transient cleanup failure")
        original_rmtree(path)

    monkeypatch.setattr(
        "backend.app.agent_workspace.workspace_builder.shutil.rmtree", flaky_rmtree,
    )

    AgentWorkspaceBuilder(cleanup_attempts=3, cleanup_delay=0).destroy(workspace)

    assert calls == 3
    assert not workspace.root.exists()


def test_workspace_destroy_reports_persistent_rmtree_failure(
    monkeypatch, notebook_payload,
):
    builder, workspace = _workspace(notebook_payload)
    monkeypatch.setattr(
        "backend.app.agent_workspace.workspace_builder.shutil.rmtree",
        lambda _path: (_ for _ in ()).throw(OSError("persistent cleanup failure")),
    )

    with pytest.raises(WorkspaceCleanupError, match="could not be removed") as error:
        AgentWorkspaceBuilder(cleanup_attempts=2, cleanup_delay=0).destroy(workspace)

    assert error.value.root == workspace.root
    assert workspace.root.exists()
    # Restore cleanup for the test process after the injected failure.
    monkeypatch.undo()
    builder.destroy(workspace)


def test_partial_build_cleanup_failure_does_not_mask_primary_error(
    monkeypatch, notebook_payload, tmp_path, caplog,
):
    documents = NotebookDocumentService()
    snapshot = documents.import_notebook(notebook_payload())
    scope = FrozenTurnScope.create(
        turn_id="turn", session_id=snapshot.session_id,
        notebook_revision=snapshot.revision,
        selection=ScopeSelection(("editable",), ()), prompt="update",
    )
    root = tmp_path / "workspace-dual-failure"

    def make_workspace(*_args, **_kwargs):
        root.mkdir()
        return str(root)

    original_write = type(root).write_text

    def failing_write(path, *args, **kwargs):
        if path.name == "AGENT_CELL_MANIFEST.json":
            raise OSError("primary write failure")
        return original_write(path, *args, **kwargs)

    monkeypatch.setattr(
        "backend.app.agent_workspace.workspace_builder.tempfile.mkdtemp",
        make_workspace,
    )
    monkeypatch.setattr(type(root), "write_text", failing_write)
    monkeypatch.setattr(
        "backend.app.agent_workspace.workspace_builder.shutil.rmtree",
        lambda _path: (_ for _ in ()).throw(OSError("cleanup also failed")),
    )

    with pytest.raises(OSError, match="primary write failure") as error:
        AgentWorkspaceBuilder(cleanup_attempts=2, cleanup_delay=0).build(
            snapshot, scope,
        )

    assert any("could not be removed" in note for note in error.value.__notes__)
    assert "Failed to remove partially built agent workspace" in caplog.text
    assert root.exists()
    monkeypatch.undo()
    AgentWorkspaceBuilder(cleanup_delay=0)._remove_root(root)


def test_collects_only_changed_declared_regular_utf8_files(notebook_payload):
    builder, workspace = _workspace(notebook_payload)
    try:
        (workspace.root / "editable/cell_editable.py").write_text("value = 2\n")
        assert WorkspaceAuditor().collect(workspace) == {"editable": "value = 2\n"}
    finally:
        builder.destroy(workspace)


@pytest.mark.parametrize(
    "violation",
    ["undeclared", "protected", "protected_symlink", "symlink", "fifo", "invalid_utf8"],
)
def test_rejects_workspace_boundary_violations(notebook_payload, tmp_path, violation):
    builder, workspace = _workspace(notebook_payload)
    editable = workspace.root / "editable/cell_editable.py"
    try:
        if violation == "undeclared":
            (workspace.root / "outside.txt").write_text("bad")
        elif violation == "protected":
            protected = workspace.root / "notebook.ipynb"
            protected.unlink()
            protected.write_text("{}")
        elif violation == "protected_symlink":
            protected = workspace.root / "notebook.ipynb"
            content = protected.read_bytes()
            target = tmp_path / "notebook-copy"
            target.write_bytes(content)
            protected.unlink()
            protected.symlink_to(target)
        elif violation == "symlink":
            editable.unlink()
            editable.symlink_to(tmp_path / "target")
        elif violation == "fifo":
            editable.unlink()
            os.mkfifo(editable)
        else:
            editable.write_bytes(b"\xff")
        with pytest.raises(WorkspaceBoundaryError):
            WorkspaceAuditor().collect(workspace)
    finally:
        builder.destroy(workspace)


def test_rejects_aggregate_candidate_limit(notebook_payload):
    builder, workspace = _workspace(notebook_payload)
    try:
        (workspace.root / "editable/cell_editable.py").write_text("12345")
        with pytest.raises(WorkspaceBoundaryError):
            WorkspaceAuditor(per_file_limit=10, aggregate_limit=4).collect(workspace)
    finally:
        builder.destroy(workspace)


def test_fake_adapter_enforces_timeout(notebook_payload):
    builder, workspace = _workspace(notebook_payload)
    try:
        with pytest.raises(AgentTimedOut):
            FakeAgentAdapter([FakeAttempt(delay=0.05)]).run(
                workspace, timeout=0.01, cancel_event=Event()
            )
    finally:
        builder.destroy(workspace)


def test_claude_adapter_is_version_gated_and_effectively_whitelists_tools(
    monkeypatch, notebook_payload,
):
    builder, workspace = _workspace(notebook_payload)
    captured = {}

    class StubRunner:
        def run(self, args, **kwargs):
            captured["args"] = args
            return "finished", ""

    monkeypatch.setattr(
        "backend.app.agent_workspace.adapters.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout="2.1.203", stderr=""),
    )
    try:
        result = ClaudeAgentAdapter(runner=StubRunner()).run(
            workspace, timeout=1, cancel_event=Event()
        )
        assert result.final_output == "finished"
        args = captured["args"]
        assert "--allowedTools" not in args
        tools = args[args.index("--tools") + 1].split(",")
        assert set(tools) == {"Read", "Edit", "Write"}
        assert "Bash" not in tools
        assert "--safe-mode" in args
        assert "--disable-slash-commands" in args
        assert "--strict-mcp-config" in args
        mcp_config = args[args.index("--mcp-config") + 1]
        assert mcp_config == '{"mcpServers":{}}'
        assert "--no-session-persistence" in args
        monkeypatch.setattr(
            "backend.app.agent_workspace.adapters.subprocess.run",
            lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout="2.2.0", stderr=""),
        )
        with pytest.raises(AgentAdapterError):
            ClaudeAgentAdapter().verify_supported()
    finally:
        builder.destroy(workspace)


def test_model_and_plan_mode_shape_cli_args_and_prompt(notebook_payload, monkeypatch):
    builder, workspace = _workspace(notebook_payload)
    captured = {}

    class StubRunner:
        def run(self, args, **kwargs):
            captured["args"] = args
            return "finished", ""

    monkeypatch.setattr(
        "backend.app.agent_workspace.adapters.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout="2.1.203", stderr=""),
    )
    try:
        # Default: no --model, acceptEdits, unmodified prompt.
        ClaudeAgentAdapter(runner=StubRunner()).run(
            workspace, timeout=1, cancel_event=Event()
        )
        args = captured["args"]
        assert "--model" not in args
        assert args[args.index("--permission-mode") + 1] == "acceptEdits"
        assert not args[args.index("-p") + 1].startswith("You are operating in plan mode")

        # Chosen model + plan mode: --model opus, plan, prompt reframed.
        ClaudeAgentAdapter(runner=StubRunner()).run(
            workspace, timeout=1, cancel_event=Event(),
            model="opus", permission_mode="plan",
        )
        args = captured["args"]
        assert args[args.index("--model") + 1] == "opus"
        assert args[args.index("--permission-mode") + 1] == "plan"
        assert args[args.index("-p") + 1].startswith("You are operating in plan mode")

        # Unknown values fall back to safe defaults.
        ClaudeAgentAdapter(runner=StubRunner()).run(
            workspace, timeout=1, cancel_event=Event(),
            model="evil --dangerously", permission_mode="bypassPermissions",
        )
        args = captured["args"]
        assert "--model" not in args
        assert args[args.index("--permission-mode") + 1] == "acceptEdits"
    finally:
        builder.destroy(workspace)


def _read_only_workspace(notebook_payload):
    documents = NotebookDocumentService()
    snapshot = documents.import_notebook(notebook_payload())
    scope = FrozenTurnScope.create(
        turn_id="turn", session_id=snapshot.session_id,
        notebook_revision=snapshot.revision,
        selection=ScopeSelection((), ("intro",)), prompt="explain the notebook",
    )
    builder = AgentWorkspaceBuilder()
    return builder, builder.build(snapshot, scope)


def test_editable_turn_instructions_frame_editing_as_optional(notebook_payload):
    builder, workspace = _workspace(notebook_payload)
    try:
        instructions = (workspace.root / "INSTRUCTIONS.md").read_text().lower()
        assert "optional" in instructions
        assert "answer the" in instructions
        assert "editable/cell_editable.py" in instructions
    finally:
        builder.destroy(workspace)


def test_read_only_turn_uses_read_only_tools_and_writes_no_editable_files(notebook_payload, monkeypatch):
    builder, workspace = _read_only_workspace(notebook_payload)
    captured = {}

    class StubRunner:
        def run(self, args, **kwargs):
            captured["args"] = args
            return "explanation", ""

    monkeypatch.setattr(
        "backend.app.agent_workspace.adapters.subprocess.run",
        lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout="2.1.203", stderr=""),
    )
    try:
        assert workspace.manifest.editable_cells == ()
        assert list((workspace.root / "editable").iterdir()) == []
        assert "read-only turn" in (workspace.root / "INSTRUCTIONS.md").read_text().lower()
        result = ClaudeAgentAdapter(runner=StubRunner()).run(
            workspace, timeout=1, cancel_event=Event()
        )
        assert result.final_output == "explanation"
        tools = captured["args"][captured["args"].index("--tools") + 1].split(",")
        assert tools == ["Read"]
    finally:
        builder.destroy(workspace)


def _wait_process_gone(pid: int) -> bool:
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.02)
    return False


def _descendant_script(pid_file, *, output=b"ok", sleep=30, parent_sleep=0):
    return (
        "import pathlib,subprocess,sys; "
        f"p=subprocess.Popen([sys.executable,'-c','import time;time.sleep({sleep})']); "
        f"pathlib.Path({str(pid_file)!r}).write_text(str(p.pid)); "
        f"sys.stdout.buffer.write({output!r}); sys.stdout.flush(); "
        f"import time;time.sleep({parent_sleep})"
    )


def test_process_runner_cleans_descendant_after_success_and_bounds_output(tmp_path):
    pid_file = tmp_path / "child.pid"
    stdout, _ = ProcessRunner(max_capture_bytes=64).run(
        [sys.executable, "-c", _descendant_script(pid_file, output=b"x" * 1000)],
        cwd=tmp_path, timeout=2, cancel_event=Event(), grace_period=0.05,
    )
    assert len(stdout) == 64
    assert _wait_process_gone(int(pid_file.read_text()))


def test_process_runner_timeout_cleans_process_group(tmp_path):
    pid_file = tmp_path / "child.pid"
    with pytest.raises(AgentTimedOut):
        ProcessRunner().run(
            [sys.executable, "-c", _descendant_script(pid_file, parent_sleep=30)],
            cwd=tmp_path, timeout=0.1, cancel_event=Event(), grace_period=0.05,
        )
    assert _wait_process_gone(int(pid_file.read_text()))


def test_process_runner_cancellation_cleans_process_group(tmp_path):
    pid_file = tmp_path / "child.pid"
    cancelled = Event()
    timer = Timer(0.1, cancelled.set)
    timer.start()
    try:
        with pytest.raises(AgentCancelled):
            ProcessRunner().run(
                [sys.executable, "-c", _descendant_script(pid_file, parent_sleep=30)],
                cwd=tmp_path, timeout=2, cancel_event=cancelled,
                grace_period=0.05,
            )
    finally:
        timer.cancel()
    assert _wait_process_gone(int(pid_file.read_text()))


def test_process_runner_decode_error_still_cleans_descendant(tmp_path):
    pid_file = tmp_path / "child.pid"
    with pytest.raises(AgentAdapterError, match="valid UTF-8"):
        ProcessRunner().run(
            [sys.executable, "-c", _descendant_script(pid_file, output=b"\xff")],
            cwd=tmp_path, timeout=2, cancel_event=Event(), grace_period=0.05,
        )
    assert _wait_process_gone(int(pid_file.read_text()))


def test_process_runner_shutdown_rejects_new_runs(tmp_path):
    runner = ProcessRunner()
    runner.shutdown(grace_period=0.01)

    with pytest.raises(AgentAdapterError, match="shutting down"):
        runner.run(
            [sys.executable, "-c", "print('late')"], cwd=tmp_path,
            timeout=1, cancel_event=Event(),
        )


def test_configured_adapter_defaults_to_claude(monkeypatch):
    monkeypatch.delenv("NOTEBOOK_AGENT_ADAPTER", raising=False)
    assert isinstance(configured_agent_adapter(), ClaudeAgentAdapter)


def test_configured_adapter_requires_explicit_fake_mode(monkeypatch):
    monkeypatch.setenv("NOTEBOOK_AGENT_ADAPTER", "fake")
    assert isinstance(configured_agent_adapter(), DevelopmentFakeAgentAdapter)
    monkeypatch.setenv("NOTEBOOK_AGENT_ADAPTER", "unknown")
    with pytest.raises(RuntimeError, match="must be either"):
        configured_agent_adapter()
