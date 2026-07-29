from __future__ import annotations

import copy
import hashlib
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from threading import Event, RLock, Thread, current_thread
from typing import Any, Sequence
from uuid import uuid4

from ..agent_workspace.models import (
    AgentAdapter, AgentCancelled, WorkspaceBoundaryError, WorkspaceCleanupError,
)
from ..agent_workspace.workspace_auditor import WorkspaceAuditor
from ..agent_workspace.workspace_builder import AgentWorkspaceBuilder
from ..boundary_validation.validator import BoundaryValidator, CandidateCellSourceChange
from ..notebook_document.models import (
    CellNotFound, MutationConflict, NotebookDomainError, RevisionConflict,
    SessionConflict,
)
from ..notebook_document.service import NotebookDocumentService
from .operations import (
    ACCEPTED, APPLIED_STATES, PENDING, REJECTED, TurnOperation, build_operations,
    compose, is_stale, with_state,
)
from ..turn_scope.models import FrozenTurnScope
from ..turn_scope.service import TurnScopeService
from ..kernel_execution.service import KernelExecutionService
from ..kernel_execution.models import ExecutionTimedOut
from ..session_events.service import SessionEventService


logger = logging.getLogger(__name__)


TERMINAL_STATES = {"completed", "failed", "cancelled", "validation_incomplete"}
MAX_TERMINAL_TURNS = 50
MAX_TURN_HISTORY_BYTES = 2 * 1024 * 1024
# Turns still holding unreviewed operations are kept past the ordinary history
# limits, so a diff on screen always has a live ledger behind it. Bounded, so
# an abandoned review backlog cannot pin history indefinitely; past the ceiling
# the oldest is force-settled to accepted, which discards review state only and
# never changes notebook content.
MAX_PENDING_REVIEW_TURNS = 10
# UI "mode" (edit/plan) maps to a Claude CLI permission mode.
PERMISSION_MODE_BY_MODE = {"edit": "acceptEdits", "plan": "plan"}


class AgentTurnNotFound(NotebookDomainError):
    code = "agent_turn_not_found"
    message = "Agent turn was not found"
    status_code = 404

    def __init__(self, turn_id: str) -> None:
        super().__init__(turnId=turn_id)


class UndoConflict(NotebookDomainError):
    code = "undo_conflict"
    message = "Agent turn can no longer be undone"
    status_code = 409


class RevertConflict(NotebookDomainError):
    code = "revert_conflict"
    message = "Cell source no longer matches the applied agent change"
    status_code = 409


class OperationNotFound(NotebookDomainError):
    code = "turn_operation_not_found"
    message = "Agent turn operation was not found"
    status_code = 404

    def __init__(self, operation_id: str) -> None:
        super().__init__(operationId=operation_id)


class OperationConflict(NotebookDomainError):
    code = "operation_conflict"
    message = "Cell no longer matches this turn's recorded changes"
    status_code = 409


class AgentTurnServiceShuttingDown(NotebookDomainError):
    code = "agent_turn_service_shutting_down"
    message = "Agent turn service is shutting down"
    status_code = 503


@dataclass
class AgentTurn:
    turn_id: str
    session_id: str
    base_revision: int
    prompt: str
    model: str = "default"
    mode: str = "edit"
    editable_cell_ids: tuple[str, ...] = ()
    context_cell_ids: tuple[str, ...] = ()
    state: str = "created"
    attempts: int = 0
    final_output: str = ""
    changes: tuple[CandidateCellSourceChange, ...] = ()
    operations: tuple[TurnOperation, ...] = ()
    applied_revision: int | None = None
    execution_operation_id: str | None = None
    checkpoint: dict[str, Any] | None = None
    error: dict[str, Any] | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    completed_at: datetime | None = None
    accepted_cancel_revision: int | None = None
    accepted_cancel_lineage_revision: int | None = None
    cancel_event: Event = field(default_factory=Event, repr=False)


