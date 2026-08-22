"""A second kernel that renders previews and commits nothing.

Every other execution path in this app is cell-bound and committing: it takes a
mutation lease, runs a cell the document owns, and writes the outputs back into
the document. The shadow is the exact inverse — **execute, return outputs, commit
nothing, touch no document** — because the product rule is that dragging a knob
is free and only Apply costs anything.

That inversion is why this module talks to `KernelSession` directly rather than
to `KernelExecutionService`. The service exists to commit; using it here would
quietly give the panel the power the whole design says it must not have. The
shadow therefore owns a private kernel, keeps a private copy of the chain
source, and has no reference to the document service at all. A preview cannot
mutate the notebook because there is nothing here to mutate it with.

Three pieces of state make the rest of the file make sense:

* **committed sources** — the chain as the notebook has it. Never changed here.
* **requested sources** — committed source with the knob literals rewritten, for
  one particular set of values. Built per preview and thrown away.
* **resident sources** — what the shadow kernel actually last ran, and how far it
  got. This is what decides where a re-run has to start; see `_start_position`.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from threading import RLock
from typing import Any
from uuid import uuid4

from ..kernel_execution.kernel_session import KernelSession
from .discovery import ChainCell, Knob, Rejection, discover
from .rewrite import rewrite_chain

#: How long a shadow may sit unused before its kernel is worth reclaiming. A
#: shadow duplicates the notebook's resident data, so this is a memory decision
#: rather than a correctness one — it is a constructor default so the owning
#: service (and tests) can pick a different one without touching the logic.
DEFAULT_IDLE_TIMEOUT_SECONDS = 10 * 60


class ShadowError(RuntimeError):
    """Base for the ways a caller can misuse a shadow session."""


class ShadowClosed(ShadowError):
    """The shadow's kernel is gone; warm a new one."""


class ShadowNotReady(ShadowError):
    """Preview was asked for before a successful warm-up."""


class ShadowBlocked(ShadowError):
    """The chain could not be analysed, so there is nothing to tune."""


class UnknownKnob(ShadowError):
    """A preview named a value the scan never offered."""


@dataclass(frozen=True)
class CellTiming:
    """How long one chain cell took to replay.

    Kept per cell rather than as a single total because the user-facing message
    is "replaying cell 2 of 7 — 47s". A warm-up that takes two minutes is
    tolerable when you can see which cell is eating them and that it is moving;
    the same two minutes behind an undifferentiated spinner reads as a hang.
    """

    cell_id: str
    #: 1-based position in the chain, i.e. the "2" and "7" of "cell 2 of 7".
    ordinal: int
    total: int
    seconds: float
    failed: bool = False


@dataclass(frozen=True)
class WarmProgress:
    """The cell being replayed right now, for a client that polls mid-warm.

    `CellTiming` only exists once a cell has finished, which is precisely when
    the user stops needing to be told about it. This carries the in-flight one.
    """

    cell_id: str
    ordinal: int
    total: int
    elapsed_seconds: float
    completed: tuple[CellTiming, ...]


@dataclass(frozen=True)
class _InFlight:
    """The cell currently being replayed. `started_at` is on the timer clock."""

    cell_id: str
    ordinal: int
    total: int
    started_at: float
    completed: tuple[CellTiming, ...]


@dataclass(frozen=True)
class WarmResult:
    timings: tuple[CellTiming, ...]
    knobs: tuple[Knob, ...]
    rejections: tuple[Rejection, ...]
    #: The target cell's outputs at the committed values — the "before" picture.
    #: Empty when the replay failed before reaching the target.
    outputs: tuple[dict[str, Any], ...] = ()
    #: Set when a chain cell errored during replay. The notebook itself is
    #: broken at these values, so this is reported rather than raised: the panel
    #: shows the traceback, and `preview` stays refused until a re-warm.
    failed_cell_id: str | None = None

    @property
    def failed(self) -> bool:
        return self.failed_cell_id is not None

    @property
    def total_seconds(self) -> float:
        return sum(timing.seconds for timing in self.timings)


