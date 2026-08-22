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
*not* "wrap the HTTP API as tools." Five properties of the current design break
in a way that is silent rather than loud when the caller changes from a human at
a loopback browser to a model holding tool definitions. Section 5 is the real
content of this document; section 6 reviews the whole plan against published
tool-design guidance and the MCP specification, and records what that changed.

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
| 8 | A whole-notebook read is far too large for a tool response | Ran `examples/plot-gallery.ipynb` (18 cells) and measured `GET /notebooks/current` | **92,550 bytes (~23K tokens)**, ~86 KB of it base64 PNG |
| 9 | No `Origin` or `Host` validation | Sent `Origin: https://evil.example` and `Host: evil.example` | Both **processed** — CORS withholds the read, it does not refuse the request |

Findings 7, 8, and 9 are the important ones — see §5.1, §5.4, §5.5. Checks 8 and
9 came out of the §6 guidance review, not the first pass.

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

Deliberately much smaller than the HTTP API, and domain-prefixed (§6) so the
names stay unambiguous next to other servers' tools in the same client.

| Tool | Endpoint | Notes |
|---|---|---|
| `notebook_open(path)` | `POST /notebooks/open` | Starts server if needed, opens the browser tab, returns session id + revision |
| `notebook_read(cells?, include_outputs?, format?)` | `GET /notebooks/current` | **Must shape its response — §5.4.** Selector + `concise`/`detailed` |
| `notebook_status()` | `GET /session/status` | Revision, active execution, turn history |
| `notebook_set_cell_source(cell_id, source)` | `POST /cells/{id}/source` | Carries the revision precondition |
| `notebook_run_cell(cell_id)` / `notebook_run_all()` | `POST /execution/…` | **Must be gated — §5.1.** May block on a human click |
| `notebook_save()` | `POST /notebooks/save` | |
| `notebook_show()` | — | Re-open/focus the browser tab |

**Excluded on purpose:** `/agent-turns/*` (§5.2), `/files` + `/files/search`
(§5.3), and the plot-tuning shadow-kernel routes (`/tuning/*`) — those are an
interactive drag-a-knob loop whose value is the live preview; a model calling
`preview` in a loop is just a slower `notebook_set_cell_source`.

---

## 5. The five problems that must be solved first

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

### 5.4 `notebook_read` is a context bomb as specified

Measured, not estimated. `examples/plot-gallery.ipynb` — 18 cells, a deliberately
small example — run to completion and then read back through
`GET /notebooks/current`:

```
TOTAL payload: 92,550 bytes  (~23,000 tokens)
  cell mixed:       image/png = 17,764 bytes
  cell subplots:    image/png = 15,228 bytes
  cell fig-finish:  image/png = 12,904 bytes
  … 8 base64 PNG outputs totalling ~86 KB
```

~93% of that payload is base64 PNG, which is worthless to a model as text. A
*small* example notebook already sits at the ~25K-token ceiling published tool
guidance recommends as an upper bound (§6). `MAX_NOTEBOOK_BYTES` is 5 MB, so a
real plotting notebook can exceed a context window in a single tool call.

**Required:** `notebook_read` must shape its response rather than proxy the
snapshot — elide image payloads to a descriptor (`<image/png, 17.7 KB, cell
"mixed">`), truncate long text outputs with a marker, accept a cell selector, and
offer `format: "concise" | "detailed"`. The full bytes stay one click away in the
browser tab, which is the right renderer for them.

### 5.5 No Origin or Host validation, with a kernel attached

The MCP specification requires that servers **MUST** validate the `Origin`
header on incoming HTTP connections to prevent DNS rebinding, and that local
servers **SHOULD** bind to loopback only (§6). This app does the second and not
the first. Verified against the running server:

| Request | Result |
|---|---|
| `POST /notebooks/open` with `Origin: https://evil.example` | processed (reached application logic) |
| `GET /kernel/status` with `Host: evil.example` | **200** |

