"""HTTP surface for the notebook overview panel.

The route split is the same one `tuning_routes.py` makes, for the same reason:
free analysis on one endpoint, the thing that costs a model call on another, so
the client decides when to spend. `GET /overview` never calls the model —
opening a notebook must not spend anything — and `POST /overview/generate` is
always an explicit user action.

Both carry `sessionId` and `expectedDocumentRevision` like every other route
here, and 409 through the shared handler when they do not match.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from ..notebook_overview.models import Block, Overview
from ..notebook_overview.service import NotebookOverviewService

router = APIRouter(prefix="/overview")


class OverviewRequest(BaseModel):
    session_id: str = Field(alias="sessionId")
    expected_revision: int = Field(alias="expectedDocumentRevision")


def _service(request: Request) -> NotebookOverviewService:
    return request.app.state.notebook_overview_service


def serialize_block(block: Block) -> dict[str, Any]:
    return {
        "start": block.start,
        "end": block.end,
        # Empty in the deterministic fallback, where names are genuinely
        # unavailable. The client renders the range rather than inventing one.
        "name": block.name,
        "state": block.state,
        "produces": list(block.produces),
        "defines": [
            {"name": item.name, "callSites": list(item.call_sites)}
            for item in block.defines
        ],
        "marks": list(block.marks),
    }


def serialize_overview(overview: Overview) -> dict[str, Any]:
    return {
        "blocks": [serialize_block(block) for block in overview.blocks],
        # Which of the two maps this is. The panel has to say so: the fallback's
        # boundaries are measurably degraded, not merely terser (spec §6).
        "generated": overview.generated,
        "stale": overview.stale,
        "cellCount": overview.cell_count,
        "error": overview.error,
    }


@router.get("")
def get_overview(
    sessionId: str, expectedDocumentRevision: int, request: Request,
) -> dict[str, Any]:
    """Computed fields plus cached blocks if any. Free — starts nothing."""
    return serialize_overview(_service(request).get(
        session_id=sessionId, expected_revision=expectedDocumentRevision,
    ))


@router.post("/generate")
def generate_overview(body: OverviewRequest, request: Request) -> dict[str, Any]:
    """Run the model pass and cache it. One CLI call, always user-initiated.

    A failed generation is not an HTTP error: the response carries the fallback
    map and an `error` string, because the panel stays useful either way and
    "generation failed" alone is not enough for the user to act on.
    """
    return serialize_overview(_service(request).generate(
        session_id=body.session_id, expected_revision=body.expected_revision,
    ))