@dataclass(frozen=True)
class PreviewResult:
    """One rendered preview: only the target cell's outputs, never persisted."""

    outputs: tuple[dict[str, Any], ...]
    #: The complete knob tuple this render corresponds to — every knob, not just
    #: the changed ones. It is also the cache key, so a client can tell whether
    #: what it is looking at still matches the rail without re-deriving it.
    values: tuple[tuple[str, Any], ...]
    executed_cell_ids: tuple[str, ...] = ()
    seconds: float = 0.0
    cached: bool = False
    failed: bool = False
    failed_cell_id: str | None = None


class ShadowSession:
    """One preview kernel, replaying one chain, for one plot cell.

    One of these exists at a time (see the owning service). It is created for a
    specific document revision and is invalidated when that moves, because it
    replayed source that no longer exists. It deliberately does **not** care
    about the live kernel restarting: it replayed the chain from the top, so it
    never depended on live kernel state in the first place.
    """

    def __init__(
        self, *, target_cell_id: str, document_revision: int,
        kernel: KernelSession | None = None,
        idle_timeout_seconds: float = DEFAULT_IDLE_TIMEOUT_SECONDS,
        clock: Callable[[], float] = time.monotonic,
        timer: Callable[[], float] = time.perf_counter,
    ) -> None:
        self.shadow_id = uuid4().hex
        self.target_cell_id = target_cell_id
        self.document_revision = document_revision
        self.idle_timeout_seconds = idle_timeout_seconds
        # Two clocks on purpose: `perf_counter` for durations we show the user,
        # `monotonic` for the idle deadline. Separating them lets a test wind
        # idle time forward without also faking every measured duration.
        self._clock = clock
        self._timer = timer
        self._kernel = kernel if kernel is not None else KernelSession()

        # Kernel work is serialized by `_run_lock`; the mutable bookkeeping is
        # guarded by `_state_lock`, which is only ever held for a few
        # statements. Splitting them is what lets `close()` land while a warm-up
        # is mid-replay — teardown that has to wait for a seven-minute replay to
        # finish is not teardown.
        self._run_lock = RLock()
        self._state_lock = RLock()

        self._closed = False
        self._ready = False
        self._chain: tuple[ChainCell, ...] = ()
        self._target_position = -1
        self._committed_sources: dict[str, str] = {}
        self._committed_values: dict[str, Any] = {}
        self._knobs: tuple[Knob, ...] = ()
        self._rejections: tuple[Rejection, ...] = ()
        self._resident_sources: dict[str, str] = {}
        #: Chain position of the last cell the kernel ran to completion. Cells
        #: after it hold stale or half-built state and must be re-run.
        self._resident_upto = -1
        self._cache: dict[tuple[tuple[str, Any], ...], PreviewResult] = {}
        self._timings: tuple[CellTiming, ...] = ()
        self._progress: _InFlight | None = None
        self._last_used = clock()

    # ---- read-only view -------------------------------------------------

    @property
    def is_alive(self) -> bool:
        with self._state_lock:
            return not self._closed

    @property
    def is_ready(self) -> bool:
        with self._state_lock:
            return self._ready and not self._closed

    @property
    def knobs(self) -> tuple[Knob, ...]:
        with self._state_lock:
            return self._knobs

    @property
    def rejections(self) -> tuple[Rejection, ...]:
        with self._state_lock:
            return self._rejections

    @property
    def timings(self) -> tuple[CellTiming, ...]:
        with self._state_lock:
            return self._timings

    @property
    def progress(self) -> WarmProgress | None:
        """The in-flight replay, or None when nothing is being replayed."""
        with self._state_lock:
            in_flight = self._progress
        if in_flight is None:
            return None
        return WarmProgress(
            cell_id=in_flight.cell_id, ordinal=in_flight.ordinal,
            total=in_flight.total, completed=in_flight.completed,
            elapsed_seconds=self._timer() - in_flight.started_at,
        )

    @property
    def idle_seconds(self) -> float:
        with self._state_lock:
            return self._clock() - self._last_used

    @property
    def cached_preview_count(self) -> int:
        with self._state_lock:
            return len(self._cache)

    # ---- lifecycle ------------------------------------------------------

    def warm(
        self, chain_cells: Sequence[ChainCell],
        *, on_progress: Callable[[CellTiming], None] | None = None,
    ) -> WarmResult:
        """Replay the chain from the top in the shadow kernel.

        From the top, and not from anywhere cleverer, because that is the only
        starting state we can be sure of. The cost is one full upstream run per
        panel session; what it buys is a shadow whose correctness does not
        depend on the live kernel having executed anything.
        """
        chain = tuple(chain_cells)
        target_position = next(
            (index for index, cell in enumerate(chain)
             if cell.cell_id == self.target_cell_id),
            None,
        )
        if target_position is None:
            raise ShadowError("The plot cell is not in the chain.")

        # Discovery runs off the shadow's own copy so that `preview` can take a
        # bare `{name: value}` map: the knobs (and their source ranges) are a
        # property of this chain, and the caller should not have to hand them
        # back on every keystroke for us to rewrite the right bytes.
        discovery = discover(chain, self.target_cell_id)
        if discovery.blocked is not None:
            raise ShadowBlocked(discovery.blocked)

        with self._run_lock:
            with self._state_lock:
                self._require_open()
                self._chain = chain
                self._target_position = target_position
                self._committed_sources = {cell.cell_id: cell.source for cell in chain}
                self._knobs = discovery.knobs
                self._rejections = discovery.rejections
                self._committed_values = {knob.name: knob.value for knob in discovery.knobs}
                self._resident_sources = {}
                self._resident_upto = -1
                self._cache = {}
                self._ready = False
                self._timings = ()
                self._touch_locked()

            timings: list[CellTiming] = []
            outputs: tuple[dict[str, Any], ...] = ()
            failed_cell_id: str | None = None
            for position, cell in enumerate(chain):
                with self._state_lock:
                    self._require_open()
                    self._progress = _InFlight(
                        cell_id=cell.cell_id, ordinal=position + 1, total=len(chain),
                        started_at=self._timer(), completed=tuple(timings),
                    )
                started = self._timer()
                try:
                    result = self._kernel.execute(
                        cell.source, f"{self.shadow_id}-warm-{position}",
                    )
                except Exception as error:
                    elapsed = self._timer() - started
                    timing = CellTiming(
                        cell_id=cell.cell_id, ordinal=position + 1,
                        total=len(chain), seconds=elapsed, failed=True,
                    )
                    timings.append(timing)
                    if on_progress is not None:
                        on_progress(timing)
                    failed_cell_id = cell.cell_id
                    outputs = (_error_output(error),)
                    break
                elapsed = self._timer() - started

                timing = CellTiming(
                    cell_id=cell.cell_id, ordinal=position + 1, total=len(chain),
                    seconds=elapsed, failed=result.error,
                )
                timings.append(timing)
                if on_progress is not None:
                    on_progress(timing)

                with self._state_lock:
                    self._resident_sources[cell.cell_id] = cell.source
                    if not result.error:
                        self._resident_upto = position
                if result.error:
                    failed_cell_id = cell.cell_id
                    outputs = tuple(result.outputs)
                    break
                if position == target_position:
                    outputs = tuple(result.outputs)

            with self._state_lock:
                self._progress = None
                self._timings = tuple(timings)
                self._ready = failed_cell_id is None
                if self._ready:
                    # The replay just ran the whole chain at committed values,
                    # so the "before" picture is already paid for. Seeding the
                    # cache with it means returning every knob to its committed
                    # value is instant rather than another full chain run.
                    committed = self._value_tuple(self._committed_values)
                    self._cache[committed] = PreviewResult(
                        outputs=outputs, values=committed,
                        executed_cell_ids=tuple(cell.cell_id for cell in chain),
                        seconds=sum(timing.seconds for timing in timings),
                    )
                self._touch_locked()

            return WarmResult(
                timings=tuple(timings), knobs=discovery.knobs,
                rejections=discovery.rejections, outputs=outputs,
                failed_cell_id=failed_cell_id,
            )

    def cancel(self) -> None:
        """Interrupt whatever the shadow kernel is running.

        The interrupt surfaces as an error output on the running cell, which the
        replay loop treats like any other failure: it stops and reports. The
        session stays alive so the user can re-warm without a fresh spawn.
        """
        with self._state_lock:
            if self._closed:
                return
        self._kernel.interrupt()

    def close(self) -> None:
        """Shut the shadow kernel down. Idempotent."""
        with self._state_lock:
            if self._closed:
                return
            self._closed = True
            self._ready = False
            self._cache = {}
            self._progress = None
        # Outside the state lock: shutdown can block on the kernel process, and
        # a caller polling `is_alive` should not have to wait behind it.
        self._kernel.shutdown()

    def invalidate_if_stale(self, document_revision: int) -> bool:
        """Close the shadow if the document has moved on. Returns True if closed.

        A revision bump means the source the shadow replayed no longer exists,
        so every cached preview is a picture of code the user does not have.
        Serving one would be worse than making them wait for a re-warm.
        """
        if document_revision == self.document_revision:
            return False
        self.close()
        return True

    def close_if_idle(self) -> bool:
        """Reclaim the kernel when nobody has previewed for a while."""
        if self.idle_seconds < self.idle_timeout_seconds:
            return False
        self.close()
        return True

    # ---- preview --------------------------------------------------------

    def preview(self, values: Mapping[str, Any]) -> PreviewResult:
        """Render the target cell at `values`, changing nothing anywhere.

        `values` may name any subset of the knobs; the rest are taken at their
        committed value, so the result always corresponds to a complete, stated
        knob tuple rather than to "whatever was left over from last time".
        """
        with self._run_lock:
            with self._state_lock:
                self._require_ready()
                requested_values = dict(self._committed_values)
                for name, value in values.items():
                    knob = next((item for item in self._knobs if item.name == name), None)
                    if knob is None:
                        raise UnknownKnob(f"`{name}` is not a knob on this cell.")
                    requested_values[name] = _normalize(knob.kind, value)
                key = self._value_tuple(requested_values)
                hit = self._cache.get(key)
                if hit is not None:
                    self._touch_locked()
                    return replace(hit, cached=True)

                edits = [
                    (knob, requested_values[knob.name]) for knob in self._knobs
                    if requested_values[knob.name] != self._committed_values[knob.name]
                ]
                chain = self._chain
                target_position = self._target_position
                committed_sources = dict(self._committed_sources)

            # Rewriting happens on the shadow's own copy of the source. A
            # `RewriteError` here propagates untouched: it means the requested
            # value does not survive a round-trip through the file, which is a
            # bug worth hearing about, and it has not touched the kernel, so the
            # session is still perfectly usable.
            requested_sources = dict(committed_sources)
            requested_sources.update(rewrite_chain(committed_sources, edits))

            with self._state_lock:
                start = self._start_position(chain, requested_sources, target_position)

            outputs: tuple[dict[str, Any], ...] = ()
            executed: list[str] = []
            failed_cell_id: str | None = None
            cacheable = True
            started = self._timer()
            for position in range(start, target_position + 1):
                cell = chain[position]
                with self._state_lock:
                    self._require_open()
                source = requested_sources[cell.cell_id]
                try:
                    result = self._kernel.execute(
                        source, f"{self.shadow_id}-preview-{uuid4().hex[:8]}",
                    )
                except Exception as error:
                    # A timeout, an output-limit trip or a kernel that wants a
                    # restart says nothing about the *values* asked for, so this
                    # is reported but never cached — retrying the same knobs has
                    # to be allowed to succeed. The shadow stays up either way.
                    with self._state_lock:
                        self._resident_sources[cell.cell_id] = source
                        self._resident_upto = position - 1
                    outputs = (_error_output(error),)
                    executed.append(cell.cell_id)
                    failed_cell_id = cell.cell_id
                    cacheable = False
                    break
                executed.append(cell.cell_id)
                with self._state_lock:
                    self._resident_sources[cell.cell_id] = source
                    # On failure the cell is left half-executed, so the resident
                    # marker stops *before* it: the next preview must re-run it
                    # and everything after it rather than trusting that state.
                    self._resident_upto = position if not result.error else position - 1
                if result.error:
                    outputs = tuple(result.outputs)
                    failed_cell_id = cell.cell_id
                    break
                if position == target_position:
                    outputs = tuple(result.outputs)

            elapsed = self._timer() - started
            preview = PreviewResult(
                outputs=outputs, values=key, executed_cell_ids=tuple(executed),
                seconds=elapsed, failed=failed_cell_id is not None,
                failed_cell_id=failed_cell_id,
            )
            with self._state_lock:
                if cacheable:
                    # Failures from the code itself (`bins=0` divides by zero)
                    # *are* cached: they are a deterministic property of these
                    # values, and paying the chain again to be told the same
                    # thing helps nobody.
                    self._cache[key] = preview
                self._touch_locked()
            return preview

    # ---- internals ------------------------------------------------------

    def _start_position(
        self, chain: tuple[ChainCell, ...], requested_sources: dict[str, str],
        target_position: int,
    ) -> int:
        """The earliest cell that has to run for the kernel to be right.

        "Earliest rewritten cell" is the obvious answer and it is wrong on the
        second preview. Tune a knob in cell 2, then tune one in cell 5 back at
        cell 2's committed value: cell 2 is no longer *rewritten*, but the
        kernel still holds the value from the previous preview, so starting at
        cell 5 would render a plot for a knob tuple the user never asked for.
        The comparison has to be against what the kernel actually holds.

        The second term covers a chain that stopped early — after a failed
        preview everything past the failure is stale or never ran.
        """
        first_differing = next(
            (position for position, cell in enumerate(chain)
             if self._resident_sources.get(cell.cell_id) != requested_sources[cell.cell_id]),
            None,
        )
        candidates = [self._resident_upto + 1]
        if first_differing is not None:
            candidates.append(first_differing)
        # Nothing differs and nothing is stale: re-render the target alone. That
        # only happens when the cache was cleared, since an unchanged kernel
        # state would otherwise have been a cache hit.
        return min(min(candidates), target_position)

    def _value_tuple(self, values: Mapping[str, Any]) -> tuple[tuple[str, Any], ...]:
        """The cache key: every knob, sorted by name.

        Sorted so that `{"bins": 10, "alpha": 0.5}` and `{"alpha": 0.5,
        "bins": 10}` are the same key — the user drags knobs in whatever order
        they like and must not pay twice for it. Complete (rather than only the
        changed knobs) so that two different partial maps that mean the same
        full state cannot end up as two entries. `_normalize` pins each value to
        its knob's declared type, so `True` and `1` cannot collide either.
        """
        return tuple(sorted(
            (knob.name, values[knob.name]) for knob in self._knobs
        ))

    def _require_open(self) -> None:
        if self._closed:
            raise ShadowClosed("This preview session has been closed.")

    def _require_ready(self) -> None:
        self._require_open()
        if not self._ready:
            raise ShadowNotReady("The preview session has not finished warming up.")

    def _touch_locked(self) -> None:
        self._last_used = self._clock()


