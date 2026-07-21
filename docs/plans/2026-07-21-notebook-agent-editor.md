# Local Notebook Agent Editor Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Build the accepted local notebook editor specification as a tested FastAPI and React application with scoped agent edits, notebook execution, undo, and a polished browser UI.

**Architecture:** A Python package owns notebook state, revisions, mutation leases, workspace validation, the Claude CLI adapter, and Jupyter execution behind thin FastAPI routes. A Vite React application consumes those routes and server-sent operation events. Domain services remain independently testable; browser tests exercise the integrated application with a deterministic fake agent and a small notebook fixture.

**Tech Stack:** Python 3.11+, FastAPI, Pydantic, nbformat, jupyter_client, pytest, React 19, TypeScript, Vite, CodeMirror 6, Lucide React, Vitest, Testing Library, Playwright.

---

### Task 1: Project Foundation And Notebook Document Domain

**Files:**
- Create: `pyproject.toml`
- Create: `backend/app/__init__.py`
- Create: `backend/app/main.py`
- Create: `backend/app/notebook_document/models.py`
- Create: `backend/app/notebook_document/service.py`
- Create: `backend/app/notebook_document/mutation_coordinator.py`
- Create: `backend/app/api/notebook_routes.py`
- Create: `backend/tests/conftest.py`
- Create: `backend/tests/test_notebook_document.py`
- Create: `backend/tests/test_notebook_api.py`
- Create: `examples/sample.ipynb`

**Requirements:**
- Configure installable backend and test dependencies.
- Implement notebook import with 5 MB limit, basic validation, missing/invalid/duplicate cell-ID normalization, full nbformat validation, dirty state, and monotonic revision.
- Implement one active `NotebookSession`, consistent snapshots, source edits guarded by `expectedDocumentRevision`, and a mutation coordinator that returns conflicts for concurrent ownership.
- Implement upload, current-notebook, download, and cell-source routes with structured errors.
- Add a compact notebook fixture containing Markdown, safe code, downstream code, and one cell that can be edited during tests.
- Follow TDD: write behavioral tests first, run them red, implement minimally, then run the focused and full backend suite green.

**Verification:**
- Run: `python3 -m pytest backend/tests/test_notebook_document.py backend/tests/test_notebook_api.py -v`
- Expected: all tests pass.
- Commit: `feat: add notebook document domain`

### Task 2: Turn Scope, Agent Workspace, Validation, And Undo

**Files:**
- Create: `backend/app/turn_scope/models.py`
- Create: `backend/app/turn_scope/service.py`
- Create: `backend/app/agent_workspace/models.py`
- Create: `backend/app/agent_workspace/workspace_builder.py`
- Create: `backend/app/agent_workspace/workspace_auditor.py`
- Create: `backend/app/agent_workspace/adapters.py`
- Create: `backend/app/agent_workspace/runner.py`
- Create: `backend/app/boundary_validation/validator.py`
- Create: `backend/app/agent_turns/service.py`
- Create: `backend/app/api/turn_scope_routes.py`
- Create: `backend/app/api/agent_turn_routes.py`
- Create: `backend/tests/test_turn_scope.py`
- Create: `backend/tests/test_agent_workspace.py`
- Create: `backend/tests/test_agent_turns.py`
- Modify: `backend/app/main.py`
- Modify: `backend/app/notebook_document/service.py`

**Requirements:**
- Implement editable/context scope with freeze/clear/history and terminal expiration.
- Build a fresh workspace containing a protected notebook, manifest, instructions, and plain editable files. Retain the authoritative manifest in memory.
- Collect only regular, non-symlink, contained UTF-8 editable files within aggregate size limits; audit protected and undeclared paths.
- Implement a deterministic fake adapter for tests and a version-gated Claude adapter using non-interactive mode with only `Read,Edit,Write` tools and no Bash.
- Implement turn lifecycle, atomic apply, two fresh-workspace retries for boundary violations, timeout/cancellation cleanup, no-op turns, operation status, final agent output, and mutation-lease release in all terminal paths.
- Implement checkpoint lineage, latest-applied-turn whole undo, and source-hash-guarded per-cell revert.
- Follow TDD and cover out-of-scope writes, symlinks, stale revisions, cancellation, retry, atomicity, and undo lineage.

**Verification:**
- Run: `python3 -m pytest backend/tests/test_turn_scope.py backend/tests/test_agent_workspace.py backend/tests/test_agent_turns.py -v`
- Expected: all tests pass.
- Commit: `feat: add scoped agent turn pipeline`

### Task 3: Kernel Execution And Operation Events

