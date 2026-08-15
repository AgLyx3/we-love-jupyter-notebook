"""Applying tuned values: atomic, reviewable, undoable, and re-run for real.

The interesting failures here are all "half done" ones — a rewrite that fails on
the second of two cells, an undo that recomposes a cell the user has since
edited, a re-run that starts below the last cell the live kernel actually ran.
Each gets an explicit test, because each looks like a working feature right up
until it silently is not.
"""

from __future__ import annotations

import dataclasses
import json

import pytest

from backend.app.kernel_execution.kernel_session import KernelResult
from backend.app.kernel_execution.service import KernelExecutionService
from backend.app.notebook_document.models import RevisionConflict, SessionConflict
from backend.app.notebook_document.service import NotebookDocumentService
from backend.app.plot_tuning.apply import (
    MAX_RECORDS, ORIGIN_TUNE, TuningApplyService, TuningOperationConflict,
    TuningRewriteFailed, serialize_record,
)
from backend.app.plot_tuning.discovery import ChainCell, discover


SETUP = "import math\n"
UPSTREAM = "n_samples = 100\n"
KNOBS = "bins = 30\ngap = 1\nalpha = 0.5\n"
PLOT = "plot(n_samples, bins, alpha)\n"


class FakeKernel:
    """Enough of `KernelSession` for the execution service, and no more.

    Records every source it is handed, which is how the prefix-guard tests see
    *which* cells the live re-run actually replayed.
    """

    def __init__(self) -> None:
        self.sources: list[str] = []
        self.kernel_session_id = "kernel-1"
        self.busy_attempt_id = None
        self.startup_timeout = self.cell_timeout = self.recovery_timeout = 1
        self.max_output_items = 100
        self.max_output_bytes = 100_000
        self._restarts = 0
        self._started = False

    @property
    def status(self) -> str:
        return "idle" if self._started else "not_started"

    def execute(self, source: str, _attempt_id: str) -> KernelResult:
        self._started = True
        self.sources.append(source)
        return KernelResult([], len(self.sources), False)

    def restart(self) -> None:
        self._restarts += 1
        self.kernel_session_id = f"kernel-{self._restarts + 1}"

    def execution_correlation(self):
        return self.kernel_session_id, self.busy_attempt_id

    def interrupt(self) -> None:
        pass

    def interrupt_correlated(self, kernel_session_id, attempt_id) -> bool:
        return (
            kernel_session_id == self.kernel_session_id
            and attempt_id == self.busy_attempt_id
        )

    def restart_correlated(self, kernel_session_id, attempt_id, on_matched=None) -> bool:
        if not self.interrupt_correlated(kernel_session_id, attempt_id):
            return False
        if on_matched is not None:
            on_matched()
        self.restart()
        return True

    def shutdown(self) -> None:
        pass


def _payload(sources=(SETUP, UPSTREAM, KNOBS, PLOT)) -> bytes:
    cells = [
        {
            "cell_type": "code", "id": f"cell{index}", "metadata": {},
            "source": source, "execution_count": None, "outputs": [],
        }
        for index, source in enumerate(sources)
    ]
    return json.dumps({
        "cells": cells, "metadata": {}, "nbformat": 4, "nbformat_minor": 5,
    }).encode()


def _services(*, with_kernel: bool = False, sources=(SETUP, UPSTREAM, KNOBS, PLOT)):
    documents = NotebookDocumentService()
    snapshot = documents.import_notebook(_payload(sources))
    kernel = FakeKernel() if with_kernel else None
    executions = (
        KernelExecutionService(documents=documents, kernel=kernel)
        if with_kernel else None
    )
    service = TuningApplyService(documents=documents, executions=executions)
    return documents, service, snapshot, kernel


def _knob(snapshot, name: str, *, target: str = "cell3"):
    """Discover a real knob, so the tests exercise the offsets Stage 0 produces."""
    cells = tuple(
        ChainCell(cell_id=cell["id"], index=index, source=_source_of(cell))
        for index, cell in enumerate(snapshot.notebook["cells"])
    )
    found = discover(cells, target)
    return next(knob for knob in found.knobs if knob.name == name)


def _source_of(cell) -> str:
    source = cell.get("source", "")
    return "".join(source) if isinstance(source, list) else source


