from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from ..notebook_document.models import NotebookDomainError


class WorkspaceBoundaryError(NotebookDomainError):
    code = "workspace_boundary_violation"
    message = "Agent workspace violated the editable-cell boundary"
    status_code = 422

    def __init__(self, violations: list[str]) -> None:
        self.violations = violations
        super().__init__(violations=violations)


class WorkspaceCleanupError(RuntimeError):
    def __init__(self, root: Path, attempts: int, cause: OSError) -> None:
        super().__init__(
            f"Agent workspace {root} could not be removed after {attempts} attempts"
        )
        self.root = root
        self.attempts = attempts
        self.__cause__ = cause


class AgentAdapterError(NotebookDomainError):
    code = "agent_adapter_error"
    message = "Agent adapter failed"
    status_code = 502


class AgentTimedOut(AgentAdapterError):
    code = "agent_timed_out"
    message = "Agent attempt timed out"
    status_code = 504


class AgentCancelled(AgentAdapterError):
    code = "agent_cancelled"
    message = "Agent turn was cancelled"
    status_code = 409


@dataclass(frozen=True)
class EditableCellManifest:
    cell_id: str
    index: int
    cell_type: str
    relative_path: str
    original_source: str


@dataclass(frozen=True)
class ContextCellManifest:
    cell_id: str
    index: int
    cell_type: str
    preview: str


@dataclass(frozen=True)
class WorkspaceManifest:
    notebook_path: str
    editable_cells: tuple[EditableCellManifest, ...]
    context_cells: tuple[ContextCellManifest, ...]


@dataclass(frozen=True)
class StructuralCellManifest:
    """One cell in a Trusted-turn workspace. Every cell is writable."""

    cell_id: str
    index: int
    cell_type: str
    relative_path: str
    original_source: str


@dataclass(frozen=True)
class TrustedWorkspaceManifest:
    """The frozen, in-memory original structure for a Trusted turn.

    Held immutably by the backend and diffed against the agent-written
    ``structure.json``; the on-disk structure file is never trusted as the
    source of the original order (mirrors the Blocking manifest rule).
    """

    notebook_path: str
    structure_path: str
    cells: tuple[StructuralCellManifest, ...]
    context_cell_ids: tuple[str, ...]


@dataclass(frozen=True)
class ReturnedStructureEntry:
    """One agent-returned structure.json entry with its resolved source.

    ``is_add`` marks an ``{"op": "add", ...}`` entry (no cellId); ``cell_id`` is
    then ``None``. The string ``"new"`` is never a sentinel, so a real cell whose
    id happens to be ``new`` is unambiguous. For an existing entry, ``cell_id`` is
    the referenced id (``None`` if the agent omitted/garbled it — the validator
    rejects that).
    """

    is_add: bool
    cell_id: str | None
    cell_type: str
    relative_path: str
    content: str


@dataclass
class AgentWorkspace:
    root: Path
    manifest: WorkspaceManifest | TrustedWorkspaceManifest
    baseline_hashes: dict[str, str]

    @property
    def is_trusted(self) -> bool:
        return isinstance(self.manifest, TrustedWorkspaceManifest)


@dataclass(frozen=True)
class AdapterResult:
    final_output: str


class AgentAdapter(Protocol):
    auxiliary_paths: frozenset[str]

    def run(
        self, workspace: AgentWorkspace, *, timeout: float, cancel_event: object,
        model: str | None = None, permission_mode: str = "acceptEdits",
    ) -> AdapterResult: ...

    def run_prompt(
        self, prompt: str, *, timeout: float, cancel_event: object,
        model: str | None = None,
    ) -> AdapterResult: ...


class PromptAdapter(Protocol):
    """The read-only half of an adapter: text in, text out, no workspace.

    The notebook overview's segmentation pass needs a model call that writes
    nothing anywhere — no workspace to build, no files to audit, no cells to
    make editable. `run` cannot express that: it reads its prompt out of
    `INSTRUCTIONS.md` inside a workspace root and hands the CLI edit tools
    whenever any cell is editable.

    This is the narrow path the overview spec (§4.2) asks for in preference to
    a second, private route to the CLI. Everything that makes the existing
    adapter trustworthy — the version check, the MCP and slash-command
    lockdown, the process-group teardown in ProcessRunner — is shared with
    `run`; the only difference is that no tools are enabled at all.
    """

    def run_prompt(
        self, prompt: str, *, timeout: float, cancel_event: object,
        model: str | None = None,
    ) -> AdapterResult: ...
