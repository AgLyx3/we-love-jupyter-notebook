from __future__ import annotations

import copy
import hashlib
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from threading import Event, RLock, Thread, current_thread
from typing import Any
from uuid import uuid4

from ..agent_workspace.models import (
    AgentAdapter, AgentCancelled, WorkspaceBoundaryError, WorkspaceCleanupError,
)
from ..agent_workspace.workspace_auditor import WorkspaceAuditor
from ..agent_workspace.workspace_builder import AgentWorkspaceBuilder
from ..boundary_validation.validator import BoundaryValidator, CandidateCellSourceChange
from ..boundary_validation.structural_validator import StructuralOp, derive_structural_plan
from ..notebook_document.models import (
    CellNotFound, MutationConflict, NotebookDomainError, RevisionConflict,
    SessionConflict,
)
from ..notebook_document.service import NotebookDocumentService
from ..turn_scope.models import FrozenTurnScope
from ..turn_scope.service import TurnScopeService
from ..kernel_execution.service import KernelExecutionService
from ..kernel_execution.models import ExecutionTimedOut
from ..session_events.service import SessionEventService


logger = logging.getLogger(__name__)


TERMINAL_STATES = {"completed", "failed", "cancelled", "validation_incomplete"}
MAX_TERMINAL_TURNS = 50
MAX_TURN_HISTORY_BYTES = 2 * 1024 * 1024
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
    write_scope: str = "blocking"
    editable_cell_ids: tuple[str, ...] = ()
    context_cell_ids: tuple[str, ...] = ()
    state: str = "created"
    attempts: int = 0
    final_output: str = ""
    changes: tuple[CandidateCellSourceChange, ...] = ()
    structural_ops: tuple[StructuralOp, ...] = ()
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
        model: str = "default", mode: str = "edit",
        write_scope: str = "blocking", background: bool = True,
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
                    model=model, mode=mode, write_scope=write_scope,
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
        with self._lock:
            if self._latest_applied_turn_id != turn.turn_id:
                return False
        try:
            snapshot = self.documents.get_snapshot()
        except NotebookDomainError:
            return False
        eligible = (
            turn.applied_revision is not None
            and turn.session_id == snapshot.session_id
            and turn.applied_revision == snapshot.revision
        )
        if not eligible:
            with self._lock:
                if self._latest_applied_turn_id == turn.turn_id:
                    self._latest_applied_turn_id = None
                    stored = self._turns.get(turn.turn_id)
                    if stored is not None:
                        stored.checkpoint = None
        return eligible

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
            # A turn that applied no changes (e.g. the agent asked a clarifying
            # question) leaves the notebook revision unchanged, so keep the
            # editable/context selection for the follow-up turn instead of forcing
            # the user to re-scope. Once an edit is applied, scope expires as usual.
            preserve_scope = turn.applied_revision is None and bool(
                scope.editable_cell_ids or scope.context_cell_ids
            )
            try:
                self.scopes.expire(scope, outcome, preserve_selection=preserve_scope)
            except Exception as error:
                outcome = "failed"
                terminal_error = error
            with self._lock:
                # R16: a Trusted turn whose atomic structural apply already
                # committed stays "completed" (and undoable) even if a cancel
                # lost the race — the whole-notebook change happened, so labeling
                # it "cancelled" would mislead. Blocking keeps its prior behavior.
                committed_trusted = (
                    turn.write_scope == "trusted" and turn.applied_revision is not None
                )
                if (
                    turn.cancel_event.is_set()
                    and outcome == "completed"
                    and not committed_trusted
                ):
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
        if turn.write_scope == "trusted":
            return self._run_trusted(turn, scope, lease, frozen_snapshot)
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

    def _run_trusted(self, turn: AgentTurn, scope: FrozenTurnScope, lease, frozen_snapshot):
        """Trusted turn: whole-notebook structural editing.

        Bounded structural-format retry (R3), no scope retry; applies structure
        only (no auto-execution, R4); captures the agent's attempted structure on
        terminal failure before the workspace is destroyed (R2).
        """
        correction = None
        last_violation: WorkspaceBoundaryError | None = None
        plan = None
        for attempt_number in range(1, 4):
            if turn.cancel_event.is_set():
                raise AgentCancelled()
            self._set_state(turn, "agent_running")
            workspace = self.builder.build(
                frozen_snapshot, scope, write_scope="trusted", correction=correction
            )
            attempt_error: BaseException | None = None
            cleanup_error: WorkspaceCleanupError | None = None
            try:
                with self._lock:
                    turn.attempts = attempt_number
                result = self.adapter.run(
                    workspace, timeout=self.timeout, cancel_event=turn.cancel_event,
                    model=None if turn.model == "default" else turn.model,
                    permission_mode="acceptEdits",
                )
                with self._lock:
                    turn.final_output = result.final_output
                self._set_state(turn, "validating")
                entries = self.auditor.collect_trusted(workspace)
                plan = derive_structural_plan(manifest=workspace.manifest, entries=entries)
                last_violation = None
                break
            except WorkspaceBoundaryError as error:
                attempt_error = error
                last_violation = error
                correction = "; ".join(error.violations)
                if attempt_number == 3:
                    self._attach_trusted_salvage(error, workspace)
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
                        "Failed to remove trusted workspace %s", workspace.root,
                    )
                    if attempt_error is None:
                        raise
                    attempt_error.add_note(str(error))
            if cleanup_error is not None and attempt_error is not None:
                raise attempt_error
        if last_violation is not None:
            raise last_violation
        if plan is None or plan.is_noop:
            with self._lock:
                if turn.cancel_event.is_set():
                    return "cancelled", AgentCancelled()
                turn.changes = ()
                turn.structural_ops = ()
                return "completed", None
        self._begin_commit(turn)
        next_cells = [
            {"origin_id": cell.origin_id, "cell_type": cell.cell_type, "source": cell.source}
            for cell in plan.next_cells
        ]
        updated = self.documents.apply_structural_changes_under_lease(
            next_cells=next_cells, expected_session_id=scope.session_id,
            expected_revision=scope.notebook_revision, owner=turn.turn_id, lease=lease,
        )
        # Surface per-cell source diffs for edited (surviving) cells so the inline
        # diff renders. These are display-only: per-cell revert is not offered on
        # Trusted turns (whole-turn undo only), enforced in revert_cell and the UI.
        # NOTE: workspace.manifest is in-memory; the on-disk workspace was already
        # destroyed in the loop's finally, but the frozen original sources live on
        # the manifest object, so reading them here is intentional and safe.
        frozen_source = {cell.cell_id: cell.original_source for cell in workspace.manifest.cells}
        edits = tuple(
            CandidateCellSourceChange(
                cell_id=cell.origin_id,
                previous_source=frozen_source.get(cell.origin_id, ""),
                next_source=cell.source,
            )
            for cell in plan.next_cells
            if cell.origin_id is not None and cell.source != frozen_source.get(cell.origin_id, "")
        )
        with self._lock:
            turn.structural_ops = tuple(plan.ops)
            turn.changes = edits
            turn.applied_revision = updated.revision
            self._latest_applied_turn_id = turn.turn_id
        if self.events is not None:
            self.events.publish("notebook.updated", {"sessionId": turn.session_id, "revision": updated.revision, "ownerId": turn.turn_id})
        # R4: Trusted structural turns apply structure only; the user runs cells
        # after reviewing the diff. No automatic downstream execution.
        return "completed", None

    @staticmethod
    def _attach_trusted_salvage(error: WorkspaceBoundaryError, workspace) -> None:
        """Best-effort: attach the agent's attempted structure.json to the error
        before the workspace is destroyed, so the UI can surface it (R2)."""
        try:
            text = (workspace.root / "structure.json").read_text(encoding="utf-8")
        except OSError:
            return
        error.details["attemptedStructure"] = text[:16384]

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

    def revert_cell(
        self, turn_id: str, cell_id: str, *, session_id: str,
        expected_revision: int,
    ):
        turn = self._require_revertible(turn_id)
        # Trusted turns are whole-turn undo only; per-cell source diffs are shown
        # for review but per-cell revert is not offered (R20).
        if turn.write_scope == "trusted":
            raise RevertConflict()
        change = next((item for item in turn.changes if item.cell_id == cell_id), None)
        if change is None:
            raise CellNotFound(cell_id)
        lease = self.documents.coordinator.acquire(
            operation_type="agent_revert", operation_id=f"{turn_id}:{cell_id}"
        )
        try:
            snapshot = self.documents.get_snapshot()
            self.documents.check_snapshot_preconditions(snapshot, session_id, expected_revision)
            cell = next((item for item in snapshot.notebook["cells"] if item["id"] == cell_id), None)
            if cell is None:
                raise CellNotFound(cell_id)
            source = cell.get("source", "")
            source = "".join(source) if isinstance(source, list) else source
            if self._source_hash(source) != self._source_hash(change.next_source):
                raise RevertConflict()
            updated = self.documents.apply_source_changes_under_lease(
                changes={cell_id: change.previous_source},
                expected_revision=expected_revision,
                owner=f"revert:{turn_id}", lease=lease,
            )
            if self.events is not None:
                self.events.publish("notebook.updated", {"sessionId": updated.session_id, "revision": updated.revision, "ownerId": f"revert:{turn_id}"})
            return updated
        finally:
            self.documents.coordinator.release(lease)

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

    @staticmethod
    def _history_size(turn: AgentTurn) -> int:
        values = [turn.prompt, turn.final_output]
        values.extend(turn.editable_cell_ids)
        values.extend(turn.context_cell_ids)
        for change in turn.changes:
            values.extend((change.cell_id, change.previous_source, change.next_source))
        for op in turn.structural_ops:
            values.extend((op.op, op.cell_id or "", str(op.detail)))
        if turn.error is not None:
            values.append(str(turn.error))
        return sum(len(value.encode("utf-8")) for value in values) + 512
