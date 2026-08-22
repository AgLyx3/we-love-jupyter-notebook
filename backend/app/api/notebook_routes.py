from __future__ import annotations

from typing import Annotated, Any, Literal

from fastapi import APIRouter, File, Form, Request, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, Field

from ..notebook_document.models import NotebookSnapshot
from ..notebook_document.service import MAX_NOTEBOOK_BYTES, NotebookDocumentService

router = APIRouter()


class CellSourceRequest(BaseModel):
    session_id: str = Field(alias="sessionId")
    expected_revision: int = Field(alias="expectedDocumentRevision")
    source: str


class CellInsertRequest(BaseModel):
    session_id: str = Field(alias="sessionId")
    expected_revision: int = Field(alias="expectedDocumentRevision")
    source: str = ""
    cell_type: Literal["code", "markdown"] = Field(default="code", alias="cellType")
    # Where the new cell lands. Omitted, it goes at the end — the common case,
    # and the one that does not require the caller to have counted anything.
    index: int | None = None


class CellDeleteRequest(BaseModel):
    session_id: str = Field(alias="sessionId")
    expected_revision: int = Field(alias="expectedDocumentRevision")


class NotebookCloseRequest(BaseModel):
    session_id: str = Field(alias="sessionId")
    expected_revision: int = Field(alias="expectedDocumentRevision")


class NotebookOpenRequest(BaseModel):
    path: str
    workspace_root: str | None = Field(default=None, alias="workspaceRoot")
    session_id: str | None = Field(default=None, alias="sessionId")
    expected_revision: int | None = Field(
        default=None, alias="expectedDocumentRevision"
    )


class NotebookSaveRequest(BaseModel):
    session_id: str = Field(alias="sessionId")
    expected_revision: int = Field(alias="expectedDocumentRevision")


class NotebookSaveAsRequest(BaseModel):
    path: str
    session_id: str = Field(alias="sessionId")
    expected_revision: int = Field(alias="expectedDocumentRevision")
    workspace_root: str | None = Field(default=None, alias="workspaceRoot")


def _service(request: Request) -> NotebookDocumentService:
    return request.app.state.notebook_service


def _cell_source(cell: dict[str, Any]) -> str:
    source = cell.get("source", "")
    return "".join(source) if isinstance(source, list) else source


def serialize_snapshot(snapshot: NotebookSnapshot) -> dict[str, Any]:
    cells = []
    for index, cell in enumerate(snapshot.notebook["cells"]):
        cells.append(
            {
                "cellId": cell["id"],
                "index": index,
                "cellType": cell["cell_type"],
                "source": _cell_source(cell),
                "metadata": cell.get("metadata", {}),
                "outputs": cell.get("outputs", []),
                "executionCount": cell.get("execution_count"),
            }
        )
    return {
        "sessionId": snapshot.session_id,
        "filename": snapshot.filename,
        "revision": snapshot.revision,
        "dirty": snapshot.dirty,
        "metadata": snapshot.notebook.get("metadata", {}),
        "nbformat": snapshot.notebook["nbformat"],
        "nbformatMinor": snapshot.notebook["nbformat_minor"],
        "notebookPath": snapshot.notebook_path,
        "workspaceRoot": snapshot.workspace_root,
        "cells": cells,
    }


@router.post("/notebooks/upload", status_code=201)
async def upload_notebook(
    request: Request,
    file: Annotated[UploadFile, File()],
    session_id: Annotated[str | None, Form(alias="sessionId")] = None,
    expected_revision: Annotated[
        int | None, Form(alias="expectedDocumentRevision")
    ] = None,
) -> dict[str, Any]:
    payload = await file.read(MAX_NOTEBOOK_BYTES + 1)
    snapshot = _service(request).import_notebook(
        payload,
        filename=file.filename or "notebook.ipynb",
        expected_session_id=session_id,
        expected_revision=expected_revision,
    )
    request.app.state.session_event_service.publish(
        "notebook.updated",
        {"sessionId": snapshot.session_id, "revision": snapshot.revision, "ownerId": "upload"},
    )
    return serialize_snapshot(snapshot)


