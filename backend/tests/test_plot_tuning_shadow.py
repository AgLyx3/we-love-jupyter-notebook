"""The preview kernel: what it runs, what it caches, and what it must not touch.

Most of this runs against a fake kernel, because a real one costs seconds per
test and the behaviour under test here — replay order, where a re-run starts,
cache identity, teardown — is all bookkeeping. One test at the bottom drives a
real `KernelSession` over the committed fixture notebook and asserts a real PNG
comes back, so the fake cannot quietly drift away from the thing it stands in
for.

The load-bearing test is `test_preview_mutates_neither_the_document_nor_the_live
_kernel`. Everything else is a feature; that one is the promise.
"""

from __future__ import annotations

import copy
import json
import pathlib

import pytest

from backend.app.kernel_execution.kernel_session import KernelResult, KernelSession
from backend.app.kernel_execution.models import KernelCellTimeout
from backend.app.kernel_execution.service import KernelExecutionService
from backend.app.notebook_document.mutation_coordinator import MutationCoordinator
from backend.app.notebook_document.service import NotebookDocumentService
from backend.app.plot_tuning.discovery import ChainCell
from backend.app.plot_tuning.shadow import (
    DEFAULT_IDLE_TIMEOUT_SECONDS, ShadowBlocked, ShadowClosed, ShadowError,
    ShadowNotReady, ShadowSession, UnknownKnob,
)

FIXTURE = pathlib.Path(__file__).resolve().parents[2] / "examples" / "plot-tuning.ipynb"


class FakeClock:
    """A clock the test winds by hand, for both durations and idle time."""

    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeKernel:
    """Stands in for `KernelSession`, recording exactly what it was asked to run.

    Its output echoes the source, which is what lets a test assert *which*
    version of a cell produced the picture it is looking at.
    """

    def __init__(
        self, *, timer: FakeClock | None = None, seconds_per_cell: float = 0.0,
    ) -> None:
        self.sources: list[str] = []
        self.attempt_ids: list[str] = []
        self.shutdowns = 0
        self.interrupts = 0
        #: Substrings that make a cell report a Python-level error.
        self.error_on: tuple[str, ...] = ()
        #: Substrings that make the kernel itself blow up (timeout, output limit).
        self.raise_on: tuple[str, ...] = ()
        self._timer = timer
        self._seconds_per_cell = seconds_per_cell

    def execute(self, source: str, attempt_id: str) -> KernelResult:
        self.sources.append(source)
        self.attempt_ids.append(attempt_id)
        if self._timer is not None:
            self._timer.advance(self._seconds_per_cell)
        if any(marker in source for marker in self.raise_on):
            raise KernelCellTimeout(recovered=True)
        if any(marker in source for marker in self.error_on):
            return KernelResult(
                [{"output_type": "error", "ename": "ValueError",
                  "evalue": source.strip(), "traceback": ["ValueError"]}],
                len(self.sources), True,
            )
        return KernelResult(
            [{"output_type": "display_data",
              "data": {"text/plain": source}, "metadata": {}}],
            len(self.sources), False,
        )

    def interrupt(self) -> None:
        self.interrupts += 1

    def shutdown(self) -> None:
        self.shutdowns += 1


SETUP = "SCALE = 2\n"
MIDDLE = "BINS = 30\nvalues = [i * SCALE for i in range(BINS)]\n"
PLOT = "TITLE = 'hist'\nprint(TITLE, values)\n"


def chain(setup: str = SETUP, middle: str = MIDDLE, plot: str = PLOT):
    return (
        ChainCell(cell_id="setup", index=0, source=setup),
        ChainCell(cell_id="middle", index=1, source=middle),
        ChainCell(cell_id="plot", index=2, source=plot),
    )


def warmed(kernel=None, **kwargs):
    """A shadow that has already replayed the three-cell chain."""
    session = ShadowSession(
        target_cell_id="plot", document_revision=7,
        kernel=kernel if kernel is not None else FakeKernel(), **kwargs,
    )
    session.warm(chain())
    return session


# ---- warm-up ------------------------------------------------------------


def test_warm_replays_the_whole_chain_from_the_top_in_order():
    kernel = FakeKernel()
    session = ShadowSession(
        target_cell_id="plot", document_revision=1, kernel=kernel,
    )

    result = session.warm(chain())

    assert kernel.sources == [SETUP, MIDDLE, PLOT]
    assert [timing.cell_id for timing in result.timings] == ["setup", "middle", "plot"]
    assert session.is_ready


