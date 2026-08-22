"""Commit tuned knob values: rewrite, apply atomically, re-run, stay undoable.

This is the only part of plot tuning that mutates anything. Preview
(`ShadowSession`) exists precisely so that everything up to this point is free;
Apply is where the user says "this one", and where the cost is paid — one
all-or-nothing document edit plus one live chain re-run (design §3.4, D8, D9).

Three things this module is deliberately *not*:

* It is not an agent turn. Per decision D6 a tune is a user-driven edit with no
  adapter, no prompt, no write scope and no cancellable model run, so injecting
  a synthetic record into `AgentTurnService` would make every one of that
  service's methods ask "does this one assume an agent?". What it reuses
  instead is the genuinely generic layer: `operations.py`, the document apply
  path, and the downstream execution path.
* It is not a second undo mechanism. Undo is the existing move — flip the
  operation state to ``rejected`` and recompose from the pre-edit source. No
  inverse patches, no stored reverse diffs (`operations.compose` is pure, so
  the cell's expected content is always recomputable).
* It is not a second notebook mutator. Every write goes through
  `NotebookDocumentService` under a coordinator lease, so the product invariant
  — the Notebook Document domain owns mutation — holds here as it does for the
  agent path.

The record carries an ``origin`` discriminator so the review bar can say **"You
tuned this cell"** rather than "Agent changed this cell". The per-hunk Keep/Undo
controls behind those two labels are the same code.
"""

from __future__ import annotations

import copy
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from threading import Event, RLock, Thread, current_thread
from typing import Any, Sequence
from uuid import uuid4

from ..agent_turns.operations import (
    ACCEPTED, APPLIED_STATES, PENDING, REJECTED,
    TurnOperation, build_operations, compose, is_stale, with_state,
)
from ..kernel_execution.service import KernelExecutionService
from ..notebook_document.models import (
    CellNotFound, MutationLease, NotebookDomainError, NotebookSnapshot,
    SessionConflict,
)
from ..notebook_document.service import NotebookDocumentService
from ..session_events.service import SessionEventService
from .discovery import Knob
from .rewrite import RewriteError, rewrite_chain


logger = logging.getLogger(__name__)


#: The edit-origin discriminator. The frontend labels a cell's review bar from
#: this, so it is part of the record's contract rather than a display detail.
ORIGIN_TUNE = "tune"

#: Tuning records are small — index ranges plus the two source strings per
#: edited cell — but they are unbounded in number, so cap the history. Losing an
#: evicted record loses only the ability to review or undo that tune; the
#: notebook keeps exactly the content the tune applied.
MAX_RECORDS = 20

TERMINAL_RECORD_STATES = frozenset({
    "completed", "failed", "cancelled", "timed_out", "validation_incomplete",
})

#: Kernel states in which nothing can have been executed (or can be executed)
#: in the current session, so the live prefix is certainly cold.
COLD_KERNEL_STATES = frozenset({"not_started", "restart_required"})


class TuningRecordNotFound(NotebookDomainError):
    code = "plot_tuning_record_not_found"
    message = "Plot tuning record was not found"
    status_code = 404

    def __init__(self, record_id: str) -> None:
        super().__init__(recordId=record_id)


class TuningRewriteFailed(NotebookDomainError):
    """A knob could not be written back into its cell source.

    Nearly always means the knob is stale: the cell has been edited since the
    panel scanned it, so the recorded source range no longer points at the
    literal. `rewrite` re-parses and re-reads every edited name, which is what
    turns that into this loud 409 instead of a wrong value on disk.
    """

    code = "plot_tuning_rewrite_failed"
    message = "Tuned values could not be written back into the cell source"
    status_code = 409

    def __init__(self, reason: str) -> None:
        super().__init__(reason=reason)


class TuningOperationNotFound(NotebookDomainError):
    code = "plot_tuning_operation_not_found"
    message = "Plot tuning operation was not found"
    status_code = 404

    def __init__(self, operation_id: str) -> None:
        super().__init__(operationId=operation_id)


