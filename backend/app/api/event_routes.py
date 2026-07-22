from __future__ import annotations

import json
from dataclasses import asdict

from fastapi import APIRouter, Header, Query, Request
from fastapi.responses import StreamingResponse

router = APIRouter()


@router.get("/events")
async def events(
    request: Request,
    session_id: str | None = Query(default=None, alias="sessionId"),
    after: int = Query(default=0, ge=0),
    last_event_id: str | None = Header(default=None, alias="Last-Event-ID"),
) -> StreamingResponse:
    service = request.app.state.session_event_service
    target_session_id = (
        session_id or request.app.state.notebook_service.get_snapshot().session_id
    )
    header_cursor: int | None = None
    if last_event_id is not None:
        try:
            parsed = int(last_event_id.strip())
            if parsed >= 0:
                header_cursor = parsed
        except ValueError:
            pass
    cursor = max(after, header_cursor or 0)

    async def generate():
        async for event in service.stream(
            session_id=target_session_id, after=cursor,
            is_disconnected=request.is_disconnected,
        ):
            if event is None:
                yield ": keep-alive\n\n"
            else:
                payload = json.dumps(asdict(event), separators=(",", ":"))
                yield f"id: {event.sequence}\nevent: {event.event_type}\ndata: {payload}\n\n"

    return StreamingResponse(generate(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})
