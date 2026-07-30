from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, Request

from .agent_turn_routes import serialize_turn_summary
from ..kernel_execution.service import serialize_operation

router = APIRouter(prefix="/session")
MAX_TURN_HISTORY_RESPONSE_BYTES = 1024 * 1024


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
    summaries = []
    summary_bytes = 2
    for item in turn_history:
        summary = serialize_turn_summary(
            item,
            undo_eligible=request.app.state.agent_turn_service.is_undo_eligible(item, snapshot),
            stale_cell_ids=request.app.state.agent_turn_service.stale_cell_ids(item, snapshot),
        )
        size = len(json.dumps(summary, separators=(",", ":")).encode()) + 1
        if summary_bytes + size > MAX_TURN_HISTORY_RESPONSE_BYTES:
            break
        summaries.append(summary)
        summary_bytes += size
    return {
        "sessionId": snapshot.session_id,
        "documentRevision": snapshot.revision,
        "activeTurn": (
            serialize_turn_summary(
                turn,
                undo_eligible=request.app.state.agent_turn_service.is_undo_eligible(turn, snapshot),
                stale_cell_ids=request.app.state.agent_turn_service.stale_cell_ids(turn, snapshot),
            ) if turn is not None else None
        ),
        "turnHistory": summaries,
        "turnHistoryTruncated": len(summaries) < len(turn_history),
        "activeExecution": (
            serialize_operation(execution) if execution is not None else None
        ),
    }
