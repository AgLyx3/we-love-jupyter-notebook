from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request

from .agent_turn_routes import serialize_turn
from ..kernel_execution.service import serialize_operation

router = APIRouter(prefix="/session")


@router.get("/status")
def session_status(request: Request) -> dict[str, Any]:
    snapshot = request.app.state.notebook_service.get_snapshot()
    turn = request.app.state.agent_turn_service.active_for_session(snapshot.session_id)
    execution = request.app.state.kernel_execution_service.active_for_session(
        snapshot.session_id,
    )
    turn_history = request.app.state.agent_turn_service.history_for_session(
        snapshot.session_id,
    )
    return {
        "sessionId": snapshot.session_id,
        "documentRevision": snapshot.revision,
        "activeTurn": (
            serialize_turn(
                turn,
                undo_eligible=request.app.state.agent_turn_service.is_undo_eligible(turn),
            ) if turn is not None else None
        ),
        "turnHistory": [
            serialize_turn(
                item,
                undo_eligible=request.app.state.agent_turn_service.is_undo_eligible(item),
            )
            for item in turn_history
        ],
        "activeExecution": (
            serialize_operation(execution) if execution is not None else None
        ),
    }
