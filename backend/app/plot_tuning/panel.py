"""The panel's own service: analysis without a kernel, then one shadow at a time.

This is the piece that sits between the HTTP layer and the two halves of the
feature. It exists mainly to keep one promise structural rather than
conventional:

    Opening the panel must not start anything.

`open_panel` runs discovery and the risky-cell classification and returns both,
having spawned nothing. Only `warm` — which the client calls *after* the user has
approved whatever the classifier flagged — creates a kernel. Decision D5 in the
design rests on that ordering: the user is told what a warm-up will re-fire
*before* it re-fires it, and a panel they open and immediately close costs
nothing at all.

It also owns the "one shadow at a time" rule that `ShadowSession` documents but
deliberately does not enforce itself — a session cannot know about its siblings.
"""

from __future__ import annotations

from dataclasses import dataclass
from threading import RLock
from typing import Any

from ..kernel_execution.risky_cell_classifier import RiskyCellClassifier
from ..notebook_document.models import NotebookSnapshot
from ..notebook_document.service import NotebookDocumentService
from .bounds import bounds_for
from .discovery import ChainCell, Discovery, Knob, discover
from .shadow import ShadowError, ShadowSession


class PanelError(RuntimeError):
    """The panel cannot be opened or driven for this cell."""


class UnknownShadow(PanelError):
    """No live shadow with that id — it was closed, timed out, or invalidated."""


@dataclass(frozen=True)
class RiskyChainCell:
    """One upstream cell a warm-up would re-run, and what it would do again."""

    cell_id: str
    cell_index: int
    categories: tuple[str, ...]
    reasons: tuple[str, ...]


@dataclass(frozen=True)
class PanelPlan:
    """Everything the panel needs to render *before* any kernel exists."""

    target_cell_id: str
    document_revision: int
    discovery: Discovery
    chain: tuple[ChainCell, ...]
    risky: tuple[RiskyChainCell, ...]


def cell_source(cell: dict[str, Any]) -> str:
    source = cell.get("source", "")
    return "".join(source) if isinstance(source, list) else str(source)


def chain_for(snapshot: NotebookSnapshot, target_cell_id: str) -> tuple[ChainCell, ...]:
    """The code cells a replay must run: everything up to and including the target.

    Markdown and raw cells are dropped rather than replayed as no-ops, because
    `index` here is the position in the *chain*, and discovery's rejection
    messages quote it back to the user ("cell 4 is assigned twice"). Numbering
    those against a list that silently included prose would make the message
    point at the wrong cell.
    """
    cells = snapshot.notebook.get("cells", [])
    target_position = next(
        (index for index, cell in enumerate(cells) if cell.get("id") == target_cell_id),
        None,
    )
    if target_position is None:
        raise PanelError(f"Cell {target_cell_id} is not in this notebook.")
    return tuple(
        ChainCell(cell_id=cell["id"], index=index, source=cell_source(cell))
        for index, cell in enumerate(cells[: target_position + 1])
        if cell.get("cell_type") == "code"
    )


def serialize_knob(knob: Knob) -> dict[str, Any]:
    limits = bounds_for(knob.kind, knob.value) if knob.kind in {"int", "float"} else None
    return {
        "name": knob.name,
        "cellId": knob.cell_id,
        "cellIndex": knob.cell_index,
        "kind": knob.kind,
        "value": list(knob.value) if isinstance(knob.value, tuple) else knob.value,
        "bounds": None if limits is None else {
            "minimum": limits.minimum, "maximum": limits.maximum, "step": limits.step,
        },
    }


