# Local Notebook Agent Editor

## Demo

https://github.com/user-attachments/assets/e52a4ee7-3bb2-4953-a515-4c32f8742c6f


A local FastAPI + React editor for working on Jupyter notebooks with scoped AI
agent editing. You open a local `.ipynb` file (or a project folder), edit and
execute cells, and save in place. When you ask the agent to make a change, it
can only rewrite the source of the cells you explicitly mark editable — the
backend applies or rejects its proposed edits; the agent never mutates your
notebook directly.

Everything runs on your machine and binds to loopback only. Read
[Security Limits](#security-limits) before using it with untrusted notebooks.

## Prerequisites

- **Python** 3.11+
- **Node.js** 20.19+ or 22.12+, with **npm**
- A local Python kernel (installed with the backend below via `ipykernel`)
- **Claude CLI** — required only for agent turns (see [Claude CLI](#claude-cli))
- **macOS or Linux.** The launcher and Playwright cleanup use POSIX process
  groups/signals. Windows is out of scope for v1.

## Setup

From the repository root:

```bash
python -m venv .venv
.venv/bin/pip install -e '.[test]'
npm install
```

Playwright is only needed if you plan to run the end-to-end suite:

```bash
npx playwright install chromium
```

## Claude CLI

Agent turns shell out to the `claude` command-line tool. You can open, edit,
run, and save notebooks without it — the CLI is only invoked when you send an
agent turn.

**1. Install it** using either official method:

```bash
# Native installer (installs to ~/.local/bin)
curl -fsSL https://claude.ai/install.sh | bash

# …or via npm
npm install -g @anthropic-ai/claude-code
```

Make sure `claude` is on your `PATH` (the app runs the `claude` executable
directly):

```bash
which claude
```

**2. Authenticate it once.** Run `claude` on its own and complete the login
flow (an Anthropic account / Claude subscription, or an API key). The app
launches the CLI non-interactively per turn and does not handle login for you.

**3. Match the supported version.** The adapter verifies the CLI before every
turn and **requires `claude >= 2.1.203` and `< 2.2.0`**. It relies on
`--safe-mode`, `--disable-slash-commands`, `--strict-mcp-config`, `--tools`,
and `--permission-mode`. Check your version:

```bash
claude --version
```

If it falls outside that range, agent turns fail fast with **"Unsupported
Claude CLI version"**; if `claude` is missing from `PATH`, you get **"Claude
CLI is unavailable."** In both cases opening/editing/running notebooks still
works — only agent turns are blocked.

> **Just want to try the UI without Claude?** Start the app with
> `--test-agent` (see [Run](#run)) to use a built-in deterministic adapter.
> It produces canned edits for demoing the flow and never calls the real CLI —
> it is not a substitute for a real agent.

## Run

One command starts FastAPI at `http://127.0.0.1:8000` and Vite at
`http://127.0.0.1:5173`:

```bash
.venv/bin/python scripts/dev.py
```

Then open **http://127.0.0.1:5173**.

Useful flags:

```bash
# Run against the built-in deterministic adapter instead of the real Claude CLI
.venv/bin/python scripts/dev.py --test-agent

# Use different ports (the Vite dev server proxies /api to the backend port)
.venv/bin/python scripts/dev.py --backend-port 8055 --frontend-port 5199
```

## Using it

1. **Open a notebook.** From the start screen choose a local `.ipynb` file, or
   open a project folder to browse and edit its notebooks in place. There's
   also an upload/download path if you prefer working on a copy.
2. **Edit and run cells.** Edit cell source directly and run cells against the
   local kernel. Toggle **Auto-save** or use **Save** / **Save As** to write
   back to disk.
3. **Scope cells for the agent.** Mark cells **editable** (the agent may
   rewrite their source) and/or add cells as **context** (attention only — see
   the security note). Sending a prompt with no editable cells is a valid
   read-only turn: the agent answers but writes nothing.
4. **Pick a model and mode** in the agent composer:
   - **Model** — Default, Opus, Sonnet, or Haiku. Default defers to the CLI's
     own default; otherwise it is passed as `--model`.
   - **Mode** — **Edit** applies scoped changes; **Plan** returns a
     step-by-step plan and writes nothing.
5. **Send.** Review the agent's answer and any inline diffs on the cells it
   changed; you can undo an applied turn.

## Verify

```bash
.venv/bin/python -m pytest backend/tests -q   # backend unit/integration tests
npm test -- --run                             # frontend unit tests
npm run build                                 # type-check + production build
npm run test:e2e                              # Playwright end-to-end (needs chromium)
```

The Playwright suite opens `examples/sample.ipynb` and covers desktop and
mobile workflows. Screenshots and failure traces are written under
`test-results/`.

## Security Limits

The editor binds to loopback and keeps one active notebook in process memory.
The agent workspace contains the full notebook, so the agent can read every
cell. Context selection is an attention signal in the manifest and prompt, not
a confidentiality control. Writes are imported only from manifest-listed
editable-cell source files; notebook structure, metadata, outputs, and
unselected-cell writes are rejected at the workspace boundary. Risk
classification pauses selected downstream operations for explicit approval.

This is not an operating-system sandbox. The CLI and executed notebook code run
with the current user's permissions. Risk classification is heuristic, approval
does not make code safe, and notebook execution can read files, use
credentials, access the network, or start processes. Use the editor only with
notebooks and agent instructions you trust.

## Contributing

Contributions are welcome — see [CONTRIBUTING.md](CONTRIBUTING.md) for setup,
the checks CI runs, and pull-request guidelines. Participation is governed by
our [Code of Conduct](CODE_OF_CONDUCT.md). To report a vulnerability, see
[SECURITY.md](SECURITY.md).

## License

Released under the [MIT License](LICENSE).
