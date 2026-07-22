from __future__ import annotations

from datetime import datetime, timezone
from threading import RLock
from uuid import uuid4

from ..notebook_document.models import CellNotFound, MutationLease
from ..notebook_document.service import NotebookDocumentService
from .models import (
    EmptyEditableScope, FrozenTurnScope, ScopeSelection, StaleTurnScope,
    TerminalScopeRecord,
)

MAX_TERMINAL_SCOPE_RECORDS = 100
MAX_TERMINAL_SCOPE_BYTES = 512 * 1024
MAX_TERMINAL_SCOPE_RECORD_BYTES = 64 * 1024


class TurnScopeService:
    def __init__(self, documents: NotebookDocumentService) -> None:
        self.documents = documents
        self._lock = RLock()
        self._editable: list[str] = []
        self._context: list[str] = []
        self._frozen: FrozenTurnScope | None = None
        self._history: list[TerminalScopeRecord] = []
        self._selection_session_id: str | None = None
        self._selection_revision: int | None = None
        self.documents.register_session_replacement_listener(
            self._on_session_replaced
        )

    def current(self) -> ScopeSelection:
        with self._lock:
            return ScopeSelection(
                tuple(self._editable), tuple(self._context),
                self._selection_session_id, self._selection_revision,
            )

    @property
    def history(self) -> tuple[TerminalScopeRecord, ...]:
        with self._lock:
            return tuple(self._history)

    def add(
        self, cell_id: str, *, editable: bool,
        session_id: str | None = None, revision: int | None = None,
    ) -> ScopeSelection:
        lease = self.documents.coordinator.acquire(
            operation_type="turn_scope", operation_id=uuid4().hex
        )
        try:
            snapshot = self.documents.get_snapshot()
            if session_id is not None and revision is not None:
                self.documents.check_snapshot_preconditions(snapshot, session_id, revision)
            if not any(cell["id"] == cell_id for cell in snapshot.notebook["cells"]):
                raise CellNotFound(cell_id)
            cell = next(cell for cell in snapshot.notebook["cells"] if cell["id"] == cell_id)
            if editable and cell["cell_type"] not in {"code", "markdown"}:
                raise CellNotFound(cell_id)
            with self._lock:
                if self._selection_session_id is not None and (
                    self._selection_session_id != snapshot.session_id
                    or self._selection_revision != snapshot.revision
                ):
                    self._editable.clear()
                    self._context.clear()
                self._selection_session_id = snapshot.session_id
                self._selection_revision = snapshot.revision
                target = self._editable if editable else self._context
                other = self._context if editable else self._editable
                if cell_id in other:
                    other.remove(cell_id)
                if cell_id not in target:
                    target.append(cell_id)
                return self.current()
        finally:
            self.documents.coordinator.release(lease)

    def clear(
        self, *, session_id: str | None = None, revision: int | None = None
    ) -> ScopeSelection:
        lease = self.documents.coordinator.acquire(
            operation_type="turn_scope", operation_id=uuid4().hex
        )
        try:
            if session_id is not None and revision is not None:
                self.documents.check_snapshot_preconditions(
                    self.documents.get_snapshot(), session_id, revision
                )
            with self._lock:
                self._editable.clear()
                self._context.clear()
                self._selection_session_id = None
                self._selection_revision = None
                return self.current()
        finally:
            self.documents.coordinator.release(lease)

    def freeze(
        self, *, turn_id: str, session_id: str, revision: int, prompt: str,
        lease: MutationLease,
    ) -> FrozenTurnScope:
        self.documents.assert_lease(lease)
        snapshot = self.documents.get_snapshot()
        self.documents.check_snapshot_preconditions(snapshot, session_id, revision)
        valid_ids = {cell["id"] for cell in snapshot.notebook["cells"]}
        with self._lock:
            if not self._editable:
                raise EmptyEditableScope()
            if self._selection_session_id != session_id:
                raise EmptyEditableScope()
            if self._selection_revision != revision:
                raise StaleTurnScope(
                    scope_revision=self._selection_revision or 0,
                    current_revision=revision,
                )
            if not set(self._editable + self._context) <= valid_ids:
                missing = next(iter(set(self._editable + self._context) - valid_ids))
                raise CellNotFound(missing)
            self._frozen = FrozenTurnScope.create(
                turn_id=turn_id, session_id=session_id,
                notebook_revision=revision, selection=self.current(), prompt=prompt,
            )
            return self._frozen

    def expire(self, scope: FrozenTurnScope, outcome: str) -> None:
        with self._lock:
            if self._frozen is not None and self._frozen.turn_id == scope.turn_id:
                self._frozen = None
            self._editable.clear()
            self._context.clear()
            self._selection_session_id = None
            self._selection_revision = None
            record = TerminalScopeRecord(
                scope=scope, outcome=outcome, completed_at=datetime.now(timezone.utc)
            )
            self._history.append(self._bound_record(record))
            self._prune_history_locked()

    def _on_session_replaced(
        self, _session_id: str | None, _revision: int,
    ) -> None:
        with self._lock:
            self._editable.clear()
            self._context.clear()
            self._frozen = None
            self._selection_session_id = None
            self._selection_revision = None
            if _session_id is None:
                self._history.clear()

    def update_terminal_outcome(self, turn_id: str, outcome: str) -> None:
        with self._lock:
            for index in range(len(self._history) - 1, -1, -1):
                record = self._history[index]
                if record.scope.turn_id == turn_id:
                    self._history[index] = TerminalScopeRecord(
                        scope=record.scope,
                        outcome=outcome,
                        completed_at=record.completed_at,
                    )
                    return

    def _prune_history_locked(self) -> None:
        retained: list[TerminalScopeRecord] = []
        retained_bytes = 0
        for record in reversed(self._history):
            scope = record.scope
            size = (
                len(scope.prompt.encode("utf-8"))
                + sum(len(value.encode("utf-8")) for value in scope.editable_cell_ids)
                + sum(len(value.encode("utf-8")) for value in scope.context_cell_ids)
                + len(record.outcome.encode("utf-8"))
                + 256
            )
            if retained and (
                len(retained) >= MAX_TERMINAL_SCOPE_RECORDS
                or retained_bytes + size > MAX_TERMINAL_SCOPE_BYTES
            ):
                continue
            retained.append(record)
            retained_bytes += size
        self._history = list(reversed(retained))

    @staticmethod
    def _bound_record(record: TerminalScopeRecord) -> TerminalScopeRecord:
        def truncate(value: str, max_bytes: int) -> str:
            encoded = value.encode("utf-8")
            if len(encoded) <= max_bytes:
                return value
            return encoded[:max_bytes - 3].decode("utf-8", errors="ignore") + "..."

        id_budget = 32 * 1024
        editable: list[str] = []
        context: list[str] = []
        used = 0
        for source, target in (
            (record.scope.editable_cell_ids, editable),
            (record.scope.context_cell_ids, context),
        ):
            for value in source:
                bounded = truncate(value, 256)
                size = len(bounded.encode("utf-8"))
                if used + size > id_budget:
                    break
                target.append(bounded)
                used += size
        scope = FrozenTurnScope(
            turn_id=truncate(record.scope.turn_id, 256),
            session_id=truncate(record.scope.session_id, 256),
            notebook_revision=record.scope.notebook_revision,
            editable_cell_ids=tuple(editable), context_cell_ids=tuple(context),
            prompt=truncate(record.scope.prompt, 24 * 1024),
            frozen_at=record.scope.frozen_at,
        )
        return TerminalScopeRecord(
            scope=scope, outcome=truncate(record.outcome, 256),
            completed_at=record.completed_at,
        )
