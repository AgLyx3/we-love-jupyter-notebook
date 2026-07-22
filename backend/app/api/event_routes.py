from __future__ import annotations

import json
from dataclasses import asdict

from fastapi import APIRouter, Query, Request
from fastapi.responses import StreamingResponse

router = APIRouter()


@router.get("/events")
async def events(
    request: Request,
    session_id: str | None = Query(default=None, alias="sessionId"),
    after: int = Query(default=0, ge=0),
) -> StreamingResponse:
    service = request.app.state.session_event_service
    target_session_id = (
        session_id or request.app.state.notebook_service.get_snapshot().session_id
    )

    async def generate():
        async for event in service.stream(
            session_id=target_session_id, after=after,
            is_disconnected=request.is_disconnected,
        ):
            if event is None:
                yield ": keep-alive\n\n"
            else:
                payload = json.dumps(asdict(event), separators=(",", ":"))
                yield f"id: {event.sequence}\nevent: {event.event_type}\ndata: {payload}\n\n"

    return StreamingResponse(generate(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})
