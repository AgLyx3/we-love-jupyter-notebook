import os
import pathlib
import sys
import time
from threading import Event
from threading import Timer
from types import SimpleNamespace

import pytest

from backend.app.agent_workspace.models import WorkspaceBoundaryError, WorkspaceCleanupError
from backend.app.agent_workspace.adapters import (
    ClaudeAgentAdapter,
    CodexAgentAdapter,
    DevelopmentFakeAgentAdapter,
    FakeAgentAdapter,
    FakeAttempt,
)
from backend.app.main import configured_agent_adapters
from backend.app.agent_workspace.models import AgentAdapterError, AgentTimedOut
from backend.app.agent_workspace.models import AgentCancelled
from backend.app.agent_workspace.runner import ProcessRunner
from backend.app.agent_workspace.workspace_auditor import WorkspaceAuditor
from backend.app.agent_workspace.workspace_builder import AgentWorkspaceBuilder
from backend.app.notebook_document.service import NotebookDocumentService
from backend.app.turn_scope.models import FrozenTurnScope, ScopeSelection


@pytest.fixture(autouse=True)
def _clean_agent_env(monkeypatch):
    monkeypatch.delenv("NOTEBOOK_AGENT_ADAPTER", raising=False)
    monkeypatch.delenv("NOTEBOOK_DEFAULT_AGENT", raising=False)


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


def _trusted_workspace(notebook_payload):
    documents = NotebookDocumentService()
    snapshot = documents.import_notebook(notebook_payload())
    scope = FrozenTurnScope.create(
        turn_id="turn", session_id=snapshot.session_id,
        notebook_revision=snapshot.revision,
        selection=ScopeSelection((), ("intro",)), prompt="restructure",
    )
    builder = AgentWorkspaceBuilder()
    return builder, builder.build(snapshot, scope, write_scope="trusted")


def test_trusted_build_materializes_all_cells_and_writable_structure(notebook_payload):
    import json as _json

    builder, workspace = _trusted_workspace(notebook_payload)
    try:
        assert workspace.is_trusted
        # Every cell gets a writable source file under cells/.
        assert (workspace.root / "cells/cell_intro.md").read_text() == "# Example notebook\n"
        assert (workspace.root / "cells/cell_editable.py").read_text() == "value = 1\n"
        # Ordered, agent-writable structure.json mirrors notebook order.
        structure = _json.loads((workspace.root / "structure.json").read_text())
        assert [entry["cellId"] for entry in structure["cells"]] == ["intro", "editable"]
        assert structure["cells"][1]["source"] == "cells/cell_editable.py"
        # Manifest is the frozen original the backend diffs against.
        assert [cell.cell_id for cell in workspace.manifest.cells] == ["intro", "editable"]
        assert workspace.manifest.context_cell_ids == ("intro",)
        assert "Trusted turn" in (workspace.root / "INSTRUCTIONS.md").read_text()
    finally:
        builder.destroy(workspace)


def test_trusted_build_protects_only_readonly_notebook_and_instructions(notebook_payload):
    builder, workspace = _trusted_workspace(notebook_payload)
    try:
        # structure.json and cells/ are agent-writable → NOT in the protected baseline.
        assert workspace.baseline_hashes.keys() == {
            "notebook.readonly.ipynb", "INSTRUCTIONS.md"
        }
        # The read-only whole-notebook copy is chmod 444; structure.json is writable.
        assert not (os.stat(workspace.root / "notebook.readonly.ipynb").st_mode & 0o222)
        assert os.stat(workspace.root / "structure.json").st_mode & 0o200
    finally:
        builder.destroy(workspace)


def _rewrite_structure(workspace, cells):
    import json as _json

    (workspace.root / "structure.json").write_text(
        _json.dumps({"cells": cells}) + "\n", encoding="utf-8"
    )


