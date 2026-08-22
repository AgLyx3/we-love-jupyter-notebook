"""The one-process bundle: SPA and API on a single loopback origin."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.app import bundled as bundled_module
from backend.app.agent_workspace.adapters import ClaudeAgentAdapter, FakeAgentAdapter
from backend.app.bundled import (
    DIST_DIR_ENV_VAR,
    _resolve_within,
    candidate_dist_dirs,
    create_bundled_app,
    request_is_local,
)

# A directory that exists and is not the one a test names explicitly, used to
# show the explicit choice is not silently replaced by a discovered build.
DEFAULT_REAL_DIST = Path(__file__).resolve().parents[2] / "dist"


INDEX_HTML = (
    '<!doctype html><html><head><title>Notebook Agent Editor</title>'
    '<script type="module" src="/assets/index-abc.js"></script></head>'
    '<body><div id="root"></div></body></html>'
)


@pytest.fixture
def dist(tmp_path):
    """A stand-in for `npm run build` output, same shape as the real one."""
    root = tmp_path / "dist"
    (root / "assets").mkdir(parents=True)
    (root / "index.html").write_text(INDEX_HTML, encoding="utf-8")
    (root / "assets" / "index-abc.js").write_text("export const x = 1;\n", encoding="utf-8")
    (root / "favicon.svg").write_text("<svg/>", encoding="utf-8")
    return root


@pytest.fixture
def bundled(dist):
    # A loopback base_url: the app refuses anything else, which is the point.
    with TestClient(
        create_bundled_app(dist_dir=dist), base_url="http://127.0.0.1:8000"
    ) as test_client:
        yield test_client


def test_requires_a_built_frontend(tmp_path):
    with pytest.raises(RuntimeError, match="npm run build"):
        create_bundled_app(dist_dir=tmp_path / "nonexistent")


def test_the_error_names_every_place_it_looked(tmp_path, monkeypatch):
    """So a packager can see why the search missed, not just that it did."""
    searched = (tmp_path / "nowhere", tmp_path / "also-nowhere")
    monkeypatch.setattr(bundled_module, "candidate_dist_dirs", lambda: searched)
    with pytest.raises(RuntimeError) as raised:
        create_bundled_app()
    message = str(raised.value)
    assert DIST_DIR_ENV_VAR in message
    for candidate in searched:
        assert str(candidate.resolve()) in message


def test_no_candidate_location_is_an_absolute_literal():
    """Every candidate is derived — from this module or the environment.

    A path baked into the source would tie the bundle to the machine it was
    written on, which is the one thing a distributed artefact cannot be.
    """
    source = Path(bundled_module.__file__).read_text(encoding="utf-8")
    for marker in ("/home/", "/Users/", "/tmp/", "/root/", "C:\\\\"):
        assert marker not in source, f"absolute path literal {marker!r} in bundled.py"


def test_an_installed_layout_finds_the_frontend_inside_the_package(tmp_path, monkeypatch):
    """The layout a wheel produces.

    Installed, this module lives in site-packages and the repo-root `dist/`
    walk lands on site-packages itself, so the frontend has to travel inside
    the package instead. Simulated here by pointing the package-local
    candidate at a real build.
    """
    package_web = tmp_path / "web"
    (package_web / "assets").mkdir(parents=True)
    (package_web / "index.html").write_text(INDEX_HTML, encoding="utf-8")

    monkeypatch.setattr(
        bundled_module,
        "candidate_dist_dirs",
        lambda: (package_web, tmp_path / "absent"),
    )
    app = create_bundled_app(agent_adapter=FakeAgentAdapter())
    assert app.state.dist_dir == package_web.resolve()


def test_the_environment_override_wins(tmp_path, monkeypatch):
    chosen = tmp_path / "packaged-elsewhere"
    (chosen / "assets").mkdir(parents=True)
    (chosen / "index.html").write_text(INDEX_HTML, encoding="utf-8")
    monkeypatch.setenv(DIST_DIR_ENV_VAR, str(chosen))
    assert candidate_dist_dirs()[0] == chosen

    app = create_bundled_app(agent_adapter=FakeAgentAdapter())
    assert app.state.dist_dir == chosen.resolve()


def test_an_explicit_dist_dir_is_not_searched_around(tmp_path, monkeypatch):
    """A caller naming a directory is told about that one, not quietly served
    a build discovered somewhere else."""
    monkeypatch.setenv(DIST_DIR_ENV_VAR, str(DEFAULT_REAL_DIST))
    with pytest.raises(RuntimeError) as raised:
        create_bundled_app(dist_dir=tmp_path / "named-but-empty")
    assert "named-but-empty" in str(raised.value)
    assert str(DEFAULT_REAL_DIST) not in str(raised.value)


def test_defaults_to_the_configured_agent_adapter(dist, monkeypatch):
    """Not to `create_app`'s own default, which is the test fake.

    The browser path to agent turns stays open in the bundle, so falling
    through to the test fake would answer a human's real prompt with canned
    edits and nothing to indicate why.
    """
    monkeypatch.setenv("NOTEBOOK_AGENT_ADAPTER", "claude")
    app = create_bundled_app(dist_dir=dist)
    adapter = app.state.api.state.agent_turn_service.adapter
    assert isinstance(adapter, ClaudeAgentAdapter)


def test_an_explicit_adapter_still_wins(dist):
    injected = FakeAgentAdapter()
    app = create_bundled_app(dist_dir=dist, agent_adapter=injected)
    assert app.state.api.state.agent_turn_service.adapter is injected


def test_the_mounted_api_is_shut_down_with_the_bundle(dist):
    """Starlette does not run a mounted app's lifespan on its own.

    Without the bundle delegating to it, the kernel subprocess, agent-turn
    threads, and shadow kernels survive the server — a launcher that restarts
    the bundle would leak one kernel per run.
    """
    app = create_bundled_app(dist_dir=dist, agent_adapter=FakeAgentAdapter())
    api = app.state.api
    stopped: list[str] = []
    for name in ("kernel_execution_service", "agent_turn_service"):
        service = getattr(api.state, name)
        original = service.shutdown
        # Default-bind both so the late-binding loop variable is not captured.
        def record(*args, _name=name, _original=original, **kwargs):
            stopped.append(_name)
            return _original(*args, **kwargs)

        service.shutdown = record

    with TestClient(app, base_url="http://127.0.0.1:8000") as client:
        assert client.get("/api/health/ready").status_code == 200

    assert set(stopped) == {"kernel_execution_service", "agent_turn_service"}


def test_serves_the_spa_shell_at_the_root(bundled):
    response = bundled.get("/")
    assert response.status_code == 200
    assert "<div id=\"root\">" in response.text


def test_serves_hashed_assets(bundled):
    response = bundled.get("/assets/index-abc.js")
    assert response.status_code == 200
    assert "export const x" in response.text


def test_serves_a_real_file_at_the_dist_root(bundled):
    assert bundled.get("/favicon.svg").text == "<svg/>"


def test_unknown_paths_fall_back_to_the_spa_shell(bundled):
    """A client-router deep link the server has never heard of still loads."""
    response = bundled.get("/some/client/route")
    assert response.status_code == 200
    assert "<div id=\"root\">" in response.text


def test_the_api_is_reachable_under_its_prefix(bundled):
    response = bundled.get("/api/health/ready")
    assert response.status_code == 200
    assert response.json() == {"status": "ready"}


def test_an_unknown_api_route_404s_as_json(bundled):
    """The SPA fallback must not swallow the API's own 404s.

    If the catch-all were matched first, a typo'd endpoint would answer 200
    with HTML and the caller would see a parse error instead of a 404.
    """
    response = bundled.get("/api/no/such/route")
    assert response.status_code == 404
    assert "text/html" not in response.headers["content-type"]


def test_a_notebook_can_be_driven_through_the_prefix(bundled, notebook_payload):
    """The mount is not just reachable — it carries real state through."""
    created = bundled.post(
        "/api/notebooks/upload",
        files={"file": ("sample.ipynb", notebook_payload(), "application/x-ipynb+json")},
    )
    assert created.status_code == 201
    session = created.json()

    current = bundled.get("/api/notebooks/current")
    assert current.status_code == 200
    assert current.json()["sessionId"] == session["sessionId"]

    edited = bundled.post(
        "/api/cells/editable/source",
        json={
            "sessionId": session["sessionId"],
            "expectedDocumentRevision": 0,
            "source": "value = 7\n",
        },
    )
    assert edited.status_code == 200
    assert edited.json()["revision"] == 1


def test_no_cross_origin_allowance_is_advertised(bundled):
    """Same-origin serving means the dev server's CORS grant is gone."""
    response = bundled.get(
        "/api/health/ready", headers={"Origin": "http://127.0.0.1:5173"}
    )
    assert "access-control-allow-origin" not in response.headers


