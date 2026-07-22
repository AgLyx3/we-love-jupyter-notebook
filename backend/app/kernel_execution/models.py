from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from threading import Event
from typing import Any

from ..notebook_document.models import NotebookDomainError


class ExecutionNotFound(NotebookDomainError):
    code = "execution_not_found"
    message = "Execution operation was not found"
    status_code = 404

    def __init__(self, execution_id: str) -> None:
        super().__init__(executionAttemptId=execution_id)


class ExecutionDecisionConflict(NotebookDomainError):
    code = "execution_decision_conflict"
    message = "Execution decision is stale or conflicts with an earlier decision"
    status_code = 409


class StaleExecutionResult(NotebookDomainError):
    code = "stale_execution_result"
    message = "Execution result no longer matches the active notebook operation"
    status_code = 409


class KernelSessionConflict(NotebookDomainError):
    code = "kernel_session_conflict"
    message = "Kernel session no longer matches"
    status_code = 409


class KernelCellTimeout(TimeoutError):
    def __init__(self, *, recovered: bool) -> None:
        super().__init__("Cell execution timed out")
        self.recovered = recovered


class KernelExecutionCancelled(Exception):
    """The correlated attempt was invalidated while the kernel was running."""


class KernelOutputLimitExceeded(Exception):
    def __init__(self, *, recovered: bool) -> None:
        super().__init__("Kernel output exceeded the configured limit")
        self.recovered = recovered


class KernelRestartRequired(NotebookDomainError):
    code = "kernel_restart_required"
    message = "Kernel must be restarted before execution can continue"
    status_code = 409


class ExecutionTimedOut(NotebookDomainError):
    code = "cell_timed_out"
    message = "Cell execution timed out"
    status_code = 408

    def __init__(self, *, recovered: bool) -> None:
        super().__init__(kernelRecovered=recovered)


@dataclass(frozen=True)
class RiskClassification:
    level: str
    reasons: tuple[str, ...] = ()
    matched_patterns: tuple[str, ...] = ()


@dataclass
class CellExecutionAttempt:
    attempt_id: str
    operation_id: str
    cell_id: str
    cell_index: int
    source_hash: str
    starting_revision: int
    source_preview: str = ""
    state: str = "created"
    risk: RiskClassification = field(default_factory=lambda: RiskClassification("safe"))
    decision: str | None = None
    outputs: list[dict[str, Any]] = field(default_factory=list)
    execution_count: int | None = None
    error: dict[str, Any] | None = None
    active: bool = True
    decision_event: Event = field(default_factory=Event, repr=False)


@dataclass
class ExecutionOperation:
    operation_id: str
    session_id: str
    base_revision: int
    kind: str
    parent_turn_id: str | None = None
    state: str = "created"
    current_revision: int | None = None
    current_attempt_id: str | None = None
    attempts: list[CellExecutionAttempt] = field(default_factory=list)
    error: dict[str, Any] | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    completed_at: datetime | None = None
    cancel_event: Event = field(default_factory=Event, repr=False)


TERMINAL_EXECUTION_STATES = {
    "completed", "failed", "cancelled", "validation_incomplete", "timed_out",
}