def test_collect_trusted_returns_existing_entries_unchanged(notebook_payload):
    builder, workspace = _trusted_workspace(notebook_payload)
    try:
        entries = WorkspaceAuditor().collect_trusted(workspace)
        assert [e.cell_id for e in entries] == ["intro", "editable"]
        assert all(e.is_add is False for e in entries)
        assert entries[1].content == "value = 1\n"
    finally:
        builder.destroy(workspace)


def test_collect_trusted_accepts_add_entry_with_new_file(notebook_payload):
    builder, workspace = _trusted_workspace(notebook_payload)
    try:
        (workspace.root / "cells/new_1.py").write_text("print('hi')\n", encoding="utf-8")
        _rewrite_structure(workspace, [
            {"cellId": "intro", "cellType": "markdown", "source": "cells/cell_intro.md"},
            {"cellId": "editable", "cellType": "code", "source": "cells/cell_editable.py"},
            {"op": "add", "cellType": "code", "source": "cells/new_1.py"},
        ])
        entries = WorkspaceAuditor().collect_trusted(workspace)
        assert len(entries) == 3
        assert entries[2].is_add is True and entries[2].cell_id is None
        assert entries[2].content == "print('hi')\n"
    finally:
        builder.destroy(workspace)


def test_trusted_audit_honours_adapter_declared_auxiliary_paths(notebook_payload):
    # Regression: audit() took auxiliary_paths and audit_trusted() did not, so an
    # adapter whose CLI writes a declared scratch path worked on every Blocking
    # turn and failed every Trusted one. Both current adapters declare nothing,
    # which is the only reason this stayed invisible.
    builder, workspace = _trusted_workspace(notebook_payload)
    try:
        (workspace.root / "scratch").mkdir()
        (workspace.root / "scratch/state.json").write_text("{}\n", encoding="utf-8")
        (workspace.root / "agent.log").write_text("noise\n", encoding="utf-8")
        auditor = WorkspaceAuditor()

        undeclared = auditor.audit_trusted(workspace)
        assert "undeclared path: scratch" in undeclared
        assert "undeclared path: scratch/state.json" in undeclared
        assert "undeclared path: agent.log" in undeclared

        declared = auditor.audit_trusted(
            workspace, auxiliary_paths=frozenset({"scratch", "agent.log"}),
        )
        assert declared == []

        # Declaring one path must not blanket-allow the others.
        partial = auditor.audit_trusted(workspace, auxiliary_paths=frozenset({"scratch"}))
        assert partial == ["undeclared path: agent.log"]

        entries = auditor.collect_trusted(
            workspace, auxiliary_paths=frozenset({"scratch", "agent.log"}),
        )
        assert [e.cell_id for e in entries] == ["intro", "editable"]
    finally:
        builder.destroy(workspace)


def test_collect_trusted_rejects_source_escaping_cells_dir(notebook_payload):
    builder, workspace = _trusted_workspace(notebook_payload)
    try:
        _rewrite_structure(workspace, [
            {"cellId": "intro", "cellType": "markdown",
             "source": "cells/../notebook.readonly.ipynb"},
        ])
        with pytest.raises(WorkspaceBoundaryError):
            WorkspaceAuditor().collect_trusted(workspace)
    finally:
        builder.destroy(workspace)


def test_collect_trusted_rejects_hardlink_under_cells(notebook_payload, tmp_path):
    builder, workspace = _trusted_workspace(notebook_payload)
    try:
        external = tmp_path / "outside.txt"
        external.write_text("secret\n", encoding="utf-8")
        os.link(external, workspace.root / "cells/hard.py")
        _rewrite_structure(workspace, [
            {"op": "add", "cellType": "code", "source": "cells/hard.py"},
        ])
        with pytest.raises(WorkspaceBoundaryError):
            WorkspaceAuditor().collect_trusted(workspace)
    finally:
        builder.destroy(workspace)


def test_collect_trusted_rejects_symlink_under_cells(notebook_payload, tmp_path):
    builder, workspace = _trusted_workspace(notebook_payload)
    try:
        external = tmp_path / "outside.txt"
        external.write_text("secret\n", encoding="utf-8")
        os.symlink(external, workspace.root / "cells/link.py")
        _rewrite_structure(workspace, [
            {"op": "add", "cellType": "code", "source": "cells/link.py"},
        ])
        with pytest.raises(WorkspaceBoundaryError):
            WorkspaceAuditor().collect_trusted(workspace)
    finally:
        builder.destroy(workspace)


