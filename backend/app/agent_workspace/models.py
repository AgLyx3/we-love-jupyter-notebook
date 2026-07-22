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


@dataclass
class AgentWorkspace:
    root: Path
    manifest: WorkspaceManifest
    baseline_hashes: dict[str, str]


@dataclass(frozen=True)
class AdapterResult:
    final_output: str


class AgentAdapter(Protocol):
    auxiliary_paths: frozenset[str]

    def run(self, workspace: AgentWorkspace, *, timeout: float, cancel_event: object) -> AdapterResult: ...
