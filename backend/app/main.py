from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from .api.notebook_routes import router as notebook_router
from .api.agent_turn_routes import router as agent_turn_router
from .api.turn_scope_routes import router as turn_scope_router
from .agent_turns.service import AgentTurnService
from .agent_workspace.adapters import FakeAgentAdapter
from .agent_workspace.models import AgentAdapter
from .notebook_document.models import NotebookDomainError
from .notebook_document.service import NotebookDocumentService
from .turn_scope.service import TurnScopeService


def create_app(*, agent_adapter: AgentAdapter | None = None) -> FastAPI:
    app = FastAPI(title="Local Notebook Agent Editor")
    app.state.notebook_service = NotebookDocumentService()
    app.state.turn_scope_service = TurnScopeService(app.state.notebook_service)
    app.state.agent_turn_service = AgentTurnService(
        documents=app.state.notebook_service,
        scopes=app.state.turn_scope_service,
        adapter=agent_adapter or FakeAgentAdapter(),
    )
    app.include_router(notebook_router)
    app.include_router(turn_scope_router)
    app.include_router(agent_turn_router)

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

    @app.exception_handler(RequestValidationError)
    async def request_validation_error_handler(
        _request: Request, error: RequestValidationError
    ) -> JSONResponse:
        return JSONResponse(
            status_code=422,
            content={
                "error": {
                    "code": "invalid_request",
                    "message": "Request validation failed",
                    "details": {"errors": jsonable_encoder(error.errors())},
                }
            },
        )

    return app


app = create_app()