def test_warm_records_a_duration_and_a_position_for_every_cell():
    # The panel says "replaying cell 2 of 3 — 0.5s", so both halves of that
    # sentence have to survive out of the replay loop.
    timer = FakeClock()
    kernel = FakeKernel(timer=timer, seconds_per_cell=0.5)
    session = ShadowSession(
        target_cell_id="plot", document_revision=1, kernel=kernel, timer=timer,
    )

    result = session.warm(chain())

    assert [(timing.ordinal, timing.total) for timing in result.timings] == [
        (1, 3), (2, 3), (3, 3),
    ]
    assert [timing.seconds for timing in result.timings] == [0.5, 0.5, 0.5]
    assert result.total_seconds == 1.5
    assert session.timings == result.timings


def test_warm_reports_each_finished_cell_to_a_progress_callback():
    seen = []
    session = ShadowSession(
        target_cell_id="plot", document_revision=1, kernel=FakeKernel(),
    )

    session.warm(chain(), on_progress=seen.append)

    assert [timing.cell_id for timing in seen] == ["setup", "middle", "plot"]


def test_the_cell_being_replayed_is_visible_while_it_runs():
    # A timing only exists once its cell has finished, which is exactly when the
    # user has stopped caring about it. A poll mid-replay has to see the cell
    # that is currently costing the wait.
    timer = FakeClock()
    observed = []

    class ObservingKernel(FakeKernel):
        def execute(self, source, attempt_id):
            timer.advance(2.0)
            observed.append(session.progress)
            return super().execute(source, attempt_id)

    session = ShadowSession(
        target_cell_id="plot", document_revision=1,
        kernel=ObservingKernel(), timer=timer,
    )
    session.warm(chain())

    assert [(item.ordinal, item.total, item.cell_id) for item in observed] == [
        (1, 3, "setup"), (2, 3, "middle"), (3, 3, "plot"),
    ]
    assert [item.elapsed_seconds for item in observed] == [2.0, 2.0, 2.0]
    assert [len(item.completed) for item in observed] == [0, 1, 2]
    assert session.progress is None


def test_warm_offers_the_knobs_it_found_in_the_chain():
    session = ShadowSession(
        target_cell_id="plot", document_revision=1, kernel=FakeKernel(),
    )

    result = session.warm(chain())

    assert {knob.name: knob.value for knob in result.knobs} == {
        "SCALE": 2, "BINS": 30, "TITLE": "hist",
    }
    assert session.knobs == result.knobs


def test_warm_returns_the_target_cells_committed_outputs():
    session = ShadowSession(
        target_cell_id="plot", document_revision=1, kernel=FakeKernel(),
    )

    result = session.warm(chain())

    assert result.outputs[0]["data"]["text/plain"] == PLOT
    assert not result.failed


def test_a_chain_that_cannot_be_analysed_never_spawns_a_replay():
    kernel = FakeKernel()
    session = ShadowSession(
        target_cell_id="plot", document_revision=1, kernel=kernel,
    )

    with pytest.raises(ShadowBlocked):
        session.warm(chain(setup="%%bash\necho hello\n"))

    assert kernel.sources == []


def test_a_failing_replay_is_reported_rather_than_raised_and_refuses_preview():
    # The notebook is broken at its own committed values. That is worth showing
    # the user with the traceback, not turning into an exception every caller
    # has to remember to catch.
    kernel = FakeKernel()
    kernel.error_on = ("values = ",)
    session = ShadowSession(
        target_cell_id="plot", document_revision=1, kernel=kernel,
    )

    result = session.warm(chain())

    assert result.failed_cell_id == "middle"
    assert result.outputs[0]["output_type"] == "error"
    assert kernel.sources == [SETUP, MIDDLE]  # stopped, did not run the plot
    assert session.is_alive and not session.is_ready
    with pytest.raises(ShadowNotReady):
        session.preview({"BINS": 10})


# ---- preview ------------------------------------------------------------


def test_preview_returns_only_the_target_cells_outputs():
    # Two cells run — the rewritten one and the plot — but only the plot's
    # picture comes back. The fake kernel echoes the source it ran, so an
    # implementation that returned everything would show the middle cell here.
    session = warmed()

    result = session.preview({"BINS": 12})

    assert result.executed_cell_ids == ("middle", "plot")
    assert [output["data"]["text/plain"] for output in result.outputs] == [PLOT]


def test_preview_rewrites_only_the_shadows_own_copy_of_the_source():
    cells = chain()
    kernel = FakeKernel()
    session = ShadowSession(
        target_cell_id="plot", document_revision=1, kernel=kernel,
    )
    session.warm(cells)

    session.preview({"BINS": 12})

    assert "BINS = 12" in kernel.sources[-2]
    # The caller's cells are untouched; nothing outside this session has moved.
    assert [cell.source for cell in cells] == [SETUP, MIDDLE, PLOT]
    assert session.preview({}).outputs[0]["data"]["text/plain"] == PLOT