def test_collect_trusted_rejects_duplicate_source(notebook_payload):
    builder, workspace = _trusted_workspace(notebook_payload)
    try:
        _rewrite_structure(workspace, [
            {"cellId": "intro", "cellType": "markdown", "source": "cells/cell_intro.md"},
            {"op": "add", "cellType": "markdown", "source": "cells/cell_intro.md"},
        ])
        with pytest.raises(WorkspaceBoundaryError):
            WorkspaceAuditor().collect_trusted(workspace)
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


def test_turn_instructions_include_notebook_reasoning_context(notebook_payload):
    """Both edit and read-only turns should tell the agent it is in a Jupyter
    notebook and to diagnose undefined-name errors against the whole notebook
    (an unrun upstream cell), not just the failing cell."""
    for factory in (_workspace, _read_only_workspace):
        builder, workspace = factory(notebook_payload)
        try:
            instructions = (workspace.root / "INSTRUCTIONS.md").read_text().lower()
            assert "jupyter notebook" in instructions
            assert "notebook.ipynb" in instructions
            assert "nameerror" in instructions
            assert "has not been run yet" in instructions
            # The guidance must be actionable: how to spot the unrun defining cell.
            assert "execution_count" in instructions
        finally:
            builder.destroy(workspace)


def test_shell_rule_is_adapter_aware_across_every_workspace_shape(notebook_payload):
    # Regression: the flat "Do not run shell commands." is correct for an agent
    # with dedicated file tools but denies Codex — whose only file API is the
    # shell — any access to notebook.ipynb at all. Every shape that emits the
    # rule must be able to relax it, and none may relax it by default.
    shapes = [
        (_workspace, {}),
        (_read_only_workspace, {}),
        (_trusted_workspace, {"write_scope": "trusted"}),
    ]
    for factory, kwargs in shapes:
        builder, workspace = factory(notebook_payload)
        try:
            assert "Do not run shell commands." in (
                workspace.root / "INSTRUCTIONS.md"
            ).read_text()
        finally:
            builder.destroy(workspace)

    documents = NotebookDocumentService()
    snapshot = documents.import_notebook(notebook_payload())
    builder = AgentWorkspaceBuilder()
    # The third element is the file access the shell rule should grant: a turn
    # that may not write must not be told it may write.
    for selection, kwargs, access in (
        (ScopeSelection(("editable",), ("intro",)), {}, "read and write"),
        (ScopeSelection((), ("intro",)), {}, "read"),
        (ScopeSelection((), ("intro",)), {"write_scope": "trusted"}, "read and write"),
    ):
        scope = FrozenTurnScope.create(
            turn_id="turn", session_id=snapshot.session_id,
            notebook_revision=snapshot.revision, selection=selection, prompt="go",
        )
        workspace = builder.build(
            snapshot, scope, file_access_via_shell=True, **kwargs
        )
        try:
            instructions = (workspace.root / "INSTRUCTIONS.md").read_text()
            assert "Do not run shell commands." not in instructions
            assert f"shell/exec tool only to {access} files" in instructions
            if "This is a read-only turn" in instructions:
                assert "read and write" not in instructions
        finally:
            builder.destroy(workspace)


def test_claude_adapter_grants_write_tools_for_trusted_workspace(
    monkeypatch, notebook_payload,
):
    # Regression: the real adapter read workspace.manifest.editable_cells, which a
    # Trusted (TrustedWorkspaceManifest) workspace does not have, so a Trusted turn
    # crashed with AttributeError. It must grant edit/write tools (whole notebook
    # is editable). The fake test adapter never hit this path.
    builder, workspace = _trusted_workspace(notebook_payload)
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
        tools = captured["args"][captured["args"].index("--tools") + 1].split(",")
        assert set(tools) == {"Read", "Edit", "Write"}
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


