from __future__ import annotations

import copy
import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from threading import Event, RLock, Thread
from typing import Any
from uuid import uuid4

from ..agent_workspace.models import (
    AgentAdapter, AgentCancelled, WorkspaceBoundaryError,
)
from ..agent_workspace.workspace_auditor import WorkspaceAuditor
from ..agent_workspace.workspace_builder import AgentWorkspaceBuilder
from ..boundary_validation.validator import BoundaryValidator, CandidateCellSourceChange
from ..notebook_document.models import (
    CellNotFound, MutationConflict, NotebookDomainError, RevisionConflict,
)
from ..notebook_document.service import NotebookDocumentService
from ..turn_scope.models import FrozenTurnScope
from ..turn_scope.service import TurnScopeService


TERMINAL_STATES = {"completed", "failed", "cancelled", "validation_incomplete"}


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


@dataclass
class AgentTurn:
    turn_id: str
    session_id: str
    base_revision: int
    prompt: str
    state: str = "created"
    attempts: int = 0
    final_output: str = ""
    changes: tuple[CandidateCellSourceChange, ...] = ()
    applied_revision: int | None = None
    checkpoint: dict[str, Any] | None = None
    error: dict[str, Any] | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    completed_at: datetime | None = None
    cancel_event: Event = field(default_factory=Event, repr=False)


class AgentTurnService:
    def __init__(
        self, *, documents: NotebookDocumentService, scopes: TurnScopeService,
        adapter: AgentAdapter, builder: AgentWorkspaceBuilder | None = None,
        auditor: WorkspaceAuditor | None = None,
        validator: BoundaryValidator | None = None, timeout: float = 600,
    ) -> None:
        self.documents = documents
        self.scopes = scopes
        self.adapter = adapter
        self.builder = builder or AgentWorkspaceBuilder()
        self.auditor = auditor or WorkspaceAuditor()
        self.validator = validator or BoundaryValidator()
        self.timeout = timeout
        self._lock = RLock()
        self._turns: dict[str, AgentTurn] = {}
        self._latest_applied_turn_id: str | None = None

    def start(
        self, *, prompt: str, session_id: str, expected_revision: int,
        background: bool = True,
    ) -> AgentTurn:
        turn_id = uuid4().hex
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
                checkpoint=copy.deepcopy(snapshot.notebook),
            )
            with self._lock:
                self._turns[turn_id] = turn
        except Exception:
            self.documents.coordinator.release(lease)
            raise
        if background:
            Thread(
                target=self._run_guarded, args=(turn, scope, lease, snapshot),
                name=f"agent-turn-{turn_id[:8]}", daemon=True,
            ).start()
        else:
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

    def cancel(self, turn_id: str) -> AgentTurn:
        with self._lock:
            turn = self._turns.get(turn_id)
            if turn is None:
                raise AgentTurnNotFound(turn_id)
            if turn.state not in TERMINAL_STATES:
                turn.cancel_event.set()
        return self.get(turn_id)

    def _run_guarded(self, turn: AgentTurn, scope: FrozenTurnScope, lease, frozen_snapshot) -> None:
        outcome = "failed"
        try:
            self._run(turn, scope, lease, frozen_snapshot)
            outcome = turn.state
        except AgentCancelled as error:
            self._finish(turn, "cancelled", error)
            outcome = "cancelled"
        except NotebookDomainError as error:
            self._finish(turn, "failed", error)
        except Exception as error:  # keep worker failures observable
            self._finish(turn, "failed", error)
        finally:
            self.documents.coordinator.release(lease)
            self.scopes.expire(scope, outcome)

    def _run(self, turn: AgentTurn, scope: FrozenTurnScope, lease, frozen_snapshot) -> None:
        correction = None
        last_violation: WorkspaceBoundaryError | None = None
        for attempt_number in range(1, 4):
            if turn.cancel_event.is_set():
                raise AgentCancelled()
            self._set_state(turn, "agent_running")
            workspace = self.builder.build(
                frozen_snapshot, scope, correction=correction
            )
            try:
                turn.attempts = attempt_number
                result = self.adapter.run(
                    workspace, timeout=self.timeout, cancel_event=turn.cancel_event
                )
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
                last_violation = error
                correction = "; ".join(error.violations)
                if attempt_number == 3:
                    raise
            finally:
                self.builder.destroy(workspace)
        if last_violation is not None:
            raise last_violation
        if turn.cancel_event.is_set():
            raise AgentCancelled()
        if not changes:
            turn.changes = ()
            self._finish(turn, "completed")
            return
        self._set_state(turn, "applying")
        updated = self.documents.apply_source_changes_under_lease(
            changes={change.cell_id: change.next_source for change in changes},
            expected_revision=scope.notebook_revision, owner=turn.turn_id, lease=lease,
        )
        turn.changes = changes
        turn.applied_revision = updated.revision
        with self._lock:
            self._latest_applied_turn_id = turn.turn_id
        self._finish(turn, "completed")

    def undo(self, turn_id: str, *, session_id: str, expected_revision: int):
        turn = self._require_applied(turn_id)
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
            return restored
        finally:
            self.documents.coordinator.release(lease)

    def revert_cell(
        self, turn_id: str, cell_id: str, *, session_id: str,
        expected_revision: int,
    ):
        turn = self._require_applied(turn_id)
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
            return self.documents.apply_source_changes_under_lease(
                changes={cell_id: change.previous_source},
                expected_revision=expected_revision,
                owner=f"revert:{turn_id}", lease=lease,
            )
        finally:
            self.documents.coordinator.release(lease)

    def _require_applied(self, turn_id: str) -> AgentTurn:
        turn = self.get(turn_id)
        if turn.applied_revision is None or turn.checkpoint is None:
            raise UndoConflict()
        return turn

    @staticmethod
    def _source_hash(source: str) -> str:
        return hashlib.sha256(source.encode()).hexdigest()

    def _set_state(self, turn: AgentTurn, state: str) -> None:
        with self._lock:
            if turn.state in TERMINAL_STATES:
                return
            turn.state = state

    def _finish(self, turn: AgentTurn, state: str, error: Exception | None = None) -> None:
        with self._lock:
            turn.state = state
            turn.completed_at = datetime.now(timezone.utc)
            if error is not None:
                if isinstance(error, NotebookDomainError):
                    turn.error = {"code": error.code, "message": error.message, "details": error.details}
                else:
                    turn.error = {"code": "internal_error", "message": str(error), "details": {}}
