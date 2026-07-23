# Engineering Handoff

Last updated: 2026-07-22

This document records the implemented state of the local notebook agent editor,
the invariants that must survive future changes, and the main unresolved risks.
Read this together with `AGENTS.md` and `docs/notebook-agent-editor-spec.md`.
The spec is the product and architecture authority; this file is the practical
handoff for continuing development.

## Current State

The repository contains a working local FastAPI and React notebook editor. It
can upload one `.ipynb` file, edit and render cells, execute cells through a
local Jupyter kernel, scope cells for an external agent, apply validated agent
source changes, inspect turn history and diffs, undo eligible changes, download
the notebook, and explicitly close it without stopping the application.

The normal development launcher uses the real Claude CLI adapter. The fake
adapter is opt-in and exists for deterministic tests and demos.

Latest verified baseline:

- Backend: 232 tests passed.
- Frontend: 43 tests passed.
- Production TypeScript/Vite build passed.
- Playwright: 3 scenarios passed in both desktop and mobile projects, for 6
  passing project runs.
- The final close/replacement lifecycle review found no Critical or Important
  issues.

The working tree also contains user-owned, untracked `AGENTS.md` and
`observability-report.html` files. Do not delete, overwrite, or commit them
without explicit direction.

## Run And Verify

Setup requirements and dependency installation are in `README.md`. The common
commands are:

```bash
# Real Claude CLI adapter; FastAPI 8000, Vite 5173
.venv/bin/python scripts/dev.py

# Deterministic fake adapter
.venv/bin/python scripts/dev.py --fake-agent

# Full verification
.venv/bin/python -m pytest backend/tests -q
npm test -- --run
npm run build
npm run test:e2e
```

Playwright starts its own test-agent app on backend port 8001 and frontend port
5174 by default. It runs serially in desktop Chrome and a Pixel 5 viewport. The
main fixture is `examples/sample.ipynb`; traces, screenshots, and videos are
retained under `test-results/` on failure.

The combined launcher uses POSIX process groups. `Ctrl+C` stops the backend,
frontend, active CLI subprocesses, and kernels. macOS and Linux are the current
targets; Windows process management is not implemented.

## Component Map

Backend:

- `backend/app/notebook_document/`: authoritative in-memory notebook, cell ID
  normalization, document revisions, mutation lease, upload/replacement,
  source/output commits, export, and close.
- `backend/app/turn_scope/`: mutable edit/context selection and immutable scope
  frozen for a turn.
- `backend/app/agent_workspace/`: temporary workspace construction, CLI
  adapters, process runner, protected-file audit, and cleanup.
- `backend/app/boundary_validation/`: converts audited editable files into
  candidate cell-source changes and rejects scope/revision violations.
- `backend/app/agent_turns/`: turn state machine, retries, apply, downstream
  execution, cancellation, history, checkpoint undo, and per-cell revert.
- `backend/app/kernel_execution/`: kernel lifecycle, manual/automatic
  execution, risky-cell approval, output bounds, timeouts, and retained
  execution history.
- `backend/app/session_events/`: bounded active-session SSE event journal.
- `backend/app/api/`: FastAPI request/response boundary and correlated mutation
  preconditions.

Frontend:

- `frontend/src/App.tsx`: session orchestration, API reconciliation, SSE with
  polling fallback, mutation generations, notebook replacement, and close.
- `frontend/src/notebook/`: notebook cells, CodeMirror source editing,
  Markdown/output rendering, inline diffs, and cell actions.
- `frontend/src/agentChat/`: turn scope, prompt submission, turn history,
  cancellation, undo, and execution status.
- `frontend/src/execution/`: kernel controls and risky execution confirmation.
- `frontend/src/fileOperations/`: upload/download/close controls and the dirty
  notebook discard dialog.
- `frontend/src/api/client.ts`: API contracts and EventSource connection.

## Non-Negotiable Invariants

1. The external agent never owns live notebook mutation. It edits candidate
   source files in a temporary workspace; `NotebookDocumentService` is the only
   authority that applies validated changes.
2. Agent write permission is turn-scoped. Only manifest-listed editable cell
   source files may be imported. Context cells are an attention signal, not a
   confidentiality boundary.
3. Every mutation is correlated to `sessionId` and document revision. Backend
   validation is authoritative even when the UI disables conflicting actions.
4. Only one notebook-mutating operation may hold the session mutation lease.
   Late CLI results, kernel results, approvals, cancels, undo, and close actions
   must not commit against a different revision lineage.
5. A notebook replacement or close starts a new lifecycle boundary. It must
   clear turn/checkpoint/change records, execution/attempt records, terminal
   scope history, SSE events, current selection, and the old kernel. Old
   GET-by-ID resources must return not found.
6. Close confirmation is bound to the exact session and revision that opened
   it. A refresh or replacement dismisses the dialog, and an old close response
   must never clear a newer notebook snapshot.
7. Local unsaved editor drafts disable source-dependent mutations. Preserve the
   draft during a conflict refresh so the user can reconcile it manually.
8. CLI and kernel processes require bounded waits and cleanup. Do not add an
   unbounded subprocess, worker join, kernel read, or approval wait.