def _source(documents, cell_id: str) -> str:
    snapshot = documents.get_snapshot()
    cell = next(item for item in snapshot.notebook["cells"] if item["id"] == cell_id)
    return _source_of(cell)


# ------------------------------------------------------------------ apply

def test_applies_several_cells_as_one_edit():
    documents, service, snapshot, _kernel = _services()
    record = service.apply(
        session_id=snapshot.session_id, expected_revision=snapshot.revision,
        edits=[
            (_knob(snapshot, "n_samples"), 500),
            (_knob(snapshot, "bins"), 60),
        ],
        background=False,
    )
    # D9: two cells, one revision. Two applies would leave a moment where the
    # notebook holds half a tune.
    assert documents.get_snapshot().revision == snapshot.revision + 1
    assert _source(documents, "cell1") == "n_samples = 500\n"
    assert _source(documents, "cell2") == "bins = 60\ngap = 1\nalpha = 0.5\n"
    assert record.applied_revision == snapshot.revision + 1
    assert {item.cell_id for item in record.operations} == {"cell1", "cell2"}
    assert documents.coordinator.active_lease is None


def test_a_rewrite_failure_in_the_second_cell_leaves_both_untouched():
    # The atomicity that matters is not "the document apply is atomic" — it is
    # that a knob which cannot be rewritten aborts before anything is committed,
    # even though the cell before it rewrote perfectly well.
    documents, service, snapshot, _kernel = _services()
    good = _knob(snapshot, "n_samples")
    # A knob whose end offset falls inside its literal: the rewrite produces
    # syntactically valid source with the wrong value, which is exactly what
    # `rewrite`'s read-back check exists to catch.
    broken = dataclasses.replace(_knob(snapshot, "bins"), end_col_offset=8)

    with pytest.raises(TuningRewriteFailed):
        service.apply(
            session_id=snapshot.session_id, expected_revision=snapshot.revision,
            edits=[(good, 500), (broken, 77)], background=False,
        )

    assert documents.get_snapshot().revision == snapshot.revision
    assert _source(documents, "cell1") == UPSTREAM
    assert _source(documents, "cell2") == KNOBS
    assert documents.coordinator.active_lease is None


def test_a_stale_revision_is_rejected_before_anything_is_written():
    documents, service, snapshot, _kernel = _services()
    documents.update_cell_source(
        cell_id="cell0", source="import math  # touched\n",
        expected_revision=snapshot.revision, owner="user",
    )
    with pytest.raises(RevisionConflict):
        service.apply(
            session_id=snapshot.session_id, expected_revision=snapshot.revision,
            edits=[(_knob(snapshot, "bins"), 60)], background=False,
        )
    assert _source(documents, "cell2") == KNOBS
    assert documents.coordinator.active_lease is None


def test_a_different_session_is_rejected():
    documents, service, snapshot, _kernel = _services()
    with pytest.raises(SessionConflict):
        service.apply(
            session_id="not-this-notebook", expected_revision=snapshot.revision,
            edits=[(_knob(snapshot, "bins"), 60)], background=False,
        )
    assert documents.get_snapshot().revision == snapshot.revision


def test_applying_the_committed_values_changes_nothing():
    documents, service, snapshot, _kernel = _services()
    record = service.apply(
        session_id=snapshot.session_id, expected_revision=snapshot.revision,
        edits=[(_knob(snapshot, "bins"), 30)], background=False,
    )
    assert record.changes == ()
    assert record.operations == ()
    assert record.execution_operation_id is None
    assert documents.get_snapshot().revision == snapshot.revision


def test_the_record_carries_the_tune_origin():
    # The frontend labels the review bar from this and nothing else; without it
    # a tuned cell reads "Agent changed this cell", which is false.
    _documents, service, snapshot, _kernel = _services()
    record = service.apply(
        session_id=snapshot.session_id, expected_revision=snapshot.revision,
        edits=[(_knob(snapshot, "bins"), 60)], background=False,
    )
    assert record.origin == ORIGIN_TUNE == "tune"
    assert serialize_record(record)["origin"] == "tune"
    assert serialize_record(record)["knobs"] == ["bins"]


