from __future__ import annotations

import json
from dataclasses import asdict

from fastapi import APIRouter, Query, Request
from fastapi.responses import StreamingResponse

router = APIRouter()


@router.get("/events")
def events(request: Request, after: int = Query(default=0, ge=0)) -> StreamingResponse:
    service = request.app.state.session_event_service

    def generate():
        for event in service.stream(after):
            if event is None:
                yield ": keep-alive\n\n"
            else:
                payload = json.dumps(asdict(event), separators=(",", ":"))
                yield f"id: {event.sequence}\nevent: {event.event_type}\ndata: {payload}\n\n"

    return StreamingResponse(generate(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})
