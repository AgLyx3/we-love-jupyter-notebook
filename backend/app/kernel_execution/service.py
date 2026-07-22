from __future__ import annotations

import copy
import hashlib
import json
from datetime import datetime, timezone
from threading import RLock, Thread
from typing import Any
from uuid import uuid4

from ..notebook_document.models import CellNotFound, MutationLease, NotebookDomainError
from ..notebook_document.service import NotebookDocumentService
from ..session_events.service import SessionEventService
from .kernel_session import KernelSession
from .models import (
    CellExecutionAttempt, ExecutionDecisionConflict, ExecutionNotFound,
    ExecutionOperation, KernelCellTimeout, KernelExecutionCancelled,
    KernelOutputLimitExceeded, KernelRestartRequired, KernelSessionConflict,
    StaleExecutionResult,
    TERMINAL_EXECUTION_STATES,
)
from .risky_cell_classifier import RiskyCellClassifier


DEFAULT_TERMINAL_OPERATION_LIMIT = 100
DEFAULT_TERMINAL_RETENTION_SECONDS = 3600
DEFAULT_OPERATION_HISTORY_BYTES = 16 * 1024 * 1024


class KernelExecutionService:
    """Executes cells and retains up to one hour/100 terminal operations for polling."""
    def __init__(
        self, *, documents: NotebookDocumentService,
        events: SessionEventService | None = None,
        classifier: RiskyCellClassifier | None = None,
        kernel: KernelSession | None = None,
        max_terminal_operations: int = DEFAULT_TERMINAL_OPERATION_LIMIT,
        terminal_retention_seconds: float = DEFAULT_TERMINAL_RETENTION_SECONDS,
        max_history_bytes: int = DEFAULT_OPERATION_HISTORY_BYTES,
    ) -> None:
        self.documents = documents
        self.events = events or SessionEventService()
        self.classifier = classifier or RiskyCellClassifier()
        self.kernel = kernel or KernelSession()
        self.max_terminal_operations = max_terminal_operations
        self.terminal_retention_seconds = terminal_retention_seconds
        self.max_history_bytes = max_history_bytes
        self._lock = RLock()
        self._operations: dict[str, ExecutionOperation] = {}
        self._attempts: dict[str, tuple[ExecutionOperation, CellExecutionAttempt]] = {}
        self._kernel_notebook_session_id: str | None = None
        documents.register_session_replacement_listener(self._on_session_replaced)

    def start_cell(self, *, cell_id: str, session_id: str, expected_revision: int) -> ExecutionOperation:
        return self._start_manual(cell_id=cell_id, session_id=session_id, expected_revision=expected_revision)

    def start_all(self, *, session_id: str, expected_revision: int) -> ExecutionOperation:
        return self._start_manual(cell_id=None, session_id=session_id, expected_revision=expected_revision)

    def _start_manual(self, *, cell_id: str | None, session_id: str, expected_revision: int) -> ExecutionOperation:
        if self.kernel.status == "restart_required":
            raise KernelRestartRequired()
        operation_id = uuid4().hex
        lease = self.documents.coordinator.acquire(operation_type="manual_execution", operation_id=operation_id)
        try:
            snapshot = self.documents.get_snapshot()
            self.documents.check_snapshot_preconditions(snapshot, session_id, expected_revision)
            operation = ExecutionOperation(operation_id, session_id, expected_revision, "manual", current_revision=expected_revision)
            with self._lock:
                self._operations[operation_id] = operation
                self._prune_history_locked()
                created = self._copy_operation(operation)
            self._publish(operation)
        except Exception:
            self.documents.coordinator.release(lease)
            raise
        Thread(target=self._run_manual_guarded, args=(operation, lease, cell_id), daemon=True, name=f"execution-{operation_id[:8]}").start()
        return created

    def _run_manual_guarded(self, operation: ExecutionOperation, lease: MutationLease, cell_id: str | None) -> None:
        try:
            snapshot = self.documents.get_snapshot()
            if cell_id is None:
                indexes = range(len(snapshot.notebook["cells"]))
            else:
                index = next((index for index, cell in enumerate(snapshot.notebook["cells"]) if cell["id"] == cell_id), None)
                if index is None:
                    raise CellNotFound(cell_id)
                indexes = [index]
            self._run(operation, lease, indexes, prompt_for_risk=False)
        except Exception as error:
            self._fail(operation, error)
        finally:
            self.documents.coordinator.release(lease)
            self._publish(operation)

    def execute_downstream(
        self, *, parent_turn_id: str, session_id: str, expected_revision: int,
        changed_cell_ids: set[str], lease: MutationLease, cancel_event,
    ) -> ExecutionOperation:
        operation = self.create_downstream(
            parent_turn_id=parent_turn_id, session_id=session_id,
            expected_revision=expected_revision, cancel_event=cancel_event,
        )
        return self.run_downstream(
            operation.operation_id, changed_cell_ids=changed_cell_ids, lease=lease,
        )

    def create_downstream(
        self, *, parent_turn_id: str, session_id: str,
        expected_revision: int, cancel_event,
    ) -> ExecutionOperation:
        operation = ExecutionOperation(
            uuid4().hex, session_id, expected_revision, "agent_downstream",
            parent_turn_id=parent_turn_id, current_revision=expected_revision,
            cancel_event=cancel_event,
        )
        with self._lock:
            self._operations[operation.operation_id] = operation
            self._prune_history_locked()
            created = self._copy_operation(operation)
        self._publish(operation)
        return created

    def run_downstream(
        self, operation_id: str, *, changed_cell_ids: set[str],
        lease: MutationLease,
    ) -> ExecutionOperation:
        with self._lock:
            operation = self._operations.get(operation_id)
            if operation is None:
                raise ExecutionNotFound(operation_id)
        snapshot = self.documents.get_snapshot()
        indexes_by_id = {cell["id"]: index for index, cell in enumerate(snapshot.notebook["cells"])}
        start = min(indexes_by_id[cell_id] for cell_id in changed_cell_ids)
        try:
            self._run(operation, lease, range(start, len(snapshot.notebook["cells"])), prompt_for_risk=True)
        except Exception as error:
            self._fail(operation, error)
        with self._lock:
            return self._copy_operation(operation)

    def _run(self, operation: ExecutionOperation, lease: MutationLease, indexes, *, prompt_for_risk: bool) -> None:
        self._ensure_kernel_session(operation.session_id)
        self._set_operation_state(operation, "running")
        for index in indexes:
            if operation.cancel_event.is_set():
                self._finish(operation, "cancelled")
                return
            snapshot = self.documents.get_snapshot()
            if snapshot.session_id != operation.session_id or snapshot.revision != operation.current_revision:
                raise StaleExecutionResult()
            cell = snapshot.notebook["cells"][index]
            if cell["cell_type"] != "code":
                continue
            source = cell.get("source", "")
            source = "".join(source) if isinstance(source, list) else source
            source_hash = hashlib.sha256(source.encode()).hexdigest()
            attempt = CellExecutionAttempt(
                uuid4().hex, operation.operation_id, cell["id"], index,
                source_hash, snapshot.revision,
                source_preview=(source[:400] + ("..." if len(source) > 400 else "")),
                risk=self.classifier.classify(source),
            )
            with self._lock:
                operation.attempts.append(attempt)
                operation.current_attempt_id = attempt.attempt_id
                self._attempts[attempt.attempt_id] = (operation, attempt)
            if prompt_for_risk and attempt.risk.level == "confirm":
                attempt.state = "awaiting_approval"
                self._set_operation_state(operation, "awaiting_approval")
                while not attempt.decision_event.wait(0.1):
                    if operation.cancel_event.is_set():
                        attempt.active = False
                        attempt.state = "cancelled"
                        self._finish(operation, "cancelled")
                        return
                if operation.cancel_event.is_set() or not attempt.active:
                    attempt.state = "cancelled"
                    self._finish(operation, "cancelled")
                    return
                if attempt.decision == "skip":
                    attempt.state = "skipped"
                    attempt.active = False
                    self._finish(operation, "validation_incomplete")
                    return
                if attempt.decision == "cancel":
                    attempt.state = "cancelled"
                    attempt.active = False
                    self._finish(operation, "cancelled")
                    return
            self._set_operation_state(operation, "running")
            with self._lock:
                if operation.cancel_event.is_set() or not attempt.active:
                    attempt.state = "cancelled"
                    self._finish(operation, "cancelled")
                    return
                attempt.state = "running"
            self._publish(operation)
            try:
                result = self.kernel.execute(source, attempt.attempt_id)
            except KernelCellTimeout as error:
                with self._lock:
                    attempt.active = False
                    attempt.state = "timed_out"
                    attempt.error = {
                        "code": "cell_timed_out",
                        "message": "Cell execution timed out",
                        "details": {"kernelRecovered": error.recovered},
                    }
                    operation.error = copy.deepcopy(attempt.error)
                self._finish(operation, "timed_out")
                return
            except KernelExecutionCancelled:
                with self._lock:
                    attempt.active = False
                    if operation.state != "failed":
                        attempt.state = "cancelled"
                if operation.state not in TERMINAL_EXECUTION_STATES:
                    self._finish(operation, "cancelled")
                return
            except KernelOutputLimitExceeded as error:
                with self._lock:
                    attempt.active = False
                    attempt.state = "failed"
                    attempt.error = {
                        "code": "output_limit_exceeded",
                        "message": "Kernel output exceeded the configured limit",
                        "details": {
                            "truncated": True,
                            "kernelRecovered": error.recovered,
                        },
                    }
                    operation.error = copy.deepcopy(attempt.error)
                self._finish(operation, "failed")
                return
            except KernelRestartRequired as error:
                with self._lock:
                    attempt.active = False
                    attempt.state = "failed"
                    attempt.error = {
                        "code": error.code, "message": error.message,
                        "details": {},
                    }
                    operation.error = copy.deepcopy(attempt.error)
                self._finish(operation, "failed")
                return
            except Exception as error:
                with self._lock:
                    attempt.active = False
                    attempt.state = "failed"
                    attempt.error = {
                        "code": "kernel_execution_failed",
                        "message": str(error), "details": {},
                    }
                    operation.error = copy.deepcopy(attempt.error)
                raise
            if operation.cancel_event.is_set():
                attempt.state = "cancelled"
                attempt.active = False
                self._finish(operation, "cancelled")
                return
            try:
                # Shared cancellation/commit gate: cancellation makes the
                # attempt inactive under this same lock, so an accepted cancel
                # can never be followed by an output commit.
                with self._lock:
                    if (
                        operation.current_attempt_id != attempt.attempt_id
                        or attempt.state != "running"
                        or not attempt.active
                        or operation.cancel_event.is_set()
                    ):
                        raise StaleExecutionResult()
                    updated = self.documents.apply_execution_result_under_lease(
                        cell_id=attempt.cell_id, outputs=result.outputs,
                        execution_count=result.execution_count,
                        expected_revision=attempt.starting_revision,
                        expected_source_hash=attempt.source_hash,
                        owner=operation.parent_turn_id or operation.operation_id,
                        lease=lease,
                    )
                    attempt.active = False
                    attempt.outputs = result.outputs
                    attempt.execution_count = result.execution_count
                    attempt.state = "failed" if result.error else "completed"
                    operation.current_revision = updated.revision
                    self._bound_operation_outputs_locked(operation)
            except NotebookDomainError as error:
                raise StaleExecutionResult() from error
            self.events.publish("notebook.updated", {"sessionId": operation.session_id, "revision": updated.revision, "ownerId": operation.parent_turn_id or operation.operation_id, "executionAttemptId": attempt.attempt_id})
            self._publish(operation)
            if result.error:
                raise RuntimeError("Cell execution failed")
        self._finish(operation, "completed")

    def decide(
        self, attempt_id: str, decision: str, *, session_id: str,
        expected_revision: int, turn_id: str, cell_id: str,
    ) -> ExecutionOperation:
        with self._lock:
            pair = self._attempts.get(attempt_id)
            if pair is None:
                raise ExecutionDecisionConflict()
            operation, attempt = pair
            if (
                operation.session_id != session_id
                or operation.parent_turn_id != turn_id
                or attempt.cell_id != cell_id
                or attempt.starting_revision != expected_revision
            ):
                raise ExecutionDecisionConflict()
            if attempt.decision is not None:
                if attempt.decision != decision:
                    raise ExecutionDecisionConflict()
                return self._copy_operation(operation)
            if (
                operation.current_attempt_id != attempt_id
            ):
                raise ExecutionDecisionConflict()
            if attempt.state != "awaiting_approval":
                raise ExecutionDecisionConflict()
            attempt.decision = decision
            if decision == "cancel":
                operation.cancel_event.set()
                attempt.active = False
            attempt.decision_event.set()
        self._publish(operation)
        with self._lock:
            return self._copy_operation(operation)

    def approve(self, attempt_id: str, **kwargs) -> ExecutionOperation:
        return self.decide(attempt_id, "approve", **kwargs)

    def skip(self, attempt_id: str, **kwargs) -> ExecutionOperation:
        return self.decide(attempt_id, "skip", **kwargs)

    def cancel(self, attempt_id: str, **kwargs) -> ExecutionOperation:
        session_id = kwargs["session_id"]
        expected_revision = kwargs["expected_revision"]
        turn_id = kwargs.get("turn_id")
        cell_id = kwargs.get("cell_id")
        with self._lock:
            pair = self._attempts.get(attempt_id)
            if pair is None:
                raise ExecutionDecisionConflict()
            operation, attempt = pair
            turn_matches = (
                turn_id is None
                if operation.kind == "manual"
                else turn_id is not None and operation.parent_turn_id == turn_id
            )
            if (
                operation.session_id != session_id
                or operation.current_revision != expected_revision
                or operation.current_attempt_id != attempt_id
                or not turn_matches
                or attempt.cell_id != cell_id
            ):
                raise ExecutionDecisionConflict()
            if operation.cancel_event.is_set():
                return self._copy_operation(operation)
            if operation.state in TERMINAL_EXECUTION_STATES or attempt.state in {"completed", "failed", "skipped"}:
                raise ExecutionDecisionConflict()
            operation.cancel_event.set()
            attempt.active = False
            if attempt.state == "awaiting_approval" and attempt.decision is None:
                attempt.decision = "cancel"
            attempt.decision_event.set()
        correlation = self.kernel.execution_correlation()
        if correlation[1] == attempt_id:
            self.kernel.interrupt_correlated(*correlation)
        self._publish(operation)
        with self._lock:
            return self._copy_operation(operation)

    def cancel_parent(self, turn_id: str) -> None:
        with self._lock:
            operations = [item for item in self._operations.values() if item.parent_turn_id == turn_id and item.state not in TERMINAL_EXECUTION_STATES]
            attempt_ids = set()
            for operation in operations:
                operation.cancel_event.set()
                if operation.current_attempt_id:
                    pair = self._attempts.get(operation.current_attempt_id)
                    if pair:
                        attempt = pair[1]
                        attempt.active = False
                        attempt.state = "cancelled"
                        if attempt.decision is None:
                            attempt.decision = "cancel"
                        attempt.decision_event.set()
                        attempt_ids.add(attempt.attempt_id)
        correlation = self.kernel.execution_correlation()
        if correlation[1] in attempt_ids:
            self.kernel.interrupt_correlated(*correlation)

    def get(self, execution_id: str) -> ExecutionOperation:
        with self._lock:
            self._prune_history_locked()
            operation = self._operations.get(execution_id)
            if operation is None and execution_id in self._attempts:
                operation = self._attempts[execution_id][0]
            if operation is None:
                raise ExecutionNotFound(execution_id)
            return self._copy_operation(operation)

    def active_for_session(self, session_id: str) -> ExecutionOperation | None:
        with self._lock:
            self._prune_history_locked()
            active = [
                operation for operation in self._operations.values()
                if operation.session_id == session_id
                and operation.state not in TERMINAL_EXECUTION_STATES
            ]
            if not active:
                return None
            return self._copy_operation(max(active, key=lambda item: item.created_at))

    def kernel_status(self) -> dict[str, Any]:
        return {"kernelSessionId": self.kernel.kernel_session_id, "state": self.kernel.status, "executionAttemptId": self.kernel.busy_attempt_id}

    def interrupt(self, kernel_session_id: str, *, execution_attempt_id: str | None = None) -> dict[str, Any]:
        if not self.kernel.interrupt_correlated(
            kernel_session_id, execution_attempt_id,
        ):
            raise KernelSessionConflict()
        return self.kernel_status()

    def restart(self, kernel_session_id: str, *, execution_attempt_id: str | None = None) -> dict[str, Any]:
        matched_operation = None

        def invalidate() -> None:
            nonlocal matched_operation
            if execution_attempt_id is None:
                return
            with self._lock:
                pair = self._attempts.get(execution_attempt_id)
                if pair is None:
                    return
                operation, attempt = pair
                attempt.active = False
                attempt.state = "cancelled"
                operation.cancel_event.set()
                operation.state = "cancelled"
                operation.completed_at = datetime.now(timezone.utc)
                matched_operation = operation

        try:
            matched = self.kernel.restart_correlated(
                kernel_session_id, execution_attempt_id,
                on_matched=invalidate,
            )
        except Exception as error:
            if matched_operation is not None:
                with self._lock:
                    attempt = next(
                        item for item in matched_operation.attempts
                        if item.attempt_id == execution_attempt_id
                    )
                    attempt.state = "failed"
                    attempt.error = {
                        "code": "kernel_restart_failed",
                        "message": str(error), "details": {},
                    }
                    matched_operation.state = "failed"
                    matched_operation.error = copy.deepcopy(attempt.error)
                self._publish(matched_operation)
                with self._lock:
                    self._prune_history_locked()
            raise
        if not matched:
            raise KernelSessionConflict()
        if matched_operation is not None:
            self._publish(matched_operation)
            with self._lock:
                self._prune_history_locked()
        return self.kernel_status()

    def shutdown(self) -> None:
        self.kernel.shutdown()

    def _ensure_kernel_session(self, notebook_session_id: str) -> None:
        old_kernel = None
        with self._lock:
            if self._kernel_notebook_session_id not in {None, notebook_session_id}:
                old_kernel = self.kernel
                self.kernel = KernelSession(
                    startup_timeout=old_kernel.startup_timeout,
                    cell_timeout=old_kernel.cell_timeout,
                    recovery_timeout=old_kernel.recovery_timeout,
                    max_output_items=old_kernel.max_output_items,
                    max_output_bytes=old_kernel.max_output_bytes,
                )
            self._kernel_notebook_session_id = notebook_session_id
        if old_kernel is not None:
            old_kernel.shutdown()

    def _on_session_replaced(self, session_id: str, _revision: int) -> None:
        with self._lock:
            old = self._kernel_notebook_session_id
            if old is None or old == session_id:
                self._kernel_notebook_session_id = session_id
                return
            old_kernel = self.kernel
            replacement = KernelSession(
                startup_timeout=old_kernel.startup_timeout,
                cell_timeout=old_kernel.cell_timeout,
                recovery_timeout=old_kernel.recovery_timeout,
                max_output_items=old_kernel.max_output_items,
                max_output_bytes=old_kernel.max_output_bytes,
            )
            self.kernel = replacement
            self._kernel_notebook_session_id = session_id
        old_kernel.shutdown()

    def _set_operation_state(self, operation: ExecutionOperation, state: str) -> None:
        with self._lock:
            if operation.state not in TERMINAL_EXECUTION_STATES:
                operation.state = state
        self._publish(operation)

    def _finish(self, operation: ExecutionOperation, state: str) -> None:
        with self._lock:
            operation.state = state
            operation.completed_at = datetime.now(timezone.utc)
            self._prune_history_locked()
        self._publish(operation)

    def _fail(self, operation: ExecutionOperation, error: Exception) -> None:
        with self._lock:
            operation.state = "failed"
            operation.completed_at = datetime.now(timezone.utc)
            if operation.error is not None:
                pass
            elif isinstance(error, NotebookDomainError):
                operation.error = {"code": error.code, "message": error.message, "details": error.details}
            else:
                operation.error = {"code": "execution_failed", "message": str(error), "details": {}}
            self._prune_history_locked()
        self._publish(operation)

    def _publish(self, operation: ExecutionOperation) -> None:
        with self._lock:
            payload = serialize_operation_summary(operation)
        self.events.publish("execution.updated", payload)

    def _bound_operation_outputs_locked(
        self, operation: ExecutionOperation,
    ) -> None:
        retained: list[CellExecutionAttempt] = []
        for attempt in operation.attempts:
            if attempt.outputs:
                retained.append(attempt)
        for attempt in retained:
            if self._operation_history_size(operation) <= self.max_history_bytes:
                break
            attempt.outputs = []
            attempt.outputs_truncated = True

    @staticmethod
    def _operation_history_size(operation: ExecutionOperation) -> int:
        return len(json.dumps(
            serialize_operation(operation), default=str, separators=(",", ":"),
        ).encode())

    def _prune_history_locked(self) -> None:
        now = datetime.now(timezone.utc)
        terminal = sorted(
            (
                item for item in self._operations.values()
                if item.state in TERMINAL_EXECUTION_STATES
                and item.completed_at is not None
            ),
            key=lambda item: item.completed_at or item.created_at,
        )
        expired = {
            item.operation_id for item in terminal
            if (now - (item.completed_at or item.created_at)).total_seconds()
            > self.terminal_retention_seconds
        }
        for item in terminal:
            self._bound_operation_outputs_locked(item)
        total_bytes = sum(self._operation_history_size(item) for item in terminal)
        while terminal and (
            len(terminal) > self.max_terminal_operations
            or expired
            or total_bytes > self.max_history_bytes
        ):
            victim = next(
                (item for item in terminal if item.operation_id in expired),
                terminal[0],
            )
            terminal.remove(victim)
            expired.discard(victim.operation_id)
            total_bytes -= self._operation_history_size(victim)
            self._operations.pop(victim.operation_id, None)
            for attempt in victim.attempts:
                self._attempts.pop(attempt.attempt_id, None)

    @staticmethod
    def _copy_operation(operation: ExecutionOperation) -> ExecutionOperation:
        result = copy.copy(operation)
        result.attempts = []
        for item in operation.attempts:
            attempt = copy.copy(item)
            attempt.outputs = copy.deepcopy(item.outputs)
            attempt.error = copy.deepcopy(item.error)
            result.attempts.append(attempt)
        result.error = copy.deepcopy(operation.error)
        return result


