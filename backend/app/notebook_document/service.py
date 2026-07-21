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
    NotebookSizeError,
    NotebookSnapshot,
    ReplacementPreconditionRequired,
    RevisionConflict,
    SessionConflict,
)
from .mutation_coordinator import MutationCoordinator

MAX_NOTEBOOK_BYTES = 5 * 1024 * 1024
_VALID_CELL_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")


def _new_cell_id() -> str:
    return uuid4().hex[:8]


def _is_valid_cell_id(value: Any) -> bool:
    return isinstance(value, str) and _VALID_CELL_ID.fullmatch(value) is not None


class NotebookDocumentService:
    """Owns the active document and its short, atomic mutations.

    Mutation ordering is always coordinator lease, then document lock. A future
    long-lived operation must retain its lease and enter only the document lock
    when committing; no code may wait for a lease while holding the document lock.
    """

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
        lease = self._acquire_lease(
            operation_type="notebook_import", operation_id=uuid4().hex
        )
        try:
            with self._lock:
                candidate, normalized = self._parse_and_validate(payload)
                if self._notebook is not None:
                    if expected_session_id is None or expected_revision is None:
                        raise ReplacementPreconditionRequired()
                    self._check_preconditions(expected_session_id, expected_revision)

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

    def assert_lease(self, lease: MutationLease) -> None:
        active = self.coordinator.active_lease
        if active is None or active.token != lease.token:
            raise MutationConflict(
                active_operation_type=active.operation_type if active else "none",
                active_operation_id=active.operation_id if active else "none",
            )

    @staticmethod
    def check_snapshot_preconditions(
        snapshot: NotebookSnapshot, session_id: str, revision: int
    ) -> None:
        if snapshot.session_id != session_id:
            raise SessionConflict(snapshot.session_id)
        if snapshot.revision != revision:
            raise RevisionConflict(snapshot.revision)

    def apply_source_changes_under_lease(
        self, *, changes: dict[str, str], expected_revision: int,
        owner: str, lease: MutationLease,
    ) -> NotebookSnapshot:
        self.assert_lease(lease)
        with self._lock:
            self._require_notebook()
            if expected_revision != self._revision:
                raise RevisionConflict(self._revision)
            candidate = copy.deepcopy(self._notebook)
            indexed = {cell["id"]: cell for cell in candidate["cells"]}
            for cell_id, source in changes.items():
                if cell_id not in indexed:
                    raise CellNotFound(cell_id)
                indexed[cell_id]["source"] = source
            if len(self._serialize_notebook(candidate)) > MAX_NOTEBOOK_BYTES:
                raise NotebookSizeError(MAX_NOTEBOOK_BYTES)
            if changes:
                self._notebook = candidate
                self._revision += 1
                self._dirty = True
                self._last_mutation_owner = owner
            return self._snapshot_unlocked()

    def restore_under_lease(
        self, *, notebook: dict[str, Any], expected_revision: int,
        owner: str, lease: MutationLease,
    ) -> NotebookSnapshot:
        self.assert_lease(lease)
        with self._lock:
            self._require_notebook()
            if expected_revision != self._revision:
                raise RevisionConflict(self._revision)
            candidate = copy.deepcopy(notebook)
            try:
                nbformat.validate(nbformat.from_dict(candidate))
            except (NotebookValidationError, AttributeError, TypeError, ValueError) as error:
                raise NotebookImportError() from error
            self._notebook = candidate
            self._revision += 1
            self._dirty = True
            self._last_mutation_owner = owner
            return self._snapshot_unlocked()

    def export_notebook(self) -> tuple[str, bytes]:
        snapshot = self.get_snapshot()
        content = self._serialize_notebook(snapshot.notebook)
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
        lease = self._acquire_lease(
            operation_type=owner,
            operation_id=uuid4().hex,
        )
        try:
            with self._lock:
                self._require_notebook()
                if (
                    expected_session_id is not None
                    and expected_session_id != self._session_id
                ):
                    raise SessionConflict(self._session_id or "")
                if expected_revision != self._revision:
                    raise RevisionConflict(self._revision)

                cell = next(
                    (cell for cell in self._notebook["cells"] if cell["id"] == cell_id),
                    None,
                )
                if cell is None:
                    raise CellNotFound(cell_id)
                candidate = copy.deepcopy(self._notebook)
                candidate_cell = next(
                    item for item in candidate["cells"] if item["id"] == cell_id
                )
                candidate_cell["source"] = source
                if len(self._serialize_notebook(candidate)) > MAX_NOTEBOOK_BYTES:
                    raise NotebookSizeError(MAX_NOTEBOOK_BYTES)

                self._notebook = candidate
                self._revision += 1
                self._dirty = True
                self._last_mutation_owner = owner
                return self._snapshot_unlocked()
        finally:
            self.coordinator.release(lease)

    def _parse_and_validate(self, payload: bytes) -> tuple[dict[str, Any], bool]:
        if len(payload) > MAX_NOTEBOOK_BYTES:
            raise NotebookSizeError(MAX_NOTEBOOK_BYTES)
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
        if len(self._serialize_notebook(notebook)) > MAX_NOTEBOOK_BYTES:
            raise NotebookSizeError(MAX_NOTEBOOK_BYTES)
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
        reserved = {
            cell["id"]
            for cell in notebook["cells"]
            if _is_valid_cell_id(cell.get("id"))
        }
        used = set(reserved)
        seen_valid: set[str] = set()
        changed = False
        for cell in notebook["cells"]:
            cell_id = cell.get("id")
            if _is_valid_cell_id(cell_id) and cell_id not in seen_valid:
                seen_valid.add(cell_id)
                continue

            cell_id = _new_cell_id()
            while cell_id in used:
                cell_id = _new_cell_id()
            cell["id"] = cell_id
            used.add(cell_id)
            changed = True
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

    @staticmethod
    def _serialize_notebook(notebook: dict[str, Any]) -> bytes:
        return (json.dumps(notebook, ensure_ascii=False, indent=1) + "\n").encode(
            "utf-8"
        )

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