def test_configured_adapters_default_to_claude():
    adapters, default = configured_agent_adapters()
    assert default == "claude"
    assert isinstance(adapters["claude"], ClaudeAgentAdapter)
    assert isinstance(adapters["codex"], CodexAgentAdapter)


def test_configured_adapters_codex_mode_registers_both(monkeypatch):
    monkeypatch.setenv("NOTEBOOK_DEFAULT_AGENT", "codex")
    adapters, default = configured_agent_adapters()
    assert default == "codex"
    assert set(adapters) == {"claude", "codex"}


def test_configured_adapters_fake_and_invalid(monkeypatch):
    monkeypatch.setenv("NOTEBOOK_DEFAULT_AGENT", "fake")
    adapters, default = configured_agent_adapters()
    assert default == "fake"
    assert set(adapters) == {"fake"}
    assert isinstance(adapters["fake"], DevelopmentFakeAgentAdapter)
    monkeypatch.setenv("NOTEBOOK_DEFAULT_AGENT", "gemini")
    with pytest.raises(RuntimeError, match="claude.*codex.*fake"):
        configured_agent_adapters()


def test_configured_adapters_rejects_the_old_variable(monkeypatch):
    # It used to name the *only* adapter. Someone with NOTEBOOK_AGENT_ADAPTER=fake
    # still exported must not silently get a real CLI instead.
    monkeypatch.setenv("NOTEBOOK_AGENT_ADAPTER", "fake")
    with pytest.raises(RuntimeError, match="NOTEBOOK_DEFAULT_AGENT"):
        configured_agent_adapters()


def _codex_version_stub(version="codex-cli 0.133.0", returncode=0):
    return lambda *args, **kwargs: SimpleNamespace(
        returncode=returncode, stdout=version, stderr="",
    )


def test_codex_adapter_version_gate(monkeypatch):
    monkeypatch.setattr(
        "backend.app.agent_workspace.adapters.subprocess.run",
        _codex_version_stub("codex-cli 0.133.5"),
    )
    assert CodexAgentAdapter().verify_supported() == "0.133.5"
    # A later 0.x minor is the ordinary release train, not a break: 0.135.0 is
    # what a machine that auto-updated actually has, and it must still run.
    monkeypatch.setattr(
        "backend.app.agent_workspace.adapters.subprocess.run",
        _codex_version_stub("codex-cli 0.135.0"),
    )
    assert CodexAgentAdapter().verify_supported() == "0.135.0"
    for bad in ("codex-cli 0.132.9", "codex-cli 1.0.0", "garbage"):
        monkeypatch.setattr(
            "backend.app.agent_workspace.adapters.subprocess.run",
            _codex_version_stub(bad),
        )
        with pytest.raises(AgentAdapterError):
            CodexAgentAdapter().verify_supported()
    monkeypatch.setattr(
        "backend.app.agent_workspace.adapters.subprocess.run",
        _codex_version_stub(returncode=1),
    )
    with pytest.raises(AgentAdapterError):
        CodexAgentAdapter().verify_supported()


