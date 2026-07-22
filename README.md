# Local Notebook Agent Editor

A local FastAPI and React editor for uploading, editing, executing, and downloading Jupyter notebooks. Agent turns operate on an explicit set of editable cells and may only read notebook cells separately added as context.

## Setup

Requires Python 3.11+, Node.js 20+, a local Python kernel, and npm.

```bash
python -m venv .venv
.venv/bin/pip install -e '.[test]'
npm install
npx playwright install chromium
```

## Run

One command starts FastAPI at `http://127.0.0.1:8000` and Vite at `http://127.0.0.1:5173`:

```bash
.venv/bin/python scripts/dev.py
```

The normal mode uses the `claude` executable. It requires Claude CLI `>=2.1.203,<2.2.0` with support for `--safe-mode`, `--disable-slash-commands`, `--strict-mcp-config`, `--tools`, and `--permission-mode`. The adapter verifies the CLI version before every turn and rejects unsupported or unavailable installations.

For deterministic local demos and tests, opt into the fake adapter explicitly:

```bash
.venv/bin/python scripts/dev.py --fake-agent
```

Fake mode recognizes `[safe]` and `[risk]` in prompts and must never be used as a production default. `NOTEBOOK_AGENT_ADAPTER` accepts only `claude` or `fake`.

## Verify

```bash
.venv/bin/python -m pytest backend/tests -q
npm test -- --run
npm run build
npm run test:e2e
```

The Playwright suite uploads `examples/sample.ipynb` and covers desktop and mobile workflows. Screenshots and failure traces are written under `test-results/`.

## Security Limits

The editor binds to loopback and keeps one active notebook in process memory. Agent writes are imported only from manifest-listed cell source files, and notebook structure, metadata, outputs, and unselected cells are rejected at the workspace boundary. Risk classification pauses selected downstream operations for explicit approval.

This is not an operating-system sandbox. The CLI and executed notebook code run with the current user's permissions. Risk classification is heuristic, approval does not make code safe, and notebook execution can read files, use credentials, access the network, or start processes. Use the editor only with notebooks and agent instructions you trust.