The CORS middleware (`main.py:59`) is not a defense here. It withholds
`access-control-allow-origin` so a browser cannot *read* the response, but the
request is still *executed* — and under an actual DNS rebind the page's origin
*is* the rebound host, so CORS never engages at all.

Today this is a dev server the user starts and stops. Under the bundle it is a
long-lived loopback port, up for a whole MCP session, attached to a Jupyter
kernel that runs arbitrary Python. That turns a rebinding attack into local code
execution. I originally filed this under "lower stakes"; the spec language and
the kernel on the other end make it a blocker.

**Required:** validate `Origin`/`Host` against the bundle's own origin and reject
with 403 otherwise; bind loopback on an ephemeral port; carry a per-launch token
in the browser URL. Drop the 5173 CORS pin from the bundled app (same-origin
serving makes it dead config) while leaving it for `scripts/dev.py`.

### On the singleton session — a feature, not a chore

One notebook per process (`_notebook`, `_session_id`). `notebook_open` on an
already-open notebook returns **409** unless it carries `sessionId` +
`expectedDocumentRevision` — observed directly during check 2, and again in the
§5.5 table.

My first draft called for giving 409 "a defined retry contract (re-read
`session_status`, retry once)". **That was wrong** and is withdrawn. A blind
retry re-issues the write against whatever revision it just discovered — which is
precisely how you clobber an edit the human made in the browser tab a moment
earlier. The revision precondition *is* the staleness check that published
guidance names as a first-class reason to build a dedicated tool instead of
handing over raw access (§6). Defeating it to make tool calls tidier gives up the
main thing the design is buying.

**Required:** a 409 surfaces to the model as an actionable error — what changed,
what the current revision is, and to re-read before deciding — never as an
automatic write retry.

---

## 6. Review against published tool-design guidance

The plan above was drafted from the codebase alone, then reviewed against
Anthropic's agent/tool design guidance (the bundled `claude-api` skill's
`shared/agent-design.md`, and the published *Writing effective tools for AI
agents*) and the MCP specification's transport security requirements. Sources at
the end of this section.

### What the guidance confirmed

- **"Start with bash for breadth. Promote to dedicated tools when you need to
  gate, render, audit, or parallelize."** The narrow typed surface in §4 is the
  recommended shape, and §5.1's approval gate is the textbook reason to promote:
  *"Actions that require gating are natural candidates. Reversibility is a useful
  criterion — hard-to-reverse actions can be gated behind user confirmation."*
  Running a notebook cell is irreversible (it mutates kernel state and can touch
  the filesystem and network).
- **Blocking-until-answered is an established pattern**, not an awkward one:
  *"Claude Code promotes question-asking to a tool so it can render as a modal,
  present options, and block the agent loop until answered."* That is exactly
  §5.1 — the tool call blocks, the browser tab renders the approval.
- **Staleness checks justify dedicated tools:** *"A dedicated edit tool can
  reject writes if the file changed since Claude last read it. Bash can't enforce
  that invariant."* The app already has this in `expectedDocumentRevision`.
- **Read-only tools are parallel-safe.** `notebook_read` and `notebook_status`
  can be marked so; the mutating ones must stay serialized, which the mutation
  lease already enforces.

### What the guidance changed

| Guidance | Change to the plan |
|---|---|
| *"Prefix tools with their domain for clarity and scalability"* | Renamed every tool `notebook_*` (§4). `save()` and `show()` were unacceptably generic next to other servers in one client. |
| *"Restrict responses to ~25,000 tokens. Implement pagination, filtering, and truncation with sensible defaults"* | Added §5.4 — measured a small example notebook at ~23K tokens, 93% base64 PNG. `get_notebook()` as first specified was a context bomb. |
| *"Expose a `response_format` enum … 'concise' or 'detailed'"* | `notebook_read` takes `format` and a cell selector. |
| *"Replace opaque error codes with specific, actionable guidance"* | The app's `{code, message, details}` errors get mapped at the MCP layer — `notebook_not_loaded` becomes "no notebook is open; call `notebook_open` with a path". |
| *Tool descriptions should state when NOT to use a tool, token limits, and expected response times* | `notebook_run_all`'s description must say it can block indefinitely on a human approval click, and that it is the wrong tool for reading state. |
| *"Prototype → Evaluate → Collaborate"; generate eval tasks grounded in real use* | Added an evaluation phase to the build order below. It was missing entirely. |
| MCP spec: servers **MUST** validate `Origin`; local servers **SHOULD** bind loopback | Added §5.5 and promoted it from "lower stakes" to a blocker. |
| *"Strict JSON Schema with `additionalProperties: false`"* | Applies to every tool schema. |

