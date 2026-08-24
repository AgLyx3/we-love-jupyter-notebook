from __future__ import annotations

from collections.abc import Sequence
from contextlib import asynccontextmanager
import os
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware

from .api.notebook_routes import router as notebook_router
from .api.file_routes import router as file_router
from .api.agent_turn_routes import router as agent_turn_router
from .api.turn_scope_routes import router as turn_scope_router
from .api.execution_routes import router as execution_router
from .api.event_routes import router as event_router
from .api.session_routes import router as session_router
from .api.tuning_routes import router as tuning_router
from .api.overview_routes import router as overview_router
from .agent_turns.service import AgentTurnService
from .agent_workspace.adapters import ClaudeAgentAdapter, DevelopmentFakeAgentAdapter, FakeAgentAdapter
from .agent_workspace.models import AgentAdapter
from .notebook_document.models import NotebookDomainError
from .notebook_document.service import NotebookDocumentService
from .notebook_overview.service import NotebookOverviewService
from .turn_scope.service import TurnScopeService
from .kernel_execution.service import KernelExecutionService
from .plot_tuning.apply import TuningApplyService
from .plot_tuning.panel import PlotTuningPanelService
from .session_events.service import SessionEventService
from .workspace_confinement import UNCONFINED, WorkspaceConfinement


DEV_SERVER_ORIGINS = ("http://127.0.0.1:5173", "http://localhost:5173")


def create_app(
    *,
    agent_adapter: AgentAdapter | None = None,
    cors_origins: Sequence[str] | None = None,
    workspace_root: str | Path | None = None,
) -> FastAPI:
    """Build the API.

    ``cors_origins`` defaults to the Vite dev server, which is the only
    cross-origin caller in the two-process development layout. Pass ``()`` when
    the API is served from the same origin as the frontend (see
    ``bundled.create_bundled_app``), where the allowance is dead config.

    ``workspace_root`` confines every path the server will open, list, search
    or write to that directory. Omitted, nothing is confined and the file
    picker reaches the whole filesystem as before — right for a person driving
    it, wrong once something else can call the same endpoints.
    """
    confinement = (
        WorkspaceConfinement(workspace_root) if workspace_root is not None else UNCONFINED
    )
    notebook_service = NotebookDocumentService(confinement=confinement)
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
            try:
                # Before the live kernel, so a shadow mid-replay is torn down by
                # its owner rather than left holding a process nobody tracks.
                _app.state.plot_tuning_panel_service.shutdown()
                _app.state.plot_tuning_apply_service.shutdown()
            finally:
                _app.state.kernel_execution_service.shutdown()

    app = FastAPI(title="Local Notebook Agent Editor", lifespan=lifespan)
    allowed_origins = list(
        DEV_SERVER_ORIGINS if cors_origins is None else cors_origins
    )
    if allowed_origins:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=allowed_origins,
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
        )
    app.state.workspace_confinement = confinement
    app.state.notebook_service = notebook_service
    app.state.session_event_service = session_event_service
    app.state.kernel_execution_service = kernel_execution_service
    app.state.turn_scope_service = TurnScopeService(notebook_service)
    app.state.agent_turn_service = AgentTurnService(
        documents=app.state.notebook_service,
        scopes=app.state.turn_scope_service,
        adapter=agent_adapter or FakeAgentAdapter(),
        executions=app.state.kernel_execution_service,
        events=app.state.session_event_service,
    )
    app.state.plot_tuning_panel_service = PlotTuningPanelService(
        documents=app.state.notebook_service,
    )
    app.state.plot_tuning_apply_service = TuningApplyService(
        documents=app.state.notebook_service,
        executions=app.state.kernel_execution_service,
        events=app.state.session_event_service,
    )
    # Shares the agent adapter rather than owning a second route to the CLI:
    # segmentation is a read-only prompt call on the same class that runs agent
    # turns (overview spec §4.2). It starts nothing and caches in memory, so it
    # needs no lifespan shutdown hook.
    app.state.notebook_overview_service = NotebookOverviewService(
        documents=app.state.notebook_service,
        adapter=agent_adapter or FakeAgentAdapter(),
    )
    app.include_router(notebook_router)
    app.include_router(file_router)
    app.include_router(turn_scope_router)
    app.include_router(agent_turn_router)
    app.include_router(execution_router)
    app.include_router(event_router)
    app.include_router(session_router)
    app.include_router(tuning_router)
    app.include_router(overview_router)

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


WORKSPACE_ROOT_ENV_VAR = "NOTEBOOK_WORKSPACE_ROOT"


def configured_workspace_root() -> str | None:
    """The confinement root for the module-level app, if one is set.

    Read from the environment because `scripts/dev.py` starts uvicorn against
    `backend.app.main:app` rather than calling `create_app` itself. Unset — the
    ordinary development case — nothing is confined.
    """
    root = os.getenv(WORKSPACE_ROOT_ENV_VAR, "").strip()
    if not root:
        return None
    resolved = Path(root).expanduser()
    if not resolved.is_dir():
        raise RuntimeError(
            f"{WORKSPACE_ROOT_ENV_VAR} is not a directory: {resolved}"
        )
    return str(resolved)


def configured_agent_adapter() -> AgentAdapter:
    mode = os.getenv("NOTEBOOK_AGENT_ADAPTER", "claude").strip().lower()
    if mode == "claude":
        return ClaudeAgentAdapter()
    if mode == "fake":
        return DevelopmentFakeAgentAdapter()
    raise RuntimeError("NOTEBOOK_AGENT_ADAPTER must be either 'claude' or 'fake'")


app = create_app(
    agent_adapter=configured_agent_adapter(),
    workspace_root=configured_workspace_root(),
)