def test_codex_editable_turn_uses_workspace_write_sandbox(notebook_payload, monkeypatch):
    builder, workspace = _workspace(notebook_payload)
    captured = {}

    class StubRunner:
        def run(self, args, **kwargs):
            captured["args"] = args
            # Simulate Codex writing the final message file.
            out = args[args.index("--output-last-message") + 1]
            pathlib.Path(out).write_text("codex finished\n", encoding="utf-8")
            return "progress noise", ""

    monkeypatch.setattr(
        "backend.app.agent_workspace.adapters.subprocess.run", _codex_version_stub(),
    )
    try:
        result = CodexAgentAdapter(runner=StubRunner()).run(
            workspace, timeout=1, cancel_event=Event()
        )
        assert result.final_output == "codex finished"
        args = captured["args"]
        assert args[:2] == ["codex", "exec"]
        assert args[args.index("--sandbox") + 1] == "workspace-write"
        assert "--ephemeral" in args
        assert "--ignore-user-config" in args
        assert "--skip-git-repo-check" in args
        # workspace-write also grants $TMPDIR and /tmp unless both are excluded,
        # and the audit below only ever looks at the workspace root.
        overrides = {args[i + 1] for i, a in enumerate(args) if a == "-c"}
        assert overrides == {
            "sandbox_workspace_write.network_access=false",
            "sandbox_workspace_write.exclude_tmpdir_env_var=true",
            "sandbox_workspace_write.exclude_slash_tmp=true",
        }
        assert args[args.index("-C") + 1] == str(workspace.root)
        # The `apps` feature exposes account-level connectors (GitHub write,
        # site deploys) that no sandbox or config flag reaches — verified
        # against codex-cli 0.135.0, where the model called one and it ran.
        assert args[args.index("--disable") + 1] == "apps"
        # The prompt is last and behind "--", so a prompt starting with "-"
        # (a pasted markdown bullet, a diff line) is not parsed as a flag.
        assert args[-2] == "--"
        assert not args[-1].startswith("--ephemeral")
        # Final-message capture must live outside the audited workspace.
        out = pathlib.Path(args[args.index("--output-last-message") + 1])
        assert workspace.root not in out.parents
        assert "--model" not in args
    finally:
        builder.destroy(workspace)


def test_cli_availability_is_probed_once_per_process(monkeypatch):
    # The capabilities endpoint asks on every app load, and the probe shells out
    # to `<cli> --version`, so the answer is remembered rather than re-run.
    calls = []

    def stub(*args, **kwargs):
        calls.append(args[0])
        return SimpleNamespace(returncode=0, stdout="codex-cli 0.135.0", stderr="")

    monkeypatch.setattr("backend.app.agent_workspace.adapters.subprocess.run", stub)
    adapter = CodexAgentAdapter()
    assert adapter.is_available() is True
    assert adapter.is_available() is True
    assert len(calls) == 1

    monkeypatch.setattr(
        "backend.app.agent_workspace.adapters.subprocess.run",
        lambda *a, **k: (_ for _ in ()).throw(OSError("no such file")),
    )
    missing = CodexAgentAdapter()
    assert missing.is_available() is False
    # A separate instance must not inherit another's answer.
    assert ClaudeAgentAdapter()._available is None


def test_codex_adapter_grants_write_sandbox_for_trusted_workspace(
    monkeypatch, notebook_payload,
):
    # Regression: the adapter read workspace.manifest.editable_cells, which a
    # Trusted (TrustedWorkspaceManifest) workspace does not have, so a Trusted turn
    # crashed with AttributeError. It must get workspace-write (whole notebook is
    # editable) — read-only would silently strand every structural edit.
    builder, workspace = _trusted_workspace(notebook_payload)
    captured = {}

    class StubRunner:
        def run(self, args, **kwargs):
            captured["args"] = args
            return "restructured", ""

    monkeypatch.setattr(
        "backend.app.agent_workspace.adapters.subprocess.run", _codex_version_stub(),
    )
    try:
        result = CodexAgentAdapter(runner=StubRunner()).run(
            workspace, timeout=1, cancel_event=Event()
        )
        assert result.final_output == "restructured"
        assert captured["args"][captured["args"].index("--sandbox") + 1] == "workspace-write"
    finally:
        builder.destroy(workspace)