**Files:**
- Create: `backend/app/kernel_execution/models.py`
- Create: `backend/app/kernel_execution/risky_cell_classifier.py`
- Create: `backend/app/kernel_execution/kernel_session.py`
- Create: `backend/app/kernel_execution/service.py`
- Create: `backend/app/session_events/service.py`
- Create: `backend/app/api/execution_routes.py`
- Create: `backend/app/api/event_routes.py`
- Create: `backend/tests/test_risky_cell_classifier.py`
- Create: `backend/tests/test_execution_service.py`
- Create: `backend/tests/test_execution_api.py`
- Modify: `backend/app/agent_turns/service.py`
- Modify: `backend/app/main.py`

**Requirements:**
- Implement AST/text risky classification with the spec's safe/confirm categories.
- Implement manual run-cell/run-all, kernel status, interrupt, and restart.
- After agent apply, execute from the earliest edited code cell, skip Markdown, pause on risky cells, and expose correlated approve/skip/cancel operations.
- Commit output only for the active lease, execution attempt, revision lineage, and source hash; stop on errors and discard stale results.
- Publish turn, execution, notebook, and approval state through session events with an SSE route and polling-compatible operation resources.
- Use a real local Jupyter kernel in integration tests, with bounded timeouts and cleanup.
- Follow TDD for classifier, state transitions, stale decisions/results, and kernel output persistence.

**Verification:**
- Run: `python3 -m pytest backend/tests/test_risky_cell_classifier.py backend/tests/test_execution_service.py backend/tests/test_execution_api.py -v`
- Expected: all tests pass and no kernel processes remain.
- Commit: `feat: add guarded notebook execution`

### Task 4: React Notebook Editor UI

**Files:**
- Create: `package.json`
- Create: `vite.config.ts`
- Create: `tsconfig.json`
- Create: `index.html`
- Create: `frontend/src/main.tsx`
- Create: `frontend/src/App.tsx`
- Create: `frontend/src/styles.css`
- Create: `frontend/src/api/client.ts`
- Create: `frontend/src/notebook/NotebookView.tsx`
- Create: `frontend/src/notebook/NotebookCell.tsx`
- Create: `frontend/src/notebook/CellEditor.tsx`
- Create: `frontend/src/turnScope/TurnScopePanel.tsx`
- Create: `frontend/src/agentChat/AgentChatPanel.tsx`
- Create: `frontend/src/execution/KernelControls.tsx`
- Create: `frontend/src/execution/RiskyExecutionDialog.tsx`
- Create: `frontend/src/fileOperations/FileToolbar.tsx`
- Create: `frontend/src/test/setup.ts`
- Create: `frontend/src/App.test.tsx`
- Modify: `backend/app/main.py`

**Requirements:**
- Build the actual editor as the first screen: compact toolbar, notebook surface, and agent chat/scope side panel.
- Render Markdown and code cells with stable dimensions, CodeMirror editing, execution output, dirty/revision state, hover gutter actions, edit/context selection, and focused-cell navigation.
- Implement upload/download, source save, run cell/all, interrupt/restart, agent prompt submission, progress state, cancellation, diff presentation, whole-turn undo, and per-cell revert.
- Subscribe to SSE with polling fallback and render risky-cell approval in chat.
- Use Lucide icons and tooltips, restrained operational styling, no nested cards, no marketing hero, and responsive desktop/mobile layouts without overlaps.
- Follow component TDD for loading, scope actions, revision conflicts, and approval UI.

**Verification:**
- Run: `npm test -- --run`
- Run: `npm run build`
- Expected: tests and type-checked production build pass.
- Commit: `feat: build notebook agent editor interface`

### Task 5: Integrated Playwright Verification And Hardening

**Files:**
- Create: `playwright.config.ts`
- Create: `e2e/notebook-editor.spec.ts`
- Create: `scripts/dev.py`
- Create: `README.md`
- Modify: files found defective during integrated verification only.

**Requirements:**
- Add one command that starts the FastAPI backend and Vite frontend for development.
- Exercise `examples/sample.ipynb` through upload, manual edit, edit/context scope, fake-agent turn, diff, downstream execution, undo/revert, risky approval, download, and revision-conflict behavior.
- Use Playwright at desktop and mobile viewports. Capture screenshots and inspect them for blank regions, clipping, overflow, text overlap, and unusable controls.
- Verify console and network logs have no unexpected errors.
- Run the complete backend, frontend, build, and end-to-end suites.
- Document setup, Claude adapter capability requirements, fake adapter mode, commands, and security limitations.

**Verification:**
- Run: `python3 -m pytest backend/tests -q`
- Run: `npm test -- --run`
- Run: `npm run build`
- Run: `npm run test:e2e`
- Expected: all pass at desktop and mobile viewports.
- Commit: `test: verify notebook editor end to end`

