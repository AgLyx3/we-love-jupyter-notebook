from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from ..agent_turns.service import AgentTurn
from .notebook_routes import serialize_snapshot

router = APIRouter(prefix="/agent-turns")


class StartTurnRequest(BaseModel):
    session_id: str = Field(alias="sessionId")
    expected_revision: int = Field(alias="expectedDocumentRevision")
    prompt: str = Field(min_length=1)


class MutationRequest(BaseModel):
    session_id: str = Field(alias="sessionId")
    expected_revision: int = Field(alias="expectedDocumentRevision")


def serialize_turn(turn: AgentTurn, *, undo_eligible: bool = False) -> dict[str, Any]:
    return {
        "turnId": turn.turn_id,
        "sessionId": turn.session_id,
        "baseRevision": turn.base_revision,
        "prompt": turn.prompt,
        "editableCellIds": list(turn.editable_cell_ids),
        "contextCellIds": list(turn.context_cell_ids),
        "undoEligible": undo_eligible,
        "state": turn.state,
        "attempts": turn.attempts,
        "finalOutput": turn.final_output,
        "appliedRevision": turn.applied_revision,
        "executionOperationId": turn.execution_operation_id,
        "changes": [
            {"cellId": item.cell_id, "previousSource": item.previous_source, "nextSource": item.next_source}
            for item in turn.changes
        ],
        "error": turn.error,
        "createdAt": turn.created_at.isoformat(),
        "completedAt": turn.completed_at.isoformat() if turn.completed_at else None,
    }


@router.post("", status_code=202)
def start_turn(body: StartTurnRequest, request: Request) -> dict[str, Any]:
    service = request.app.state.agent_turn_service
    turn = service.start(
        prompt=body.prompt, session_id=body.session_id,
        expected_revision=body.expected_revision,
    )
    return serialize_turn(turn, undo_eligible=service.is_undo_eligible(turn))


@router.get("/{turn_id}")
def get_turn(turn_id: str, request: Request) -> dict[str, Any]:
    service = request.app.state.agent_turn_service
    turn = service.get(turn_id)
    return serialize_turn(turn, undo_eligible=service.is_undo_eligible(turn))


@router.post("/{turn_id}/cancel")
def cancel_turn(
    turn_id: str, body: MutationRequest, request: Request,
) -> dict[str, Any]:
    service = request.app.state.agent_turn_service
    turn = service.cancel(
        turn_id, session_id=body.session_id,
        expected_revision=body.expected_revision,
    )
    return serialize_turn(turn, undo_eligible=service.is_undo_eligible(turn))


@router.post("/{turn_id}/undo")
def undo_turn(turn_id: str, body: MutationRequest, request: Request) -> dict[str, Any]:
    return serialize_snapshot(request.app.state.agent_turn_service.undo(
        turn_id, session_id=body.session_id, expected_revision=body.expected_revision,
    ))


@router.post("/{turn_id}/cells/{cell_id}/revert")
def revert_cell(
    turn_id: str, cell_id: str, body: MutationRequest, request: Request,
) -> dict[str, Any]:
    return serialize_snapshot(request.app.state.agent_turn_service.revert_cell(
        turn_id, cell_id, session_id=body.session_id,
        expected_revision=body.expected_revision,
    ))