def test_preview_starts_at_the_earliest_rewritten_cell():
    kernel = FakeKernel()
    session = warmed(kernel)
    kernel.sources.clear()

    session.preview({"SCALE": 5})

    assert [source.split(" =")[0] for source in kernel.sources] == [
        "SCALE", "BINS", "TITLE",
    ]


def test_a_knob_in_the_target_cell_alone_reruns_only_that_cell():
    kernel = FakeKernel()
    session = warmed(kernel)
    kernel.sources.clear()

    result = session.preview({"TITLE": "other"})

    assert result.executed_cell_ids == ("plot",)
    assert len(kernel.sources) == 1


def test_a_knob_returned_to_its_committed_value_replays_the_cell_that_holds_it():
    # The subtle one. After tuning `SCALE`, the kernel holds SCALE = 5. Tuning
    # only `TITLE` next means `SCALE` is no longer *rewritten*, so an
    # earliest-rewritten-cell rule would start at the plot cell and render a
    # picture for SCALE = 5 while the rail says 2.
    kernel = FakeKernel()
    session = warmed(kernel)
    session.preview({"SCALE": 5})
    kernel.sources.clear()

    result = session.preview({"TITLE": "other"})

    assert result.executed_cell_ids == ("setup", "middle", "plot")
    assert kernel.sources[0] == SETUP


def test_preview_accepts_a_partial_map_and_reports_the_full_knob_tuple():
    session = warmed()

    result = session.preview({"BINS": 12})

    assert result.values == (("BINS", 12), ("SCALE", 2), ("TITLE", "hist"))


def test_an_unknown_name_is_refused_rather_than_silently_ignored():
    session = warmed()

    with pytest.raises(UnknownKnob):
        session.preview({"BINZ": 12})


def test_a_value_of_the_wrong_shape_is_refused_without_touching_the_kernel():
    kernel = FakeKernel()
    session = warmed(kernel)
    kernel.sources.clear()

    with pytest.raises(ShadowError):
        session.preview({"BINS": "twelve"})

    assert kernel.sources == []


def test_a_json_float_for_an_int_knob_is_written_back_as_an_int():
    # Values arrive over JSON, where 12 and 12.0 are the same thing. `bins=12.0`
    # is not.
    kernel = FakeKernel()
    session = warmed(kernel)

    result = session.preview({"BINS": 12.0})

    assert "BINS = 12\n" in kernel.sources[-2]
    assert result.values == (("BINS", 12), ("SCALE", 2), ("TITLE", "hist"))


# ---- cache --------------------------------------------------------------


def test_a_revisited_value_is_served_from_cache_without_re_executing():
    kernel = FakeKernel()
    session = warmed(kernel)
    session.preview({"BINS": 12})
    executions = len(kernel.sources)

    again = session.preview({"BINS": 12})

    assert len(kernel.sources) == executions
    assert again.cached
    assert again.outputs == session.preview({"BINS": 12}).outputs


def test_the_cache_key_does_not_depend_on_the_order_knobs_were_set():
    kernel = FakeKernel()
    session = warmed(kernel)
    session.preview({"BINS": 12, "SCALE": 5})
    executions = len(kernel.sources)

    again = session.preview({"SCALE": 5, "BINS": 12})

    assert again.cached
    assert len(kernel.sources) == executions


def test_the_cache_key_does_not_depend_on_which_knobs_were_named():
    # `{"BINS": 30}` and `{}` describe the same state when 30 is committed.
    kernel = FakeKernel()
    session = warmed(kernel)
    executions = len(kernel.sources)

    assert session.preview({"BINS": 30}).cached
    assert session.preview({}).cached
    assert len(kernel.sources) == executions


def test_the_committed_values_are_cached_by_the_warm_up_itself():
    # The replay already rendered the target at its committed values, so putting
    # every knob back must not cost a second chain run.
    kernel = FakeKernel()
    session = warmed(kernel)
    session.preview({"BINS": 12})
    executions = len(kernel.sources)

    back = session.preview({"BINS": 30})

    assert back.cached
    assert back.outputs[0]["data"]["text/plain"] == PLOT
    assert len(kernel.sources) == executions


# ---- failure keeps the session ------------------------------------------


