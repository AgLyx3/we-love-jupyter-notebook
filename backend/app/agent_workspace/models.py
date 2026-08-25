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


@dataclass(frozen=True)
class MemoryOperation:
    """One source change a past turn made, with its outcome as known now.

    Outcome comes from the per-operation ledger, so a single cell can produce
    two of these: the hunks that survived and the hunks the user undid are
    different facts with different statuses, and collapsing them would report
    one of the two wrongly.

    - ``KEPT``: the user reviewed it and kept it.
    - ``APPLIED``: it is in the notebook but the user has not reviewed it yet.
      Distinct from ``KEPT`` on purpose — an unreviewed change carries no signal
      of approval, and reporting it as kept would invent one.
    - ``UNDONE``: the user rejected it. This is the only status that carries a
      diff, because it is the only content that exists nowhere else.
    """

    cell_id: str
    status: str  # "KEPT" | "APPLIED" | "UNDONE"
    previous_source: str
    next_source: str
    # The cell has diverged from the ledger since — a hand edit in the tab, an
    # MCP client's set_cell_source, or the plot-tuning panel writing back a
    # tuned literal all do it, and the check that derives this cannot tell them
    # apart. Orthogonal to status rather than a fourth value of it: a hunk can
    # be kept *and* since overwritten, and the agent needs both facts. Without
    # it the feed asserts an account of the cell the document no longer matches.
    stale: bool = False
    # "edit", "add", "delete", "move" or "retype". Only "edit" carries a
    # before/after pair to diff. The other four are structural: the ledger keeps
    # a hash for an add and nothing at all for the rest, so they are described
    # rather than diffed. They still have to appear — a Trusted turn that only
    # added, deleted, or moved cells would otherwise render as "It made no
    # changes", which is a false statement about a destructive edit and invites
    # the agent to make it again.
    kind: str = "edit"


@dataclass(frozen=True)
class MemoryEntry:
    """One past turn, as the thread memory feed will render it.

    `turn_status` is set only when the turn as a whole ended without a settled
    per-operation outcome (cancelled, failed). It is never inferred: a cancelled
    turn can have applied its changes before cancelling, so the feed states what
    happened and points at the notebook rather than guessing.
    """

    prompt: str
    mode: str
    operations: tuple[MemoryOperation, ...] = ()
    reply: str = ""
    turn_status: str = ""


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
