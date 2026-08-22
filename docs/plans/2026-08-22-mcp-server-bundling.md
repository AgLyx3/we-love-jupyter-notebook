# Bundling the editor as an MCP server

Status: investigation complete, not built
Branch: `claude/app-mcp-browser-bundling-c11ed2`
Date: 2026-08-22

Read alongside `docs/notebook-agent-editor-spec.md` (product/architecture
authority) and `docs/engineering-handoff.md` (implemented state).

---

## 1. The question

Some MCP servers do more than answer tool calls: they own a local process, and
one of their tools opens a browser tab onto a UI that server is hosting. The
model drives the state; the human watches and intervenes in a real window.

Can this editor be bundled that way — an MCP server that a Claude Code (or any
MCP client) session starts, which brings up the notebook editor in a browser and
exposes notebook operations as tools?

**Verdict: yes, and the app is unusually well shaped for it.** Every finding
below was verified by running the app, not by reading it. But the useful shape is
*not* "wrap the HTTP API as tools." Three properties of the current design break
in a way that is silent rather than loud when the caller changes from a human at
a loopback browser to a model holding tool definitions. Section 5 is the real
content of this document.

---

## 2. What was verified

All checks were run against this tree at `437f725` (Python 3.11.15, Node
v22.22.2).

| # | Claim | How it was checked | Result |
|---|---|---|---|
| 1 | The backend runs with no Node and no frontend | `uvicorn backend.app.main:app` alone | `/health/ready` → `{"status":"ready"}` |
| 2 | A full notebook session is drivable over HTTP with no browser | `POST /notebooks/open` → `POST /execution/run-all` → poll → `GET /notebooks/current` on `examples/sample.ipynb` | Real kernel; `Total: 12` and `4.0` captured as cell outputs; revision advanced 0 → 4, `dirty: true` |
| 3 | The frontend is a static bundle | `npm run build` | Clean build, 904K `dist/`, absolute `/assets/…` refs — no dev server needed at runtime |
| 4 | One process can serve both UI and API on one port | PoC: `app.mount("/api", create_app())` + `StaticFiles` on `dist/` + SPA fallback | SPA at `/`, assets 200, `/api/health/ready` 200, `/api/notebooks/open` 200, SSE frames streaming from `/api/events` |
| 5 | The browser sees model-driven changes live | `curl -sN /api/events` while mutating | `event: notebook.updated` with `sequence`/`revision` arrived on the stream |
| 6 | The Claude CLI dependency is real and version-pinned | `ClaudeAgentAdapter().verify_supported()` | Returned `2.1.240`; adapter hard-requires `>= 2.1.203, < 2.2.0` |
| 7 | The risk gate does **not** apply to manual runs | Ran a `subprocess.run([...])` cell via `POST /execution/cells/{id}/run` | Classified `risk.level: "confirm"` — and **ran anyway**, `decision: null`, `state: completed` |

Finding 7 is the important one. See §5.1.

---

## 3. Why the app fits the pattern

**The frontend holds no authoritative state.** `frontend/src/api/client.ts:197`
sets `const API = "/api"` and every UI action is a server round-trip. Cell source
edits, scope selection, execution, agent turns, plot tuning — all of it is
backend state. There is no store to reconcile, so a tool call and a click are the
same kind of event to the server. Check 2 confirmed a complete session with the
browser never involved.

**Session state is server-side and shared.** `NotebookDocumentService` owns one
active document (`_session_id`, `_revision`, `_notebook`); `TurnScopeService`,
`AgentTurnService`, and `KernelExecutionService` all key off that session id.
So an MCP client and an open browser tab are not two sessions — they are two
views of one. That is exactly the property the "browser dashboard" pattern needs,
and it is already there.

**There is a push channel.** `GET /events` (`backend/app/api/event_routes.py`) is
an SSE stream with sequence-numbered replay via `Last-Event-ID`. When a tool call
mutates the notebook, an already-open tab updates itself (check 5). Nothing needs
to be built for the human's view to stay live.

**The UI ships as static files.** Check 3/4 mean a bundle needs Python and a
built `dist/` — not Node, not `npm install`, not a second port. The current
two-process `scripts/dev.py` (uvicorn + Vite on 8000/5173) is a *development*
arrangement, not the shape a bundle would ship.