def test_a_preview_that_errors_keeps_the_knobs_and_the_shadow_alive():
    kernel = FakeKernel()
    session = warmed(kernel)
    kernel.error_on = ("BINS = 0",)

    failed = session.preview({"BINS": 0})

    assert failed.failed and failed.failed_cell_id == "middle"
    assert failed.outputs[0]["output_type"] == "error"
    assert session.is_alive and session.is_ready
    assert session.knobs  # the rail survives the failure
    # And the next value still previews normally.
    assert not session.preview({"BINS": 12}).failed


def test_a_preview_that_trips_the_kernel_itself_survives_and_is_not_cached():
    # A timeout says nothing about the values that were asked for, so retrying
    # them has to be allowed to succeed — unlike a traceback, which is a
    # property of the values and worth remembering.
    kernel = FakeKernel()
    session = warmed(kernel)
    kernel.raise_on = ("BINS = 99",)

    failed = session.preview({"BINS": 99})

    assert failed.failed and not failed.cached
    assert failed.outputs[0]["ename"] == "KernelCellTimeout"
    assert session.is_alive and session.is_ready

    kernel.raise_on = ()
    retried = session.preview({"BINS": 99})
    assert not retried.failed and not retried.cached


def test_a_deterministic_failure_is_cached_like_any_other_render():
    kernel = FakeKernel()
    session = warmed(kernel)
    kernel.error_on = ("BINS = 0",)
    session.preview({"BINS": 0})
    executions = len(kernel.sources)

    again = session.preview({"BINS": 0})

    assert again.cached and again.failed
    assert len(kernel.sources) == executions


def test_the_next_preview_reruns_everything_after_a_failed_cell():
    # The failed cell was left half-executed and the cells after it never ran,
    # so nothing downstream of the failure can be trusted, even if the only
    # knob that moved lives in the target cell.
    kernel = FakeKernel()
    session = warmed(kernel)
    kernel.error_on = ("BINS = 0",)
    session.preview({"BINS": 0})
    kernel.error_on = ()
    kernel.sources.clear()

    result = session.preview({"TITLE": "other"})

    assert result.executed_cell_ids == ("middle", "plot")


# ---- lifecycle ----------------------------------------------------------


def test_close_shuts_the_kernel_down_and_refuses_further_previews():
    kernel = FakeKernel()
    session = warmed(kernel)

    session.close()

    assert kernel.shutdowns == 1
    assert not session.is_alive and not session.is_ready
    with pytest.raises(ShadowClosed):
        session.preview({"BINS": 12})
    with pytest.raises(ShadowClosed):
        session.warm(chain())


def test_close_is_idempotent():
    kernel = FakeKernel()
    session = warmed(kernel)

    session.close()
    session.close()

    assert kernel.shutdowns == 1


def test_close_drops_the_cached_previews_with_the_kernel():
    kernel = FakeKernel()
    session = warmed(kernel)
    session.preview({"BINS": 12})
    assert session.cached_preview_count

    session.close()

    assert session.cached_preview_count == 0


def test_cancel_interrupts_the_replay_without_closing_the_session():
    kernel = FakeKernel()
    session = warmed(kernel)

    session.cancel()

    assert kernel.interrupts == 1
    assert session.is_alive


def test_a_document_revision_change_invalidates_the_shadow():
    kernel = FakeKernel()
    session = warmed(kernel)  # warmed at revision 7

    assert session.invalidate_if_stale(7) is False
    assert session.is_alive

    assert session.invalidate_if_stale(8) is True
    assert kernel.shutdowns == 1
    with pytest.raises(ShadowClosed):
        session.preview({"BINS": 12})


def test_the_idle_timeout_is_a_setting_not_a_hardcoded_number():
    clock = FakeClock()
    kernel = FakeKernel()
    session = warmed(kernel, clock=clock, idle_timeout_seconds=60)

    clock.advance(59)
    assert session.close_if_idle() is False

    clock.advance(2)
    assert session.close_if_idle() is True
    assert kernel.shutdowns == 1


def test_previewing_keeps_an_idle_shadow_alive():
    clock = FakeClock()
    session = warmed(clock=clock, idle_timeout_seconds=60)

    clock.advance(50)
    session.preview({"BINS": 12})
    clock.advance(50)

    assert session.close_if_idle() is False
    assert session.idle_seconds == 50


def test_the_default_idle_timeout_is_about_ten_minutes():
    assert DEFAULT_IDLE_TIMEOUT_SECONDS == 600
    assert ShadowSession(
        target_cell_id="plot", document_revision=1, kernel=FakeKernel(),
    ).idle_timeout_seconds == DEFAULT_IDLE_TIMEOUT_SECONDS


# ---- the load-bearing test ----------------------------------------------