class PlotTuningPanelService:
    """Owns the analysis pass and, at most, one live shadow."""

    def __init__(
        self, *, documents: NotebookDocumentService,
        classifier: RiskyCellClassifier | None = None,
        idle_timeout_seconds: float | None = None,
    ) -> None:
        self.documents = documents
        self.classifier = classifier or RiskyCellClassifier()
        self.idle_timeout_seconds = idle_timeout_seconds
        self._lock = RLock()
        self._shadow: ShadowSession | None = None
        self._session_id: str | None = None
        # Revision alone cannot detect a notebook swap: revisions restart at 0
        # per notebook, so a shadow warmed at revision 3 of one file passes the
        # staleness check against revision 3 of the next one and serves previews
        # of source that is no longer open anywhere. Every other stateful service
        # here listens for the replacement; this one was the exception.
        documents.register_session_replacement_listener(self._on_session_replaced)

    def _on_session_replaced(self, session_id: str | None, _revision: int) -> None:
        self.close_current()
        with self._lock:
            self._session_id = session_id

    # ------------------------------------------------------------- analysis

    def open_panel(
        self, *, session_id: str, expected_revision: int, cell_id: str,
    ) -> PanelPlan:
        """Discovery plus risk, with no kernel started. See the module docstring."""
        snapshot = self.documents.get_snapshot()
        self.documents.check_snapshot_preconditions(
            snapshot, session_id, expected_revision,
        )
        chain = chain_for(snapshot, cell_id)
        if not any(cell.cell_id == cell_id for cell in chain):
            # A markdown or raw cell. Discovery would refuse anyway, but saying
            # so here is clearer than "the plot cell is not in the chain".
            raise PanelError("Only code cells can be tuned.")
        return PanelPlan(
            target_cell_id=cell_id,
            document_revision=snapshot.revision,
            discovery=discover(chain, cell_id),
            chain=chain,
            risky=self._classify(chain),
        )

    def _classify(self, chain: tuple[ChainCell, ...]) -> tuple[RiskyChainCell, ...]:
        """Which upstream cells a warm-up would re-fire, and what they would do.

        Every cell in the chain is replayed, so every cell is classified — not
        just the ones holding knobs. The point of D5 is to tell the user what the
        *replay* will do to the outside world, and a `requests.post` three cells
        above the knob is exactly the case worth warning about.
        """
        flagged = []
        for cell in chain:
            classification = self.classifier.classify(cell.source)
            # "confirm" is the classifier's own word for "ask the user first";
            # anything else is "safe". Keying off `level` rather than truthiness
            # of the reasons keeps this aligned with the execution path, which
            # gates on exactly the same value.
            if classification.level != "confirm":
                continue
            flagged.append(RiskyChainCell(
                cell_id=cell.cell_id, cell_index=cell.index,
                categories=classification.matched_patterns,
                reasons=classification.reasons,
            ))
        return tuple(flagged)

    # --------------------------------------------------------------- shadow

    def warm(
        self, *, session_id: str, expected_revision: int, cell_id: str,
    ) -> tuple[ShadowSession, Any]:
        """Create the shadow and replay the chain. The first thing that costs."""
        plan = self.open_panel(
            session_id=session_id, expected_revision=expected_revision, cell_id=cell_id,
        )
        if plan.discovery.blocked is not None:
            raise PanelError(plan.discovery.blocked)

        # One at a time. A second shadow would double the resident copy of the
        # user's data for a panel they are no longer looking at.
        self.close_current()
        kwargs: dict[str, Any] = {}
        if self.idle_timeout_seconds is not None:
            kwargs["idle_timeout_seconds"] = self.idle_timeout_seconds
        shadow = ShadowSession(
            target_cell_id=cell_id, document_revision=plan.document_revision, **kwargs,
        )
        with self._lock:
            self._shadow = shadow
            self._session_id = session_id
        try:
            result = shadow.warm(plan.chain)
        except ShadowError:
            self.close_current()
            raise
        return shadow, result

    def get_shadow(self, shadow_id: str) -> ShadowSession:
        """The live shadow, if it is still the one asked for and still valid.

        Checked against the *current* document revision on every call rather than
        on a timer: a preview rendered from source the user has since edited is
        worse than no preview, because it looks authoritative.
        """
        with self._lock:
            shadow = self._shadow
        if shadow is None or shadow.shadow_id != shadow_id or not shadow.is_alive:
            raise UnknownShadow("That preview session is no longer available.")
        snapshot_session = self.documents.get_snapshot().session_id
        with self._lock:
            owner = self._session_id
        if owner is not None and owner != snapshot_session:
            self.close_current()
            raise UnknownShadow("The notebook changed since this preview session started.")
        if shadow.invalidate_if_stale(self.documents.get_snapshot().revision):
            with self._lock:
                if self._shadow is shadow:
                    self._shadow = None
            raise UnknownShadow(
                "The notebook changed since this preview session started.",
            )
        if shadow.close_if_idle():
            with self._lock:
                if self._shadow is shadow:
                    self._shadow = None
            raise UnknownShadow("This preview session timed out.")
        return shadow

    def close_current(self) -> None:
        with self._lock:
            shadow, self._shadow = self._shadow, None
        if shadow is not None:
            shadow.close()

    def shutdown(self) -> None:
        self.close_current()