class TuningOperationConflict(NotebookDomainError):
    code = "plot_tuning_operation_conflict"
    message = "Cell no longer matches this tune's recorded changes"
    status_code = 409


@dataclass(frozen=True)
class TunedCellChange:
    """One cell's source before and after the tune.

    Deliberately its own type rather than the agent path's
    `CandidateCellSourceChange`: that name means "a change an agent proposed and
    the boundary validator accepted", and nothing here was proposed or
    validated. `compose` only needs the two strings.
    """

    cell_id: str
    previous_source: str
    next_source: str


@dataclass
class TuningRecord:
    """One Apply: what it wrote, what is reviewable, and what it re-ran."""

    record_id: str
    session_id: str
    base_revision: int
    #: Edit origin. Always ORIGIN_TUNE here; present so the frontend can
    #: discriminate this record from an agent turn without inferring it from a
    #: URL or a missing prompt.
    origin: str = ORIGIN_TUNE
    knob_names: tuple[str, ...] = ()
    changes: tuple[TunedCellChange, ...] = ()
    #: Per-hunk ledger, identical in kind to an agent turn's.
    operations: tuple[TurnOperation, ...] = ()
    applied_revision: int | None = None
    execution_operation_id: str | None = None
    #: Cell index the live re-run started from, and whether the §3.6 prefix
    #: guard widened it to the top of the notebook. The UI needs the second one:
    #: "re-ran the whole notebook" is a materially different bill from "re-ran
    #: from your edit down", and the user should be told which they got.
    execution_start_index: int | None = None
    extended_to_top: bool = False
    state: str = "applied"
    error: dict[str, Any] | None = None
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    cancel_event: Event = field(default_factory=Event, repr=False)


