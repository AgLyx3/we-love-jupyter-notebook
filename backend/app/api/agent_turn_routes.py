from __future__ import annotations

import json
from typing import Any, Literal

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from ..agent_turns.service import AgentTurn
from .notebook_routes import serialize_snapshot

router = APIRouter(prefix="/agent-turns")
MAX_TURN_SUMMARY_BYTES = 128 * 1024


class StartTurnRequest(BaseModel):
    session_id: str = Field(alias="sessionId")
    expected_revision: int = Field(alias="expectedDocumentRevision")
    prompt: str = Field(min_length=1)
    model: Literal["default", "opus", "sonnet", "haiku"] = "default"
    mode: Literal["edit", "plan"] = "edit"


class MutationRequest(BaseModel):
    session_id: str = Field(alias="sessionId")
    expected_revision: int = Field(alias="expectedDocumentRevision")


class AcceptRequest(BaseModel):
    """Accept settles review state only, so it needs no expected revision."""

    session_id: str = Field(alias="sessionId")


def serialize_operations(
    turn: AgentTurn, *, stale_cell_ids: frozenset[str] = frozenset(),
) -> list[dict[str, Any]]:
    """Serialize the ledger as index ranges — never hunk text.

    The line content is reconstructible from ``changes``; repeating it here
    would blow the turn-summary budget for exactly the large turns that most
    need reviewing.
    """
    return [
        {
            "operationId": item.operation_id,
            "cellId": item.cell_id,
            "kind": item.kind,
            "ordinal": item.ordinal,
            "state": "stale" if (
                item.state == "pending" and item.cell_id in stale_cell_ids
            ) else item.state,
            "previousRange": [item.hunk.prev_start, item.hunk.prev_end],
            "nextRange": [item.hunk.next_start, item.hunk.next_end],
        }
        for item in turn.operations
    ]


def serialize_turn(
    turn: AgentTurn, *, undo_eligible: bool = False,
    stale_cell_ids: frozenset[str] = frozenset(),
) -> dict[str, Any]:
    return {
        "operations": serialize_operations(turn, stale_cell_ids=stale_cell_ids),
        "turnId": turn.turn_id,
        "sessionId": turn.session_id,
        "baseRevision": turn.base_revision,
        "prompt": turn.prompt,
        "model": turn.model,
        "mode": turn.mode,
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
        "historyTruncated": False,
    }


def _truncate(value: str, max_bytes: int) -> str:
    encoded = value.encode("utf-8")
    if len(encoded) <= max_bytes:
        return value
    return encoded[:max(0, max_bytes - 3)].decode("utf-8", errors="ignore") + "..."


