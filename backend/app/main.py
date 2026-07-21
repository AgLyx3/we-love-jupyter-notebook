from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from .api.notebook_routes import router as notebook_router
from .notebook_document.models import NotebookDomainError
from .notebook_document.service import NotebookDocumentService


def create_app() -> FastAPI:
    app = FastAPI(title="Local Notebook Agent Editor")
    app.state.notebook_service = NotebookDocumentService()
    app.include_router(notebook_router)

    @app.exception_handler(NotebookDomainError)
    async def notebook_error_handler(
        _request: Request, error: NotebookDomainError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=error.status_code,
            content={
                "error": {
                    "code": error.code,
                    "message": error.message,
                    "details": error.details,
                }
            },
        )

    return app


app = create_app()