def test_history_is_bounded():
    _documents, service, snapshot, _kernel = _services()
    for _ in range(MAX_RECORDS + 3):
        # No-op applies still mint a record; the cap must not depend on the
        # tune having written anything.
        service.apply(
            session_id=snapshot.session_id, expected_revision=snapshot.revision,
            edits=[(_knob(snapshot, "bins"), 30)], background=False,
        )
    assert len(service.history_for_session(snapshot.session_id)) == MAX_RECORDS


# ------------------------------------------------------------------- undo

def _apply_two_hunks(service, snapshot):
    return service.apply(
        session_id=snapshot.session_id, expected_revision=snapshot.revision,
        edits=[(_knob(snapshot, "bins"), 60), (_knob(snapshot, "alpha"), 0.9)],
        background=False,
    )


def test_undoing_one_hunk_leaves_the_other_applied():
    documents, service, snapshot, _kernel = _services()
    record = _apply_two_hunks(service, snapshot)
    assert _source(documents, "cell2") == "bins = 60\ngap = 1\nalpha = 0.9\n"
    assert len(record.operations) == 2

    bins_hunk = record.operations[0]
    record = service.reject_operations(
        record.record_id, [bins_hunk.operation_id],
        session_id=snapshot.session_id, expected_revision=record.applied_revision,
    )
    assert _source(documents, "cell2") == "bins = 30\ngap = 1\nalpha = 0.9\n"

    record = service.reject_operations(
        record.record_id, [record.operations[1].operation_id],
        session_id=snapshot.session_id, expected_revision=record.applied_revision,
    )
    # Every hunk rejected reproduces the pre-tune source exactly, character for
    # character — that is `compose`'s contract and the whole reason undo needs
    # no inverse patch.
    assert _source(documents, "cell2") == KNOBS


def test_undo_restores_every_edited_cell():
    documents, service, snapshot, _kernel = _services()
    record = service.apply(
        session_id=snapshot.session_id, expected_revision=snapshot.revision,
        edits=[(_knob(snapshot, "n_samples"), 500), (_knob(snapshot, "bins"), 60)],
        background=False,
    )
    record = service.undo(
        record.record_id, session_id=snapshot.session_id,
        expected_revision=record.applied_revision,
    )
    assert _source(documents, "cell1") == UPSTREAM
    assert _source(documents, "cell2") == KNOBS
    assert all(item.state == "rejected" for item in record.operations)


def test_undo_also_undoes_hunks_the_user_kept():
    # "Undo this tune" means all of it. Reusing the unreviewed-only selection
    # would silently keep whichever hunk the user had already pressed Keep on.
    documents, service, snapshot, _kernel = _services()
    record = _apply_two_hunks(service, snapshot)
    record = service.accept_operations(
        record.record_id, [record.operations[0].operation_id],
        session_id=snapshot.session_id,
    )
    assert record.operations[0].state == "accepted"

    service.undo(
        record.record_id, session_id=snapshot.session_id,
        expected_revision=record.applied_revision,
    )
    assert _source(documents, "cell2") == KNOBS


def test_undoing_twice_is_harmless():
    documents, service, snapshot, _kernel = _services()
    record = _apply_two_hunks(service, snapshot)
    record = service.undo(
        record.record_id, session_id=snapshot.session_id,
        expected_revision=record.applied_revision,
    )
    settled_revision = documents.get_snapshot().revision
    service.undo(
        record.record_id, session_id=snapshot.session_id,
        expected_revision=record.applied_revision,
    )
    assert documents.get_snapshot().revision == settled_revision
    assert _source(documents, "cell2") == KNOBS


def test_keeping_a_hunk_that_was_already_undone_conflicts():
    _documents, service, snapshot, _kernel = _services()
    record = _apply_two_hunks(service, snapshot)
    record = service.reject_operations(
        record.record_id, [record.operations[0].operation_id],
        session_id=snapshot.session_id, expected_revision=record.applied_revision,
    )
    with pytest.raises(TuningOperationConflict):
        service.accept_operations(
            record.record_id, [record.operations[0].operation_id],
            session_id=snapshot.session_id,
        )


def test_a_hand_edited_cell_refuses_a_targeted_undo():
    documents, service, snapshot, _kernel = _services()
    record = _apply_two_hunks(service, snapshot)
    edited = documents.update_cell_source(
        cell_id="cell2", source="bins = 60\ngap = 2\nalpha = 0.9\n",
        expected_revision=record.applied_revision, owner="user",
    )
    with pytest.raises(TuningOperationConflict):
        service.reject_operations(
            record.record_id, [record.operations[0].operation_id],
            session_id=snapshot.session_id, expected_revision=edited.revision,
        )
    assert service.stale_cell_ids(record) == frozenset({"cell2"})