class TuningApplyService:
    """Writes tuned literals back, atomically, and re-runs the live chain.

    One instance per app. Holds no kernel and no shadow: preview lives entirely
    on the other side of the feature, and mixing the two here would blur the one
    distinction the whole design rests on (playing is free, confirming costs).
    """

    def __init__(
        self, *, documents: NotebookDocumentService,
        executions: KernelExecutionService | None = None,
        events: SessionEventService | None = None,
    ) -> None:
        self.documents = documents
        self.executions = executions
        self.events = events
        self._lock = RLock()
        self._records: dict[str, TuningRecord] = {}
        self._workers: dict[Thread, str] = {}
        #: Kernel session under which this service last re-ran the notebook from
        #: cell 0. The whole of the §3.6 prefix guard hangs off this one field —
        #: see `_prefix_is_warm`.
        self._warm_kernel_session_id: str | None = None
        documents.register_session_replacement_listener(self._on_session_replaced)

    # ---------------------------------------------------------------- apply

    def apply(
        self, *, session_id: str, expected_revision: int,
        edits: Sequence[tuple[Knob, Any]], background: bool = True,
    ) -> TuningRecord:
        """Rewrite every knob, commit all affected cells as one edit, re-run.

        Returns as soon as the *document* edit is committed (or has failed), so
        a caller learns about a revision conflict immediately rather than after
        a multi-minute chain re-run. The re-run then proceeds on a worker that
        keeps the mutation lease until it finishes; its progress is observable
        through the ordinary execution endpoints via
        ``record.execution_operation_id``.
        """
        record_id = uuid4().hex
        lease = self.documents.coordinator.acquire(
            operation_type="plot_tuning_apply", operation_id=record_id,
        )
        handed_off = False
        try:
            snapshot = self.documents.get_snapshot()
            self.documents.check_snapshot_preconditions(
                snapshot, session_id, expected_revision,
            )
            sources = self._sources_for(snapshot, edits)
            try:
                rewritten = rewrite_chain(sources, list(edits))
            except RewriteError as error:
                # Atomicity, part one. Every cell is rewritten in memory before
                # anything is committed, so a knob that fails to rewrite —
                # whichever cell it lives in — leaves the document untouched.
                # Part two is that the commit below is a single apply.
                raise TuningRewriteFailed(str(error)) from error

            changes = tuple(
                TunedCellChange(
                    cell_id=cell_id,
                    previous_source=sources[cell_id],
                    next_source=next_source,
                )
                for cell_id, next_source in sorted(rewritten.items())
                if next_source != sources[cell_id]
            )
            record = TuningRecord(
                record_id=record_id, session_id=session_id,
                base_revision=expected_revision,
                knob_names=tuple(knob.name for knob, _value in edits),
                changes=changes,
            )
            if not changes:
                # Applying the values the cells already hold must not bump the
                # revision, and must certainly not spend a live re-run.
                record.state = "completed"
                self._remember(record)
                return self.get(record_id)

            record.operations = tuple(
                operation
                for change in changes
                for operation in build_operations(
                    turn_id=record_id, cell_id=change.cell_id,
                    previous_source=change.previous_source,
                    next_source=change.next_source,
                )
            )
            # D9: several knobs, possibly across several cells, one
            # all-or-nothing edit guarded by the revision the panel was showing.
            updated = self.documents.apply_source_changes_under_lease(
                changes={change.cell_id: change.next_source for change in changes},
                expected_revision=expected_revision, owner=record_id, lease=lease,
            )
            record.applied_revision = updated.revision
            self._remember(record)
            self._publish_notebook_updated(updated, record_id)

            handed_off = True
            if background:
                worker = Thread(
                    target=self._execute_guarded, args=(record_id, updated, lease),
                    name=f"plot-tune-{record_id[:8]}", daemon=True,
                )
                with self._lock:
                    self._workers[worker] = record_id
                worker.start()
            else:
                self._execute_guarded(record_id, updated, lease)
            return self.get(record_id)
        finally:
            if not handed_off:
                self.documents.coordinator.release(lease)

    def _sources_for(
        self, snapshot: NotebookSnapshot, edits: Sequence[tuple[Knob, Any]],
    ) -> dict[str, str]:
        cells = {cell["id"]: cell for cell in snapshot.notebook["cells"]}
        sources: dict[str, str] = {}
        for knob, _value in edits:
            cell = cells.get(knob.cell_id)
            if cell is None:
                raise CellNotFound(knob.cell_id)
            if cell.get("cell_type") != "code":
                # Discovery only ever parses code cells, so this means the
                # client sent a knob for a cell that has since been retyped.
                raise TuningRewriteFailed(
                    f"`{knob.name}` is no longer in a code cell",
                )
            sources[knob.cell_id] = _cell_source(cell)
        return sources

    # ------------------------------------------------------- live execution

    def _execute_guarded(
        self, record_id: str, snapshot: NotebookSnapshot, lease: MutationLease,
    ) -> None:
        try:
            self._execute(record_id, snapshot, lease)
        except NotebookDomainError as error:
            self._fail(record_id, error.code, error.message, error.details)
        except Exception as error:  # keep worker failures observable
            logger.exception("Plot tuning re-run failed for record %s", record_id)
            self._fail(record_id, "plot_tuning_execution_failed", str(error), {})
        finally:
            self.documents.coordinator.release(lease)
            with self._lock:
                self._workers.pop(current_thread(), None)

    def _execute(
        self, record_id: str, snapshot: NotebookSnapshot, lease: MutationLease,
    ) -> None:
        """D8: the code, the kernel and the outputs must agree after Apply.

        Writing the literals alone would leave the live kernel holding the old
        values, so every downstream cell would silently disagree with the plot
        above it. Transplanting the shadow's image instead was rejected for
        exactly the same reason.
        """
        record = self.get(record_id)
        if self.executions is None:
            self._set_state(record_id, "completed")
            return

        cells = snapshot.notebook["cells"]
        indexes = {cell["id"]: index for index, cell in enumerate(cells)}
        anchors = {change.cell_id for change in record.changes}
        start = min(indexes[cell_id] for cell_id in anchors)
        extended = start > 0 and not self._prefix_is_warm()
        if extended:
            # §3.6, the prefix guard. Re-running "from the earliest changed
            # cell" silently assumes the live kernel already ran everything
            # before it. On a fresh or restarted kernel it has not, and the
            # re-run dies on a NameError that reads as this feature being
            # broken. `run_downstream` starts at the earliest of the ids it is
            # given, so anchoring on the first cell widens it to the whole
            # notebook.
            anchors.add(cells[0]["id"])
            start = 0
        with self._lock:
            stored = self._records.get(record_id)
            if stored is not None:
                stored.execution_start_index = start
                stored.extended_to_top = extended

        execution = self.executions.create_downstream(
            parent_turn_id=record_id, session_id=record.session_id,
            expected_revision=snapshot.revision, cancel_event=record.cancel_event,
        )
        with self._lock:
            stored = self._records.get(record_id)
            if stored is not None:
                stored.execution_operation_id = execution.operation_id
        self._set_state(record_id, "executing")
        # Risky cells in the widened prefix are not re-fired silently:
        # `run_downstream` prompts per cell through the existing approval flow,
        # the same one the shadow warm-up uses (D5).
        execution = self.executions.run_downstream(
            execution.operation_id, changed_cell_ids=anchors, lease=lease,
        )
        with self._lock:
            stored = self._records.get(record_id)
            if stored is not None:
                stored.applied_revision = execution.current_revision
                stored.state = execution.state
                stored.error = copy.deepcopy(execution.error)
        if execution.state == "completed" and start == 0:
            # Only a run that started at cell 0 *and* finished proves the whole
            # notebook is resident in this kernel session.
            self._warm_kernel_session_id = self._kernel_session_id()

    def _prefix_is_warm(self) -> bool:
        """Whether the live kernel provably holds everything above the edit.

        The only proof this service can honestly produce is its own: a re-run it
        started at cell 0 and saw complete, in the kernel session that is still
        current. `run_downstream` runs to the *end* of the notebook, so one such
        run warms every prefix there is.

        Pessimistic on purpose. The document's `execution_count` values look
        like the obvious evidence and are not: a restart resets the kernel's
        counter while the committed counts stay behind, so a restarted kernel
        would read as warm — and a restarted kernel is exactly the case §3.6
        singles out as the subtle one. Being wrong in this direction costs one
        extra full replay per kernel session; being wrong in the other costs a
        NameError the user reads as a broken feature.
        """
        status = self.executions.kernel_status()
        if status.get("state") in COLD_KERNEL_STATES:
            return False
        return (
            self._warm_kernel_session_id is not None
            and status.get("kernelSessionId") == self._warm_kernel_session_id
        )

    def _kernel_session_id(self) -> str | None:
        return self.executions.kernel_status().get("kernelSessionId")

    def cancel(self, record_id: str) -> TuningRecord:
        """Stop a re-run in flight.

        Worth having precisely because of the prefix guard: on a cold kernel a
        tune of one literal can re-run the entire notebook, and a user who
        realises that halfway through needs a way out that is not "kill the
        app".
        """
        record = self.get(record_id)
        record.cancel_event.set()
        with self._lock:
            stored = self._records.get(record_id)
            if stored is not None:
                stored.cancel_event.set()
        if self.executions is not None:
            self.executions.cancel_parent(record_id)
        return self.get(record_id)

    # --------------------------------------------------------------- review

    def get(self, record_id: str) -> TuningRecord:
        with self._lock:
            stored = self._records.get(record_id)
            if stored is None:
                raise TuningRecordNotFound(record_id)
            result = copy.copy(stored)
            result.error = copy.deepcopy(stored.error)
            return result

    def history_for_session(
        self, session_id: str, *, limit: int = MAX_RECORDS,
    ) -> list[TuningRecord]:
        with self._lock:
            records = sorted(
                (
                    item for item in self._records.values()
                    if item.session_id == session_id
                ),
                key=lambda item: item.created_at, reverse=True,
            )[:limit]
            return [self.get(item.record_id) for item in records]

    def stale_cell_ids(
        self, record: TuningRecord, snapshot: NotebookSnapshot | None = None,
    ) -> frozenset[str]:
        """Cells whose ledger no longer describes the document.

        Derived on read, never stored, for the same reason as on the agent path:
        a hand edit reaches the document through a path that knows nothing about
        this ledger, so a stored flag would only become true the next time
        someone tried to undo — leaving live-looking controls on dead operations.
        """
        if not record.operations:
            return frozenset()
        try:
            snapshot = snapshot if snapshot is not None else self.documents.get_snapshot()
        except NotebookDomainError:
            return frozenset()
        if snapshot.session_id != record.session_id:
            return frozenset(item.cell_id for item in record.operations)
        cells = {cell["id"]: cell for cell in snapshot.notebook["cells"]}
        sources = {change.cell_id: change for change in record.changes}
        stale: set[str] = set()
        for cell_id in {item.cell_id for item in record.operations}:
            change = sources.get(cell_id)
            cell = cells.get(cell_id)
            if change is None or cell is None:
                stale.add(cell_id)
                continue
            if is_stale(
                current_source=_cell_source(cell),
                previous_source=change.previous_source,
                next_source=change.next_source,
                operations=[
                    item for item in record.operations if item.cell_id == cell_id
                ],
            ):
                stale.add(cell_id)
        return frozenset(stale)

    def accept_operations(
        self, record_id: str, operation_ids: Sequence[str] | None, *,
        session_id: str,
    ) -> TuningRecord:
        """Keep hunks. Never touches the document.

        Accept only settles review state for content that is already in the
        notebook, so it takes no lease and no expected revision: a stale
        revision cannot make "I have read this diff" unsafe.
        """
        with self._lock:
            record = self._require_locked(record_id, session_id)
            targets = self._select_locked(record, operation_ids)
            operations = record.operations
            for target in targets:
                if target.state == REJECTED:
                    # Flipping a rejected hunk back to accepted would silently
                    # re-apply content the user has already undone.
                    raise TuningOperationConflict(operationId=target.operation_id)
                operations = with_state(operations, target.operation_id, ACCEPTED)
            record.operations = operations
        return self.get(record_id)

    def reject_operations(
        self, record_id: str, operation_ids: Sequence[str] | None, *,
        session_id: str, expected_revision: int,
    ) -> TuningRecord:
        """Undo hunks: flip them to rejected and recommit the recomposed cells.

        No inverse patch is stored or computed. `compose` rebuilds each cell
        from the pre-tune source plus the current operation states, so undo is
        a state flip and a recomputation — and undoing one knob out of three
        leaves the other two exactly as applied.
        """
        lease = self.documents.coordinator.acquire(
            operation_type="plot_tuning_undo", operation_id=record_id,
        )
        try:
            snapshot = self.documents.get_snapshot()
            self.documents.check_snapshot_preconditions(
                snapshot, session_id, expected_revision,
            )
            with self._lock:
                record = self._require_locked(record_id, session_id)
                targets = self._select_locked(record, operation_ids)
                if operation_ids is None:
                    # "Undo all" undoes everything it still can. One cell the
                    # user has since hand-edited must not block the rest; those
                    # cells keep their operations and go on reporting
                    # themselves as stale, so nothing disappears silently.
                    stale = self.stale_cell_ids(record, snapshot)
                    targets = tuple(
                        item for item in targets if item.cell_id not in stale
                    )
                updated_operations = record.operations
                for target in targets:
                    updated_operations = with_state(
                        updated_operations, target.operation_id, REJECTED,
                    )
                changes = self._recompose_locked(
                    snapshot, record, updated_operations,
                    {target.cell_id for target in targets},
                )
            # An empty change set is a no-op apply that does not bump the
            # revision, which is what makes a double-clicked Undo harmless.
            committed = self.documents.apply_source_changes_under_lease(
                changes=changes, expected_revision=expected_revision,
                owner=f"undo:{record_id}", lease=lease,
            )
            with self._lock:
                stored = self._records.get(record_id)
                if stored is not None:
                    # Re-apply the transitions to whatever the ledger holds
                    # *now* rather than writing back the tuple captured before
                    # the commit: accept takes no lease, so an accept landing
                    # mid-undo would otherwise be silently rolled back.
                    current = stored.operations
                    for target in targets:
                        current = with_state(
                            current, target.operation_id, REJECTED,
                        )
                    stored.operations = current
                    stored.applied_revision = committed.revision
            if changes:
                self._publish_notebook_updated(committed, f"undo:{record_id}")
            return self.get(record_id)
        finally:
            self.documents.coordinator.release(lease)

    def undo(
        self, record_id: str, *, session_id: str, expected_revision: int,
    ) -> TuningRecord:
        """Undo the whole tune.

        Not `reject_operations(None)`: that selects only *unreviewed* hunks, so
        a user who kept one hunk and then chose "undo this tune" would keep it
        by accident. Whole-record undo means every hunk still applied.
        """
        record = self.get(record_id)
        operation_ids = [
            item.operation_id for item in record.operations
            if item.state in APPLIED_STATES
        ]
        return self.reject_operations(
            record_id, operation_ids, session_id=session_id,
            expected_revision=expected_revision,
        )

    def _recompose_locked(
        self, snapshot: NotebookSnapshot, record: TuningRecord,
        updated_operations: tuple[TurnOperation, ...], cell_ids: set[str],
    ) -> dict[str, str]:
        cells = {cell["id"]: cell for cell in snapshot.notebook["cells"]}
        sources = {change.cell_id: change for change in record.changes}
        changes: dict[str, str] = {}
        for cell_id in sorted(cell_ids):
            change = sources.get(cell_id)
            cell = cells.get(cell_id)
            if change is None or cell is None:
                raise CellNotFound(cell_id)
            source = _cell_source(cell)
            # The guard is "this cell is exactly what the ledger says it should
            # be". Hashing the whole cell against next_source cannot work: once
            # one hunk is undone the cell no longer matches, which would make
            # every remaining hunk permanently un-undoable.
            if is_stale(
                current_source=source, previous_source=change.previous_source,
                next_source=change.next_source,
                operations=[
                    item for item in record.operations if item.cell_id == cell_id
                ],
            ):
                raise TuningOperationConflict()
            composed = compose(
                previous_source=change.previous_source,
                next_source=change.next_source,
                operations=[
                    item for item in updated_operations if item.cell_id == cell_id
                ],
            )
            if composed != source:
                changes[cell_id] = composed
        return changes

    def _select_locked(
        self, record: TuningRecord, operation_ids: Sequence[str] | None,
    ) -> tuple[TurnOperation, ...]:
        """Resolve requested operation IDs, or every unreviewed one when None."""
        if operation_ids is None:
            return tuple(
                item for item in record.operations if item.state == PENDING
            )
        known = {item.operation_id: item for item in record.operations}
        missing = [item for item in operation_ids if item not in known]
        if missing:
            raise TuningOperationNotFound(missing[0])
        return tuple(known[item] for item in operation_ids)

    def _require_locked(self, record_id: str, session_id: str) -> TuningRecord:
        record = self._records.get(record_id)
        if record is None:
            raise TuningRecordNotFound(record_id)
        if record.session_id != session_id:
            raise SessionConflict(record.session_id)
        return record

    # ------------------------------------------------------------ lifecycle

    def shutdown(self, *, timeout: float = 5.0) -> None:
        """Stop in-flight re-runs and wait for their leases to be released."""
        deadline = time.monotonic() + max(timeout, 0)
        with self._lock:
            active = tuple(
                record for record in self._records.values()
                if record.state not in TERMINAL_RECORD_STATES
            )
            workers = tuple(self._workers)
            for record in active:
                record.cancel_event.set()
        if self.executions is not None:
            for record in active:
                self.executions.cancel_parent(record.record_id)
        for worker in workers:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            worker.join(remaining)

    def _on_session_replaced(self, _session_id: str | None, _revision: int) -> None:
        with self._lock:
            self._records.clear()
        # A different notebook means a different kernel session, and the
        # execution service swaps the kernel to match. Forget the warm prefix
        # rather than carry a claim about a kernel that no longer exists.
        self._warm_kernel_session_id = None

    def _remember(self, record: TuningRecord) -> None:
        with self._lock:
            self._records[record.record_id] = record
            if len(self._records) <= MAX_RECORDS:
                return
            ordered = sorted(self._records.values(), key=lambda item: item.created_at)
            for victim in ordered[: len(self._records) - MAX_RECORDS]:
                if any(item.state == PENDING for item in victim.operations):
                    logger.info(
                        "Auto-kept unreviewed tuned changes for record %s: the "
                        "review backlog exceeded the retained history limit",
                        victim.record_id,
                    )
                self._records.pop(victim.record_id, None)

    def _set_state(self, record_id: str, state: str) -> None:
        with self._lock:
            record = self._records.get(record_id)
            if record is not None:
                record.state = state

    def _fail(
        self, record_id: str, code: str, message: str, details: dict[str, Any],
    ) -> None:
        with self._lock:
            record = self._records.get(record_id)
            if record is None:
                return
            record.state = "failed"
            if record.error is None:
                record.error = {
                    "code": code, "message": message,
                    "details": copy.deepcopy(details),
                }

    def _publish_notebook_updated(
        self, snapshot: NotebookSnapshot, owner: str,
    ) -> None:
        if self.events is None:
            return
        self.events.publish("notebook.updated", {
            "sessionId": snapshot.session_id, "revision": snapshot.revision,
            "ownerId": owner,
        })