These invariants are backed by regression tests. Changes to session lifecycle,
React reconciliation, or mutation concurrency should receive both focused tests
and an independent review.

## How Agent Editing Actually Works

For each turn, the backend freezes the current editable/context selection and
copies the full notebook into a temporary directory named like
`notebook-turn-<turn>-*`. The workspace contains:

- read-only `notebook.ipynb` with the full notebook;
- `AGENT_CELL_MANIFEST.json` describing editable and context cells;
- `INSTRUCTIONS.md` containing the prompt and boundary instructions;
- one writable `editable/cell_<id>.py` or `.md` file per editable cell.

The production adapter runs the local `claude` executable non-interactively
with `--no-session-persistence`, slash commands and Chrome disabled, and an
explicitly empty MCP configuration. Its tool list is `Read,Edit,Write`; the
adapter does not expose Bash. Supported Claude CLI versions are deliberately
fail-closed at `>=2.1.203,<2.2.0`, and the version is checked before every turn.

`create_app()` itself defaults to `FakeAgentAdapter` so tests and injected app
instances are deterministic. The module-level `backend.app.main:app` explicitly
calls `configured_agent_adapter()` and defaults to real Claude. Alternate
launchers must make this choice deliberately instead of assuming factory-created
apps use the production adapter.

After the CLI exits, the workspace auditor rejects protected-file changes,
unexpected files, out-of-scope edits, non-UTF-8 content, and size violations.
The entire candidate set is rejected atomically on a boundary violation and may
be retried with correction feedback up to two times. A valid set is applied
under the original mutation lease. Code cells from the first edited cell onward
then execute; automatically reached risky cells pause for user approval.

Important security distinction: this is a write-boundary design, not an OS
sandbox. The CLI receives the full notebook and runs with the current user's
filesystem and network permissions. Tool flags are defense in depth, not proof
that the child process cannot access the host.

The in-process test adapter is selected only with `--test-agent` (automated
tests only). It edits the first editable cell to a deterministic
value; `[risk]` in the prompt produces a cell that triggers approval. Never make
fake mode the production default.

## Notebook And UI Behavior

- V1 supports one active notebook and one active mutating operation.
- Upload accepts nbformat 4 notebooks up to 5 MiB. Missing, invalid, and
  duplicate standard cell IDs are normalized, which immediately marks the
  imported notebook dirty. The first upload has no preconditions; replacing an
  active notebook requires the current session and revision.
- Manual cell drafts are local until the cell Save action or CodeMirror save
  shortcut is used. A saved mutation increments the document revision.
- Agent prompts require at least one editable cell. Enter sends, Shift+Enter
  inserts a newline, and IME composition Enter is ignored.
- After a valid agent edit, downstream code execution starts at the first
  edited code cell. Manual Run Cell and Run All are explicit user execution
  decisions and do not show the automatic risky-cell prompt.
- Agent checkpoint undo restores notebook document state only. It does not
  restore Python kernel memory. Only the latest applied turn with an unbroken
  ownership lineage is eligible for full undo; per-cell revert is source-hash
  guarded.
- The X button in the file toolbar closes the current notebook. Backend close
  uses session/revision preconditions and leaves the app running. Dirty notebook
  state requires an accessible discard dialog; local unsaved cell drafts keep
  Close disabled. Cleanup failures are shown after the UI returns to upload
  state.
- Closing or replacing a notebook intentionally removes its in-memory history
  and shuts down its kernel. Nothing in V1 is durable except an exported file.

## Bounds And Failure Behavior

Notable defaults:

- Notebook and aggregate candidate source limit: 5 MiB.
- Agent turn timeout: 600 seconds.
- Kernel startup timeout: 30 seconds.
- Cell execution timeout: 300 seconds; recovery waits up to 5 seconds.
- Kernel output: at most 1,000 items or 5 MiB per cell execution.
- Terminal turns: at most 50 and 2 MiB retained in process.
- Terminal scope records: at most 100 and 512 KiB retained in process.
- SSE journal: at most 1,000 events, 4 MiB, and one hour for the active session.
- CLI stdout and stderr capture: 1 MiB per stream.

API conflicts return structured `409` errors. The frontend refreshes the
authoritative notebook and requires the user to retry instead of replaying a
mutation automatically. EventSource is the normal update path; a disconnect
enables a 1.5 second full-status polling fallback.

Kernel communication is local TCP without transport encryption, and Jupyter may
print a transport warning when a kernel starts. This is accepted for the
loopback-only v1 target, but it is not suitable for a remote or multi-user
deployment.

The current backend test run also emits an upstream Starlette deprecation
warning about `httpx`/`TestClient`. It does not fail tests, but dependency
upgrades should resolve it deliberately rather than suppressing it blindly.

## Known Risks And Potential Bugs

These items are not fixed by the current baseline. Reproduce and add tests
before changing behavior.

### High Priority

1. **Dirty notebook replacement has no discard confirmation.** The toolbar
   Upload action is allowed when the backend notebook is dirty, as long as no
   local cell draft or active mutation exists. Selecting another file replaces
   the in-memory notebook and purges its history. Apply the same immutable
   session/revision confirmation pattern used by Close before replacement.