def test_path_traversal_below_dist_is_refused(bundled, dist):
    (dist.parent / "secret.txt").write_text("private", encoding="utf-8")
    response = bundled.get("/../secret.txt")
    assert "private" not in response.text


@pytest.mark.parametrize(
    "escape",
    [
        "../secret.txt",
        "/../secret.txt",
        "assets/../../secret.txt",
        "..%2fsecret.txt",
        "/etc/passwd",
    ],
)
def test_containment_refuses_paths_outside_dist(dist, escape):
    """Asserted on the guard itself.

    The HTTP test above is worth keeping but proves less than it appears to:
    the client normalises `/../x` to `/x` before the request is sent, so the
    traversal never reaches the resolver. These go straight at it.
    """
    (dist.parent / "secret.txt").write_text("private", encoding="utf-8")
    assert _resolve_within(dist, escape) is None


def test_containment_resolves_a_file_that_is_inside(dist):
    assert _resolve_within(dist, "index.html") == dist / "index.html"
    assert _resolve_within(dist, "nope.txt") is None


# --- Origin / Host validation (DNS rebinding) ------------------------------


def test_a_rebound_origin_is_refused(bundled):
    response = bundled.get(
        "/api/health/ready", headers={"Origin": "https://evil.example"}
    )
    assert response.status_code == 403
    assert response.json()["error"]["code"] == "forbidden_origin"


