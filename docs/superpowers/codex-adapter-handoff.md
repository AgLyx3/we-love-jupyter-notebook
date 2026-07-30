# Codex Agent Adapter — Handoff Note

Date: 2026-07-27
Branch: `feature/codex-agent-adapter`
Issue: https://github.com/AgLyx3/we-love-jupyter-notebook/issues/3

This note hands off an in-progress feature branch that adds an OpenAI Codex CLI
agent adapter alongside the existing Claude adapter, selectable per turn from the
chat composer. It was built with a spec → plan → subagent-driven execution flow.
Five of six implementation tasks are fully committed and independently reviewed.
The sixth (real-CLI smoke verification) is **not done**: it wrote the smoke
utility and uncovered a real blocker — Codex will not read the notebook under the
Claude-oriented "do not run shell commands" instruction — but the attempted fix
is insufficient, so a real Codex turn does not yet do useful work. That work is
uncommitted and unreviewed, and the end-to-end suite is unverified in this
environment. **Do not treat this branch as ready to merge.** See "Task 6" below
for the specific blocker and the recommended fix.

Authoritative companions:
- Design spec: `docs/superpowers/specs/2026-07-23-codex-agent-adapter-design.md`
- Implementation plan: `docs/superpowers/plans/2026-07-23-codex-agent-adapter.md`
- Per-task progress + review notes: `.superpowers/sdd/progress.md`

## What this feature does

- Adds `CodexAgentAdapter` (`backend/app/agent_workspace/adapters.py`), a second
  real agent backend that shells out to the local `codex` CLI, mirroring
  `ClaudeAgentAdapter`'s contract (`run` / `verify_supported` /
  `auxiliary_paths` / `shutdown` via the shared `ProcessRunner`).
- Makes the agent selectable **per turn**. `AgentTurnService` now holds an
  adapter *registry* keyed by agent id; the turn request carries an `agent`
  field; `GET /agent-adapters` exposes the registry so the frontend only offers
  agents the backend actually registered. The chat composer gains an **Agent**
  selector (Claude | Codex); the Model options change with the selected agent;
  Mode (Edit | Plan) applies to both.
- `NOTEBOOK_AGENT_ADAPTER` now accepts `claude` (default), `codex`, or `fake`.
  `claude`/`codex` both register *both* real adapters and only differ in which
  is the default; `fake` registers only the fake adapter. Invalid values raise a
  `RuntimeError` naming all three.

### Codex adapter specifics (verified against codex-cli 0.133.0)

Command shape:
```
codex exec <prompt> --ephemeral --ignore-user-config --skip-git-repo-check
  --color never -C <workspace-root> --sandbox <mode>
  -c sandbox_workspace_write.network_access=false
  -o <last-message-file-outside-workspace> [-m <model>]
```
- **Write boundary via sandbox, not per-tool.** Editable turns get
  `--sandbox workspace-write`; read-only and plan turns get
  `--sandbox read-only`. Codex scopes writes by sandbox directory rather than a
  per-tool allow-list (Claude uses `--tools Read`), so within-workspace
  protection on editable turns relies on the existing workspace audit rejecting
  any write outside `editable/`. Network is disabled explicitly.