def test_process_runner_gives_the_cli_no_stdin(tmp_path):
    # Regression: stdin was inherited. `codex exec` reads stdin even when the
    # prompt is an argument, so when the server's own stdin never reaches EOF
    # the CLI blocks until the 600s turn timeout, holding the document lease
    # throughout. The bundled editor launched by mcp/supervisor.py inherits the
    # MCP stdio pipe, which never sees EOF, so that is the live case.
    #
    # To reproduce, this process's fd 0 IS an open pipe for the duration: an
    # inherited stdin then blocks and the run times out, while DEVNULL reads
    # EOF at once. Verified against the real CLI too — DEVNULL exits in ~3s,
    # an open pipe was still running at 20s.
    read_fd, write_fd = os.pipe()
    saved_stdin = os.dup(0)
    try:
        os.dup2(read_fd, 0)  # nothing is ever written, so no EOF arrives
        runner = ProcessRunner()
        try:
            stdout, _ = runner.run(
                [sys.executable, "-c", "import sys; print(len(sys.stdin.read()))"],
                cwd=tmp_path, timeout=5, cancel_event=Event(),
            )
        finally:
            runner.shutdown()
        assert stdout.strip() == "0", "the child should read EOF immediately"
    finally:
        os.dup2(saved_stdin, 0)
        os.close(saved_stdin)
        os.close(read_fd)
        os.close(write_fd)


def test_plan_turn_with_editable_cells_is_not_told_it_may_write(notebook_payload):
    # Regression: writable was keyed on "are there editable cells", but a Plan
    # turn can have a selection and still write nothing — the adapter gives it
    # --sandbox read-only. So a plan-with-selection turn was told to "read and
    # write" while the sandbox forbade writing.
    documents = NotebookDocumentService()
    snapshot = documents.import_notebook(notebook_payload())
    scope = FrozenTurnScope.create(
        turn_id="turn", session_id=snapshot.session_id,
        notebook_revision=snapshot.revision,
        selection=ScopeSelection(("editable",), ("intro",)), prompt="plan it",
    )
    builder = AgentWorkspaceBuilder()
    workspace = builder.build(
        snapshot, scope, file_access_via_shell=True, writable=False,
    )
    try:
        instructions = (workspace.root / "INSTRUCTIONS.md").read_text()
        assert "Editable files:" in instructions, "the selection is still declared"
        assert "shell/exec tool only to read files" in instructions
        assert "read and write" not in instructions
    finally:
        builder.destroy(workspace)


def test_codex_read_only_and_plan_turns_use_read_only_sandbox(notebook_payload, monkeypatch):
    captured = {}

    class StubRunner:
        def run(self, args, **kwargs):
            captured["args"] = args
            return "explanation", ""

    monkeypatch.setattr(
        "backend.app.agent_workspace.adapters.subprocess.run", _codex_version_stub(),
    )
    builder, workspace = _read_only_workspace(notebook_payload)
    try:
        result = CodexAgentAdapter(runner=StubRunner()).run(
            workspace, timeout=1, cancel_event=Event()
        )
        # No final-message file written -> falls back to stdout.
        assert result.final_output == "explanation"
        args = captured["args"]
        assert args[args.index("--sandbox") + 1] == "read-only"
    finally:
        builder.destroy(workspace)
    builder, workspace = _workspace(notebook_payload)
    try:
        CodexAgentAdapter(runner=StubRunner()).run(
            workspace, timeout=1, cancel_event=Event(), permission_mode="plan",
        )
        args = captured["args"]
        assert args[args.index("--sandbox") + 1] == "read-only"
        # The prompt is the last argument, behind "--", so a prompt beginning
        # with "-" cannot be parsed as a flag.
        assert args[-2] == "--"
        assert args[-1].startswith("You are operating in plan mode")
    finally:
        builder.destroy(workspace)


def test_codex_model_allow_list(notebook_payload, monkeypatch):
    captured = {}

    class StubRunner:
        def run(self, args, **kwargs):
            captured["args"] = args
            return "done", ""

    monkeypatch.setattr(
        "backend.app.agent_workspace.adapters.subprocess.run", _codex_version_stub(),
    )
    builder, workspace = _workspace(notebook_payload)
    try:
        CodexAgentAdapter(runner=StubRunner()).run(
            workspace, timeout=1, cancel_event=Event(), model="gpt-5.5",
        )
        args = captured["args"]
        assert args[args.index("--model") + 1] == "gpt-5.5"
        CodexAgentAdapter(runner=StubRunner()).run(
            workspace, timeout=1, cancel_event=Event(), model="opus",
        )
        assert "--model" not in captured["args"]
    finally:
        builder.destroy(workspace)
