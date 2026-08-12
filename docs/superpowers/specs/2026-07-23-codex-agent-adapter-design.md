# Codex Agent Adapter Design

Date: 2026-07-23
Issue: https://github.com/AgLyx3/we-love-jupyter-notebook/issues/3
Status: Approved by user (conversation, 2026-07-23)

## Summary

Add an OpenAI Codex CLI adapter as a second real agent backend alongside the
Claude CLI adapter, and make the agent selectable per turn from the chat
composer. The composer gains an **Agent** selector (Claude | Codex); the Model
options change with the selected agent; Mode (Edit | Plan) works for both.
This extends issue #3, which asked only for env-var selection — the per-turn
UI selector was added at the user's request.

Verified against the local environment: `codex-cli 0.133.0`, logged in via
ChatGPT (auth lives in `CODEX_HOME`, so it survives `--ignore-user-config`).

## Scope

In scope:

- `CodexAgentAdapter` in `backend/app/agent_workspace/adapters.py`.
- Per-turn adapter registry in `AgentTurnService` plus an `agent` field on the
  turn request/record.
- `configured_agent_adapters()` wiring in `backend/app/main.py`
  (~~`NOTEBOOK_AGENT_ADAPTER`~~ **renamed `NOTEBOOK_DEFAULT_AGENT` on 2026-08-12**,
  since it names the default rather than the only adapter; the old name now
  raises rather than being reinterpreted) = `claude` | `codex` | `fake` selects
  the *default* agent; `claude`/`codex` both register both real adapters.
- `GET /agent-adapters` capabilities endpoint so the UI renders only what the
  backend actually has.
- Frontend Agent selector with per-agent model options.
- Tests (backend + frontend) and docs (`docs/notebook-agent-editor-spec.md`,
  `docs/engineering-handoff.md`).
- One real Codex smoke turn through the running app.

Out of scope (unchanged): turn scoping, workspace protocol, boundary
validation, diff/apply, downstream execution, undo.

## CodexAgentAdapter

Mirrors `ClaudeAgentAdapter`: same constructor shape
(`executable="codex"`, injectable `ProcessRunner`), same
`run(workspace, *, timeout, cancel_event, model=None, permission_mode="acceptEdits")`
signature, `auxiliary_paths = frozenset()`, `shutdown()` delegates to the
runner.

### verify_supported()

`codex --version` prints `codex-cli 0.133.0`. Parse the semver; accept
`>= 0.133.0, < 0.134.0` — fail-closed on the current minor, mirroring the
Claude gate (`>=2.1.203,<2.2.0`). Raise `AgentAdapterError` with
"Codex CLI is unavailable" / "Unsupported Codex CLI version". Checked at the
start of every `run()`.

### run()

- Prompt read from `INSTRUCTIONS.md` in the workspace root; plan mode prepends
  the same `_PLAN_PREAMBLE` text used by the Claude adapter (extract the
  constant so both adapters share it).
- Command:

```
codex exec <prompt>
  --ephemeral                # no session persistence
  --ignore-user-config       # drop user MCP servers/hooks/plugins; auth kept
  --skip-git-repo-check      # temp workspace is not a git repo
  --color never
  -C <workspace.root>        # agent working root (cwd also workspace.root)
  --sandbox <mode>
  -c sandbox_workspace_write.network_access=false
  -o <last-message-file>
  [-m <model>]
```

- Sandbox mapping (the write boundary):
  - editable turn + `acceptEdits` → `--sandbox workspace-write`
  - read-only turn (no editable cells) → `--sandbox read-only`
  - plan mode → `--sandbox read-only` (regardless of editable cells)
- Documented boundary difference vs Claude: Codex scopes writes by sandbox
  directory, not per-tool, so on editable turns Codex could write any file in
  the temp workspace; the existing workspace audit rejects changes outside
  `editable/` and protected-path edits, exactly as it does today. Network is
  denied by the sandbox (`network_access=false` set explicitly for defense in
  depth). MCP is empty because `--ignore-user-config` drops the user's config.
- Final message capture (structured): `-o/--output-last-message` writes the
  agent's final message to a file. The file lives in its own
  `tempfile.TemporaryDirectory` *outside* the workspace so the workspace audit
  never sees it. `AdapterResult(final_message.strip())`; if the file is
  missing or empty after a zero-exit run, fall back to `stdout.strip()`.
- Model allow-list (from the local models cache): `gpt-5.5`, `gpt-5.4`,
  `gpt-5.4-mini`. Anything else (including `None`/"default") omits `-m` and
  uses the CLI default — same fallback pattern as Claude's alias set.

## Per-Turn Adapter Selection

- `AgentTurnService` gains an adapter registry: `adapters: dict[str,
  AgentAdapter]` plus a `default_agent: str`. The existing single-`adapter`
  path remains for injected test apps (registered under the default key).
