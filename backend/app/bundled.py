"""One process, one origin: the built frontend and the API together.

``scripts/dev.py`` runs uvicorn and Vite as two processes on two ports and lets
Vite proxy ``/api`` to the backend. That is the right shape for development and
the wrong shape for anything that ships: it needs Node at runtime, it occupies
two ports, and it puts the browser on a different origin from the API.

The bundled app has neither problem. The built SPA is served from ``dist/`` at
the root, the API is mounted under ``/api`` — which is the prefix the frontend
already calls (``frontend/src/api/client.ts``) — and a local client and the
browser tab therefore share one origin.

Because that origin is a loopback port with a Jupyter kernel behind it, every
request is checked against ``Origin``/``Host`` first. See ``_looks_loopback``.
"""

from __future__ import annotations

import ipaddress
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .agent_workspace.models import AgentAdapter
from .main import configured_agent_adapter, create_app


# The repository root, where `npm run build` writes `dist/`.
DEFAULT_DIST_DIR = Path(__file__).resolve().parents[2] / "dist"

_LOOPBACK_NAMES = frozenset({"localhost"})


def _looks_loopback(hostname: str | None) -> bool:
    """Whether a hostname denotes this machine's loopback interface.

    This is the DNS-rebinding check the MCP specification requires of a local
    HTTP server, and the reason it works is worth stating: a rebinding attack
    gets a browser to resolve the *attacker's* name to 127.0.0.1, so the request
    arrives carrying ``Host: evil.example`` — a real name, not a loopback
    literal. Requiring a literal rejects it while leaving genuine local clients
    (which address the port as ``127.0.0.1`` or ``localhost``) untouched.

    CORS does not cover this. It withholds the response from a cross-origin
    reader but still lets the request execute, and after a rebind the page's
    origin *is* the rebound host, so it never engages at all.
    """
    if not hostname:
        return False
    name = hostname.strip().lower().strip("[]")
    if name in _LOOPBACK_NAMES:
        return True
    try:
        return ipaddress.ip_address(name).is_loopback
    except ValueError:
        # A name that is not an IP literal and not "localhost" — including the
        # rebound attacker domain this check exists to reject.
        return False


def _hostname_of(value: str | None) -> str | None:
    """The hostname in an Origin URL or a Host header, port and brackets removed."""
    if not value:
        return None
    candidate = value.strip()
    if "//" not in candidate:
        # A Host header is bare authority ("127.0.0.1:8000", "[::1]:8000").
        candidate = "//" + candidate
    try:
        return urlsplit(candidate).hostname
    except ValueError:
        return None


def request_is_local(origin: str | None, host: str | None) -> bool:
    """Whether a request may be served, given its ``Origin`` and ``Host``.

    An ``Origin`` is authoritative when present — including the opaque ``null``
    a sandboxed iframe sends, which is not loopback and so is refused. Requests
    without one (curl, httpx, anything not a browser) fall through to ``Host``.
    """
    if origin is not None:
        return _looks_loopback(_hostname_of(origin))
    return _looks_loopback(_hostname_of(host))


def _resolve_within(root: Path, relative: str) -> Path | None:
    """Resolve ``relative`` under ``root``, or None if it escapes or is absent."""
    try:
        candidate = (root / relative.lstrip("/")).resolve()
    except (OSError, ValueError):
        return None
    if candidate != root and root not in candidate.parents:
        return None
    return candidate if candidate.is_file() else None


def create_bundled_app(
    *,
    dist_dir: Path | None = None,
    agent_adapter: AgentAdapter | None = None,
) -> FastAPI:
    """Serve the built SPA and the API from a single loopback origin.

    ``dist_dir`` must already contain a production build (``npm run build``).
    The API keeps its own routes unchanged; it is simply mounted under ``/api``,
    so ``/api/notebooks/current`` reaches ``GET /notebooks/current``.

    ``agent_adapter`` defaults to the environment-configured one, the same as
    the module-level app in ``main``. It must not be left to ``create_app``'s
    own default, which is the *test* fake: a human sending an agent turn from
    the bundled tab would get canned answers and no indication why.
    """
    dist = (dist_dir or DEFAULT_DIST_DIR).resolve()
    index = dist / "index.html"
    if not index.is_file():
        raise RuntimeError(
            f"No built frontend at {dist}. Run `npm run build` first."
        )

    # Same origin for browser and API, so the dev server's cross-origin
    # allowance is not just unnecessary here — it is misleading.
    api = create_app(
        agent_adapter=agent_adapter or configured_agent_adapter(),
        cors_origins=(),
    )
    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        # Starlette does not run a *mounted* app's lifespan, so without this the
        # API's shutdown never fires and the kernel subprocess, agent-turn
        # threads, and plot-tuning shadow kernels all outlive the server. A
        # launcher that starts and stops the bundle repeatedly would leak one
        # kernel per run.
        async with api.router.lifespan_context(api):
            yield

    app = FastAPI(title="Local Notebook Agent Editor (bundled)", lifespan=lifespan)
    # The mounted API, reachable without walking `app.routes`. A launcher that
    # wants the services (or a test that wants the adapter) asks here.
    app.state.api = api
    app.state.dist_dir = dist

    @app.middleware("http")
    async def reject_non_loopback(request: Request, call_next):
        if not request_is_local(
            request.headers.get("origin"), request.headers.get("host")
        ):
            return JSONResponse(
                status_code=403,
                content={
                    "error": {
                        "code": "forbidden_origin",
                        "message": (
                            "This editor serves loopback requests only. Open it "
                            "at http://127.0.0.1 on this machine."
                        ),
                        "details": {},
                    }
                },
            )
        return await call_next(request)

    # Order matters: the API and the hashed assets must both be matched before
    # the catch-all below, or an unknown /api route would answer with the SPA
    # shell instead of the API's own JSON 404.
    app.mount("/api", api)
    assets = dist / "assets"
    if assets.is_dir():
        app.mount("/assets", StaticFiles(directory=assets), name="assets")

    @app.get("/{spa_path:path}")
    def serve_spa(spa_path: str) -> FileResponse:
        """Serve a real file under dist/ when one exists, else the SPA shell.

        The fallback is what lets the client router own its own URLs: a deep
        link the server has never heard of still loads the app.
        """
        if spa_path:
            existing = _resolve_within(dist, spa_path)
            if existing is not None:
                return FileResponse(existing)
        return FileResponse(index)

    return app
