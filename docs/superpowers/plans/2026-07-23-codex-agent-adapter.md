# Codex Agent Adapter Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an OpenAI Codex CLI adapter as a second real agent backend, selectable per turn from the chat composer via a new Agent selector.

**Architecture:** A new `CodexAgentAdapter` mirrors `ClaudeAgentAdapter`'s contract (run/verify_supported/auxiliary_paths/shutdown via `ProcessRunner`). `AgentTurnService` gains an adapter registry keyed by agent id, the turn request gains an `agent` field, and a `GET /agent-adapters` endpoint exposes the registry so the frontend renders only the agents the backend registered. Spec: `docs/superpowers/specs/2026-07-23-codex-agent-adapter-design.md`.

**Tech Stack:** FastAPI + pytest backend, React + vitest frontend, `codex` CLI 0.133.0.

## Global Constraints

- Codex version gate: accept `>= 0.133.0, < 0.134.0`, fail-closed, checked at the start of every `run()` (mirrors Claude's `>=2.1.203,<2.2.0` pattern).
- Codex sandbox mapping: editable turn + acceptEdits → `--sandbox workspace-write`; read-only turn (no editable cells) or plan mode → `--sandbox read-only`. Always pass `-c sandbox_workspace_write.network_access=false`.
- Codex must run with `--ephemeral --ignore-user-config --skip-git-repo-check --color never -C <workspace root>` and capture the final message with `--output-last-message` into a temp dir OUTSIDE the workspace (the workspace audit must never see it).
- Codex model allow-list: `gpt-5.5`, `gpt-5.4`, `gpt-5.4-mini`; anything else omits `--model` (CLI default). Claude's allow-list (`opus`, `sonnet`, `haiku`) is unchanged.
- `NOTEBOOK_AGENT_ADAPTER` accepts `claude` (default), `codex`, `fake`. `claude`/`codex` register BOTH real adapters and set the default; `fake` registers only the fake. Invalid values raise `RuntimeError` naming all three.
- An unknown `agent` in a turn request fails with a 4xx domain error — never silently fall back to a different CLI.
- Do not change: turn scoping, workspace protocol/builder, boundary validation, diff/apply, downstream execution, undo, Claude adapter flags.
- Run backend tests with `.venv/bin/python -m pytest backend/tests -q` from the repo root; frontend with `npm test -- --run`.
- Never push to remote. Commit locally only.

---

### Task 1: CodexAgentAdapter

**Files:**
- Modify: `backend/app/agent_workspace/adapters.py`
- Test: `backend/tests/test_agent_workspace.py`

**Interfaces:**
- Consumes: `ProcessRunner.run(args, *, cwd, timeout, cancel_event) -> (stdout, stderr)`, `AgentWorkspace` (`.root`, `.manifest.editable_cells`), `AdapterResult`, `AgentAdapterError` — all existing.
- Produces: `CodexAgentAdapter(executable="codex", runner=None)` with `run(workspace, *, timeout, cancel_event, model=None, permission_mode="acceptEdits") -> AdapterResult`, `verify_supported() -> str`, `auxiliary_paths = frozenset()`, `shutdown()`. Class attrs `display_label = "Codex"` and `model_options` (tuple of `{"value","label"}` dicts) used by Task 3's capabilities endpoint. Module constant `PLAN_PREAMBLE` shared by both adapters. Also add `display_label`/`model_options` to `ClaudeAgentAdapter` (`"Claude"`; Default/Opus/Sonnet/Haiku) and `DevelopmentFakeAgentAdapter` (`"Fake"`; Default only).

- [ ] **Step 1: Write failing tests** — append to `backend/tests/test_agent_workspace.py` (import `CodexAgentAdapter` alongside `ClaudeAgentAdapter`; reuse the existing `_workspace`/`_read_only_workspace` helpers and StubRunner pattern):

```python
def _codex_version_stub(version="codex-cli 0.133.0", returncode=0):
    return lambda *args, **kwargs: SimpleNamespace(
        returncode=returncode, stdout=version, stderr="",
    )


def test_codex_adapter_version_gate(monkeypatch):
    monkeypatch.setattr(
        "backend.app.agent_workspace.adapters.subprocess.run",
        _codex_version_stub("codex-cli 0.133.5"),
    )
    assert CodexAgentAdapter().verify_supported() == "0.133.5"
    for bad in ("codex-cli 0.132.9", "codex-cli 0.134.0", "garbage"):
        monkeypatch.setattr(
            "backend.app.agent_workspace.adapters.subprocess.run",
            _codex_version_stub(bad),
        )
        with pytest.raises(AgentAdapterError):
            CodexAgentAdapter().verify_supported()
    monkeypatch.setattr(
        "backend.app.agent_workspace.adapters.subprocess.run",
        _codex_version_stub(returncode=1),
    )
    with pytest.raises(AgentAdapterError):
        CodexAgentAdapter().verify_supported()


def test_codex_editable_turn_uses_workspace_write_sandbox(notebook_payload, monkeypatch):
    builder, workspace = _workspace(notebook_payload)
    captured = {}

    class StubRunner:
        def run(self, args, **kwargs):
            captured["args"] = args
            # Simulate Codex writing the final message file.
            out = args[args.index("--output-last-message") + 1]
            pathlib.Path(out).write_text("codex finished\n", encoding="utf-8")
            return "progress noise", ""

    monkeypatch.setattr(
        "backend.app.agent_workspace.adapters.subprocess.run", _codex_version_stub(),
    )
    try:
        result = CodexAgentAdapter(runner=StubRunner()).run(
            workspace, timeout=1, cancel_event=Event()
        )
        assert result.final_output == "codex finished"
        args = captured["args"]
        assert args[:2] == ["codex", "exec"]
        assert args[args.index("--sandbox") + 1] == "workspace-write"
        assert "--ephemeral" in args
        assert "--ignore-user-config" in args
        assert "--skip-git-repo-check" in args
        assert args[args.index("-c") + 1] == "sandbox_workspace_write.network_access=false"
        assert args[args.index("-C") + 1] == str(workspace.root)
        # Final-message capture must live outside the audited workspace.
        out = pathlib.Path(args[args.index("--output-last-message") + 1])
        assert workspace.root not in out.parents
        assert "--model" not in args
    finally:
        builder.destroy(workspace)


def test_codex_read_only_and_plan_turns_use_read_only_sandbox(notebook_payload, monkeypatch):
    captured = {}

    class StubRunner:
        def run(self, args, **kwargs):
            captured["args"] = args
            return "explanation", ""

    monkeypatch.setattr(
        "backend.app.agent_workspace.adapters.subprocess.run", _codex_version_stub(),
    )
    builder, workspace = _read_only_workspace(notebook_payload)
    try:
        result = CodexAgentAdapter(runner=StubRunner()).run(
            workspace, timeout=1, cancel_event=Event()
        )
        # No final-message file written -> falls back to stdout.
        assert result.final_output == "explanation"
        assert captured["args"][captured["args"].index("--sandbox") + 1] == "read-only"
    finally:
        builder.destroy(workspace)
    builder, workspace = _workspace(notebook_payload)
    try:
        CodexAgentAdapter(runner=StubRunner()).run(
            workspace, timeout=1, cancel_event=Event(), permission_mode="plan",
        )
        args = captured["args"]
        assert args[args.index("--sandbox") + 1] == "read-only"
        assert args[2].startswith("You are operating in plan mode")
    finally:
        builder.destroy(workspace)


def test_codex_model_allow_list(notebook_payload, monkeypatch):
    captured = {}

    class StubRunner:
        def run(self, args, **kwargs):
            captured["args"] = args
            return "done", ""

    monkeypatch.setattr(
        "backend.app.agent_workspace.adapters.subprocess.run", _codex_version_stub(),
    )
    builder, workspace = _workspace(notebook_payload)
    try:
        CodexAgentAdapter(runner=StubRunner()).run(
            workspace, timeout=1, cancel_event=Event(), model="gpt-5.5",
        )
        args = captured["args"]
        assert args[args.index("--model") + 1] == "gpt-5.5"
        CodexAgentAdapter(runner=StubRunner()).run(
            workspace, timeout=1, cancel_event=Event(), model="opus",
        )
        assert "--model" not in captured["args"]
    finally:
        builder.destroy(workspace)
```

Add `import pathlib` to the test module imports if absent.

- [ ] **Step 2: Run the new tests, verify they fail** — `.venv/bin/python -m pytest backend/tests/test_agent_workspace.py -q -k codex` → ImportError/NameError on `CodexAgentAdapter`.

- [ ] **Step 3: Implement** in `backend/app/agent_workspace/adapters.py`. Add `import tempfile` at the top. Hoist the plan preamble to a module constant and point the Claude class attr at it:

```python
PLAN_PREAMBLE = (
    "You are operating in plan mode. Do not edit, create, or modify any file.\n"
    "Investigate as needed, then respond in your final message with a concrete,\n"
    "step-by-step plan for how you would carry out the request below. Stop after\n"
    "presenting the plan.\n\n"
)
```

In `ClaudeAgentAdapter`, replace the `_PLAN_PREAMBLE = (...)` literal with `_PLAN_PREAMBLE = PLAN_PREAMBLE` and add:

```python
    display_label = "Claude"
    model_options = (
        {"value": "default", "label": "Default"},
        {"value": "opus", "label": "Opus"},
        {"value": "sonnet", "label": "Sonnet"},
        {"value": "haiku", "label": "Haiku"},
    )
```

In `DevelopmentFakeAgentAdapter` add `display_label = "Fake"` and `model_options = ({"value": "default", "label": "Default"},)`. Then add the new adapter after `ClaudeAgentAdapter`:

```python
class CodexAgentAdapter:
    auxiliary_paths = frozenset()
    display_label = "Codex"
    _VERSION = re.compile(r"(?P<major>\d+)\.(?P<minor>\d+)\.(?P<patch>\d+)")
    # Model slugs the UI may request. Anything else falls back to the CLI default.
    _MODEL_ALIASES = frozenset({"gpt-5.5", "gpt-5.4", "gpt-5.4-mini"})
    model_options = (
        {"value": "default", "label": "Default"},
        {"value": "gpt-5.5", "label": "GPT-5.5"},
        {"value": "gpt-5.4", "label": "GPT-5.4"},
        {"value": "gpt-5.4-mini", "label": "GPT-5.4 Mini"},
    )
    _PERMISSION_MODES = frozenset({"acceptEdits", "plan"})

    def __init__(self, executable: str = "codex", runner: ProcessRunner | None = None) -> None:
        self.executable = executable
        self.runner = runner or ProcessRunner()

    def verify_supported(self) -> str:
        try:
            result = subprocess.run(
                [self.executable, "--version"], capture_output=True, text=True,
                timeout=5, check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise AgentAdapterError("Codex CLI is unavailable") from error
        match = self._VERSION.search(result.stdout + result.stderr)
        version = tuple(int(match.group(name)) for name in ("major", "minor", "patch")) if match else None
        if result.returncode or version is None or not ((0, 133, 0) <= version < (0, 134, 0)):
            raise AgentAdapterError("Unsupported Codex CLI version")
        return match.group(0)

    def run(
        self, workspace: AgentWorkspace, *, timeout: float, cancel_event: Event,
        model: str | None = None, permission_mode: str = "acceptEdits",
    ) -> AdapterResult:
        self.verify_supported()
        prompt = (workspace.root / "INSTRUCTIONS.md").read_text(encoding="utf-8")
        if permission_mode not in self._PERMISSION_MODES:
            permission_mode = "acceptEdits"
        if permission_mode == "plan":
            prompt = PLAN_PREAMBLE + prompt
        # Codex scopes writes by sandbox directory rather than per tool: an
        # editable turn gets workspace-write (the workspace audit still rejects
        # writes outside editable/); read-only and plan turns get no write
        # capability at all.
        editable = bool(workspace.manifest.editable_cells) and permission_mode != "plan"
        sandbox = "workspace-write" if editable else "read-only"
        with tempfile.TemporaryDirectory(prefix="codex-final-message-") as capture_dir:
            final_message_path = Path(capture_dir) / "final-message.txt"
            args = [
                self.executable, "exec", prompt,
                "--ephemeral", "--ignore-user-config", "--skip-git-repo-check",
                "--color", "never", "-C", str(workspace.root),
                "--sandbox", sandbox,
                "-c", "sandbox_workspace_write.network_access=false",
                "--output-last-message", str(final_message_path),
            ]
            if model in self._MODEL_ALIASES:
                args += ["--model", model]
            stdout, _stderr = self.runner.run(
                args, cwd=workspace.root, timeout=timeout, cancel_event=cancel_event
            )
            final_message = ""
            if final_message_path.exists():
                final_message = final_message_path.read_text(encoding="utf-8").strip()
        return AdapterResult(final_message or stdout.strip())

    def shutdown(self) -> None:
        self.runner.shutdown()
```

- [ ] **Step 4: Run tests** — `.venv/bin/python -m pytest backend/tests/test_agent_workspace.py -q` → all pass (new and existing).

- [ ] **Step 5: Commit** — `git add backend/app/agent_workspace/adapters.py backend/tests/test_agent_workspace.py && git commit -m "backend: add Codex CLI agent adapter"`

---

### Task 2: Per-turn adapter registry and agent field

**Files:**
- Modify: `backend/app/agent_turns/service.py` (constructor, `_run`, `shutdown`)
- Modify: `backend/app/api/agent_turn_routes.py` (`StartTurnRequest`, `serialize_turn`, `start_turn`)
- Test: `backend/tests/test_agent_turns.py`

**Interfaces:**
- Consumes: `AgentAdapter` protocol; `FakeAgentAdapter`; existing `AgentTurnService(documents=, scopes=, adapter=, ...)` construction used across tests.
- Produces: `AgentTurnService(..., adapter=..., adapters: dict[str, AgentAdapter] | None = None, default_agent: str = "default")`. `service.start(..., agent: str = "default")`. `AgentTurn.agent: str`. New domain error `UnknownAgentAdapter` (code `unknown_agent_adapter`, status 422). `serialize_turn` includes `"agent": turn.agent`. `StartTurnRequest` gains `agent: str = Field(default="default", max_length=64)` and `model` relaxes from the Claude `Literal` to `str = Field(default="default", max_length=64)` (adapters own model validation and fall back safely).

- [ ] **Step 1: Write failing tests** — append to `backend/tests/test_agent_turns.py`, following that file's existing service-construction pattern (look at how it builds `documents`, `scopes`, and calls `service.start(..., background=False)`):

```python
def test_turn_routes_to_selected_adapter_and_serializes_agent(notebook_payload):
    # Build documents/scopes exactly as the nearest existing test does.
    claude_like = FakeAgentAdapter([FakeAttempt(final_output="from claude")])
    codex_like = FakeAgentAdapter([FakeAttempt(final_output="from codex")])
    service = AgentTurnService(
        documents=documents, scopes=scopes,
        adapters={"claude": claude_like, "codex": codex_like},
        default_agent="claude", timeout=1,
    )
    turn = service.start(
        prompt="explain", session_id=snapshot.session_id,
        expected_revision=snapshot.revision, agent="codex", background=False,
    )
    assert turn.agent == "codex"
    assert turn.final_output == "from codex"
    assert codex_like.call_count == 1 and claude_like.call_count == 0

    turn = service.start(
        prompt="explain", session_id=snapshot.session_id,
        expected_revision=snapshot.revision, background=False,
    )
    assert turn.agent == "claude"
    assert claude_like.call_count == 1


def test_unknown_agent_is_rejected_without_running(notebook_payload):
    adapter = FakeAgentAdapter()
    service = AgentTurnService(
        documents=documents, scopes=scopes, adapter=adapter, timeout=1,
    )
    with pytest.raises(UnknownAgentAdapter):
        service.start(
            prompt="explain", session_id=snapshot.session_id,
            expected_revision=snapshot.revision, agent="gemini", background=False,
        )
    assert adapter.call_count == 0
    # The failed start must not leak the mutation lease: a follow-up default
    # turn still runs.
    turn = service.start(
        prompt="explain", session_id=snapshot.session_id,
        expected_revision=snapshot.revision, background=False,
    )
    assert turn.agent == "default"
```

Also extend an existing route-level test (the one using `create_app(agent_adapter=FakeAgentAdapter())` around line 159) to assert the serialized start response contains `"agent": "default"` and that posting `{"agent": "nope", ...}` returns 422 with error code `unknown_agent_adapter`.

- [ ] **Step 2: Run to verify failure** — `.venv/bin/python -m pytest backend/tests/test_agent_turns.py -q -k "agent"` → NameError/TypeError.

- [ ] **Step 3: Implement.** In `backend/app/agent_turns/service.py`:

Add the domain error next to the others:

```python
class UnknownAgentAdapter(NotebookDomainError):
    code = "unknown_agent_adapter"
    message = "Requested agent backend is not available"
    status_code = 422

    def __init__(self, agent: str) -> None:
        super().__init__(agent=agent)
```

Add `agent: str = "default"` to the `AgentTurn` dataclass (after `mode`). In `AgentTurnService.__init__`, replace the single-adapter storage:

```python
        self.adapters: dict[str, AgentAdapter] = (
            dict(adapters) if adapters else {"default": adapter}
        )
        self.default_agent = default_agent if adapters else "default"
        if self.default_agent not in self.adapters:
            raise ValueError("default_agent must be a registered adapter")
```

with signature `def __init__(self, *, documents, scopes, adapter: AgentAdapter | None = None, adapters: dict[str, AgentAdapter] | None = None, default_agent: str = "default", builder=None, ...)`. Keep a `self.adapter` property returning `self.adapters[self.default_agent]` so existing internal uses and tests keep working:

```python
    @property
    def adapter(self) -> AgentAdapter:
        return self.adapters[self.default_agent]
```

(Constructor must therefore store the incoming `adapter` argument only inside the dict, and `adapter=None` with no `adapters` falls back to `{"default": FakeAgentAdapter()}` — preserve `create_app`'s current `agent_adapter or FakeAgentAdapter()` behavior at the caller instead if simpler.)

In `start(...)`: accept `agent: str = "default"`, resolve `agent = self.default_agent if agent in ("", "default") else agent`, and raise `UnknownAgentAdapter(agent)` **before** acquiring the mutation lease if `agent not in self.adapters`. Pass `agent=agent` into the `AgentTurn(...)` construction.

In `_run(...)`: replace `self.adapter.run(...)` with:

```python
                adapter = self.adapters[turn.agent]
                result = adapter.run(
                    workspace, timeout=self.timeout, cancel_event=turn.cancel_event,
                    model=None if turn.model == "default" else turn.model,
                    permission_mode=PERMISSION_MODE_BY_MODE.get(turn.mode, "acceptEdits"),
                )
```

and `auxiliary_paths=self.adapter.auxiliary_paths` with `auxiliary_paths=adapter.auxiliary_paths`.

In `shutdown(...)`: iterate every registry adapter instead of only the default (keep the existing `getattr(..., "shutdown", None)` guard, wrap each call so one failure doesn't skip the rest).

In `backend/app/api/agent_turn_routes.py`: change `StartTurnRequest.model` to `model: str = Field(default="default", max_length=64)`, add `agent: str = Field(default="default", max_length=64)`, drop the now-unused `Literal` import for model (keep it for `mode`), pass `agent=body.agent` in `start_turn`, and add `"agent": turn.agent` to `serialize_turn`.

- [ ] **Step 4: Run tests** — `.venv/bin/python -m pytest backend/tests/test_agent_turns.py backend/tests/test_agent_workspace.py -q` → pass.

- [ ] **Step 5: Commit** — `git commit -am "backend: route agent turns through a per-turn adapter registry"`

---

### Task 3: Wiring and capabilities endpoint

**Files:**
- Modify: `backend/app/main.py` (`create_app`, `configured_agent_adapter` → `configured_agent_adapters`)
- Modify: `backend/app/api/agent_turn_routes.py` (add `GET /agent-adapters`)
- Test: `backend/tests/test_agent_workspace.py` (selection tests), `backend/tests/test_agent_turns.py` (endpoint test)

**Interfaces:**
- Consumes: Task 1's adapters (`display_label`, `model_options` class attrs); Task 2's registry (`AgentTurnService(adapters=, default_agent=)`).
- Produces: `configured_agent_adapters() -> tuple[dict[str, AgentAdapter], str]` in `main.py`; `create_app(*, agent_adapter=None, agent_adapters=None, default_agent=None)`; route `GET /agent-adapters` returning `{"defaultAgent": str, "agents": [{"id", "label", "models", "modes"}]}`.

- [ ] **Step 1: Write failing tests.** In `backend/tests/test_agent_workspace.py`, replace the two `configured_agent_adapter` tests (lines ~468-478) with:

```python
def test_configured_adapters_default_to_claude(monkeypatch):
    monkeypatch.delenv("NOTEBOOK_AGENT_ADAPTER", raising=False)
    adapters, default = configured_agent_adapters()
    assert default == "claude"
    assert isinstance(adapters["claude"], ClaudeAgentAdapter)
    assert isinstance(adapters["codex"], CodexAgentAdapter)


def test_configured_adapters_codex_mode_registers_both(monkeypatch):
    monkeypatch.setenv("NOTEBOOK_AGENT_ADAPTER", "codex")
    adapters, default = configured_agent_adapters()
    assert default == "codex"
    assert set(adapters) == {"claude", "codex"}


def test_configured_adapters_fake_and_invalid(monkeypatch):
    monkeypatch.setenv("NOTEBOOK_AGENT_ADAPTER", "fake")
    adapters, default = configured_agent_adapters()
    assert default == "fake"
    assert set(adapters) == {"fake"}
    assert isinstance(adapters["fake"], DevelopmentFakeAgentAdapter)
    monkeypatch.setenv("NOTEBOOK_AGENT_ADAPTER", "gemini")
    with pytest.raises(RuntimeError, match="claude.*codex.*fake"):
        configured_agent_adapters()
```

(update the import from `backend.app.main`). In `backend/tests/test_agent_turns.py` add:

```python
def test_agent_adapters_endpoint_reflects_registry():
    app = create_app(agent_adapter=FakeAgentAdapter())
    client = TestClient(app)
    body = client.get("/agent-adapters").json()
    assert body["defaultAgent"] == "default"
    assert [a["id"] for a in body["agents"]] == ["default"]
    assert body["agents"][0]["modes"] == ["edit", "plan"]
    assert body["agents"][0]["models"][0] == {"value": "default", "label": "Default"}
```

- [ ] **Step 2: Verify failure** — `.venv/bin/python -m pytest backend/tests/test_agent_workspace.py -q -k configured` → ImportError on `configured_agent_adapters`.

- [ ] **Step 3: Implement.** In `backend/app/main.py`:

```python
def configured_agent_adapters() -> tuple[dict[str, AgentAdapter], str]:
    mode = os.getenv("NOTEBOOK_AGENT_ADAPTER", "claude").strip().lower()
    if mode in ("claude", "codex"):
        return {"claude": ClaudeAgentAdapter(), "codex": CodexAgentAdapter()}, mode
    if mode == "fake":
        return {"fake": DevelopmentFakeAgentAdapter()}, "fake"
    raise RuntimeError(
        "NOTEBOOK_AGENT_ADAPTER must be one of 'claude', 'codex', or 'fake'"
    )
```

Keep a thin `configured_agent_adapter()` only if something still imports it — otherwise delete it and update imports (check `scripts/dev.py` and tests; the module-level app becomes:

```python
_adapters, _default_agent = configured_agent_adapters()
app = create_app(agent_adapters=_adapters, default_agent=_default_agent)
```

). `create_app` gains `agent_adapters: dict[str, AgentAdapter] | None = None, default_agent: str | None = None` alongside the legacy `agent_adapter` and builds the service:

```python
    app.state.agent_turn_service = AgentTurnService(
        documents=app.state.notebook_service,
        scopes=app.state.turn_scope_service,
        adapter=agent_adapter or FakeAgentAdapter(),
        adapters=agent_adapters,
        default_agent=default_agent or "default",
        executions=app.state.kernel_execution_service,
        events=app.state.session_event_service,
    )
```

(per Task 2's constructor: when `agent_adapters` is None the legacy single-adapter path applies). In `agent_turn_routes.py` add:

```python
adapters_router = APIRouter()


@adapters_router.get("/agent-adapters")
def list_agent_adapters(request: Request) -> dict[str, Any]:
    service = request.app.state.agent_turn_service
    return {
        "defaultAgent": service.default_agent,
        "agents": [
            {
                "id": agent_id,
                "label": getattr(adapter, "display_label", agent_id.title()),
                "models": list(getattr(
                    adapter, "model_options",
                    ({"value": "default", "label": "Default"},),
                )),
                "modes": ["edit", "plan"],
            }
            for agent_id, adapter in service.adapters.items()
        ],
    }
```

and register `app.include_router(adapters_router)` in `create_app` (the main router has prefix `/agent-turns`, hence the separate router).

- [ ] **Step 4: Run tests** — `.venv/bin/python -m pytest backend/tests -q` → full backend suite passes (this catches any `configured_agent_adapter` import fallout, e.g. `test_dev_script.py`).

- [ ] **Step 5: Commit** — `git commit -am "backend: register both CLI adapters and expose agent capabilities"`

---

### Task 4: Frontend agent selector

**Files:**
- Modify: `frontend/src/api/client.ts`
- Modify: `frontend/src/App.tsx` (fetch capabilities once, pass down)
- Modify: `frontend/src/agentChat/AgentChatPanel.tsx`
- Test: `frontend/src/agentChat/agentSelector.test.tsx` (new)

**Interfaces:**
- Consumes: `GET /agent-adapters` (Task 3 shape); `POST /agent-turns` accepting `agent` (Task 2).
- Produces: `client.ts` — `export type AgentModel = string;`, `export interface AgentModelOption { value: string; label: string }`, `export interface AgentInfo { id: string; label: string; models: AgentModelOption[]; modes: AgentMode[] }`, `export interface AgentAdaptersResponse { defaultAgent: string; agents: AgentInfo[] }`, `TurnOptions` gains `agent: string`, `AgentTurn` type gains `agent: string`, `api.getAgentAdapters()`, `startTurn` body gains `agent: options?.agent ?? "default"`. `AgentChatPanel` props gain `agentAdapters?: AgentAdaptersResponse | null`.

- [ ] **Step 1: Write failing test** — `frontend/src/agentChat/agentSelector.test.tsx`. Mirror the render/props setup of `frontend/src/agentChat/attachments.test.tsx` (same mocks and minimal props for `AgentChatPanel`); the assertions that matter:

```tsx
const adapters = {
  defaultAgent: "claude",
  agents: [
    { id: "claude", label: "Claude", modes: ["edit", "plan"], models: [
      { value: "default", label: "Default" }, { value: "opus", label: "Opus" },
      { value: "sonnet", label: "Sonnet" }, { value: "haiku", label: "Haiku" }] },
    { id: "codex", label: "Codex", modes: ["edit", "plan"], models: [
      { value: "default", label: "Default" }, { value: "gpt-5.5", label: "GPT-5.5" },
      { value: "gpt-5.4", label: "GPT-5.4" }, { value: "gpt-5.4-mini", label: "GPT-5.4 Mini" }] },
  ],
};

it("switches model options with the agent and submits the selection", async () => {
  const onSubmit = vi.fn();
  render(<AgentChatPanel {...baseProps} agentAdapters={adapters} onSubmit={onSubmit} />);
  const agentSelect = screen.getByLabelText("Agent backend");
  expect(agentSelect).toHaveValue("claude");
  expect(screen.getByRole("option", { name: "Opus" })).toBeInTheDocument();
  fireEvent.change(agentSelect, { target: { value: "codex" } });
  expect(screen.getByRole("option", { name: "GPT-5.5" })).toBeInTheDocument();
  expect(screen.queryByRole("option", { name: "Opus" })).not.toBeInTheDocument();
  const modelSelect = screen.getByLabelText("Agent model");
  fireEvent.change(modelSelect, { target: { value: "gpt-5.5" } });
  fireEvent.change(screen.getByLabelText("Agent instruction"), { target: { value: "hi" } });
  fireEvent.submit(screen.getByLabelText("Agent instruction").closest("form")!);
  expect(onSubmit).toHaveBeenCalledWith("hi", { agent: "codex", model: "gpt-5.5", mode: "edit" });
});

it("resets model to default when the agent changes", () => { /* select opus, switch agent, assert model back to "default" */ });
```

- [ ] **Step 2: Verify failure** — `npm test -- --run agentSelector` → fails (no `agentAdapters` prop / no Agent select).

- [ ] **Step 3: Implement.**
  - `client.ts`: types and `getAgentAdapters: () => request<AgentAdaptersResponse>("/agent-adapters")`; `startTurn` body adds `agent: options?.agent ?? "default"`; `AgentTurn` interface gains `agent: string` (backend now always serializes it).
  - `App.tsx`: `const [agentAdapters, setAgentAdapters] = useState<AgentAdaptersResponse | null>(null);` plus a mount-effect `api.getAgentAdapters().then(setAgentAdapters).catch(() => setAgentAdapters(null));` and pass `agentAdapters={agentAdapters}` to `AgentChatPanel`.
  - `AgentChatPanel.tsx`: add prop; state `const [agent, setAgent] = useState("default");` with an effect that snaps to `agentAdapters.defaultAgent` when the current value is not a registered id:

```tsx
  useEffect(() => {
    if (agentAdapters && !agentAdapters.agents.some((a) => a.id === agent)) {
      setAgent(agentAdapters.defaultAgent);
    }
  }, [agentAdapters, agent]);
  const agentInfo = agentAdapters?.agents.find((a) => a.id === agent) ?? null;
  const modelOptions = agentInfo?.models ?? [{ value: "default", label: "Default" }];
```

Render the Agent select first in `.prompt-controls` (only when `agentAdapters` has loaded; hidden otherwise so fake/e2e snapshots stay stable... no — render it always, with a single "Default" option before load, so layout is stable):

```tsx
        <label className="prompt-select">
          <span>Agent</span>
          <select aria-label="Agent backend" value={agent} disabled={busy}
            onChange={(event) => { setAgent(event.target.value); setModel("default"); }}>
            {(agentAdapters?.agents ?? [{ id: "default", label: "Default" }]).map((a) => (
              <option key={a.id} value={a.id}>{a.label}</option>
            ))}
          </select>
        </label>
```

Replace the hardcoded model `<option>`s with `modelOptions.map(...)`, submit `{ agent, model, mode }`, and append the agent to the turn-status line so history shows which CLI ran: `{turn.agent && turn.agent !== "default" && <span className="turn-agent"> · {turn.agent}</span>}` inside the `turn-state` div.

- [ ] **Step 4: Run tests** — `npm test -- --run` and `npm run build` → pass. Existing `App.test.tsx` may need the new `/agent-adapters` fetch mocked; extend its api mock with `getAgentAdapters: vi.fn().mockResolvedValue(...)` following how the file mocks other api calls.

- [ ] **Step 5: Commit** — `git commit -am "frontend: per-turn agent selector with adapter-specific models"`

---

### Task 5: Documentation

**Files:**
- Modify: `docs/notebook-agent-editor-spec.md` (CLI Agent Execution Policy + UI Behavior composer paragraph + API Surface Draft)
- Modify: `docs/engineering-handoff.md` (How Agent Editing Actually Works, Run And Verify, Known Risks item 4)

**Interfaces:** none (prose only). Content requirements:

- [ ] **Step 1: Spec updates.**
  - In `CLI Agent Execution Policy`, add a paragraph: two production adapters exist (Claude CLI, Codex CLI); the turn request selects one; Codex runs `codex exec` with `--ephemeral --ignore-user-config --skip-git-repo-check`, sandbox `workspace-write` on editable turns and `read-only` on read-only/plan turns, network disabled; final message captured via `--output-last-message` outside the workspace; supported Codex versions `>=0.133.0,<0.134.0` fail-closed. State the boundary difference explicitly: Codex's write scope is the sandbox directory, so within-workspace protection relies on the workspace audit rather than per-tool denial.
  - In `UI Behavior` → composer paragraph: the composer exposes an **Agent** selector populated from `GET /agent-adapters`; Model options depend on the selected agent (Claude: Opus/Sonnet/Haiku; Codex: GPT-5.5/GPT-5.4/GPT-5.4 Mini); Mode applies to both.
  - In `API Surface Draft`: add `GET /agent-adapters` and note `POST /agent-turns` accepts `agent`.
- [ ] **Step 2: Handoff updates.**
  - Run And Verify: note `NOTEBOOK_AGENT_ADAPTER=codex .venv/bin/python scripts/dev.py` selects Codex as the default agent and that both real adapters are always selectable per turn from the UI.
  - How Agent Editing Actually Works: describe the registry, the Codex flags/sandbox mapping and audit-based within-workspace boundary, and the Codex version gate `>=0.133.0,<0.134.0` (checked before every turn).
  - Known Risks item 4 (narrow real-agent compatibility): extend to cover Codex auto-updates past 0.133.x stopping Codex turns until the gate is re-verified; mention `scripts/codex_smoke.py` (Task 6) as the manual smoke path.
- [ ] **Step 3: Commit** — `git commit -am "docs: document Codex adapter, per-turn agent selection, and version gates"`

---

### Task 6: Real-CLI smoke test and full verification

**Files:**
- Create: `scripts/codex_smoke.py`
- No production-code changes expected; if the real CLI reveals flag problems, fix `backend/app/agent_workspace/adapters.py` (and its tests) here.

**Interfaces:**
- Consumes: everything above; `codex` CLI 0.133.0 logged in locally; `examples/sample.ipynb`.

- [ ] **Step 1: Write the smoke script** (a manual verification utility, not part of the pytest suite):

```python
"""Manual smoke test: drive one real Codex CLI turn through the app backend.

Usage:
    .venv/bin/python scripts/codex_smoke.py             # editable turn
    .venv/bin/python scripts/codex_smoke.py --read-only # read-only turn
    .venv/bin/python scripts/codex_smoke.py --agent claude

Runs the real CLI (spends tokens); requires a logged-in codex CLI.
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

from backend.app.main import configured_agent_adapters, create_app  # noqa: E402

TERMINAL = {"completed", "failed", "cancelled", "validation_incomplete"}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--agent", default="codex")
    parser.add_argument("--read-only", action="store_true")
    parser.add_argument("--timeout", type=float, default=600.0)
    args = parser.parse_args()

    adapters, default = configured_agent_adapters()
    app = create_app(agent_adapters=adapters, default_agent=default)
    sample = Path(__file__).resolve().parent.parent / "examples/sample.ipynb"
    with TestClient(app) as client:
        upload = client.post(
            "/notebooks/upload", files={"file": ("sample.ipynb", sample.read_bytes())},
        )
        upload.raise_for_status()
        snapshot = upload.json()
        session, revision = snapshot["sessionId"], snapshot["revision"]
        if args.read_only:
            prompt = "Explain what this notebook does in two sentences."
        else:
            cell = next(c for c in snapshot["cells"] if c["cellType"] == "code")
            client.post("/turn-scope/editable-cells", json={
                "sessionId": session, "expectedDocumentRevision": revision,
                "cellId": cell["cellId"],
            }).raise_for_status()
            prompt = "Add a short clarifying comment at the top of the selected cell."
        started = client.post("/agent-turns", json={
            "sessionId": session, "expectedDocumentRevision": revision,
            "prompt": prompt, "agent": args.agent,
        })
        started.raise_for_status()
        turn_id = started.json()["turnId"]
        deadline = time.monotonic() + args.timeout
        while time.monotonic() < deadline:
            turn = client.get(f"/agent-turns/{turn_id}").json()
            if turn["state"] in TERMINAL:
                break
            time.sleep(2)
        print(f"agent: {turn['agent']}  state: {turn['state']}  attempts: {turn['attempts']}")
        print(f"changes: {[c['cellId'] for c in turn['changes']]}")
        print(f"error: {turn['error']}")
        print(f"final output:\n{turn['finalOutput']}")
        return 0 if turn["state"] == "completed" and not turn["error"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 2: Run the read-only smoke** — `.venv/bin/python scripts/codex_smoke.py --read-only` → expect `state: completed`, empty `changes`, a real explanation in final output. If Codex fails, read the printed error (the adapter surfaces CLI stderr in `AgentAdapterError` details), fix the flags in the adapter + unit tests, and re-run until it completes.
- [ ] **Step 3: Run the editable smoke** — `.venv/bin/python scripts/codex_smoke.py` → expect `state: completed`, exactly one changed cell, downstream execution finishing. Watch specifically for workspace-audit rejections (undeclared files created by Codex): if Codex drops files into the workspace, decide between `auxiliary_paths` declarations (if benign and stable) or flag changes, and update tests accordingly.
- [ ] **Step 4: Full verification** — `.venv/bin/python -m pytest backend/tests -q`, `npm test -- --run`, `npm run build`, `npm run test:e2e` → all pass.
- [ ] **Step 5: Commit** — `git add scripts/codex_smoke.py && git commit -am "scripts: add manual Codex smoke turn; verified against codex-cli 0.133.0"` (include any adapter fixes made here).
