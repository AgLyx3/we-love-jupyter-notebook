import os
from threading import Event
from types import SimpleNamespace

import pytest

from backend.app.agent_workspace.models import WorkspaceBoundaryError
from backend.app.agent_workspace.adapters import ClaudeAgentAdapter, FakeAgentAdapter, FakeAttempt
from backend.app.agent_workspace.models import AgentAdapterError, AgentTimedOut
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


def test_claude_adapter_is_version_gated_and_has_no_bash(monkeypatch, notebook_payload):
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
        assert "Bash" not in captured["args"]
        assert captured["args"][-4:] == ["--allowedTools", "Read", "Edit", "Write"]
        monkeypatch.setattr(
            "backend.app.agent_workspace.adapters.subprocess.run",
            lambda *args, **kwargs: SimpleNamespace(returncode=0, stdout="2.2.0", stderr=""),
        )
        with pytest.raises(AgentAdapterError):
            ClaudeAgentAdapter().verify_supported()
    finally:
        builder.destroy(workspace)