### One risk neither source covers, specific to this app

Notebook content — markdown cells, code, and cell outputs — flows into the
model's context through `notebook_read`, and the same model holds
`notebook_run_cell`. A notebook from an untrusted source can therefore carry
prompt-injection text directly to an agent that can execute code locally. The
app's stated threat model ("use the editor only with notebooks and agent
instructions you trust") assumed a **human** reading those cells. The bundle
changes the reader. This does not block the build, but the approval gate (§5.1)
becomes the mitigation that matters, and the README's security section needs to
say so.

**Sources.** [Writing effective tools for AI agents](https://www.anthropic.com/engineering/writing-tools-for-agents) ·
[Anthropic tool design best practices (ADR summary)](https://github.com/vishnu2kmohan/mcp-server-langgraph/blob/main/adr/adr-0023-anthropic-tool-design-best-practices.md) ·
[MCP Transports — security requirements](https://modelcontextprotocol.io/specification/2025-06-18/basic/transports) ·
[MCP security best practices](https://modelcontextprotocol.io/docs/tutorials/security/security_best_practices) ·
[OWASP MCP Security Cheat Sheet](https://cheatsheetseries.owasp.org/cheatsheets/MCP_Security_Cheat_Sheet.html) ·
bundled `claude-api` skill, `shared/agent-design.md`.

---

## 7. What this costs

Ordered so each phase is independently useful.

1. **Serve the SPA from FastAPI** — mount `/api` + `StaticFiles` + SPA fallback,
   behind a flag so `scripts/dev.py` is untouched. Proven in check 4; the PoC was
   ~20 lines. Add a build step so `dist/` exists for the bundle.
2. **Origin/Host validation + ephemeral port + launch token** (§5.5) — middleware
   on the bundled app. Small, and everything else is unsafe to ship without it.
3. **Gate model-initiated execution** (§5.1) — new operation `kind`, threaded to
   `prompt_for_risk`. The blocking/approval machinery already exists; this
   selects it. The one change that touches core execution code.
4. **Confine paths to a workspace root** (§5.3) — server-side, in
   `NotebookDocumentService.open` and `file_browser`.
5. **The MCP server itself** — process lifecycle (reuse `dev.py`'s teardown),
   browser launch, the 7 tools of §4 over `httpx`, response shaping (§5.4), and
   actionable error mapping including the 409 contract (§5.5).
6. **Evaluate** — a set of tasks grounded in real notebook work ("find why cell 4
   errors and fix it", "add a plot of X", "this notebook is slow — profile it"),
   run end to end, measuring tokens per tool call and where the model picks the
   wrong tool. Feed the results back into the descriptions and response shaping.
   Published guidance treats this as part of building the tools, not as QA after.
7. **Packaging** — console-entry-point so `claude mcp add` can name a command,
   plus docs.

Phases 1, 4, 5, 6, 7 are additive and touch nothing the current UI depends on.
Phases 2 and 3 modify existing server behavior — 2 adds a rejection path that
loopback clients already satisfy, and 3 changes execution only for a `kind` that
does not exist yet.

## 8. Recommendation

Build it, in that order, and hold three lines: **no agent-turn tools**, **no
ungated execution**, and **no unvalidated origins**. The first keeps the bundle
from nesting an agent inside an agent for no benefit. The second is what makes
the browser window meaningful — without it the tab is decoration, and the app has
quietly become a way for a model to run arbitrary local code with the approval
gate switched off. The third is what keeps that same window from being reachable
by any web page the user happens to have open.
