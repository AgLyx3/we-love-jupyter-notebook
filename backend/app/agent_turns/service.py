from __future__ import annotations

import copy
import hashlib
import logging
import time
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from threading import Event, RLock, Thread, current_thread
from typing import Any, Sequence
from uuid import uuid4

from ..agent_workspace.models import (
    AgentAdapter, AgentCancelled, MemoryEntry, MemoryOperation,
    WorkspaceBoundaryError, WorkspaceCleanupError,
)
from ..agent_workspace.workspace_auditor import WorkspaceAuditor
from ..agent_workspace.workspace_builder import AgentWorkspaceBuilder
from ..boundary_validation.validator import BoundaryValidator, CandidateCellSourceChange
from ..boundary_validation.structural_validator import StructuralOp, derive_structural_plan
from ..notebook_document.models import (
    CellNotFound, MutationConflict, NotebookDomainError, NotebookImportError,
    RevisionConflict, SessionConflict,
)
from ..notebook_document.service import NotebookDocumentService
from .operations import (
    ACCEPTED, APPLIED_STATES, KIND_SOURCE_HUNK, KIND_STRUCTURAL_ADD, PENDING,
    REJECTED,
    TurnOperation, build_add_operation, build_operations, compose, is_stale,
    source_hash, with_state,
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
# Review actions whose document mutations still count as a turn's own lineage,
# so undoing part of a turn does not end its whole-turn undo. `undo` is
# deliberately absent: restoring the checkpoint ends the lineage by definition.
LINEAGE_ACTIONS = ("reject",)
# How many past turns the agent is shown at the start of a turn. Deliberately
# far tighter than MAX_TERMINAL_TURNS, so count-based pruning can never evict a
# turn the feed still wants. Equal to MAX_PENDING_REVIEW_TURNS by coincidence,
# not by coupling: that one protects unreviewed turns from eviction, which can
# only ever help the feed.
THREAD_MEMORY_TURNS = 10
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
    write_scope: str = "blocking"
    editable_cell_ids: tuple[str, ...] = ()
    context_cell_ids: tuple[str, ...] = ()
    state: str = "created"
    attempts: int = 0
    final_output: str = ""
    changes: tuple[CandidateCellSourceChange, ...] = ()
    # Source-hunk ledger. Populated for Blocking turns, and (since T1) for the
    # reviewable part of a Trusted turn: source edits to surviving same-type
    # cells, plus whole-cell adds. Retyped, deleted, and moved cells get no
    # operations — rejecting those needs anchor math over an ordering other ops
    # also changed — so they stay whole-turn undo only; see revert_cell.
    operations: tuple[TurnOperation, ...] = ()
    structural_ops: tuple[StructuralOp, ...] = ()
    applied_revision: int | None = None
    execution_operation_id: str | None = None
    checkpoint: dict[str, Any] | None = None
    error: dict[str, Any] | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    completed_at: datetime | None = None
    # Undo is a recorded outcome, not the absence of a checkpoint: pruning also
    # clears `checkpoint`, so the two are otherwise indistinguishable and `state`
    # stays "completed" either way. Set only by undo(), never cleared.
    #
    # For a cell the ledger covers, undo() also settles its operations to
    # `rejected`, so this marker is redundant there. It is load-bearing for the
    # cells the ledger has no kind for — retyped, deleted, and moved cells in a
    # Trusted turn — where it is the only record that the change was reversed.
    undone_at: datetime | None = None
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
                # Frozen alongside the scope and the snapshot: all three boundary
                # retries must see identical memory, or a retry becomes a
                # different turn.
                memory = self._thread_memory_or_empty(session_id, snapshot)
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
                    target=self._run_guarded,
                    args=(turn, scope, lease, snapshot, memory),
                    name=f"agent-turn-{turn_id[:8]}", daemon=True,
                )
                self._workers[worker] = turn_id
                worker.start()
        if not background:
            self._run_guarded(turn, scope, lease, snapshot, memory)
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

    def thread_memory(
        self, session_id: str, *, limit: int = THREAD_MEMORY_TURNS, snapshot=None,
    ) -> tuple[MemoryEntry, ...]:
        """Past turns of this session, oldest first, as the agent will see them.

        Only settled turns qualify: an in-flight turn has no outcome to report.
        Status is derived here, at read time, and never cached on the entry.

        Callers holding a snapshot should pass it: staleness is derived per turn
        and taking one deep-copies the notebook.
        """
        # Read the document before taking our lock, and pass the result down
        # explicitly. The document service calls back into this service under
        # its own lock when a session is replaced, so reaching for the document
        # lock while holding ours inverts the order (same rule as
        # is_undo_eligible). A snapshot we cannot read costs staleness only.
        if snapshot is None:
            try:
                snapshot = self.documents.get_snapshot()
            except NotebookDomainError:
                snapshot = None
        with self._lock:
            settled = sorted(
                (
                    turn for turn in self._turns.values()
                    if turn.session_id == session_id and turn.state in TERMINAL_STATES
                ),
                key=lambda item: item.completed_at or item.created_at,
            )[-limit:]
            # Deriving staleness per turn is the whole cost of this call, and it
            # is paid on the turn-start path, holding both this lock and the
            # document lease. Measured 2026-08-12 at a worst case of a full feed
            # (10 turns) each changing six 60-line cells at ten hunks a cell:
            # 2.5 ms for the feed, 0.9 ms of it here — against 8 ms for the
            # workspace build the same turn goes on to pay before the agent even
            # starts. Left as it is deliberately: of that 0.9 ms the per-cell
            # rescan of `operations` is 0.1 ms, so grouping them first buys 3%
            # of the feed and the real cost is compose() over the sources.
            return tuple(
                self._memory_entry(
                    turn,
                    self.stale_cell_ids(turn, snapshot)
                    if snapshot is not None else frozenset(),
                )
                for turn in settled
            )

    @classmethod
    def _memory_entry(
        cls, turn: AgentTurn, stale: frozenset[str] = frozenset(),
    ) -> MemoryEntry:
        if turn.state == "cancelled":
            turn_status = "CANCELLED"
        elif turn.state in ("failed", "validation_incomplete"):
            turn_status = f"FAILED ({(turn.error or {}).get('code') or turn.state})"
        else:
            turn_status = ""
        # A turn that did not settle cleanly gets no per-operation status: _run
        # can apply changes and *then* cancel, so whether they survived is not
        # recorded anywhere and must not be guessed.
        if turn_status:
            operations = tuple(
                MemoryOperation(
                    cell_id=change.cell_id, status="",
                    previous_source=change.previous_source,
                    next_source=change.next_source,
                    # Staleness survives the omission above because it is read
                    # off the document rather than inferred from the turn.
                    # `changes` is only ever populated in the same breath as the
                    # apply, so a turn with changes to report did reach the
                    # notebook, and FAILED — unlike CANCELLED — carries no "read
                    # notebook.ipynb" line to cover for its account of the cell
                    # having since been overwritten by hand.
                    stale=change.cell_id in stale,
                )
                for change in turn.changes
            )
        else:
            operations = cls._memory_operations(turn, stale)
        return MemoryEntry(
            prompt=turn.prompt, mode=turn.mode, operations=operations,
            reply="" if operations else turn.final_output, turn_status=turn_status,
        )

    @staticmethod
    def _memory_operations(
        turn: AgentTurn, stale: frozenset[str] = frozenset(),
    ) -> tuple[MemoryOperation, ...]:
        """Split each cell change by what the ledger says actually survived.

        A cell whose hunks were reviewed differently yields two entries: one for
        the hunks still in the notebook and one for the hunks the user undid.
        Reporting a single verdict per cell would have to round one of them off,
        and rounding *towards* KEPT is the dangerous direction — it tells the
        agent its rejected code is live and invites it to build on code that is
        not there.
        """
        by_cell: dict[str, list[TurnOperation]] = {}
        for operation in turn.operations:
            if operation.hunk is not None:
                by_cell.setdefault(operation.cell_id, []).append(operation)
        entries: list[MemoryOperation] = []
        for change in turn.changes:
            operations = by_cell.get(change.cell_id, [])
            if not operations:
                # No ledger coverage: a retyped cell in a Trusted turn, or a
                # delete/move that the ledger has no kind for. The whole-turn
                # marker is all there is, which is exactly what it is for.
                entries.append(MemoryOperation(
                    cell_id=change.cell_id,
                    status="UNDONE" if turn.undone_at else "APPLIED",
                    previous_source=change.previous_source,
                    next_source=change.next_source,
                    stale=change.cell_id in stale,
                ))
                continue
            surviving = [
                item for item in operations if item.state in APPLIED_STATES
            ]
            current = compose(
                previous_source=change.previous_source,
                next_source=change.next_source, operations=operations,
            )
            if surviving:
                entries.append(MemoryOperation(
                    cell_id=change.cell_id,
                    status="KEPT" if all(
                        item.state == ACCEPTED for item in surviving
                    ) else "APPLIED",
                    # Diffed against what the cell actually holds now, so the
                    # description covers the surviving hunks only. The body is
                    # never rendered for these (D2), just the summary line.
                    previous_source=change.previous_source, next_source=current,
                    stale=change.cell_id in stale,
                ))
            if len(surviving) < len(operations):
                # What the cell would hold had the rejected hunks stood. The
                # diff current -> restored *is* the undone content, and it is
                # the only place that content still exists.
                restored = compose(
                    previous_source=change.previous_source,
                    next_source=change.next_source,
                    operations=[
                        replace(item, state=ACCEPTED)
                        if item.state == REJECTED else item
                        for item in operations
                    ],
                )
                entries.append(MemoryOperation(
                    cell_id=change.cell_id, status="UNDONE",
                    previous_source=current, next_source=restored,
                    stale=change.cell_id in stale,
                ))
        # Cells a Trusted turn added are absent from `changes` — they have no
        # origin id, so there is no before/after pair to record — and live only
        # on the ledger. Without this an add-only turn reports "no changes",
        # which is worse than silence: it is a false statement the agent may act
        # on by adding the same cell again.
        for operation in turn.operations:
            if operation.kind != KIND_STRUCTURAL_ADD:
                continue
            entries.append(MemoryOperation(
                cell_id=operation.cell_id, kind="add",
                status=(
                    "UNDONE" if operation.state == REJECTED
                    else "KEPT" if operation.state == ACCEPTED else "APPLIED"
                ),
                # The ledger keeps a hash of the added source, not the source.
                # A kept add is readable in the notebook; a rejected one is gone
                # for good, so the feed reports the fact without the content.
                previous_source="", next_source="",
                stale=operation.cell_id in stale,
            ))
        return tuple(entries)

    def _thread_memory_or_empty(
        self, session_id: str, snapshot=None,
    ) -> tuple[MemoryEntry, ...]:
        """Memory is advisory: a failure here must never fail the turn."""
        try:
            return self.thread_memory(session_id, snapshot=snapshot)
        except Exception:
            logger.exception(
                "Failed to build thread memory for session %s; running without it",
                session_id,
            )
            return ()

    def history_for_session(
        self, session_id: str, *, limit: int = 50,
    ) -> list[AgentTurn]:
        with self._lock:
            turns = sorted(
                (turn for turn in self._turns.values() if turn.session_id == session_id),
                key=lambda item: item.created_at, reverse=True,
            )[:limit]
            return [self.get(turn.turn_id) for turn in turns]

    def is_undo_eligible(self, turn: AgentTurn, snapshot=None) -> bool:
        """Whether the turn's checkpoint may still be restored.

        Callers serializing a turn should pass a snapshot they already hold:
        taking one deep-copies the notebook, and a turn response otherwise
        pays for two (here and in ``stale_cell_ids``).

        Reads the snapshot before taking ``self._lock``: the document service
        calls back into this service under its own lock when a session is
        replaced, so acquiring the document lock while holding this one would
        invert the lock order.

        The stored turn is re-read under the lock rather than trusting the
        caller's copy, because callers routinely pass a copy taken before a
        concurrent mutation and this method has a destructive side effect.
        """
        # Cheap rejection first: get_snapshot deep-copies the notebook, and the
        # session-status endpoint calls this once per turn in history.
        with self._lock:
            if self._latest_applied_turn_id != turn.turn_id:
                return False
        try:
            snapshot = snapshot if snapshot is not None else self.documents.get_snapshot()
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
            if snapshot.revision < stored.applied_revision:
                # The caller's snapshot predates this turn's own commit, so it
                # cannot show who authored anything since — its
                # last_mutation_owner is from before the turn even applied.
                # Invalidating on that evidence destroys a checkpoint that is
                # almost certainly still valid, which is exactly the race the
                # owner check below exists to prevent. Stay optimistic: undo
                # itself is revision-guarded, so a false positive costs a 409
                # while a false negative costs the checkpoint permanently.
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
    def owner_for_turn(turn_id: str, action: str | None = None) -> str:
        """Mint a mutation owner tag for a turn, or one of its review actions.

        Mint through here rather than inline: ``is_undo_eligible`` forgives a
        revision gap authored by a turn's own lineage, so a producer the parser
        does not recognise silently reintroduces the checkpoint-destroying race
        that ownership recognition exists to close.
        """
        if action is None:
            return turn_id
        if action not in LINEAGE_ACTIONS:
            raise ValueError(f"owner action {action!r} is not a turn lineage action")
        return f"{action}:{turn_id}"

    @classmethod
    def _owned_by_turn(cls, owner: str | None, turn_id: str) -> bool:
        """Whether a document mutation belongs to the turn's own lineage.

        Excludes ``undo:`` — restoring a checkpoint deliberately ends it.
        """
        if not owner:
            return False
        return owner == turn_id or owner.startswith(
            tuple(f"{action}:{turn_id}" for action in LINEAGE_ACTIONS)
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

    def _run_guarded(
        self, turn: AgentTurn, scope: FrozenTurnScope, lease, frozen_snapshot,
        memory: tuple[MemoryEntry, ...] = (),
    ) -> None:
        outcome = "failed"
        terminal_error: Exception | None = None
        try:
            outcome, terminal_error = self._run(turn, scope, lease, frozen_snapshot, memory)
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

    def _run(
        self, turn: AgentTurn, scope: FrozenTurnScope, lease, frozen_snapshot,
        memory: tuple[MemoryEntry, ...] = (),
    ):
        # A Plan turn writes nothing regardless of scope, so it must never reach the
        # structural apply path. Only dispatch to the Trusted path in Edit mode; a
        # Trusted+Plan turn (possible via the sticky writeScope) falls through to the
        # Blocking path here, which runs the adapter with permission_mode="plan".
        if turn.write_scope == "trusted" and turn.mode != "plan":
            return self._run_trusted(turn, scope, lease, frozen_snapshot, memory)
        correction = None
        last_violation: WorkspaceBoundaryError | None = None
        for attempt_number in range(1, 4):
            if turn.cancel_event.is_set():
                raise AgentCancelled()
            self._set_state(turn, "agent_running")
            workspace = self.builder.build(
                frozen_snapshot, scope, correction=correction, memory=memory
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

    def _run_trusted(
        self, turn: AgentTurn, scope: FrozenTurnScope, lease, frozen_snapshot,
        memory: tuple[MemoryEntry, ...] = (),
    ):
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
                frozen_snapshot, scope, write_scope="trusted",
                correction=correction, memory=memory,
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
        # T1 ledger for structural turns: only the position-independent op kinds
        # are individually reviewable (design doc §12.2-12.3).
        # - `edit` on a surviving, same-type cell: source recompose, apply, and
        #   the stale guard all key off the cell id, never its index, so the
        #   ordinary hunk ledger applies unchanged even if the cell was moved.
        # - `add`: undoing means deleting that one cell by its realized id,
        #   guarded only by the cell itself; needs no anchor math.
        # - `delete` / `move` / `retype` stay whole-turn: rejecting them needs a
        #   notebook-level compose (T2), and retype additionally drops outputs
        #   that only the checkpoint can restore. Cells they touch get no
        #   operations, which is what keeps their per-cell controls hidden.
        retyped = {
            op.cell_id for op in plan.ops if op.op == "retype" and op.cell_id
        }
        # apply_structural_changes_under_lease builds the committed cell list in
        # next_cells order, so the returned snapshot aligns positionally with
        # plan.next_cells; that is how an add's realized id is recovered.
        applied_cells = updated.notebook["cells"]
        if len(applied_cells) != len(plan.next_cells):
            raise RuntimeError("structural apply returned a misaligned cell list")
        operations: list[TurnOperation] = []
        for planned, applied in zip(plan.next_cells, applied_cells):
            if planned.origin_id is None:
                operations.append(build_add_operation(
                    turn_id=turn.turn_id, cell_id=applied["id"],
                    source=planned.source,
                ))
            elif planned.origin_id not in retyped:
                operations.extend(build_operations(
                    turn_id=turn.turn_id, cell_id=planned.origin_id,
                    previous_source=frozen_source.get(planned.origin_id, ""),
                    next_source=planned.source,
                ))
        with self._lock:
            turn.structural_ops = tuple(plan.ops)
            turn.changes = edits
            turn.operations = tuple(operations)
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
                    stored.undone_at = datetime.now(timezone.utc)
                    stored.checkpoint = None
                    # Restoring the checkpoint reverses every change the turn
                    # made, so its ledger is settled by definition. Leaving the
                    # operations pending would strand them: they no longer match
                    # the document, so they serialize as stale and the cell keeps
                    # advertising an undoable change that is already gone.
                    stored.operations = self._settle_all(stored.operations, REJECTED)
                self._prune_history_locked()
            if self.events is not None:
                self.events.publish("notebook.updated", {"sessionId": restored.session_id, "revision": restored.revision, "ownerId": f"undo:{turn_id}"})
            return restored
        finally:
            self.documents.coordinator.release(lease)

    def stale_cell_ids(self, turn: AgentTurn, snapshot=None) -> frozenset[str]:
        """Cells whose ledger no longer describes the document.

        Derived on read rather than stored. A manual edit reaches the document
        through a path with no ledger awareness, so a stored flag would only
        become true the next time someone attempted a reject — leaving
        live-looking controls on operations that are already dead.

        Callers serializing several turns should pass a snapshot they already
        hold: taking one deep-copies the notebook, and the session-status
        endpoint would otherwise do that once per turn in history.
        """
        if not turn.operations:
            return frozenset()
        try:
            snapshot = snapshot if snapshot is not None else self.documents.get_snapshot()
        except NotebookDomainError:
            return frozenset()
        if snapshot.session_id != turn.session_id:
            return frozenset(item.cell_id for item in turn.operations)
        cells = {cell["id"]: cell for cell in snapshot.notebook["cells"]}
        sources = {change.cell_id: change for change in turn.changes}
        stale: set[str] = set()
        for cell_id in {item.cell_id for item in turn.operations}:
            for_cell = [
                item for item in turn.operations if item.cell_id == cell_id
            ]
            cell = cells.get(cell_id)
            add = next(
                (item for item in for_cell if item.kind == KIND_STRUCTURAL_ADD),
                None,
            )
            if add is not None:
                # An added cell has no entry in `changes`; its expected content
                # is whatever the turn wrote. A rejected add is *supposed* to be
                # missing from the document, so only judge applied states.
                if add.state in APPLIED_STATES and (
                    cell is None
                    or source_hash(self._cell_source(cell)) != add.source_hash
                ):
                    stale.add(cell_id)
                continue
            change = sources.get(cell_id)
            if change is None or cell is None:
                stale.add(cell_id)
                continue
            if is_stale(
                current_source=self._cell_source(cell),
                previous_source=change.previous_source,
                next_source=change.next_source,
                operations=for_cell,
            ):
                stale.add(cell_id)
        return frozenset(stale)

    @staticmethod
    def _cell_source(cell: dict[str, Any]) -> str:
        source = cell.get("source", "")
        return "".join(source) if isinstance(source, list) else source

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
                if operation_ids is None:
                    # "Undo all" should undo everything it still can. One cell
                    # the user has since hand-edited must not block the rest;
                    # those cells keep their operations and go on explaining
                    # themselves as stale, so nothing is dropped silently.
                    stale = self.stale_cell_ids(turn, snapshot)
                    targets = tuple(
                        item for item in targets if item.cell_id not in stale
                    )
                    operations = turn.operations
                    for target in targets:
                        operations = with_state(
                            operations, target.operation_id, REJECTED
                        )
                # Already-rejected adds are skipped, not re-guarded: their cell
                # is gone, which is the state being asked for. Guarding them
                # would look up a deleted cell and conflict, making a repeated
                # Undo (a double-click) fail where the hunk path is idempotent.
                add_targets = tuple(
                    item for item in targets
                    if item.kind == KIND_STRUCTURAL_ADD
                    and item.state in APPLIED_STATES
                )
                hunk_cell_ids = {
                    item.cell_id for item in targets
                    if item.kind == KIND_SOURCE_HUNK
                }
                # Dispatch positively and fail closed. Treating "not an add" as
                # "a source hunk" would route a future kind (the structural
                # delete/move of design doc §12.4) into the recompose path,
                # where it would look for a `changes` entry it never has and
                # surface as a confusing CellNotFound instead of an unhandled
                # kind at the one site that must learn about it.
                unhandled = {
                    item.kind for item in targets
                    if item.kind not in (KIND_SOURCE_HUNK, KIND_STRUCTURAL_ADD)
                }
                if unhandled:
                    # Deliberately not a NotebookDomainError: this is a
                    # programmer error (a kind was added to the ledger without
                    # teaching reject how to undo it), not something a client
                    # did wrong. It should surface as a 500 and be fixed, not
                    # be dressed up as a 4xx the caller can act on.
                    raise NotImplementedError(
                        f"reject_operations cannot undo operation kinds: "
                        f"{sorted(unhandled)}"
                    )
                self._guard_add_targets_locked(snapshot, add_targets, conflict)
                changes = self._recompose_locked(
                    snapshot, sources, turn.operations, operations,
                    hunk_cell_ids, conflict,
                )
            # Only when a cell is actually being removed: the structural apply
            # always writes a new revision, so routing a no-op through it would
            # bump the revision for nothing. The source path already no-ops on
            # an empty change set.
            dropped = {item.cell_id for item in add_targets}
            if dropped:
                # Undoing an add removes a whole cell, so the commit goes
                # through the hardened structural path — one atomic apply for
                # the dropped cells and any recomposed sources together, and the
                # zero-cell floor / duplicate-id checks come with it.
                next_cells = [
                    {
                        "origin_id": cell["id"],
                        "cell_type": cell.get("cell_type", "code"),
                        "source": changes.get(
                            cell["id"], self._cell_source(cell)
                        ),
                    }
                    for cell in snapshot.notebook["cells"]
                    if cell["id"] not in dropped
                ]
                try:
                    updated = self.documents.apply_structural_changes_under_lease(
                        next_cells=next_cells, expected_session_id=session_id,
                        expected_revision=expected_revision,
                        owner=self.owner_for_turn(turn_id, "reject"), lease=lease,
                    )
                except NotebookImportError as error:
                    # e.g. undoing the only remaining cell: a notebook must
                    # keep at least one. A review action must not 400 as if the
                    # request were malformed.
                    raise conflict() from error
            else:
                updated = self.documents.apply_source_changes_under_lease(
                    changes=changes, expected_revision=expected_revision,
                    owner=self.owner_for_turn(turn_id, "reject"), lease=lease,
                )
            with self._lock:
                stored = self._turns.get(turn_id)
                if stored is not None:
                    # Re-apply the reject transitions to whatever the ledger
                    # holds *now*, rather than writing back the tuple captured
                    # before the commit. Accept takes no lease, so an accept
                    # landing mid-reject would otherwise be silently rolled back.
                    current = stored.operations
                    for target in targets:
                        current = with_state(
                            current, target.operation_id, REJECTED
                        )
                    stored.operations = current
                    # Keep the checkpoint alive across the turn's own review:
                    # undoing part of a turn is reviewing it, not unrelated work.
                    if self._latest_applied_turn_id == turn_id:
                        stored.applied_revision = updated.revision
            if self.events is not None:
                self.events.publish("notebook.updated", {"sessionId": updated.session_id, "revision": updated.revision, "ownerId": self.owner_for_turn(turn_id, "reject")})
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

    def _guard_add_targets_locked(
        self, snapshot, add_targets: tuple[TurnOperation, ...],
        conflict: type[NotebookDomainError],
    ) -> None:
        """Refuse to delete an added cell the user has since touched.

        Per-op-local by design: undoing an add needs nothing from the rest of
        the notebook, only that this one cell still holds exactly the source the
        turn wrote. Missing (already deleted by hand) or drifted → conflict.
        """
        cells = {cell["id"]: cell for cell in snapshot.notebook["cells"]}
        for target in add_targets:
            cell = cells.get(target.cell_id)
            if cell is None or source_hash(
                self._cell_source(cell)
            ) != target.source_hash:
                raise conflict()

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
        # Whether the cell is *governed* by the ledger — not whether anything is
        # still outstanding. Once everything is rejected there is nothing left
        # to do, and that must read as a no-op, not as "this cell is off
        # limits"; otherwise a repeated Undo conflicts on work already done.
        in_ledger = any(item.cell_id == cell_id for item in turn.operations)
        operation_ids = [
            item.operation_id for item in turn.operations
            if item.cell_id == cell_id and item.state in APPLIED_STATES
        ]
        # T1 narrows main's R20 rather than removing it: on a Trusted turn, a
        # cell is individually revertible exactly when it has ledger operations
        # (an `edit` on a surviving same-type cell, or an `add`). Cells caught
        # up in delete/move/retype have none — undoing those needs the
        # notebook-level compose (design doc §12.4) — so they keep the hard 409.
        if turn.write_scope == "trusted" and not in_ledger:
            raise RevertConflict()
        if not in_ledger and not any(
            item.cell_id == cell_id for item in turn.changes
        ):
            raise CellNotFound(cell_id)
        return self.reject_operations(
            turn_id, operation_ids, session_id=session_id,
            expected_revision=expected_revision, conflict=RevertConflict,
        )

    def _publish_turn_updated(self, turn_id: str) -> None:
        if self.events is None:
            return
        # Read the scalars under the lock rather than via get(), which
        # deep-copies the turn's checkpoint — a full copy of the pre-turn
        # notebook — on what is now the hottest path in the app: every
        # per-hunk Keep or Undo publishes this.
        with self._lock:
            turn = self._turns.get(turn_id)
            if turn is None:
                return
            payload = {
                "turnId": turn.turn_id, "sessionId": turn.session_id,
                "state": turn.state,
                "revision": turn.applied_revision or turn.base_revision,
                "executionOperationId": turn.execution_operation_id,
            }
        self.events.publish("turn.updated", payload)

    def _require_undoable(self, turn_id: str) -> AgentTurn:
        turn = self.get(turn_id)
        if turn.applied_revision is None or turn.checkpoint is None:
            raise UndoConflict()
        return turn

    def _require_revertible(self, turn_id: str) -> AgentTurn:
        turn = self.get(turn_id)
        # A turn is revertible if it applied anything reviewable. `changes`
        # alone is not that test: an add-only Trusted turn records structural
        # operations and no source changes, and gating on `changes` made its
        # added cells permanently un-undoable.
        if turn.applied_revision is None or not (turn.changes or turn.operations):
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
        # The checkpoint-protected turn is retained regardless, so its size is
        # reserved up front. Track it as already counted: it is usually *also*
        # the newest unreviewed turn, and charging it twice halved the effective
        # ceiling — a turn just over half the budget was force-settled the
        # moment it completed and could never be reviewed per operation.
        counted: set[str] = set()
        latest = self._turns.get(self._latest_applied_turn_id or "")
        if latest is not None:
            budget += self._history_size(latest)
            counted.add(latest.turn_id)
        for item in terminal:
            if not any(
                operation.state == PENDING for operation in item.operations
            ):
                continue
            size = 0 if item.turn_id in counted else self._history_size(item)
            if (
                len(keep) < MAX_PENDING_REVIEW_TURNS
                and budget + size <= MAX_TURN_HISTORY_BYTES
            ):
                keep.add(item.turn_id)
                budget += size
                counted.add(item.turn_id)
                continue
            self._force_settle_locked(item)
        return keep

    @staticmethod
    def _settle_all(
        operations: tuple[TurnOperation, ...], state: str,
    ) -> tuple[TurnOperation, ...]:
        for operation in operations:
            if operation.state == PENDING:
                operations = with_state(operations, operation.operation_id, state)
        return operations

    def _force_settle_locked(self, turn: AgentTurn) -> None:
        turn.operations = self._settle_all(turn.operations, ACCEPTED)
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
        for op in turn.structural_ops:
            values.extend((op.op, op.cell_id or "", str(op.detail)))
        if turn.error is not None:
            values.append(str(turn.error))
        # Operations are index ranges (or a fixed-size source hash for adds)
        # over sources already counted above — a fixed per-op cost, no text.
        return (
            sum(len(value.encode("utf-8")) for value in values)
            + 192 * len(turn.operations)
            + 512
        )