def _normalize(kind: str, value: Any) -> Any:
    """Coerce an incoming value to the type its knob's literal had.

    Values arrive from JSON, where an int knob is as likely to show up as `12.0`
    as `12`. Coercing here keeps the cache key canonical and stops `render` from
    writing a float into a slot the notebook reads as a count.
    """
    try:
        if kind == "bool":
            return bool(value)
        if kind == "int":
            return int(value)
        if kind == "float":
            return float(value)
        if kind == "str":
            return str(value)
        if kind in {"tuple", "list"}:
            if isinstance(value, (str, bytes)) or not isinstance(value, Iterable):
                raise TypeError(f"{value!r} is not a sequence")
            # Always a tuple, never a list: the key has to be hashable, and the
            # knob's own `kind` decides which literal gets written back out.
            return tuple(value)
    except (TypeError, ValueError) as error:
        raise ShadowError(f"{value!r} is not a valid {kind} value") from error
    raise ShadowError(f"No preview handling for knob kind {kind!r}")


def _error_output(error: BaseException) -> dict[str, Any]:
    """Turn a kernel-level failure into an output the panel can render.

    Preview failures are a *state*, not an exception: the design keeps the
    knobs, keeps the shadow, and shows the traceback inline. Raising out of
    `preview` would make every caller responsible for not throwing the session
    away, so the failure is returned in the same shape as a cell traceback.
    """
    return {
        "output_type": "error",
        "ename": type(error).__name__,
        "evalue": str(error) or type(error).__name__,
        "traceback": [],
    }