def _cell_source(cell: dict[str, Any]) -> str:
    source = cell.get("source", "")
    return "".join(source) if isinstance(source, list) else source


def serialize_record(
    record: TuningRecord, stale: frozenset[str] = frozenset(),
) -> dict[str, Any]:
    """Wire shape for the panel and the review bar.

    ``stale`` is derived per request by `TuningApplyService.stale_cell_ids` and
    folded into the operation states here, exactly as the agent path does it.
    Without it the cell's stale branch never renders and a hand-edited tuned cell
    keeps a live-looking Undo that can only answer 409.

    ``origin`` leads deliberately: it is the field that decides whether the cell
    says "You tuned this cell" or "Agent changed this cell", and the two records
    are otherwise close enough to confuse.
    """
    return {
        "recordId": record.record_id,
        "origin": record.origin,
        "sessionId": record.session_id,
        "state": record.state,
        "knobs": list(record.knob_names),
        "baseRevision": record.base_revision,
        "appliedRevision": record.applied_revision,
        "executionOperationId": record.execution_operation_id,
        "executionStartIndex": record.execution_start_index,
        "reRanFromTop": record.extended_to_top,
        "changes": [
            {
                "cellId": change.cell_id,
                "previousSource": change.previous_source,
                "nextSource": change.next_source,
            }
            for change in record.changes
        ],
        "operations": [
            {
                "operationId": operation.operation_id,
                "cellId": operation.cell_id,
                "ordinal": operation.ordinal,
                "kind": operation.kind,
                # Only an undecided hunk can go stale; an accepted or rejected
                # one is a decision the user already made.
                "state": (
                    "stale"
                    if operation.state == PENDING and operation.cell_id in stale
                    else operation.state
                ),
            }
            for operation in record.operations
        ],
        "error": copy.deepcopy(record.error),
        "createdAt": record.created_at.isoformat(),
    }