def serialize_turn_summary(
    turn: AgentTurn, *, undo_eligible: bool = False,
    stale_cell_ids: frozenset[str] = frozenset(),
) -> dict[str, Any]:
    change_count = len(turn.changes)
    source_budget = min(2048, max(64, 80_000 // max(1, change_count * 2)))
    result = serialize_turn(
        turn, undo_eligible=undo_eligible, stale_cell_ids=stale_cell_ids
    )
    result["prompt"] = _truncate(turn.prompt, 8192)
    result["finalOutput"] = _truncate(turn.final_output, 8192)
    result["editableCellIds"] = [_truncate(value, 256) for value in turn.editable_cell_ids[:128]]
    result["contextCellIds"] = [_truncate(value, 256) for value in turn.context_cell_ids[:128]]
    result["changes"] = [
        {
            "cellId": _truncate(item.cell_id, 256),
            "previousSource": _truncate(item.previous_source, source_budget),
            "nextSource": _truncate(item.next_source, source_budget),
        }
        for item in turn.changes[:128]
    ]
    if turn.error is not None:
        result["error"] = {
            "code": _truncate(str(turn.error.get("code", "turn_error")), 256),
            "message": _truncate(str(turn.error.get("message", "Agent turn failed")), 4096),
            "details": {},
        }
    result["historyTruncated"] = (
        change_count > len(result["changes"])
        or len(turn.editable_cell_ids) > len(result["editableCellIds"])
        or len(turn.context_cell_ids) > len(result["contextCellIds"])
        or result["prompt"] != turn.prompt
        or result["finalOutput"] != turn.final_output
        or (
            turn.error is not None
            and (
                result["error"]["code"] != str(turn.error.get("code", "turn_error"))
                or result["error"]["message"] != str(turn.error.get("message", "Agent turn failed"))
                or bool(turn.error.get("details"))
            )
        )
        or any(
            serialized["previousSource"] != original.previous_source
            or serialized["nextSource"] != original.next_source
            for serialized, original in zip(result["changes"], turn.changes)
        )
    )
    if len(json.dumps(result, separators=(",", ":")).encode()) <= MAX_TURN_SUMMARY_BYTES:
        return result
    result["changes"] = [
        {"cellId": item["cellId"], "previousSource": "", "nextSource": ""}
        for item in result["changes"][:64]
    ]
    result["editableCellIds"] = result["editableCellIds"][:64]
    result["contextCellIds"] = result["contextCellIds"][:64]
    result["prompt"] = _truncate(result["prompt"], 1024)
    result["finalOutput"] = _truncate(result["finalOutput"], 1024)
    result["historyTruncated"] = True
    if len(json.dumps(result, separators=(",", ":")).encode()) <= MAX_TURN_SUMMARY_BYTES:
        return result
    result["changes"] = []
    result["editableCellIds"] = []
    result["contextCellIds"] = []
    result["error"] = None
    # Dropped last: without the hunk text above, the ledger is the only thing
    # left that says a change is still unreviewed. The client refetches full
    # detail for the selected turn when historyTruncated is set.
    result["operations"] = []
    return result


def _serialize_current(service, turn: AgentTurn) -> dict[str, Any]:
    return serialize_turn(
        turn,
        undo_eligible=service.is_undo_eligible(turn),
        stale_cell_ids=service.stale_cell_ids(turn),
    )


@router.post("", status_code=202)
def start_turn(body: StartTurnRequest, request: Request) -> dict[str, Any]:
    service = request.app.state.agent_turn_service
    turn = service.start(
        prompt=body.prompt, session_id=body.session_id,
        expected_revision=body.expected_revision,
        model=body.model, mode=body.mode,
    )
    return _serialize_current(service, turn)


@router.get("/{turn_id}")
def get_turn(turn_id: str, request: Request) -> dict[str, Any]:
    service = request.app.state.agent_turn_service
    return _serialize_current(service, service.get(turn_id))


@router.post("/{turn_id}/cancel")
def cancel_turn(
    turn_id: str, body: MutationRequest, request: Request,
) -> dict[str, Any]:
    service = request.app.state.agent_turn_service
    turn = service.cancel(
        turn_id, session_id=body.session_id,
        expected_revision=body.expected_revision,
    )
    return _serialize_current(service, turn)


@router.post("/{turn_id}/operations/accept-all")
def accept_all_operations(
    turn_id: str, body: AcceptRequest, request: Request,
) -> dict[str, Any]:
    service = request.app.state.agent_turn_service
    turn = service.accept_operations(turn_id, None, session_id=body.session_id)
    return _serialize_current(service, turn)


@router.post("/{turn_id}/operations/reject-all")
def reject_all_operations(
    turn_id: str, body: MutationRequest, request: Request,
) -> dict[str, Any]:
    return serialize_snapshot(
        request.app.state.agent_turn_service.reject_operations(
            turn_id, None, session_id=body.session_id,
            expected_revision=body.expected_revision,
        )
    )


@router.post("/{turn_id}/operations/{operation_id}/accept")
def accept_operation(
    turn_id: str, operation_id: str, body: AcceptRequest, request: Request,
) -> dict[str, Any]:
    service = request.app.state.agent_turn_service
    turn = service.accept_operations(
        turn_id, [operation_id], session_id=body.session_id
    )
    return _serialize_current(service, turn)


@router.post("/{turn_id}/operations/{operation_id}/reject")
def reject_operation(
    turn_id: str, operation_id: str, body: MutationRequest, request: Request,
) -> dict[str, Any]:
    return serialize_snapshot(
        request.app.state.agent_turn_service.reject_operations(
            turn_id, [operation_id], session_id=body.session_id,
            expected_revision=body.expected_revision,
        )
    )


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