@router.post("/notebooks/open")
def open_notebook(body: NotebookOpenRequest, request: Request) -> dict[str, Any]:
    snapshot = _service(request).open_notebook_from_path(
        body.path,
        workspace_root=body.workspace_root,
        expected_session_id=body.session_id,
        expected_revision=body.expected_revision,
    )
    request.app.state.session_event_service.publish(
        "notebook.updated",
        {"sessionId": snapshot.session_id, "revision": snapshot.revision, "ownerId": "open"},
    )
    return serialize_snapshot(snapshot)


@router.post("/notebooks/save")
def save_notebook(body: NotebookSaveRequest, request: Request) -> dict[str, Any]:
    snapshot = _service(request).save_notebook_to_disk(
        expected_session_id=body.session_id,
        expected_revision=body.expected_revision,
    )
    return serialize_snapshot(snapshot)


@router.post("/notebooks/save-as")
def save_notebook_as(body: NotebookSaveAsRequest, request: Request) -> dict[str, Any]:
    snapshot = _service(request).save_notebook_as(
        body.path,
        expected_session_id=body.session_id,
        expected_revision=body.expected_revision,
        workspace_root=body.workspace_root,
    )
    return serialize_snapshot(snapshot)


@router.get("/notebooks/current")
def current_notebook(request: Request) -> dict[str, Any]:
    return serialize_snapshot(_service(request).get_snapshot())


@router.delete("/notebooks/current")
def close_notebook(
    body: NotebookCloseRequest, request: Request,
) -> dict[str, Any]:
    result = _service(request).close_notebook(
        expected_session_id=body.session_id,
        expected_revision=body.expected_revision,
    )
    return {
        "closedSessionId": result.closed_session_id,
        "cleanupErrors": list(result.cleanup_errors),
    }


@router.get("/notebooks/download")
def download_notebook(request: Request) -> Response:
    filename, content = _service(request).export_notebook()
    return Response(
        content=content,
        media_type="application/x-ipynb+json",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.post("/cells", status_code=201)
def insert_cell(body: CellInsertRequest, request: Request) -> dict[str, Any]:
    """Add a cell. It is marked as agent-authored and is NOT executed."""
    snapshot = _service(request).insert_cell(
        source=body.source,
        cell_type=body.cell_type,
        index=body.index,
        expected_session_id=body.session_id,
        expected_revision=body.expected_revision,
        owner="cell_insert",
    )
    return serialize_snapshot(snapshot)


@router.delete("/cells/{cell_id}")
def delete_cell(cell_id: str, body: CellDeleteRequest, request: Request) -> dict[str, Any]:
    snapshot = _service(request).delete_cell(
        cell_id=cell_id,
        expected_session_id=body.session_id,
        expected_revision=body.expected_revision,
        owner="cell_delete",
    )
    return serialize_snapshot(snapshot)


@router.post("/cells/{cell_id}/source")
def update_cell_source(
    cell_id: str,
    body: CellSourceRequest,
    request: Request,
) -> dict[str, Any]:
    snapshot = _service(request).update_cell_source(
        cell_id=cell_id,
        source=body.source,
        expected_revision=body.expected_revision,
        expected_session_id=body.session_id,
        owner="manual",
    )
    request.app.state.session_event_service.publish(
        "notebook.updated",
        {"sessionId": snapshot.session_id, "revision": snapshot.revision, "ownerId": "manual"},
    )
    cell = next(cell for cell in snapshot.notebook["cells"] if cell["id"] == cell_id)
    return {
        "sessionId": snapshot.session_id,
        "cellId": cell_id,
        "source": _cell_source(cell),
        "revision": snapshot.revision,
        "dirty": snapshot.dirty,
    }
