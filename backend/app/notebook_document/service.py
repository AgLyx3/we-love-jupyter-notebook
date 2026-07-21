from __future__ import annotations

import copy
import json
import re
from threading import RLock
from typing import Any
from uuid import uuid4

import nbformat
from nbformat.validator import NotebookValidationError

from .models import (
    CellNotFound,
    MutationConflict,
    MutationLease,
    NotebookImportError,
    NotebookNotLoaded,
    NotebookSnapshot,
    ReplacementPreconditionRequired,
    RevisionConflict,
    SessionConflict,
)
from .mutation_coordinator import MutationCoordinator

MAX_NOTEBOOK_BYTES = 5 * 1024 * 1024
_VALID_CELL_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


class NotebookDocumentService:
    def __init__(self, coordinator: MutationCoordinator | None = None) -> None:
        self.coordinator = coordinator or MutationCoordinator()
        self._lock = RLock()
        self._session_id: str | None = None
        self._filename = "notebook.ipynb"
        self._notebook: dict[str, Any] | None = None
        self._revision = 0
        self._dirty = False
        self._last_mutation_owner: str | None = None

    def import_notebook(
        self,
        payload: bytes,
        *,
        filename: str = "notebook.ipynb",
        expected_session_id: str | None = None,
        expected_revision: int | None = None,
    ) -> NotebookSnapshot:
        candidate, normalized = self._parse_and_validate(payload)

        with self._lock:
            if self._notebook is not None:
                if expected_session_id is None or expected_revision is None:
                    raise ReplacementPreconditionRequired()
                self._check_preconditions(expected_session_id, expected_revision)

            lease = self._acquire_lease(
                operation_type="notebook_import", operation_id=uuid4().hex
            )
            try:
                self._session_id = uuid4().hex
                self._filename = self._safe_filename(filename)
                self._notebook = candidate
                self._revision = 1 if normalized else 0
                self._dirty = normalized
                self._last_mutation_owner = "normalization" if normalized else None
                return self._snapshot_unlocked()
            finally:
                self.coordinator.release(lease)

    def get_snapshot(self) -> NotebookSnapshot:
        with self._lock:
            self._require_notebook()
            return self._snapshot_unlocked()

    def export_notebook(self) -> tuple[str, bytes]:
        snapshot = self.get_snapshot()
        content = (json.dumps(snapshot.notebook, ensure_ascii=False, indent=1) + "\n").encode(
            "utf-8"
        )
        return snapshot.filename, content

    def update_cell_source(
        self,
        *,
        cell_id: str,
        source: str,
        expected_revision: int,
        owner: str,
        expected_session_id: str | None = None,
    ) -> NotebookSnapshot:
        with self._lock:
            self._require_notebook()
            if expected_session_id is not None and expected_session_id != self._session_id:
                raise SessionConflict(self._session_id or "")
            if expected_revision != self._revision:
                raise RevisionConflict(self._revision)

            lease = self._acquire_lease(operation_type="manual_edit", operation_id=owner)
            try:
                cell = next(
                    (cell for cell in self._notebook["cells"] if cell["id"] == cell_id),
                    None,
                )
                if cell is None:
                    raise CellNotFound(cell_id)
                cell["source"] = source
                self._revision += 1
                self._dirty = True
                self._last_mutation_owner = owner
                return self._snapshot_unlocked()
            finally:
                self.coordinator.release(lease)

    def _parse_and_validate(self, payload: bytes) -> tuple[dict[str, Any], bool]:
        if len(payload) > MAX_NOTEBOOK_BYTES:
            raise NotebookImportError(
                f"Notebook exceeds the {MAX_NOTEBOOK_BYTES}-byte limit",
                code="notebook_too_large",
            )
        try:
            raw = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise NotebookImportError() from error

        self._validate_basic_structure(raw)
        candidate = copy.deepcopy(raw)
        normalized = self._normalize_cell_ids(candidate)
        try:
            notebook = nbformat.from_dict(candidate)
            nbformat.validate(notebook)
        except (NotebookValidationError, AttributeError, TypeError, ValueError) as error:
            raise NotebookImportError() from error
        return notebook, normalized

    @staticmethod
    def _validate_basic_structure(raw: Any) -> None:
        if not isinstance(raw, dict):
            raise NotebookImportError()
        if raw.get("nbformat") != 4 or not isinstance(raw.get("nbformat_minor"), int):
            raise NotebookImportError("Only nbformat 4 notebooks are supported")
        if not isinstance(raw.get("metadata"), dict) or not isinstance(raw.get("cells"), list):
            raise NotebookImportError()
        for cell in raw["cells"]:
            if not isinstance(cell, dict):
                raise NotebookImportError()
            if cell.get("cell_type") not in {"code", "markdown", "raw"}:
                raise NotebookImportError()
            if not isinstance(cell.get("metadata"), dict):
                raise NotebookImportError()
            source = cell.get("source")
            if not isinstance(source, str) and not (
                isinstance(source, list) and all(isinstance(line, str) for line in source)
            ):
                raise NotebookImportError()

    @staticmethod
    def _normalize_cell_ids(notebook: dict[str, Any]) -> bool:
        seen: set[str] = set()
        changed = False
        for cell in notebook["cells"]:
            cell_id = cell.get("id")
            if (
                not isinstance(cell_id, str)
                or _VALID_CELL_ID.fullmatch(cell_id) is None
                or cell_id in seen
            ):
                cell_id = uuid4().hex[:8]
                while cell_id in seen:
                    cell_id = uuid4().hex[:8]
                cell["id"] = cell_id
                changed = True
            seen.add(cell_id)
        return changed

    def _check_preconditions(self, session_id: str, revision: int) -> None:
        if session_id != self._session_id:
            raise SessionConflict(self._session_id or "")
        if revision != self._revision:
            raise RevisionConflict(self._revision)

    def _acquire_lease(
        self, *, operation_type: str, operation_id: str
    ) -> MutationLease:
        try:
            return self.coordinator.acquire(
                operation_type=operation_type, operation_id=operation_id
            )
        except MutationConflict as error:
            error.details["currentDocumentRevision"] = self._revision
            raise

    def _require_notebook(self) -> None:
        if self._notebook is None or self._session_id is None:
            raise NotebookNotLoaded()

    def _snapshot_unlocked(self) -> NotebookSnapshot:
        self._require_notebook()
        return NotebookSnapshot(
            session_id=self._session_id or "",
            filename=self._filename,
            notebook=copy.deepcopy(self._notebook),
            revision=self._revision,
            dirty=self._dirty,
            last_mutation_owner=self._last_mutation_owner,
        )

    @staticmethod
    def _safe_filename(filename: str) -> str:
        leaf = filename.replace("\\", "/").rsplit("/", 1)[-1]
        if not leaf.endswith(".ipynb"):
            return "notebook.ipynb"
        sanitized = re.sub(r"[^A-Za-z0-9._ -]", "_", leaf).strip(". ")
        return sanitized or "notebook.ipynb"