def serialize_operation(operation: ExecutionOperation) -> dict[str, Any]:
    return {
        "operationId": operation.operation_id,
        "sessionId": operation.session_id,
        "baseRevision": operation.base_revision,
        "currentDocumentRevision": operation.current_revision,
        "kind": operation.kind,
        "parentTurnId": operation.parent_turn_id,
        "state": operation.state,
        "currentExecutionAttemptId": operation.current_attempt_id,
        "attempts": [
            {
                "executionAttemptId": item.attempt_id,
                "cellId": item.cell_id,
                "cellIndex": item.cell_index,
                "sourcePreview": item.source_preview,
                "state": item.state,
                "risk": {"level": item.risk.level, "reasons": list(item.risk.reasons), "matchedPatterns": list(item.risk.matched_patterns)},
                "decision": item.decision,
                "outputs": copy.deepcopy(item.outputs),
                "outputsTruncated": item.outputs_truncated,
                "executionCount": item.execution_count,
                "error": copy.deepcopy(item.error),
            }
            for item in operation.attempts
        ],
        "error": copy.deepcopy(operation.error),
        "createdAt": operation.created_at.isoformat(),
        "completedAt": operation.completed_at.isoformat() if operation.completed_at else None,
    }


def serialize_operation_summary(operation: ExecutionOperation) -> dict[str, Any]:
    return {
        "operationId": operation.operation_id,
        "sessionId": operation.session_id,
        "baseRevision": operation.base_revision,
        "currentDocumentRevision": operation.current_revision,
        "kind": operation.kind,
        "parentTurnId": operation.parent_turn_id,
        "state": operation.state,
        "currentExecutionAttemptId": operation.current_attempt_id,
        "attempts": [
            {
                "executionAttemptId": item.attempt_id,
                "cellId": item.cell_id,
                "cellIndex": item.cell_index,
                "sourcePreview": item.source_preview,
                "state": item.state,
                "risk": {
                    "level": item.risk.level,
                    "reasons": list(item.risk.reasons),
                    "matchedPatterns": list(item.risk.matched_patterns),
                },
                "decision": item.decision,
                "outputsTruncated": item.outputs_truncated,
                "executionCount": item.execution_count,
                "error": copy.deepcopy(item.error),
            }
            for item in operation.attempts
        ],
        "error": copy.deepcopy(operation.error),
        "createdAt": operation.created_at.isoformat(),
        "completedAt": (
            operation.completed_at.isoformat() if operation.completed_at else None
        ),
    }
