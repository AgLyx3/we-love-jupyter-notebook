"""Orchestration and the cache. Two rules make this whole module.

**Opening a notebook never calls the model.** `get` is free: it recomputes the
five computed fields — an AST parse — and serves whatever boundaries are already
cached, or the deterministic fallback when there are none. Only `generate`
spends anything, and only when the user asks for it.

**Running a cell never invalidates the map.** The cache is keyed on a hash of
the cell *sources*, not on the document revision, because `_revision` also
increments on `set_execution_count` (`notebook_document/service.py:308`) and so
bumps on every cell run. Editing a cell changes the hash and marks the map
stale; it stays visible, and it regenerates only on request.

The per-block hashing in research §7.1 is deliberately not here (spec §5). Once
segmentation is model-generated there is no stable "block 2" to invalidate —
an edit can move a boundary — so the unit of caching is the whole notebook. At
~22 tokens per cell that is cheap enough not to need anything cleverer.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from threading import RLock
from typing import Any, Sequence

from ..agent_workspace.models import AgentAdapterError
from ..kernel_execution.risky_cell_classifier import RiskyCellClassifier
from ..notebook_document.models import NotebookSnapshot
from ..notebook_document.service import NotebookDocumentService
from . import analysis, segmenter
from .analysis import Cell
from .models import Overview, SegmentationRejected


def source_fingerprint(cells: Sequence[Cell]) -> str:
    """A hash of every cell's type and source, and of nothing else.

    Outputs and execution counts are excluded by construction, which is what
    makes running the notebook a no-op for the cache. Cell *type* is included
    because turning code into markdown changes the map even when the text does
    not.
    """
    digest = hashlib.sha256()
    for cell in cells:
        digest.update(cell.kind.encode("utf-8"))
        digest.update(b"\0")
        digest.update(cell.source.encode("utf-8"))
        digest.update(b"\0\0")
    return digest.hexdigest()


@dataclass
class _CacheEntry:
    """One generated map, and the sources it was generated from."""

    fingerprint: str
    ranges: tuple[tuple[int, int], ...]
    names: tuple[str, ...]
    #: Cell count at generation time, so a cached map is never applied to a
    #: notebook it cannot partition. Belt and braces next to the fingerprint.
    cell_count: int


class NotebookOverviewService:
    """The panel's service: free analysis, explicit generation, one cache."""

    def __init__(
        self, *, documents: NotebookDocumentService, adapter: Any,
        classifier: RiskyCellClassifier | None = None,
        model: str | None = segmenter.DEFAULT_MODEL,
    ) -> None:
        self.documents = documents
        self.adapter = adapter
        self.classifier = classifier or RiskyCellClassifier()
        self.model = model
        self._lock = RLock()
        #: Keyed by notebook path, per spec §5. An unsaved notebook has no path;
        #: it gets the session id instead, so a map generated for it survives
        #: edits and runs but not a reopen. Losing a map costs one button press.
        self._cache: dict[str, _CacheEntry] = {}
        #: The last generation failure, per key, so `get` can keep saying what
        #: went wrong instead of silently serving the fallback.
        self._errors: dict[str, str] = {}
        documents.register_session_replacement_listener(self._on_session_replaced)

    def _on_session_replaced(self, _session_id: str | None, _revision: int) -> None:
        """A different notebook is open now.

        The cache is keyed by path rather than by session, so entries for other
        notebooks stay valid and a reopen gets its map back for free. Only the
        transient error banner is cleared — it belongs to the notebook that was
        open when it happened.
        """
        with self._lock:
            self._errors.clear()

    # ------------------------------------------------------------------ read

    def get(
        self, *, session_id: str, expected_revision: int,
    ) -> Overview:
        """The free path. Never calls the model — opening a notebook is free."""
        snapshot = self._snapshot(session_id, expected_revision)
        cells = self._cells(snapshot)
        return self._compose(snapshot, cells)

    # --------------------------------------------------------------- generate

    def generate(
        self, *, session_id: str, expected_revision: int,
    ) -> Overview:
        """The costly path: one model call, always explicitly requested.

        A response that is not a valid partition is discarded rather than
        rendered (spec §4.3) — the previous map, if any, stays in the cache and
        the caller gets the fallback with the reason attached.
        """
        snapshot = self._snapshot(session_id, expected_revision)
        cells = self._cells(snapshot)
        key = self._key(snapshot)
        try:
            result = segmenter.segment(
                cells, self.adapter, model=self.model,
            )
        except (SegmentationRejected, AgentAdapterError) as error:
            with self._lock:
                self._errors[key] = str(error)
            return self._compose(snapshot, cells)
        with self._lock:
            self._errors.pop(key, None)
            self._cache[key] = _CacheEntry(
                fingerprint=source_fingerprint(cells),
                ranges=result.ranges,
                names=result.names,
                cell_count=len(cells),
            )
        return self._compose(snapshot, cells)

    # ---------------------------------------------------------------- internals

    def _snapshot(self, session_id: str, expected_revision: int) -> NotebookSnapshot:
        snapshot = self.documents.get_snapshot()
        self.documents.check_snapshot_preconditions(
            snapshot, session_id, expected_revision,
        )
        return snapshot

    def _cells(self, snapshot: NotebookSnapshot) -> list[Cell]:
        return analysis.read_cells(snapshot.notebook, classifier=self.classifier)

    @staticmethod
    def _key(snapshot: NotebookSnapshot) -> str:
        return snapshot.notebook_path or f"session:{snapshot.session_id}"

    def _compose(self, snapshot: NotebookSnapshot, cells: list[Cell]) -> Overview:
        """Cached boundaries if they still fit, else the deterministic fallback.

        The computed fields are recomputed here on every request rather than
        stored with the cache entry, so a cell that has since been run shows its
        new state under a map that was generated before the run.
        """
        key = self._key(snapshot)
        with self._lock:
            entry = self._cache.get(key)
            error = self._errors.get(key)
        if entry is None or entry.cell_count != len(cells):
            # A cell count change means the cached ranges cannot partition this
            # notebook at all: the map is not stale, it is inapplicable. Serving
            # the fallback is the only correct answer, and `stale` says why the
            # panel changed shape.
            return Overview(
                blocks=analysis.fallback_blocks(cells),
                generated=False,
                stale=entry is not None,
                cell_count=len(cells),
                error=error,
            )
        return Overview(
            blocks=analysis.annotate(cells, entry.ranges, entry.names),
            generated=True,
            stale=entry.fingerprint != source_fingerprint(cells),
            cell_count=len(cells),
            error=error,
        )