class AgentTurnService:
    def __init__(
        self, *, documents: NotebookDocumentService, scopes: TurnScopeService,
        adapter: AgentAdapter, builder: AgentWorkspaceBuilder | None = None,
        auditor: WorkspaceAuditor | None = None,
        validator: BoundaryValidator | None = None, timeout: float = 600,
        executions: KernelExecutionService | None = None,
        events: SessionEventService | None = None,
    ) -> None:
        self.documents = documents
        self.scopes = scopes
        self.adapter = adapter
        self.builder = builder or AgentWorkspaceBuilder()
        self.auditor = auditor or WorkspaceAuditor()
        self.validator = validator or BoundaryValidator()
        self.timeout = timeout
        self.executions = executions
        self.events = events
        self._lock = RLock()
        self._turns: dict[str, AgentTurn] = {}
        self._latest_applied_turn_id: str | None = None
        self._workers: dict[Thread, str] = {}
        self._shutting_down = False
        self.documents.register_session_replacement_listener(
            self._on_session_replaced
        )

    def start(
        self, *, prompt: str, session_id: str, expected_revision: int,
        model: str = "default", mode: str = "edit", background: bool = True,
    ) -> AgentTurn:
        turn_id = uuid4().hex
        with self._lock:
            if self._shutting_down:
                raise AgentTurnServiceShuttingDown()
            try:
                lease = self.documents.coordinator.acquire(
                    operation_type="agent_turn", operation_id=turn_id
                )
            except MutationConflict as error:
                error.details["currentDocumentRevision"] = self.documents.get_snapshot().revision
                raise
            scope: FrozenTurnScope | None = None
            try:
                scope = self.scopes.freeze(
                    turn_id=turn_id, session_id=session_id, revision=expected_revision,
                    prompt=prompt, lease=lease,
                )
                snapshot = self.documents.get_snapshot()
                turn = AgentTurn(
                    turn_id=turn_id, session_id=session_id,
                    base_revision=expected_revision, prompt=prompt,
                    model=model, mode=mode,
                    editable_cell_ids=scope.editable_cell_ids,
                    context_cell_ids=scope.context_cell_ids,
                    checkpoint=copy.deepcopy(snapshot.notebook),
                )
                self._turns[turn_id] = turn
            except Exception:
                try:
                    if scope is not None:
                        self.scopes.expire(scope, "failed")
                finally:
                    self.documents.coordinator.release(lease)
                raise
            if background:
                worker = Thread(
                    target=self._run_guarded, args=(turn, scope, lease, snapshot),
                    name=f"agent-turn-{turn_id[:8]}", daemon=True,
                )
                self._workers[worker] = turn_id
                worker.start()
        if not background:
            self._run_guarded(turn, scope, lease, snapshot)
        return self.get(turn_id)

    def get(self, turn_id: str) -> AgentTurn:
        with self._lock:
            try:
                result = copy.copy(self._turns[turn_id])
                result.checkpoint = copy.deepcopy(result.checkpoint)
                result.error = copy.deepcopy(result.error)
                return result
            except KeyError as error:
                raise AgentTurnNotFound(turn_id) from error

    def active_for_session(self, session_id: str) -> AgentTurn | None:
        with self._lock:
            active = [
                turn for turn in self._turns.values()
                if turn.session_id == session_id and turn.state not in TERMINAL_STATES
            ]
            if not active:
                return None
            turn = max(active, key=lambda item: item.created_at)
            result = copy.copy(turn)
            result.checkpoint = copy.deepcopy(result.checkpoint)
            result.error = copy.deepcopy(result.error)
            return result

    def history_for_session(
        self, session_id: str, *, limit: int = 50,
    ) -> list[AgentTurn]:
        with self._lock:
            turns = sorted(
                (turn for turn in self._turns.values() if turn.session_id == session_id),
                key=lambda item: item.created_at, reverse=True,
            )[:limit]
            return [self.get(turn.turn_id) for turn in turns]

    def is_undo_eligible(self, turn: AgentTurn) -> bool:
        """Whether the turn's checkpoint may still be restored.

        Reads the snapshot before taking ``self._lock``: the document service
        calls back into this service under its own lock when a session is
        replaced, so acquiring the document lock while holding this one would
        invert the lock order.

        The stored turn is re-read under the lock rather than trusting the
        caller's copy, because callers routinely pass a copy taken before a
        concurrent mutation and this method has a destructive side effect.
        """
        try:
            snapshot = self.documents.get_snapshot()
        except NotebookDomainError:
            return False
        with self._lock:
            stored = self._turns.get(turn.turn_id)
            if stored is None or self._latest_applied_turn_id != turn.turn_id:
                return False
            if (
                stored.applied_revision is None
                or stored.session_id != snapshot.session_id
            ):
                self._invalidate_checkpoint_locked(stored)
                return False
            if stored.applied_revision == snapshot.revision:
                return True
            # The document is ahead of the revision recorded on the turn. That is
            # safe only while this turn itself is the author: downstream
            # execution and per-operation rejection both commit before the turn's
            # bookkeeping catches up, and a status poll landing in that window
            # must not destroy a checkpoint that is still valid.
            if self._owned_by_turn(snapshot.last_mutation_owner, turn.turn_id):
                return True
            self._invalidate_checkpoint_locked(stored)
            return False

    @staticmethod
    def _owned_by_turn(owner: str | None, turn_id: str) -> bool:
        """Whether a document mutation belongs to the turn's own lineage.

        Excludes ``undo:`` — restoring a checkpoint deliberately ends it.
        """
        if not owner:
            return False
        return owner == turn_id or owner.startswith(
            (f"reject:{turn_id}", f"revert:{turn_id}")
        )

    def _invalidate_checkpoint_locked(self, turn: AgentTurn) -> None:
        """Drop a checkpoint that can no longer be restored.

        Named for what it does. It used to be an unnamed side effect inside
        ``is_undo_eligible``, which made a query destructive and hid the race
        the owner check above now closes.
        """
        if self._latest_applied_turn_id == turn.turn_id:
            self._latest_applied_turn_id = None
        turn.checkpoint = None

    def cancel(
        self, turn_id: str, *, session_id: str, expected_revision: int,
    ) -> AgentTurn:
        with self._lock:
            turn = self._turns.get(turn_id)
            if turn is None:
                raise AgentTurnNotFound(turn_id)
            snapshot = self.documents.get_snapshot()
            if snapshot.session_id != session_id or turn.session_id != session_id:
                raise SessionConflict(snapshot.session_id)
            if turn.accepted_cancel_revision is not None:
                if (
                    expected_revision != turn.accepted_cancel_revision
                    or snapshot.revision != turn.accepted_cancel_lineage_revision
                ):
                    raise RevisionConflict(snapshot.revision)
                return self.get(turn_id)
            correlated_revisions: set[int] = set()
            if turn.state in TERMINAL_STATES:
                if snapshot.revision == turn.base_revision:
                    correlated_revisions.add(turn.base_revision)
                if (
                    turn.applied_revision is not None
                    and snapshot.revision == turn.applied_revision
                ):
                    correlated_revisions.add(turn.applied_revision)
            else:
                correlated_revisions.add(turn.base_revision)
                if snapshot.last_mutation_owner == turn.turn_id:
                    correlated_revisions.add(snapshot.revision)
            if expected_revision not in correlated_revisions:
                raise RevisionConflict(snapshot.revision)
            if turn.state not in TERMINAL_STATES:
                turn.accepted_cancel_revision = expected_revision
                turn.accepted_cancel_lineage_revision = snapshot.revision
                turn.cancel_event.set()
                if self.executions is not None:
                    self.executions.cancel_parent(turn_id)
        return self.get(turn_id)

    def _run_guarded(self, turn: AgentTurn, scope: FrozenTurnScope, lease, frozen_snapshot) -> None:
        outcome = "failed"
        terminal_error: Exception | None = None
        try:
            outcome, terminal_error = self._run(turn, scope, lease, frozen_snapshot)
        except AgentCancelled as error:
            outcome = "cancelled"
            terminal_error = error
        except NotebookDomainError as error:
            terminal_error = error
        except Exception as error:  # keep worker failures observable
            terminal_error = error
        finally:
            self._set_state(turn, "cleaning_up")
            try:
                self.scopes.expire(scope, outcome)
            except Exception as error:
                outcome = "failed"
                terminal_error = error
            with self._lock:
                if turn.cancel_event.is_set() and outcome == "completed":
                    outcome = "cancelled"
                    terminal_error = AgentCancelled()
                try:
                    self.scopes.update_terminal_outcome(turn.turn_id, outcome)
                except Exception as error:
                    outcome = "failed"
                    terminal_error = error
                finally:
                    self.documents.coordinator.release(lease)
                self._finish_locked(turn, outcome, terminal_error)
                self._workers.pop(current_thread(), None)

    def _run(self, turn: AgentTurn, scope: FrozenTurnScope, lease, frozen_snapshot):
        correction = None
        last_violation: WorkspaceBoundaryError | None = None
        for attempt_number in range(1, 4):
            if turn.cancel_event.is_set():
                raise AgentCancelled()
            self._set_state(turn, "agent_running")
            workspace = self.builder.build(
                frozen_snapshot, scope, correction=correction
            )
            attempt_error: BaseException | None = None
            cleanup_error: WorkspaceCleanupError | None = None
            try:
                with self._lock:
                    turn.attempts = attempt_number
                result = self.adapter.run(
                    workspace, timeout=self.timeout, cancel_event=turn.cancel_event,
                    model=None if turn.model == "default" else turn.model,
                    permission_mode=PERMISSION_MODE_BY_MODE.get(turn.mode, "acceptEdits"),
                )
                with self._lock:
                    turn.final_output = result.final_output
                self._set_state(turn, "validating")
                candidates = self.auditor.collect(
                    workspace, auxiliary_paths=self.adapter.auxiliary_paths
                )
                changes = self.validator.validate(
                    snapshot=self.documents.get_snapshot(), scope=scope,
                    manifest=workspace.manifest, candidates=candidates,
                )
                last_violation = None
                break
            except WorkspaceBoundaryError as error:
                attempt_error = error
                last_violation = error
                correction = "; ".join(error.violations)
                if attempt_number == 3:
                    raise
            except BaseException as error:
                attempt_error = error
                raise
            finally:
                try:
                    self.builder.destroy(workspace)
                except WorkspaceCleanupError as error:
                    cleanup_error = error
                    logger.exception(
                        "Failed to remove agent workspace %s", workspace.root,
                    )
                    if attempt_error is None:
                        raise
                    attempt_error.add_note(str(error))
            if cleanup_error is not None and attempt_error is not None:
                # Do not retry from another workspace while sensitive data from
                # the failed attempt may still remain on disk.
                raise attempt_error
        if last_violation is not None:
            raise last_violation
        if not changes:
            with self._lock:
                if turn.cancel_event.is_set():
                    return "cancelled", AgentCancelled()
                else:
                    turn.changes = ()
                    return "completed", None
        self._begin_commit(turn)
        updated = self.documents.apply_source_changes_under_lease(
            changes={change.cell_id: change.next_source for change in changes},
            expected_revision=scope.notebook_revision, owner=turn.turn_id, lease=lease,
        )
        with self._lock:
            turn.changes = changes
            turn.operations = tuple(
                operation
                for change in changes
                for operation in build_operations(
                    turn_id=turn.turn_id, cell_id=change.cell_id,
                    previous_source=change.previous_source,
                    next_source=change.next_source,
                )
            )
            turn.applied_revision = updated.revision
            self._latest_applied_turn_id = turn.turn_id
            if turn.cancel_event.is_set():
                return "cancelled", AgentCancelled()
        if self.events is not None:
            self.events.publish("notebook.updated", {"sessionId": turn.session_id, "revision": updated.revision, "ownerId": turn.turn_id})
        # Downstream execution is triggered by, and starts from, the earliest
        # edited code cell. A turn that changes only Markdown (or other
        # non-code) cells performs no execution and completes: there is no code
        # to re-validate, so a title/prose edit must not run the notebook.
        cell_types = {cell["id"]: cell.get("cell_type") for cell in updated.notebook["cells"]}
        changed_code_cell_ids = {
            change.cell_id for change in changes
            if cell_types.get(change.cell_id) == "code"
        }
        if self.executions is not None and changed_code_cell_ids:
            execution = self.executions.create_downstream(
                parent_turn_id=turn.turn_id, session_id=turn.session_id,
                expected_revision=updated.revision,
                cancel_event=turn.cancel_event,
            )
            with self._lock:
                turn.execution_operation_id = execution.operation_id
            self._set_state(turn, "executing")
            execution = self.executions.run_downstream(
                execution.operation_id,
                changed_cell_ids=changed_code_cell_ids,
                lease=lease,
            )
            with self._lock:
                turn.applied_revision = execution.current_revision
            if execution.state == "validation_incomplete":
                return "validation_incomplete", None
            if execution.state == "cancelled":
                return "cancelled", AgentCancelled()
            if execution.state == "failed":
                return "failed", RuntimeError((execution.error or {}).get("message", "Downstream execution failed"))
            if execution.state == "timed_out":
                recovered = bool((execution.error or {}).get("details", {}).get("kernelRecovered"))
                return "failed", ExecutionTimedOut(recovered=recovered)
        return "completed", None

    def undo(self, turn_id: str, *, session_id: str, expected_revision: int):
        turn = self._require_undoable(turn_id)
        with self._lock:
            if self._latest_applied_turn_id != turn_id:
                raise UndoConflict()
        if turn.applied_revision != expected_revision or turn.session_id != session_id:
            raise UndoConflict()
        lease = self.documents.coordinator.acquire(
            operation_type="agent_undo", operation_id=turn_id
        )
        try:
            snapshot = self.documents.get_snapshot()
            self.documents.check_snapshot_preconditions(snapshot, session_id, expected_revision)
            restored = self.documents.restore_under_lease(
                notebook=turn.checkpoint or {}, expected_revision=expected_revision,
                owner=f"undo:{turn_id}", lease=lease,
            )
            with self._lock:
                self._latest_applied_turn_id = None
                stored = self._turns.get(turn_id)
                if stored is not None:
                    stored.checkpoint = None
                self._prune_history_locked()
            if self.events is not None:
                self.events.publish("notebook.updated", {"sessionId": restored.session_id, "revision": restored.revision, "ownerId": f"undo:{turn_id}"})
            return restored
        finally:
            self.documents.coordinator.release(lease)

    def stale_cell_ids(self, turn: AgentTurn) -> frozenset[str]:
        """Cells whose ledger no longer describes the document.

        Derived on read rather than stored. A manual edit reaches the document
        through a path with no ledger awareness, so a stored flag would only
        become true the next time someone attempted a reject — leaving
        live-looking controls on operations that are already dead.
        """
        if not turn.operations:
            return frozenset()
        try:
            snapshot = self.documents.get_snapshot()
        except NotebookDomainError:
            return frozenset()
        if snapshot.session_id != turn.session_id:
            return frozenset(item.cell_id for item in turn.operations)
        cells = {cell["id"]: cell for cell in snapshot.notebook["cells"]}
        sources = {change.cell_id: change for change in turn.changes}
        stale: set[str] = set()
        for cell_id in {item.cell_id for item in turn.operations}:
            change = sources.get(cell_id)
            cell = cells.get(cell_id)
            if change is None or cell is None:
                stale.add(cell_id)
                continue
            source = cell.get("source", "")
            source = "".join(source) if isinstance(source, list) else source
            if is_stale(
                current_source=source, previous_source=change.previous_source,
                next_source=change.next_source,
                operations=[
                    item for item in turn.operations if item.cell_id == cell_id
                ],
            ):
                stale.add(cell_id)
        return frozenset(stale)

    def accept_operations(
        self, turn_id: str, operation_ids: Sequence[str] | None, *, session_id: str,
    ) -> AgentTurn:
        """Mark operations reviewed. Never touches the document.

        Accept exists to settle review state for changes that are already
        applied, so it takes no lease and no expected revision: a stale revision
        cannot make it unsafe, and rejecting "I read this diff" with a 409 would
        be hostile for no gain.
        """
        with self._lock:
            turn = self._turns.get(turn_id)
            if turn is None:
                raise AgentTurnNotFound(turn_id)
            if turn.session_id != session_id:
                raise SessionConflict(turn.session_id)
            targets = self._select_operations_locked(turn, operation_ids)
            operations = turn.operations
            for target in targets:
                if target.state == REJECTED:
                    # Flipping a rejected hunk to accepted would silently
                    # re-apply content the user undid.
                    raise OperationConflict(operationId=target.operation_id)
                operations = with_state(operations, target.operation_id, ACCEPTED)
            turn.operations = operations
        self._publish_turn_updated(turn_id)
        return self.get(turn_id)

    def reject_operations(
        self, turn_id: str, operation_ids: Sequence[str] | None, *,
        session_id: str, expected_revision: int,
        conflict: type[NotebookDomainError] = OperationConflict,
    ):
        """Undo operations and commit the recomposed cells as one mutation."""
        lease = self.documents.coordinator.acquire(
            operation_type="agent_reject", operation_id=turn_id,
        )
        try:
            snapshot = self.documents.get_snapshot()
            self.documents.check_snapshot_preconditions(
                snapshot, session_id, expected_revision
            )
            with self._lock:
                turn = self._turns.get(turn_id)
                if turn is None:
                    raise AgentTurnNotFound(turn_id)
                targets = self._select_operations_locked(turn, operation_ids)
                operations = turn.operations
                for target in targets:
                    operations = with_state(
                        operations, target.operation_id, REJECTED
                    )
                sources = {change.cell_id: change for change in turn.changes}
                changes = self._recompose_locked(
                    snapshot, sources, turn.operations, operations,
                    {target.cell_id for target in targets}, conflict,
                )
            updated = self.documents.apply_source_changes_under_lease(
                changes=changes, expected_revision=expected_revision,
                owner=f"reject:{turn_id}", lease=lease,
            )
            with self._lock:
                stored = self._turns.get(turn_id)
                if stored is not None:
                    stored.operations = operations
                    # Keep the checkpoint alive across the turn's own review:
                    # undoing part of a turn is reviewing it, not unrelated work.
                    if self._latest_applied_turn_id == turn_id:
                        stored.applied_revision = updated.revision
            if self.events is not None:
                self.events.publish("notebook.updated", {"sessionId": updated.session_id, "revision": updated.revision, "ownerId": f"reject:{turn_id}"})
            self._publish_turn_updated(turn_id)
            return updated
        finally:
            self.documents.coordinator.release(lease)

    def _select_operations_locked(
        self, turn: AgentTurn, operation_ids: Sequence[str] | None,
    ) -> tuple[TurnOperation, ...]:
        """Resolve requested operation IDs, or every unsettled one when None."""
        if operation_ids is None:
            return tuple(
                item for item in turn.operations if item.state == PENDING
            )
        known = {item.operation_id: item for item in turn.operations}
        missing = [item for item in operation_ids if item not in known]
        if missing:
            raise OperationNotFound(missing[0])
        return tuple(known[item] for item in operation_ids)

    def _recompose_locked(
        self, snapshot, sources, current: tuple[TurnOperation, ...],
        updated: tuple[TurnOperation, ...], cell_ids: set[str],
        conflict: type[NotebookDomainError],
    ) -> dict[str, str]:
        cells = {cell["id"]: cell for cell in snapshot.notebook["cells"]}
        changes: dict[str, str] = {}
        for cell_id in sorted(cell_ids):
            change = sources.get(cell_id)
            cell = cells.get(cell_id)
            if change is None or cell is None:
                raise CellNotFound(cell_id)
            source = cell.get("source", "")
            source = "".join(source) if isinstance(source, list) else source
            for_cell = [item for item in current if item.cell_id == cell_id]
            # The guard is "this cell is exactly what the ledger says it should
            # be". A whole-cell hash against next_source cannot work here: after
            # one hunk is undone the cell no longer matches, which would make
            # every remaining hunk permanently unrejectable.
            if is_stale(
                current_source=source, previous_source=change.previous_source,
                next_source=change.next_source, operations=for_cell,
            ):
                raise conflict()
            composed = compose(
                previous_source=change.previous_source,
                next_source=change.next_source,
                operations=[item for item in updated if item.cell_id == cell_id],
            )
            if composed != source:
                changes[cell_id] = composed
        return changes

    def revert_cell(
        self, turn_id: str, cell_id: str, *, session_id: str,
        expected_revision: int,
    ):
        """Undo every outstanding operation for one cell.

        Retained as the cell-level entry point and now expressed on the ledger,
        so there is a single reject path rather than a parallel hash-guarded one.
        """
        turn = self._require_revertible(turn_id)
        if not any(item.cell_id == cell_id for item in turn.changes):
            raise CellNotFound(cell_id)
        operation_ids = [
            item.operation_id for item in turn.operations
            if item.cell_id == cell_id and item.state in APPLIED_STATES
        ]
        return self.reject_operations(
            turn_id, operation_ids, session_id=session_id,
            expected_revision=expected_revision, conflict=RevertConflict,
        )

    def _publish_turn_updated(self, turn_id: str) -> None:
        if self.events is None:
            return
        turn = self.get(turn_id)
        self.events.publish("turn.updated", {
            "turnId": turn.turn_id, "sessionId": turn.session_id,
            "state": turn.state,
            "revision": turn.applied_revision or turn.base_revision,
            "executionOperationId": turn.execution_operation_id,
        })

    def _require_undoable(self, turn_id: str) -> AgentTurn:
        turn = self.get(turn_id)
        if turn.applied_revision is None or turn.checkpoint is None:
            raise UndoConflict()
        return turn

    def _require_revertible(self, turn_id: str) -> AgentTurn:
        turn = self.get(turn_id)
        if turn.applied_revision is None or not turn.changes:
            raise RevertConflict()
        return turn

    def shutdown(self, *, timeout: float = 5.0) -> None:
        deadline = time.monotonic() + max(timeout, 0)
        with self._lock:
            self._shutting_down = True
            active = tuple(
                turn for turn in self._turns.values()
                if turn.state not in TERMINAL_STATES
            )
            workers = tuple(self._workers.items())
            for turn in active:
                turn.cancel_event.set()
        if self.executions is not None:
            for turn in active:
                self.executions.cancel_parent(turn.turn_id)
        adapter_shutdown = getattr(self.adapter, "shutdown", None)
        if callable(adapter_shutdown):
            adapter_shutdown()
        for worker, _turn_id in workers:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            worker.join(remaining)
        with self._lock:
            for worker, turn_id in workers:
                if worker.is_alive():
                    turn = self._turns.get(turn_id)
                    if turn is not None:
                        turn.error = {
                            "code": "shutdown_timeout",
                            "message": "Agent worker did not stop before shutdown deadline",
                            "details": {},
                        }

    def _on_session_replaced(
        self, _session_id: str | None, _revision: int,
    ) -> None:
        with self._lock:
            self._turns.clear()
            self._latest_applied_turn_id = None
            self._workers.clear()

    @staticmethod
    def _source_hash(source: str) -> str:
        return hashlib.sha256(source.encode()).hexdigest()

    def _set_state(self, turn: AgentTurn, state: str) -> None:
        with self._lock:
            if turn.state in TERMINAL_STATES:
                return
            turn.state = state
        if self.events is not None:
            self.events.publish("turn.updated", {
                "turnId": turn.turn_id, "sessionId": turn.session_id,
                "state": state,
                "revision": turn.applied_revision or turn.base_revision,
                "executionOperationId": turn.execution_operation_id,
            })

    def _begin_commit(self, turn: AgentTurn) -> None:
        """Linearize cancellation against the transition into source apply."""
        with self._lock:
            if turn.state in TERMINAL_STATES or turn.cancel_event.is_set():
                raise AgentCancelled()
            turn.state = "applying"

    def _finish(self, turn: AgentTurn, state: str, error: Exception | None = None) -> None:
        with self._lock:
            self._finish_locked(turn, state, error)

    def _finish_locked(
        self, turn: AgentTurn, state: str, error: Exception | None = None,
    ) -> None:
        turn.state = state
        turn.completed_at = datetime.now(timezone.utc)
        if error is not None:
            if isinstance(error, NotebookDomainError):
                turn.error = {
                    "code": error.code,
                    "message": error.message,
                    "details": error.details,
                }
            else:
                turn.error = {
                    "code": "internal_error",
                    "message": str(error),
                    "details": {},
                }
        if self.events is not None:
            self.events.publish("turn.updated", {
                "turnId": turn.turn_id, "sessionId": turn.session_id,
                "state": state,
                "revision": turn.applied_revision or turn.base_revision,
                "executionOperationId": turn.execution_operation_id,
                "error": copy.deepcopy(turn.error),
            })
        self._prune_history_locked()

    def _prune_history_locked(self) -> None:
        terminal = sorted(
            (item for item in self._turns.values() if item.state in TERMINAL_STATES),
            key=lambda item: item.completed_at or item.created_at,
            reverse=True,
        )
        for item in terminal:
            if item.turn_id != self._latest_applied_turn_id:
                item.checkpoint = None
        retained: set[str] = set()
        retained_bytes = 0
        protected = {self._latest_applied_turn_id} if self._latest_applied_turn_id else set()
        protected |= self._pending_review_turn_ids_locked(terminal)
        for item in terminal:
            if item.turn_id in protected:
                retained.add(item.turn_id)
                retained_bytes += self._history_size(item)
        for item in terminal:
            if item.turn_id in retained:
                continue
            size = self._history_size(item)
            if (
                len(retained) < MAX_TERMINAL_TURNS
                and retained_bytes + size <= MAX_TURN_HISTORY_BYTES
            ):
                retained.add(item.turn_id)
                retained_bytes += size
        for expired in terminal:
            if expired.turn_id in retained:
                continue
            self._turns.pop(expired.turn_id, None)
            if self._latest_applied_turn_id == expired.turn_id:
                self._latest_applied_turn_id = None

    def _pending_review_turn_ids_locked(
        self, terminal: Sequence[AgentTurn],
    ) -> set[str]:
        """Turn IDs to hold back from eviction because review is unfinished.

        Kept newest first, bounded by *both* a turn count and the same byte
        budget that governs history overall. The byte bound is the important
        one: protecting review state must not let a backlog of large unreviewed
        turns pin memory, so an oversized ledger is force-settled rather than
        retained. Anything not kept is settled to accepted and evicted normally
        — the safe direction, since the change stays in the notebook exactly as
        the turn applied it and only the record that nobody reviewed it is lost.
        """
        keep: set[str] = set()
        budget = 0
        latest = self._turns.get(self._latest_applied_turn_id or "")
        if latest is not None:
            budget += self._history_size(latest)
        for item in terminal:
            if not any(
                operation.state == PENDING for operation in item.operations
            ):
                continue
            size = self._history_size(item)
            if (
                len(keep) < MAX_PENDING_REVIEW_TURNS
                and budget + size <= MAX_TURN_HISTORY_BYTES
            ):
                keep.add(item.turn_id)
                budget += size
                continue
            self._force_settle_locked(item)
        return keep

    @staticmethod
    def _force_settle_locked(turn: AgentTurn) -> None:
        operations = turn.operations
        for operation in turn.operations:
            if operation.state == PENDING:
                operations = with_state(
                    operations, operation.operation_id, ACCEPTED
                )
        turn.operations = operations
        logger.info(
            "Auto-kept unreviewed agent changes for turn %s: review backlog "
            "exceeded the retained history budget", turn.turn_id,
        )

    @staticmethod
    def _history_size(turn: AgentTurn) -> int:
        values = [turn.prompt, turn.final_output]
        values.extend(turn.editable_cell_ids)
        values.extend(turn.context_cell_ids)
        for change in turn.changes:
            values.extend((change.cell_id, change.previous_source, change.next_source))
        if turn.error is not None:
            values.append(str(turn.error))
        # Operations are index ranges over sources already counted above, so
        # they add a fixed cost per hunk rather than duplicating any text.
        return (
            sum(len(value.encode("utf-8")) for value in values)
            + 64 * len(turn.operations)
            + 512
        )