2. **Browser refresh/tab close loses local cell drafts without warning.** Saved
   backend mutations and history reload while FastAPI remains alive, but a
   CodeMirror draft exists only in React state. A `beforeunload` guard intended
   to prevent immediate browser loss should key off local drafts, not merely the
   backend notebook's dirty flag. Separately, backend exit, restart, or crash
   loses the entire active notebook unless it was exported; durable recovery
   would require an explicit persistence design.
3. **There is no explicit way to discard a local cell draft.** A CodeMirror
   draft disables Close, Upload, execution, scope changes, and agent submission.
   The user must manually restore the exact server source or reload the page.
   Add a per-cell discard/reset action, with confirmation where appropriate,
   while preserving the rule that stale drafts cannot feed another mutation.

### Medium Priority

1. **Dirty state remains set after a successful Download.** Export currently
   returns bytes but does not mark the document clean. This is conservative
   because a browser download is a copy rather than an acknowledged save, but
   it means the UI continues to say `Unsaved` and Close continues to request
   discard confirmation. Decide whether the label means "changed since import"
   or "not exported since last mutation" before changing it.
2. **Cleanup can be incomplete after close/replacement.** Lifecycle listener
   failures are collected rather than rolling back an already unloaded
   document. The UI surfaces close cleanup errors, but a failed kernel shutdown
   may leave a local process requiring manual inspection. Replacement cleanup
   errors are available from the backend service but are not prominently shown
   by the upload flow.
3. **Risky-cell classification is heuristic.** Dynamic imports, indirect
   aliases, custom clients, native extensions, and novel side-effect APIs can
   evade static matching. Approval reduces accidental execution; it does not
   make code safe.
4. **Real-agent compatibility is intentionally narrow.** A Claude CLI automatic
   upgrade to 2.2 or later will stop all turns as unsupported until adapter
   capability tests and the version range are updated. Do not widen the range
   without verifying every required safety flag.

### Product Limits To Preserve Or Revisit Explicitly

- The full notebook is readable by the agent; context selection is not secret
  isolation.
- Notebook code and the CLI run with the local user's permissions.
- State and history are process-local and disappear on backend restart.
- Only the Python 3 kernel is currently created.
- Only one active notebook is supported; there are no tabs or durable sessions.
- Agent progress is not streamed; only the final CLI result and state changes
  are shown.
- HTML cell output is rendered in a sandboxed iframe. Rich output coverage is
  intentionally smaller than full JupyterLab compatibility.

## Development Lessons

Most serious bugs found during implementation were stale-state and lifecycle
bugs rather than notebook parsing bugs. Use these review questions for future
work:

- Can an async response commit after the notebook session or revision changed?
- Is the action bound to immutable preconditions captured when the user made
  the decision?
- Does close or replacement purge every service's retained state, including
  histories exposed by direct ID lookup?
- Can a failed kernel restart, timeout, cancellation, or output overflow leave
  a mutation lease held or a later result eligible to commit?
- Does a truncated summary preserve enough identity to fetch authoritative
  detail without overwriting newer UI state?
- Do local cell drafts survive conflict refresh while blocking operations that
  would use stale source?
- Are cleanup failures observable even when cleanup happens after the primary
  state transition?

Avoid solving these only by disabling buttons. The backend precondition,
attempt ID, source hash, mutation lease, and session lifecycle checks are the
actual integrity boundary.

## Suggested Next Work

1. Add dirty-upload replacement confirmation using a captured immutable target,
   including replacement-during-dialog and response-race tests.
2. Add a clear per-cell discard/reset path for local CodeMirror drafts.
3. Add browser unload protection for local cell drafts; treat warnings about an
   unexported backend notebook as a separate product decision.
4. Decide and document dirty semantics after successful export.
5. Exercise real Claude CLI turns in a small manual smoke test whenever adapter
   flags or workspace instructions change. The automated E2E suite deliberately
   uses the fake adapter.
6. Add a remote/multi-user threat model before changing loopback binding,
   authentication, kernel transport, or process isolation.

## Recent Hardening Areas

The most relevant commits for understanding regression history are:

- `c6978e4`: bind dirty-close confirmation to an immutable notebook target.
- `dc02d48`: purge retained state on notebook replacement.
- `fcbcc9a`: add dirty close confirmation and surface cleanup warnings.
- `6c45092`: purge notebook-scoped state on close.
- `9ddbb25` / `01099c3`: backend and frontend close lifecycle.
- `dad4563` / `682a01a`: Enter-to-send behavior and coverage.
- `0c88e42`, `1d03cc0`, `86a1514`: kernel restart, bounds, and lifecycle
  hardening.
- `43de361`, `db1be4d`, `a0c480b`: frontend reconciliation and retained history
  hardening.
- `ffd484a`: notebook replacement and temporary workspace cleanup.

Use `git show <commit>` for the exact regression tests and rationale. Keep
future changes narrowly committed by backend, frontend, E2E, and documentation
concern so another reviewer can audit lifecycle behavior without unrelated
noise.