def test_a_rebound_host_is_refused(bundled):
    """What a DNS rebind actually produces: a real name pointed at 127.0.0.1."""
    response = bundled.get("/api/health/ready", headers={"Host": "evil.example"})
    assert response.status_code == 403


def test_the_spa_itself_is_refused_to_a_rebound_host(bundled):
    """The guard covers the page, not only the API — a rebind starts here."""
    assert bundled.get("/", headers={"Host": "evil.example"}).status_code == 403


def test_a_mutating_call_is_refused_before_it_reaches_the_kernel(bundled):
    """A refusal must precede application logic, not merely hide its answer."""
    response = bundled.post(
        "/api/execution/run-all",
        headers={"Origin": "https://evil.example"},
        json={"sessionId": "whatever", "expectedDocumentRevision": 0},
    )
    assert response.status_code == 403
    assert json.loads(response.text)["error"]["code"] == "forbidden_origin"


def test_a_loopback_browser_origin_is_served(bundled):
    for origin in ("http://127.0.0.1:8000", "http://localhost:8000", "http://[::1]:8000"):
        response = bundled.get("/api/health/ready", headers={"Origin": origin})
        assert response.status_code == 200, origin


def test_a_non_browser_client_sending_no_origin_is_served(bundled):
    """curl and httpx send Host but no Origin; they must still work."""
    assert bundled.get("/api/health/ready").status_code == 200


@pytest.mark.parametrize(
    ("origin", "host", "allowed"),
    [
        # Origin wins when present, whatever Host claims.
        ("http://127.0.0.1:8000", "evil.example", True),
        ("https://evil.example", "127.0.0.1:8000", False),
        # The opaque origin of a sandboxed iframe is not loopback.
        ("null", "127.0.0.1:8000", False),
        # Host decides only in Origin's absence.
        (None, "127.0.0.1:8000", True),
        (None, "localhost", True),
        (None, "[::1]:8000", True),
        (None, "127.0.0.53", True),
        (None, "evil.example", False),
        (None, "0.0.0.0:8000", False),
        (None, "192.168.1.5:8000", False),
        (None, None, False),
        # Case is not a bypass.
        (None, "LOCALHOST:8000", True),
        ("HTTP://LocalHost:8000", None, True),
        # A name that merely contains a loopback literal is not loopback.
        (None, "127.0.0.1.evil.example", False),
        ("http://localhost.evil.example", None, False),
    ],
)
def test_locality_rules(origin, host, allowed):
    assert request_is_local(origin, host) is allowed