def test_undo_all_skips_the_hand_edited_cell_and_undoes_the_rest():
    documents, service, snapshot, _kernel = _services()
    record = service.apply(
        session_id=snapshot.session_id, expected_revision=snapshot.revision,
        edits=[(_knob(snapshot, "n_samples"), 500), (_knob(snapshot, "bins"), 60)],
        background=False,
    )
    edited = documents.update_cell_source(
        cell_id="cell2", source="bins = 60\ngap = 2\nalpha = 0.5\n",
        expected_revision=record.applied_revision, owner="user",
    )
    service.reject_operations(
        record.record_id, None, session_id=snapshot.session_id,
        expected_revision=edited.revision,
    )
    assert _source(documents, "cell1") == UPSTREAM
    assert _source(documents, "cell2") == "bins = 60\ngap = 2\nalpha = 0.5\n"


# ------------------------------------------- live re-run and prefix guard

def test_apply_re_runs_the_live_chain():
    # D8: the code, the kernel and the outputs must agree afterwards. Writing
    # the literal alone would leave the kernel holding the old value.
    documents, service, snapshot, kernel = _services(with_kernel=True)
    record = service.apply(
        session_id=snapshot.session_id, expected_revision=snapshot.revision,
        edits=[(_knob(snapshot, "bins"), 60)], background=False,
    )
    assert record.state == "completed"
    assert record.execution_operation_id is not None
    assert "bins = 60" in "".join(kernel.sources)
    assert PLOT in kernel.sources
    executed = next(
        cell for cell in documents.get_snapshot().notebook["cells"]
        if cell["id"] == "cell2"
    )
    assert executed["execution_count"] is not None


def test_a_cold_kernel_re_runs_from_the_top():
    # §3.6, the subtle one. "Re-run from the earliest changed cell" assumes the
    # live kernel already ran everything above it. A fresh kernel has not, and
    # without this guard the re-run dies on a NameError that reads as the
    # feature being broken.
    _documents, service, snapshot, kernel = _services(with_kernel=True)
    record = service.apply(
        session_id=snapshot.session_id, expected_revision=snapshot.revision,
        edits=[(_knob(snapshot, "bins"), 60)], background=False,
    )
    assert record.extended_to_top is True
    assert record.execution_start_index == 0
    assert kernel.sources[0] == SETUP
    assert kernel.sources[1] == UPSTREAM


def test_a_warm_kernel_re_runs_only_from_the_edited_cell():
    _documents, service, snapshot, kernel = _services(with_kernel=True)
    first = service.apply(
        session_id=snapshot.session_id, expected_revision=snapshot.revision,
        edits=[(_knob(snapshot, "n_samples"), 500)], background=False,
    )
    assert first.extended_to_top is True
    warm_from = len(kernel.sources)

    snapshot = service.documents.get_snapshot()
    second = service.apply(
        session_id=snapshot.session_id, expected_revision=snapshot.revision,
        edits=[(_knob(snapshot, "bins"), 60)], background=False,
    )
    assert second.extended_to_top is False
    assert second.execution_start_index == 2
    # The expensive upstream cells are not replayed a second time: that is the
    # whole point of not simply always running from the top.
    assert kernel.sources[warm_from].startswith("bins = 60")


def test_a_kernel_restart_makes_the_prefix_cold_again():
    _documents, service, snapshot, kernel = _services(with_kernel=True)
    service.apply(
        session_id=snapshot.session_id, expected_revision=snapshot.revision,
        edits=[(_knob(snapshot, "n_samples"), 500)], background=False,
    )
    service.executions.restart(kernel.kernel_session_id)
    restarted_from = len(kernel.sources)

    snapshot = service.documents.get_snapshot()
    record = service.apply(
        session_id=snapshot.session_id, expected_revision=snapshot.revision,
        edits=[(_knob(snapshot, "bins"), 60)], background=False,
    )
    # The document's execution counts still say every cell has run; only the
    # kernel session id knows better, which is why the guard keys off that.
    assert record.extended_to_top is True
    assert kernel.sources[restarted_from] == SETUP
