from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from ..turn_scope.models import ScopeSelection

router = APIRouter(prefix="/turn-scope")


class ScopeCellRequest(BaseModel):
    session_id: str = Field(alias="sessionId")
    expected_revision: int = Field(alias="expectedDocumentRevision")
    cell_id: str = Field(alias="cellId")


class ClearScopeRequest(BaseModel):
    session_id: str = Field(alias="sessionId")
    expected_revision: int = Field(alias="expectedDocumentRevision")


def _serialize(scope: ScopeSelection) -> dict[str, Any]:
    return {
        "editableCellIds": list(scope.editable_cell_ids),
        "contextCellIds": list(scope.context_cell_ids),
        "sessionId": scope.session_id,
        "notebookRevision": scope.notebook_revision,
    }


@router.get("")
def get_scope(request: Request) -> dict[str, Any]:
    return _serialize(request.app.state.turn_scope_service.current())


@router.post("/editable-cells")
def add_editable(body: ScopeCellRequest, request: Request) -> dict[str, Any]:
    return _serialize(request.app.state.turn_scope_service.add(
        body.cell_id, editable=True, session_id=body.session_id,
        revision=body.expected_revision,
    ))


@router.post("/context-cells")
def add_context(body: ScopeCellRequest, request: Request) -> dict[str, Any]:
    return _serialize(request.app.state.turn_scope_service.add(
        body.cell_id, editable=False, session_id=body.session_id,
        revision=body.expected_revision,
    ))


@router.delete("")
def clear_scope(body: ClearScopeRequest, request: Request) -> dict[str, Any]:
    return _serialize(request.app.state.turn_scope_service.clear(
        session_id=body.session_id, revision=body.expected_revision,
    ))