---

## 4. Recommended shape

A **stdio MCP server, in Python, in this repo**, that owns the app's lifecycle:

```
MCP client (Claude Code)
  └─ stdio ─> notebook-editor MCP server (python)
                ├─ lazily starts uvicorn on an ephemeral loopback port
                │    serving dist/ at / and the API at /api  (§2 check 4)
                ├─ opens the browser tab on first open_notebook  ← the "dashboard trigger"
                └─ tool calls ──HTTP──> that same server
```

Lazy start matters: the server should not boot a kernel because the client
enumerated tools. First real call starts the process; MCP shutdown tears down the
process group (`scripts/dev.py` already has correct POSIX group teardown in
`terminate_process_groups` — reuse it, don't rewrite it).

### Proposed tool surface

Deliberately much smaller than the HTTP API.

| Tool | Endpoint | Notes |
|---|---|---|
| `open_notebook(path)` | `POST /notebooks/open` | Starts server if needed, opens the browser tab, returns session id + revision |
| `get_notebook()` | `GET /notebooks/current` | Cells with source, type, outputs |
| `session_status()` | `GET /session/status` | Revision, active execution, turn history |
| `set_cell_source(cell_id, source)` | `POST /cells/{id}/source` | |
| `run_cell(cell_id)` / `run_all()` | `POST /execution/…` | **Must be gated — §5.1** |
| `save()` | `POST /notebooks/save` | |
| `show()` | — | Re-open/focus the browser tab |

**Excluded on purpose:** `/agent-turns/*` (§5.2), `/files` + `/files/search`
(§5.3), and the plot-tuning shadow-kernel routes (`/tuning/*`) — those are an
interactive drag-a-knob loop whose value is the live preview; a model calling
`preview` in a loop is just a slower `set_cell_source`.

---

## 5. The three problems that must be solved first

These are not integration chores. Each is a place where a security or design
assumption is written against *a human clicking*, and quietly stops holding when
the caller is a model.

### 5.1 The risk gate does not cover model-initiated execution

`KernelExecutionService` takes a `prompt_for_risk` flag
(`backend/app/kernel_execution/service.py:143`) and passes it **`False` for
manual runs** (`:90`, both `/execution/cells/{id}/run` and `/execution/run-all`)
and **`True` only for agent-downstream runs** (`:137`).

That asymmetry is correct today and is load-bearing: a human pressing Run *is*
the approval, so re-prompting them would be noise. Only execution the *agent*
caused needs a gate, and it gets one — `attempt.state = "awaiting_approval"`,
blocking on `decision_event` until `/execution/{id}/approve|skip|cancel`.

Check 7 confirms the consequence empirically. A cell containing
`subprocess.run(["echo","hello"])` was classified `risk.level: "confirm"` — and
ran to completion with `decision: null`, because it arrived on the manual path.

Exposing `run_cell` as an MCP tool routes **model-initiated** execution through
the **human-initiated** path. The classifier still labels the cell; nothing
stops it. The gate the design relies on for agent-caused execution is silently
absent, and the README's "Risk classification pauses selected downstream
operations for explicit approval" is no longer true of the way most execution now
enters the system.

**Required:** MCP-initiated execution must run with `prompt_for_risk=True`. The
cleanest change is an operation `kind` (`"mcp"` alongside `"manual"` and
`"agent_downstream"`) that selects the gated path, so approval surfaces in the
browser tab and the tool call blocks on the human's click. This is a real feature
of the bundle, not a tax on it: it is the reason a browser window is *needed*
rather than merely nice, and it is the honest version of "the human stays in the
loop."

### 5.2 Agent-turn tools would nest Claude inside Claude

`ClaudeAgentAdapter.run` shells out to the `claude` binary
(`backend/app/agent_workspace/adapters.py:123-154`) with `-p`, `--safe-mode`, and
`--strict-mcp-config --mcp-config '{"mcpServers":{}}'`.

If `/agent-turns` were an MCP tool, the call chain is Claude Code → MCP server →
`claude -p` subprocess. A second agent, separately billed, with its own workspace
and its own scope rules, doing work the *calling* agent could do directly with
`set_cell_source`. The scoped-workspace machinery (`AgentWorkspaceBuilder`,
`WorkspaceAuditor`, the Blocking/Trusted boundary) exists to constrain an agent
that would otherwise touch the notebook directly — but in the MCP arrangement the
outer model is already constrained, by the tool surface itself.

**Required:** exclude agent turns from the tool surface. The MCP client is the
agent. This also drops the CLI version pin (`>= 2.1.203, < 2.2.0`, check 6) from
the bundle's runtime requirements — worth having, since that window is narrow and
the bundle would otherwise break on a CLI upgrade for reasons a user cannot
diagnose.

Note this leaves the *browser* path to agent turns intact — a human in the tab can
still send one. Only the tool surface omits it.

### 5.3 The file browser becomes a filesystem primitive

`file_browser/service.py:37` — `list_directory` defaults to `Path.home()` and
resolves any path given; `search_files` walks any root (pruning `.git`,
`node_modules`, etc., excluding `.pem`/`.key`, capped at 20,000 files). There is
no workspace confinement. `POST /notebooks/open` likewise accepts any path.

For a human at a file picker this is correct — it is their machine and their
home directory. As MCP tools, `list_files` and `search_files` are a directory
enumeration and filename search primitive handed to a model, reaching the whole
home directory by default. The deny-list is tuned for prompt-context noise, not
for confinement.

**Required:** the bundle takes a workspace root at startup and confines
`open_notebook` to it; `list_files`/`search_files` stay out of the tool surface
(the human has a file picker in the tab). Path containment should be enforced
server-side, not by the MCP layer, so the browser and tool paths cannot diverge.

### Also worth fixing, lower stakes

- **CORS.** `backend/app/main.py:59` pins origins to `127.0.0.1:5173` /
  `localhost:5173`. Same-origin bundling (check 4) makes this dead config for the
  bundle; leave it for `scripts/dev.py` but do not carry it into the bundled app.
- **No authentication.** The server binds loopback with no auth, so any local
  process can drive the notebook and the kernel. Acceptable-ish for a dev server
  the user launched; weaker once a long-lived MCP server is up for a whole
  session. An ephemeral port plus a per-launch token in the browser URL is cheap.
- **Singleton session.** One notebook per process (`_notebook`, `_session_id`).
  `open_notebook` on an already-open notebook returns **409** unless it carries
  `sessionId` + `expectedDocumentRevision` — observed directly during check 2.
  Every mutating tool needs the same precondition pair, so the MCP layer must
  track the current revision and give 409 a defined retry contract (re-read
  `session_status`, retry once) instead of surfacing a raw conflict to the model.

---

## 6. What this costs

Ordered so each phase is independently useful.

1. **Serve the SPA from FastAPI** — mount `/api` + `StaticFiles` + SPA fallback,
   behind a flag so `scripts/dev.py` is untouched. Proven in check 4; the PoC was
   ~20 lines. Add a build step so `dist/` exists for the bundle.
2. **Gate model-initiated execution** (§5.1) — new operation `kind`, threaded to
   `prompt_for_risk`. The blocking/approval machinery already exists; this
   selects it. The one change that touches core execution code.
3. **Confine paths to a workspace root** (§5.3) — server-side, in
   `NotebookDocumentService.open` and `file_browser`.
4. **The MCP server itself** — process lifecycle (reuse `dev.py`'s teardown),
   browser launch, ~7 tools over `httpx`, 409 retry contract.
5. **Packaging** — console-entry-point so `claude mcp add` can name a command,
   plus docs.

Phases 1, 3, 4, 5 are additive and touch nothing the current UI depends on.
Phase 2 is the only one that modifies existing execution behavior, and it changes
behavior only for a `kind` that does not exist yet.

## 7. Recommendation

Build it, in that order, and hold two lines: **no agent-turn tools**, and **no
ungated execution**. The first keeps the bundle from nesting an agent inside an
agent for no benefit. The second is what makes the browser window meaningful —
without it the tab is decoration, and the app has quietly become a way for a
model to run arbitrary local code with the approval gate switched off.
