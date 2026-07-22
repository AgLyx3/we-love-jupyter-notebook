from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from pydantic import BaseModel, Field

from ..kernel_execution.service import KernelExecutionService, serialize_operation

router = APIRouter()


class ExecutionRequest(BaseModel):
    session_id: str = Field(alias="sessionId")
    expected_revision: int = Field(alias="expectedDocumentRevision")


class DecisionRequest(ExecutionRequest):
    turn_id: str = Field(alias="turnId")
    cell_id: str = Field(alias="cellId")


class KernelControlRequest(BaseModel):
    session_id: str = Field(alias="sessionId")
    expected_revision: int = Field(alias="expectedDocumentRevision")
    execution_attempt_id: str | None = Field(default=None, alias="executionAttemptId")


def _service(request: Request) -> KernelExecutionService:
    return request.app.state.kernel_execution_service


@router.post("/execution/cells/{cell_id}/run", status_code=202)
def run_cell(cell_id: str, body: ExecutionRequest, request: Request) -> dict[str, Any]:
    return serialize_operation(_service(request).start_cell(cell_id=cell_id, session_id=body.session_id, expected_revision=body.expected_revision))


@router.post("/execution/run-all", status_code=202)
def run_all(body: ExecutionRequest, request: Request) -> dict[str, Any]:
    return serialize_operation(_service(request).start_all(session_id=body.session_id, expected_revision=body.expected_revision))


@router.get("/execution/{execution_id}")
def get_execution(execution_id: str, request: Request) -> dict[str, Any]:
    return serialize_operation(_service(request).get(execution_id))


@router.post("/execution/{attempt_id}/{decision}")
def decide_execution(attempt_id: str, decision: str, body: DecisionRequest, request: Request) -> dict[str, Any]:
    if decision not in {"approve", "skip", "cancel"}:
        from fastapi import HTTPException
        raise HTTPException(404)
    operation = getattr(_service(request), decision)(
        attempt_id, session_id=body.session_id,
        expected_revision=body.expected_revision,
        turn_id=body.turn_id, cell_id=body.cell_id,
    )
    return serialize_operation(operation)


@router.get("/kernel/status")
def kernel_status(request: Request) -> dict[str, Any]:
    return _service(request).kernel_status()


@router.post("/kernel/{kernel_session_id}/interrupt")
def interrupt_kernel(kernel_session_id: str, body: KernelControlRequest, request: Request) -> dict[str, Any]:
    snapshot = request.app.state.notebook_service.get_snapshot()
    request.app.state.notebook_service.check_snapshot_preconditions(snapshot, body.session_id, body.expected_revision)
    return _service(request).interrupt(kernel_session_id, execution_attempt_id=body.execution_attempt_id)


@router.post("/kernel/{kernel_session_id}/restart")
def restart_kernel(kernel_session_id: str, body: KernelControlRequest, request: Request) -> dict[str, Any]:
    snapshot = request.app.state.notebook_service.get_snapshot()
    request.app.state.notebook_service.check_snapshot_preconditions(snapshot, body.session_id, body.expected_revision)
    return _service(request).restart(kernel_session_id, execution_attempt_id=body.execution_attempt_id)
