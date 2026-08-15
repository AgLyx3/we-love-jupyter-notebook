"""HTTP surface for interactive plot tuning.

The route split mirrors the one distinction the whole feature rests on: `open`
is free and starts nothing, `warm` and `preview` cost a shadow kernel but touch
no document, and only `apply` mutates the notebook. Keeping those on separate
endpoints is what lets the client put the risky-cell confirmation between
analysis and the first execution.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from ..plot_tuning.apply import TuningApplyService, serialize_record
from ..plot_tuning.discovery import Knob
from ..plot_tuning.panel import (
    PanelError,
    PlotTuningPanelService,
    UnknownShadow,
    serialize_knob,
)
from ..plot_tuning.shadow import ShadowError, ShadowNotReady, UnknownKnob

router = APIRouter(prefix="/tuning")


class PanelRequest(BaseModel):
    session_id: str = Field(alias="sessionId")
    expected_revision: int = Field(alias="expectedDocumentRevision")
    cell_id: str = Field(alias="cellId")


class PreviewRequest(BaseModel):
    values: dict[str, Any]


class ApplyRequest(BaseModel):
    session_id: str = Field(alias="sessionId")
    expected_revision: int = Field(alias="expectedDocumentRevision")
    values: dict[str, Any]


class OperationRequest(BaseModel):
    session_id: str = Field(alias="sessionId")
    expected_revision: int = Field(alias="expectedDocumentRevision")
    operation_ids: list[str] | None = Field(default=None, alias="operationIds")


class AcceptRequest(BaseModel):
    # Keeping a hunk never touches the document, so unlike every other mutating
    # call here it needs no revision to guard against.
    session_id: str = Field(alias="sessionId")
    operation_ids: list[str] | None = Field(default=None, alias="operationIds")


def _panel(request: Request) -> PlotTuningPanelService:
    return request.app.state.plot_tuning_panel_service


def _apply(request: Request) -> TuningApplyService:
    return request.app.state.plot_tuning_apply_service


def _serialize_plan(plan) -> dict[str, Any]:
    return {
        "cellId": plan.target_cell_id,
        "revision": plan.document_revision,
        "knobs": [serialize_knob(knob) for knob in plan.discovery.knobs],
        # Rejections carry their own human-readable sentence. The panel's empty
        # state shows these instead of "no tunable variables found" — see the
        # design's §3.5. Dropping `detail` here would quietly undo that.
        "rejections": [
            {"name": item.name, "reason": item.reason, "detail": item.detail}
            for item in plan.discovery.rejections
        ],
        "blocked": plan.discovery.blocked,
        "risky": [
            {
                "cellId": item.cell_id, "cellIndex": item.cell_index,
                "categories": list(item.categories), "reasons": list(item.reasons),
            }
            for item in plan.risky
        ],
    }


def _preview_payload(result) -> dict[str, Any]:
    return {
        "outputs": list(result.outputs),
        "values": {name: value for name, value in result.values},
        "executedCellIds": list(result.executed_cell_ids),
        "seconds": result.seconds,
        "cached": result.cached,
        "failed": result.failed,
        "failedCellId": result.failed_cell_id,
    }


@router.post("/open")
def open_panel(body: PanelRequest, request: Request) -> dict[str, Any]:
    """Analysis only. Deliberately starts no kernel — see the module docstring."""
    try:
        plan = _panel(request).open_panel(
            session_id=body.session_id, expected_revision=body.expected_revision,
            cell_id=body.cell_id,
        )
    except PanelError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    return _serialize_plan(plan)


@router.post("/warm")
def warm(body: PanelRequest, request: Request) -> dict[str, Any]:
    """Spawn the shadow and replay the chain. The client calls this only after
    the user has approved whatever `open` reported as risky."""
    try:
        shadow, result = _panel(request).warm(
            session_id=body.session_id, expected_revision=body.expected_revision,
            cell_id=body.cell_id,
        )
    except PanelError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except ShadowError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    return {
        "shadowId": shadow.shadow_id,
        "knobs": [serialize_knob(knob) for knob in result.knobs],
        "rejections": [
            {"name": item.name, "reason": item.reason, "detail": item.detail}
            for item in result.rejections
        ],
        "outputs": list(result.outputs),
        "timings": [
            {
                "cellId": timing.cell_id, "ordinal": timing.ordinal,
                "total": timing.total, "seconds": timing.seconds,
                "failed": timing.failed,
            }
            for timing in result.timings
        ],
        "totalSeconds": result.total_seconds,
        "failed": result.failed,
        "failedCellId": result.failed_cell_id,
    }


@router.post("/{shadow_id}/preview")
def preview(shadow_id: str, body: PreviewRequest, request: Request) -> dict[str, Any]:
    shadow = _shadow_or_404(request, shadow_id)
    try:
        return _preview_payload(shadow.preview(body.values))
    except UnknownKnob as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    except ShadowNotReady as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    except ShadowError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error


@router.post("/{shadow_id}/cancel")
def cancel(shadow_id: str, request: Request) -> dict[str, str]:
    _shadow_or_404(request, shadow_id).cancel()
    return {"shadowId": shadow_id, "state": "cancelled"}


@router.delete("/{shadow_id}")
def close(shadow_id: str, request: Request) -> dict[str, str]:
    # Closing something already gone is the success case, not a 404: the client
    # calls this on unmount, which races the idle timeout and the revision check.
    try:
        _panel(request).get_shadow(shadow_id)
    except UnknownShadow:
        return {"shadowId": shadow_id, "state": "closed"}
    _panel(request).close_current()
    return {"shadowId": shadow_id, "state": "closed"}


@router.post("/{shadow_id}/apply", status_code=202)
def apply_values(
    shadow_id: str, body: ApplyRequest, request: Request,
) -> dict[str, Any]:
    """Write the chosen values back and re-run the live chain.

    The knobs come from the shadow rather than the request body on purpose: a
    knob carries the source range that makes the rewrite safe, and accepting
    those over the wire would let a client point an edit at any bytes it liked.
    """
    shadow = _shadow_or_404(request, shadow_id)
    known = {knob.name: knob for knob in shadow.knobs}
    edits: list[tuple[Knob, Any]] = []
    for name, value in body.values.items():
        knob = known.get(name)
        if knob is None:
            raise HTTPException(status_code=400, detail=f"Unknown knob `{name}`")
        edits.append((knob, value))
    record = _apply(request).apply(
        session_id=body.session_id, expected_revision=body.expected_revision,
        edits=edits,
    )
    return serialize_record(record)


@router.get("/records/{record_id}")
def get_record(record_id: str, request: Request) -> dict[str, Any]:
    return serialize_record(_apply(request).get(record_id))


@router.post("/records/{record_id}/undo")
def undo(record_id: str, body: OperationRequest, request: Request) -> dict[str, Any]:
    """Undo the whole tune. Distinct from rejecting every operation, which
    selects only the hunks the user has not already reviewed."""
    return serialize_record(_apply(request).undo(
        record_id, session_id=body.session_id,
        expected_revision=body.expected_revision,
    ))


@router.post("/records/{record_id}/reject")
def reject(record_id: str, body: OperationRequest, request: Request) -> dict[str, Any]:
    return serialize_record(_apply(request).reject_operations(
        record_id, body.operation_ids, session_id=body.session_id,
        expected_revision=body.expected_revision,
    ))


@router.post("/records/{record_id}/accept")
def accept(record_id: str, body: AcceptRequest, request: Request) -> dict[str, Any]:
    return serialize_record(_apply(request).accept_operations(
        record_id, body.operation_ids, session_id=body.session_id,
    ))


def _shadow_or_404(request: Request, shadow_id: str):
    try:
        return _panel(request).get_shadow(shadow_id)
    except UnknownShadow as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