class RecordingCoordinator(MutationCoordinator):
    """A mutation coordinator that remembers every lease anyone took."""

    def __init__(self) -> None:
        super().__init__()
        self.acquired: list[str] = []

    def acquire(self, *, operation_type: str, operation_id: str):
        self.acquired.append(operation_type)
        return super().acquire(
            operation_type=operation_type, operation_id=operation_id,
        )


def fixture_chain():
    notebook = json.loads(FIXTURE.read_text())
    return tuple(
        ChainCell(cell_id=cell["id"], index=index, source="".join(cell["source"]))
        for index, cell in enumerate(notebook["cells"])
        if cell["cell_type"] == "code"
    )


def test_preview_mutates_neither_the_document_nor_the_live_kernel():
    """The promise: dragging a knob is free.

    Every committing path in this app is visible from here — the mutation lease,
    the document revision, the notebook JSON, and the live kernel. A shadow that
    reached for `KernelExecutionService` would take a lease, bump the revision
    and write outputs into the cell; a shadow that wrote source back would change
    the JSON. All four are pinned across a warm plus three previews.
    """
    coordinator = RecordingCoordinator()
    documents = NotebookDocumentService(coordinator=coordinator)
    documents.import_notebook(FIXTURE.read_bytes(), filename="plot-tuning.ipynb")
    live_kernel = FakeKernel()
    live = KernelExecutionService(documents=documents, kernel=live_kernel)

    before = documents.get_snapshot()
    notebook_before = copy.deepcopy(before.notebook)
    coordinator.acquired.clear()

    session = ShadowSession(
        target_cell_id="plot", document_revision=before.revision,
        kernel=FakeKernel(),
    )
    session.warm(fixture_chain())
    session.preview({"BINS": 12})
    session.preview({"N_POINTS": 50, "COLOR": "crimson"})
    session.preview({"BINS": 12})

    after = documents.get_snapshot()
    assert after.revision == before.revision
    assert after.notebook == notebook_before
    assert coordinator.acquired == []
    assert coordinator.active_lease is None
    assert live_kernel.sources == []
    assert live.kernel is live_kernel  # the live kernel was never replaced


def test_the_shadow_survives_a_restart_of_the_live_kernel():
    # It replayed the chain from the top, so it never depended on live kernel
    # state. Tearing it down here would cost the user a full re-warm for nothing.
    documents = NotebookDocumentService()
    documents.import_notebook(FIXTURE.read_bytes(), filename="plot-tuning.ipynb")
    live_kernel = FakeKernel()
    live_kernel.restart = lambda: None
    KernelExecutionService(documents=documents, kernel=live_kernel)

    shadow_kernel = FakeKernel()
    session = ShadowSession(
        target_cell_id="plot", document_revision=documents.get_snapshot().revision,
        kernel=shadow_kernel,
    )
    session.warm(fixture_chain())

    live_kernel.restart()

    assert session.is_alive and session.is_ready
    assert shadow_kernel.shutdowns == 0
    assert session.preview({"BINS": 12}).outputs


# ---- against a real kernel ----------------------------------------------


def test_a_real_kernel_renders_a_real_png_for_a_tuned_value():
    """The fake kernel cannot tell us the plumbing works. This can.

    A real `KernelSession`, the committed fixture notebook, matplotlib on the
    Agg backend: the preview has to come back as an actual `image/png` that
    differs from the committed render, having gone nowhere near a document.
    """
    session = ShadowSession(
        target_cell_id="plot", document_revision=1,
        kernel=KernelSession(startup_timeout=60, cell_timeout=120),
    )
    try:
        warm = session.warm(fixture_chain())
        assert warm.failed_cell_id is None, warm.outputs
        committed = _png(warm.outputs)
        assert committed, warm.outputs
        assert all(timing.seconds > 0 for timing in warm.timings)

        tuned = session.preview({"BINS": 5, "COLOR": "crimson"})

        assert not tuned.failed, tuned.outputs
        assert tuned.executed_cell_ids == ("params-cell", "generate", "plot")
        assert _png(tuned.outputs)
        assert _png(tuned.outputs) != committed

        # Back to the committed values: same picture, no execution at all.
        restored = session.preview({})
        assert restored.cached
        assert _png(restored.outputs) == committed

        # And a value the notebook cannot survive keeps the session usable.
        # `bins=0` is the design's own example of a knob that crashes the cell.
        broken = session.preview({"BINS": 0})
        assert broken.failed, broken.outputs
        assert broken.outputs[0]["output_type"] == "error"
        assert session.is_ready
        assert _png(session.preview({"BINS": 40}).outputs)
    finally:
        session.close()


def _png(outputs) -> str | None:
    for output in outputs:
        data = output.get("data", {})
        if "image/png" in data:
            return data["image/png"]
    return None