- `AgentTurn` gains `agent: str` (default = service default). `_run()` looks
  up the adapter by `turn.agent`; an unknown agent fails the request with a
  domain error rather than silently running a different CLI.
- `StartTurnRequest` gains `agent: str = "default"`; `"default"` resolves to
  the service's default agent. `serialize_turn` includes `agent`.
- `configured_agent_adapters()` in `main.py`:
  - `claude` (default) → `{"claude": ClaudeAgentAdapter(), "codex":
    CodexAgentAdapter()}`, default `claude`.
  - `codex` → same registry, default `codex`.
  - `fake` → `{"fake": DevelopmentFakeAgentAdapter()}`, default `fake`.
  - Anything else → `RuntimeError` naming all three modes (issue requirement).
- Model validation stays inside each adapter (unknown → CLI default), so the
  route schema relaxes `model` from a Claude-specific `Literal` to a bounded
  `str`; the adapter allow-lists remain the authority.

## Capabilities Endpoint

`GET /agent-adapters` returns the registry as UI-renderable metadata:

```json
{
  "defaultAgent": "claude",
  "agents": [
    {"id": "claude", "label": "Claude",
     "models": [{"value": "default", "label": "Default"},
                 {"value": "opus", "label": "Opus"},
                 {"value": "sonnet", "label": "Sonnet"},
                 {"value": "haiku", "label": "Haiku"}],
     "modes": ["edit", "plan"]},
    {"id": "codex", "label": "Codex",
     "models": [{"value": "default", "label": "Default"},
                 {"value": "gpt-5.5", "label": "GPT-5.5"},
                 {"value": "gpt-5.4", "label": "GPT-5.4"},
                 {"value": "gpt-5.4-mini", "label": "GPT-5.4 Mini"}],
     "modes": ["edit", "plan"]}
  ]
}
```

Static metadata declared next to each adapter (class attributes); the endpoint
reflects whatever registry `create_app` received, so fake mode lists only the
fake agent and the UI cannot offer a backend that is not registered.

## Frontend

- `client.ts`: `AgentModel` widens to `string`; new `AgentInfo`/
  `AgentAdaptersResponse` types; `TurnOptions` gains `agent`; `startTurn`
  sends `agent`; new `api.getAgentAdapters()`.
- `App.tsx` fetches capabilities once and passes them to `AgentChatPanel`.
- `AgentChatPanel`: an **Agent** select ahead of Model/Mode, defaulting to
  `defaultAgent`. Changing agent resets Model to `default` and swaps the
  Model option list. With a single registered agent the selector renders but
  is effectively fixed (keeps fake/e2e mode unchanged). The chosen agent is
  sent with the turn and shown in turn history via the serialized `agent`.

## Error Handling

- Codex CLI missing/wrong version → same fail-closed turn error path as
  Claude (`AgentAdapterError` before launch).
- Unknown `agent` in a turn request → 4xx domain error, no fallback.
- Empty last-message file on success → stdout fallback (documented).
- Cancellation/timeout: unchanged — `ProcessRunner` owns process-group
  termination for both CLIs.

## Testing

Backend (`backend/tests/test_agent_workspace.py` + routes tests):

- `verify_supported` accept (0.133.x) / reject (missing CLI, 0.132.x,
  0.134.0) via monkeypatched `subprocess.run`.
- Stubbed-runner `run()` cases: editable turn → `--sandbox workspace-write`;
  read-only turn → `--sandbox read-only` and no editable files written; plan
  mode → `read-only` + preamble; model allow-list pass-through and fallback;
  final message read from `-o` file (and stdout fallback).
- `configured_agent_adapters` selection for `claude`/`codex`/`fake`/invalid.
- Turn request with `agent` routes to the matching adapter; unknown agent
  rejected; serialized turn includes `agent`.
- `GET /agent-adapters` reflects the registry.

Frontend (vitest): selector renders from capabilities, agent switch resets
model and swaps options, submit payload carries `agent`.

Verification gate: full backend pytest, `npm test -- --run`, `npm run build`,
`npm run test:e2e`, then a real Codex smoke turn (editable + read-only)
against `examples/sample.ipynb` through `scripts/dev.py`, iterating on flags
until the real turn passes. A Claude smoke turn is not required by this
change (its flags are untouched) but the dev launcher must still default to
Claude.

## Risks

- Codex releases move fast; the narrow version gate will fail-closed after a
  CLI auto-update, by design (same posture as Claude — documented in the
  handoff).
- `codex exec` may print progress to stdout; nothing parses stdout except the
  fallback path, so noise is harmless.
- Real-turn behavior (e.g. unexpected files created in the workspace) is
  unknown until the smoke test; the audit fail-closes, and `auxiliary_paths`
  is the sanctioned escape hatch if Codex needs one.
