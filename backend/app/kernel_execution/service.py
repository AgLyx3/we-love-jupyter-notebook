from __future__ import annotations

import copy
import hashlib
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
    ExecutionOperation, KernelCellTimeout, KernelSessionConflict, StaleExecutionResult,
    TERMINAL_EXECUTION_STATES,
)
from .risky_cell_classifier import RiskyCellClassifier


class KernelExecutionService:
    def __init__(
        self, *, documents: NotebookDocumentService,
        events: SessionEventService | None = None,
        classifier: RiskyCellClassifier | None = None,
        kernel: KernelSession | None = None,
    ) -> None:
        self.documents = documents
        self.events = events or SessionEventService()
        self.classifier = classifier or RiskyCellClassifier()
        self.kernel = kernel or KernelSession()
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
        operation_id = uuid4().hex
        lease = self.documents.coordinator.acquire(operation_type="manual_execution", operation_id=operation_id)
        try:
            snapshot = self.documents.get_snapshot()
            self.documents.check_snapshot_preconditions(snapshot, session_id, expected_revision)
            operation = ExecutionOperation(operation_id, session_id, expected_revision, "manual", current_revision=expected_revision)
            with self._lock:
                self._operations[operation_id] = operation
            self._publish(operation)
        except Exception:
            self.documents.coordinator.release(lease)
            raise
        Thread(target=self._run_manual_guarded, args=(operation, lease, cell_id), daemon=True, name=f"execution-{operation_id[:8]}").start()
        return self.get(operation_id)

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
        self._publish(operation)
        return self.get(operation.operation_id)

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
        return self.get(operation.operation_id)

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
                source_hash, snapshot.revision, risk=self.classifier.classify(source),
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
                or operation.current_revision != expected_revision
                or operation.current_attempt_id != attempt_id
                or operation.parent_turn_id != turn_id
                or attempt.cell_id != cell_id
            ):
                raise ExecutionDecisionConflict()
            if attempt.decision is not None:
                if attempt.decision != decision:
                    raise ExecutionDecisionConflict()
                return self._copy_operation(operation)
            if attempt.state != "awaiting_approval":
                raise ExecutionDecisionConflict()
            attempt.decision = decision
            if decision == "cancel":
                operation.cancel_event.set()
                attempt.active = False
            attempt.decision_event.set()
        self._publish(operation)
        return self.get(operation.operation_id)

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
            if (
                operation.session_id != session_id
                or operation.current_revision != expected_revision
                or operation.current_attempt_id != attempt_id
                or operation.parent_turn_id != turn_id
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
        self.kernel.interrupt()
        self._publish(operation)
        return self.get(operation.operation_id)

    def cancel_parent(self, turn_id: str) -> None:
        with self._lock:
            operations = [item for item in self._operations.values() if item.parent_turn_id == turn_id and item.state not in TERMINAL_EXECUTION_STATES]
            for operation in operations:
                operation.cancel_event.set()
                if operation.current_attempt_id:
                    pair = self._attempts.get(operation.current_attempt_id)
                    if pair:
                        pair[1].active = False
                        pair[1].decision_event.set()
        if operations:
            self.kernel.interrupt()

    def get(self, execution_id: str) -> ExecutionOperation:
        with self._lock:
            operation = self._operations.get(execution_id)
            if operation is None and execution_id in self._attempts:
                operation = self._attempts[execution_id][0]
            if operation is None:
                raise ExecutionNotFound(execution_id)
            return self._copy_operation(operation)

    def kernel_status(self) -> dict[str, Any]:
        return {"kernelSessionId": self.kernel.kernel_session_id, "state": self.kernel.status, "executionAttemptId": self.kernel.busy_attempt_id}

    def interrupt(self, kernel_session_id: str, *, execution_attempt_id: str | None = None) -> dict[str, Any]:
        if not self.kernel.interrupt_correlated(
            kernel_session_id, execution_attempt_id,
        ):
            raise KernelSessionConflict()
        return self.kernel_status()

    def restart(self, kernel_session_id: str, *, execution_attempt_id: str | None = None) -> dict[str, Any]:
        if not self.kernel.restart_correlated(
            kernel_session_id, execution_attempt_id,
        ):
            raise KernelSessionConflict()
        return self.kernel_status()

    def shutdown(self) -> None:
        self.kernel.shutdown()

    def _ensure_kernel_session(self, notebook_session_id: str) -> None:
        with self._lock:
            if self._kernel_notebook_session_id not in {None, notebook_session_id}:
                self.kernel.shutdown()
                self.kernel = KernelSession(
                    startup_timeout=self.kernel.startup_timeout,
                    cell_timeout=self.kernel.cell_timeout,
                    recovery_timeout=self.kernel.recovery_timeout,
                )
            self._kernel_notebook_session_id = notebook_session_id

    def _on_session_replaced(self, session_id: str, _revision: int) -> None:
        with self._lock:
            old = self._kernel_notebook_session_id
            self._kernel_notebook_session_id = session_id
        if old is not None and old != session_id:
            self.kernel.shutdown()
            self.kernel = KernelSession(
                startup_timeout=self.kernel.startup_timeout,
                cell_timeout=self.kernel.cell_timeout,
                recovery_timeout=self.kernel.recovery_timeout,
            )

    def _set_operation_state(self, operation: ExecutionOperation, state: str) -> None:
        with self._lock:
            if operation.state not in TERMINAL_EXECUTION_STATES:
                operation.state = state
        self._publish(operation)

    def _finish(self, operation: ExecutionOperation, state: str) -> None:
        with self._lock:
            operation.state = state
            operation.completed_at = datetime.now(timezone.utc)
        self._publish(operation)

    def _fail(self, operation: ExecutionOperation, error: Exception) -> None:
        with self._lock:
            operation.state = "failed"
            operation.completed_at = datetime.now(timezone.utc)
            if isinstance(error, NotebookDomainError):
                operation.error = {"code": error.code, "message": error.message, "details": error.details}
            else:
                operation.error = {"code": "execution_failed", "message": str(error), "details": {}}
        self._publish(operation)

    def _publish(self, operation: ExecutionOperation) -> None:
        snapshot = self._copy_operation(operation)
        self.events.publish("execution.updated", serialize_operation(snapshot))

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
                "state": item.state,
                "risk": {"level": item.risk.level, "reasons": list(item.risk.reasons), "matchedPatterns": list(item.risk.matched_patterns)},
                "decision": item.decision,
                "outputs": copy.deepcopy(item.outputs),
                "executionCount": item.execution_count,
                "error": copy.deepcopy(item.error),
            }
            for item in operation.attempts
        ],
        "error": copy.deepcopy(operation.error),
        "createdAt": operation.created_at.isoformat(),
        "completedAt": operation.completed_at.isoformat() if operation.completed_at else None,
    }