- **Version gate** `>=0.133.0,<0.134.0`, fail-closed, checked before every turn
  (same deliberately narrow posture as Claude's `>=2.1.203,<2.2.0`). A Codex
  auto-update past 0.133.x will stop Codex turns until the gate is re-verified.
- **Structured final message** captured via `--output-last-message` into a temp
  dir *outside* the workspace (so the audit never sees it); falls back to stdout.
- **Model allow-list** `gpt-5.5`, `gpt-5.4`, `gpt-5.4-mini`; anything else uses
  the CLI default.

## Task status

| Task | Description | Committed? | Reviewed? |
|------|-------------|-----------|-----------|
| 1 | `CodexAgentAdapter` + unit tests | ✅ `2c958e6` | ✅ clean |
| 2 | Per-turn adapter registry + `agent` field | ✅ `2a6d4d3` | ✅ clean |
| 3 | `configured_agent_adapters()` wiring + `GET /agent-adapters` | ✅ `435d370` | ✅ clean |
| 4 | Frontend Agent selector | ✅ `39bad65` | ✅ clean |
| 5 | Documentation | ✅ `5d8c288` | ✅ clean |
| 6 | Real Codex smoke script + full verification | ⚠️ **uncommitted** | ❌ not reviewed |
| Final | Whole-branch review | ❌ not run | — |

## The important part: Task 6 and what it discovered

Task 6 wrote `scripts/codex_smoke.py` (a manual utility that drives one real
Codex turn through the app via `TestClient`) and ran real turns against
`examples/sample.ipynb`. **In doing so it found a genuine adapter bug** and fixed
it, but the session limit hit before the work was committed or reviewed.

The bug: Codex has no separate non-shell Read tool the way Claude does — Codex
reads files through its shell/exec tool. The turn's `INSTRUCTIONS.md` contains a
"Do not run shell commands." line (written for Claude, generated in
`backend/app/agent_workspace/workspace_builder.py:85` and `:91`), which stops
Codex from reading `notebook.ipynb` at all. The partial fix is a
`CODEX_READ_HINT` prepended to the prompt on non-plan turns, clarifying that the
shell/exec tool *may* be used to read workspace files and that the restriction is
about which files may be *written*. Plan mode's own preamble still takes
precedence.

**This fix is present but NOT yet sufficient — the feature does not work
end-to-end yet.** The read-only smoke re-run during this handoff reached
`state: completed` but with a useless answer: Codex still refused to read the
notebook, replying *"your final instruction says not to run shell commands."*
The hint is prepended at the *top* of the prompt, but
`workspace_builder.py` emits "Do not run shell commands." as the *last*
instruction, and Codex anchors on that trailing line. A `completed` turn here is
a misleading no-op, not a success. The real fix almost certainly belongs in
`workspace_builder.py`: make the "Do not run shell commands." instruction
adapter-aware (omit or reword it for Codex, whose only file access *is* the shell
tool), rather than trying to countermand it from the adapter after the fact.

This work lives in the **uncommitted** working-tree changes:
- `backend/app/agent_workspace/adapters.py`: adds the `CODEX_READ_HINT` constant
  and prepends it in `CodexAgentAdapter.run()` for non-plan turns.
- `backend/tests/test_agent_workspace.py`: asserts the hint is prepended on
  editable and read-only turns, and that plan mode's preamble wins.
- `scripts/codex_smoke.py`: the (untracked) smoke utility.

Note the unit tests only assert the hint string is *present* in the args; they do
not (and cannot) prove Codex actually reads the notebook. Only the real smoke
does that, and it currently shows the read is still blocked.

## Verification state (run in this handoff, 2026-07-27)

Green, **including** the uncommitted Task 6 changes:
- Backend: `.venv/bin/python -m pytest backend/tests -q` → **306 passed**.
- Codex-focused adapter tests → **9 passed**.
- Frontend: `npm test -- --run` → **77 passed**.
- Frontend production build: **clean**.

Not yet confirmed on this branch:
- **A real Codex turn that actually reads the notebook and does useful work.**
  The read-only smoke re-run during this handoff reached `state: completed` but
  the agent refused to read the notebook (see "Task 6" above) — so the feature is
  functionally incomplete despite the green `completed` status and green unit
  tests. The editable smoke (one changed cell + downstream execution) was not
  confirmed at all. Fixing the `workspace_builder.py` instruction for Codex is
  the first thing to do before anything else on this branch is trustworthy.
- **End-to-end (`npm run test:e2e`).** The earlier subagent run failed with all
  desktop specs timing out at the upload screen. The most likely cause is
  environmental, not a code regression: `playwright.config.ts` hardcodes
  `exec .venv/bin/python scripts/dev.py` for its web server, and the `.venv` was
  absent when that subagent ran, so the backend never started. `.venv` exists
  now, so the suite should be re-run to confirm. Treat e2e as **unverified**
  until it passes cleanly.
- **Final whole-branch review** was never dispatched.

## Recommended next steps to land this branch

1. Review and commit the Task 6 working-tree changes (the `CODEX_READ_HINT` fix,
   its tests, and `scripts/codex_smoke.py`). Suggested commit:
   `scripts: add manual Codex smoke turn; fix Codex read-tool instruction`.
2. Run the real smokes to a clean `completed`:
   `.venv/bin/python scripts/codex_smoke.py --read-only` and
   `.venv/bin/python scripts/codex_smoke.py` (editable). These spend real tokens
   and need a logged-in `codex` CLI. On failure the adapter surfaces CLI stderr
   in the `AgentAdapterError` details, and workspace-audit rejections list the
   offending paths.
3. Run `npm run test:e2e` where `.venv` exists (3 specs × desktop+mobile). If it
   still fails, check the `playwright.config.ts` web-server command actually
   starts the backend before diagnosing the app.
4. Dispatch the final whole-branch review (`superpowers:requesting-code-review`)
   and triage the Minor findings recorded per-task in `.superpowers/sdd/progress.md`.
   Notable open Minors: `AgentInfo.modes` is fetched but the Mode select still
   hardcodes edit/plan; a couple of small plan-mandated duplications; a
   pre-existing `--fake-agent` vs `--test-agent` doc flag mismatch in the
   engineering handoff.

## Guardrails that must not be weakened

The safety flags are non-negotiable and were deliberately not relaxed to make
any run pass: `--ephemeral`, `--ignore-user-config`, the `--sandbox` modes,
`network_access=false`, and the fail-closed version gate. If a future change
appears to require weakening one of these to get a turn to succeed, stop and
escalate rather than loosening the boundary — the write boundary is the whole
point of this product, and the CLI still runs with the user's ambient OS
permissions (this is a cooperative write-boundary, not an OS sandbox).
