"""One process, one origin: the built frontend and the API together.

``scripts/dev.py`` runs uvicorn and Vite as two processes on two ports and lets
Vite proxy ``/api`` to the backend. That is the right shape for development and
the wrong shape for anything that ships: it needs Node at runtime, it occupies
two ports, and it puts the browser on a different origin from the API.

The bundled app has neither problem. The built SPA is served at the root from
``web/`` beside this module, the API is mounted under ``/api`` — the prefix the
frontend already calls (``frontend/src/api/client.ts``) — and a local client
and the browser tab therefore share one origin.

Because that origin is a loopback port with a Jupyter kernel behind it, every
request is checked against ``Origin``/``Host`` first. See ``_looks_loopback``.
"""

from __future__ import annotations

import ipaddress
import os
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from .agent_workspace.models import AgentAdapter
from .main import configured_agent_adapters, configured_workspace_root, create_app


# Where to look for the built frontend, most specific first. Nothing here is an
# absolute literal — each is derived from this module's own location or the
# environment — so the bundle is not tied to one machine or one checkout.
#
# `npm run build` writes into `web/` beside this file (vite.config.ts), and
# pyproject.toml ships that directory as package data, so the same relative
# location holds whether the code is running from a checkout or from an
# installed wheel. That is the point of building into the package: a `dist/` at
# the repo root belongs to no package and travels nowhere.
#
# The repo-root `dist/` remains as a fallback for a tree built before that
# move, and the env var is the escape hatch for a packager that puts the
# frontend somewhere else again.
DIST_DIR_ENV_VAR = "NOTEBOOK_EDITOR_DIST"


def candidate_dist_dirs() -> tuple[Path, ...]:
    """Every place a built frontend might be, in the order they are tried."""
    here = Path(__file__).resolve()
    candidates = []
    override = os.environ.get(DIST_DIR_ENV_VAR)
    if override:
        candidates.append(Path(override).expanduser())
    candidates.append(here.parent / "web")       # where the build writes, and ships
    candidates.append(here.parents[2] / "dist")  # a tree built before that move
    return tuple(candidates)


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


def _locate_dist(dist_dir: Path | None) -> tuple[Path, Path]:
    """Find the built frontend, or say precisely where it was looked for.

    An explicit ``dist_dir`` is taken at its word and not searched around, so a
    caller pointing at the wrong place gets told about that place rather than
    being quietly served a build from somewhere else.
    """
    candidates = (dist_dir,) if dist_dir is not None else candidate_dist_dirs()
    for candidate in candidates:
        resolved = candidate.expanduser().resolve()
        if (resolved / "index.html").is_file():
            return resolved, resolved / "index.html"
    looked = "\n  ".join(str(c.expanduser().resolve()) for c in candidates)
    raise RuntimeError(
        "No built frontend found. Run `npm run build` in a source checkout, or "
        f"set {DIST_DIR_ENV_VAR} to the directory holding index.html.\n"
        f"Looked in:\n  {looked}"
    )


def create_bundled_app(
    *,
    dist_dir: Path | None = None,
    agent_adapter: AgentAdapter | None = None,
    workspace_root: str | Path | None = None,
) -> FastAPI:
    """Serve the built SPA and the API from a single loopback origin.

    ``dist_dir`` overrides the search; omitted, the built frontend is looked
    for in the places ``candidate_dist_dirs`` lists, which covers both a source
    checkout and an installed package.

    The API keeps its own routes unchanged; it is simply mounted under ``/api``,
    so ``/api/notebooks/current`` reaches ``GET /notebooks/current``.

    ``workspace_root`` confines every local path the API will accept. Omitted,
    it falls back to ``NOTEBOOK_WORKSPACE_ROOT`` — which is how a launcher that
    starts this module as a uvicorn *factory* configures it, since a factory
    takes no arguments. With neither, nothing is confined, which is what a
    person driving the file picker expects.

    ``agent_adapter`` pins the bundle to a single adapter; omitted, the bundle
    registers the same environment-configured set as the module-level app in
    ``main``, so the agent picker in the bundled tab offers what a developer
    run offers. It must not be left to ``create_app``'s own default, which is
    the *test* fake: a human sending an agent turn from the bundled tab would
    get canned answers and no indication why.
    """
    dist, index = _locate_dist(dist_dir)

    # An injected adapter is the whole registry, so a test (or a launcher that
    # has already chosen) is not silently given a real CLI alongside it.
    adapters, default_agent = (
        (None, None) if agent_adapter is not None else configured_agent_adapters()
    )

    # Same origin for browser and API, so the dev server's cross-origin
    # allowance is not just unnecessary here — it is misleading.
    api = create_app(
        agent_adapter=agent_adapter,
        agent_adapters=adapters,
        default_agent=default_agent,
        cors_origins=(),
        workspace_root=(
            workspace_root if workspace_root is not None else configured_workspace_root()
        ),
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
