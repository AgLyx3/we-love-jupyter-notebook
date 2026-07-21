from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from ..notebook_document.models import NotebookDomainError


class TurnScopeError(NotebookDomainError):
    code = "turn_scope_error"
    message = "Turn scope operation failed"


class EmptyEditableScope(TurnScopeError):
    code = "empty_editable_scope"
    message = "At least one editable cell is required"
    status_code = 422


@dataclass(frozen=True)
class ScopeSelection:
    editable_cell_ids: tuple[str, ...] = ()
    context_cell_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class FrozenTurnScope:
    turn_id: str
    session_id: str
    notebook_revision: int
    editable_cell_ids: tuple[str, ...]
    context_cell_ids: tuple[str, ...]
    prompt: str
    frozen_at: datetime

    @classmethod
    def create(
        cls, *, turn_id: str, session_id: str, notebook_revision: int,
        selection: ScopeSelection, prompt: str,
    ) -> "FrozenTurnScope":
        return cls(
            turn_id=turn_id, session_id=session_id,
            notebook_revision=notebook_revision,
            editable_cell_ids=selection.editable_cell_ids,
            context_cell_ids=selection.context_cell_ids, prompt=prompt,
            frozen_at=datetime.now(timezone.utc),
        )


@dataclass(frozen=True)
class TerminalScopeRecord:
    scope: FrozenTurnScope
    outcome: str
    completed_at: datetime
