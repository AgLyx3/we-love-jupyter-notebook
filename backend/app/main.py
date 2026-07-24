from __future__ import annotations

from contextlib import asynccontextmanager
import os

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware

from .api.notebook_routes import router as notebook_router
from .api.file_routes import router as file_router
from .api.agent_turn_routes import adapters_router, router as agent_turn_router
from .api.turn_scope_routes import router as turn_scope_router
from .api.execution_routes import router as execution_router
from .api.event_routes import router as event_router
from .api.session_routes import router as session_router
from .agent_turns.service import AgentTurnService
from .agent_workspace.adapters import (
    ClaudeAgentAdapter, CodexAgentAdapter, DevelopmentFakeAgentAdapter, FakeAgentAdapter,
)
from .agent_workspace.models import AgentAdapter
from .notebook_document.models import NotebookDomainError
from .notebook_document.service import NotebookDocumentService
from .turn_scope.service import TurnScopeService
from .kernel_execution.service import KernelExecutionService
from .session_events.service import SessionEventService


def create_app(
    *,
    agent_adapter: AgentAdapter | None = None,
    agent_adapters: dict[str, AgentAdapter] | None = None,
    default_agent: str | None = None,
) -> FastAPI:
    notebook_service = NotebookDocumentService()
    session_event_service = SessionEventService()
    kernel_execution_service = KernelExecutionService(
        documents=notebook_service, events=session_event_service,
    )
    notebook_service.register_session_replacement_listener(
        session_event_service.activate_session,
    )

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        yield
        try:
            _app.state.agent_turn_service.shutdown()
        finally:
            _app.state.kernel_execution_service.shutdown()

    app = FastAPI(title="Local Notebook Agent Editor", lifespan=lifespan)
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["http://127.0.0.1:5173", "http://localhost:5173"],
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
    )
    app.state.notebook_service = notebook_service
    app.state.session_event_service = session_event_service
    app.state.kernel_execution_service = kernel_execution_service
    app.state.turn_scope_service = TurnScopeService(notebook_service)
    app.state.agent_turn_service = AgentTurnService(
        documents=app.state.notebook_service,
        scopes=app.state.turn_scope_service,
        adapter=agent_adapter or FakeAgentAdapter(),
        adapters=agent_adapters,
        default_agent=default_agent or "default",
        executions=app.state.kernel_execution_service,
        events=app.state.session_event_service,
    )
    app.include_router(notebook_router)
    app.include_router(file_router)
    app.include_router(turn_scope_router)
    app.include_router(agent_turn_router)
    app.include_router(adapters_router)
    app.include_router(execution_router)
    app.include_router(event_router)
    app.include_router(session_router)

    @app.get("/health/ready")
    def health_ready() -> dict[str, str]:
        return {"status": "ready"}

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


def configured_agent_adapters() -> tuple[dict[str, AgentAdapter], str]:
    mode = os.getenv("NOTEBOOK_AGENT_ADAPTER", "claude").strip().lower()
    if mode in ("claude", "codex"):
        return {"claude": ClaudeAgentAdapter(), "codex": CodexAgentAdapter()}, mode
    if mode == "fake":
        return {"fake": DevelopmentFakeAgentAdapter()}, "fake"
    raise RuntimeError(
        "NOTEBOOK_AGENT_ADAPTER must be one of 'claude', 'codex', or 'fake'"
    )


_adapters, _default_agent = configured_agent_adapters()
app = create_app(agent_adapters=_adapters, default_agent=_default_agent)
