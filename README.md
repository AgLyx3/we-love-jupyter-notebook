# Local Notebook Agent Editor

## Demo

https://github.com/user-attachments/assets/e52a4ee7-3bb2-4953-a515-4c32f8742c6f


A local FastAPI + React editor for working on Jupyter notebooks with scoped AI
agent editing. You open a local `.ipynb` file (or a project folder), edit and
execute cells, and save in place. Each agent turn has a **scope**: in
**Blocking** mode the agent may only rewrite the source of cells you explicitly
mark editable; in **Trusted** mode the whole notebook is editable and the agent
may add, delete, reorder, and retype cells. Either way the backend validates and
applies (or rejects) every change — the agent never mutates your notebook
directly — and every change is reviewable as a diff and undoable.

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
> It produces canned Blocking-mode edits for demoing the flow and never calls
> the real CLI — it does not perform Trusted structural edits, and it is not a
> substitute for a real agent.

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

## As an MCP server

The editor can also run as an [MCP](https://modelcontextprotocol.io) server, so
an MCP client — Claude Code, for instance — edits and runs the notebook through
tools while you watch the same session in a browser tab and step in when you
want to. The tab is not a read-only view: the notebook, the kernel and the
scope are one server-side session, so a cell the client edits updates in front
of you, and a cell you edit is a cell the client is then refused for editing
against a stale read.

Install the extra and build the frontend — **both**, in this order. The tab is
served from the built frontend, so skipping `npm run build` leaves the editor
unable to start and the first tool call fails with exactly that message:

```bash
.venv/bin/pip install -e '.[mcp]'
npm run build
```

```bash
claude mcp add notebook-editor -- \
  /absolute/path/to/.venv/bin/notebook-editor-mcp \
  --workspace-root /absolute/path/to/your/project
```

Use absolute paths in both places — the client decides what directory the
server starts in, so a relative one is ambiguous. `--workspace-root` confines
every path the editor will open, list, or write to that directory; it is
optional and strongly recommended, since without it the editor reaches
anywhere you can. A typo fails at launch rather than at the first tool call.

`--no-browser` stops the tab opening by itself. On a headless or remote
machine no tab can open regardless, so `notebook_open` returns an `editorUrl`
for the client to hand you, and `notebook_show` returns it again on request.

If a client names a notebook that is not there, the error lists the `.ipynb`
files that *are* in the workspace, so it can pick one rather than guess again.

**The tools.** `notebook_open`, `notebook_read`, `notebook_status`,
`notebook_set_cell_source`, `notebook_insert_cell`, `notebook_delete_cell`,
`notebook_run_cell`, `notebook_run_all`, `notebook_save`, `notebook_show`.

Three things about them are deliberate:

- **Running a cell can stop and wait for you.** Execution asked for by a tool
  is treated as agent-initiated, so a cell the risk classifier flags pauses at
  *awaiting approval* and does not run until you approve it in the tab — the
  same gate an agent turn's downstream cells get. That pause is the reason to
  have a browser window at all.
- **Edits are checked against what the client last read.** If you changed the
  notebook in the tab in between, the edit is refused rather than applied, and
  the client is told to re-read. Nothing retries over the top of your change.
- **Images are described, not returned.** A plot comes back as its size and
  type; the picture is in the tab. A modest plotting notebook is about 23K
  tokens of base64 if forwarded whole, and unreadable to a model either way.
- **An added cell is never run for you.** `notebook_insert_cell` marks the new
  cell as agent-authored — the tab shows it with a badge reading "review before
  running" — and leaves it inert. Running it is a separate call, and goes
  through the approval gate like any other.

Agent turns are **not** exposed as tools — the client is already the agent, and
running the `claude` CLI underneath it would just nest a second one. You can
still send a turn from the tab. Neither is the file browser: the tab has a
picker, and a person browsing their own machine is a different thing from a
model enumerating it.

### Watching it work

Run as an MCP server the editor is a child process the client starts for you,
on a port chosen at random, with stdout reserved for the protocol. That is
three ways in which the usual `scripts/dev.py` habits do not apply, so:

- **The port is announced on stderr**, where MCP clients keep their server
  logs: `notebook editor ready at http://127.0.0.1:PORT (log: ...)`. Open that
  URL and you are looking at the same session the tools are driving. The same
  URL comes back as `editorUrl` from `notebook_open`, and from
  `notebook_show` on request.
- **`NOTEBOOK_EDITOR_LOG=/path/to/editor.log`** keeps the editor's own log at a
  path you choose instead of a temp file that is deleted when it stops. This is
  what to set before `tail -f`. Unset, a *failed* start is still drained and
  reported in the error, so you only need this for watching a server that is
  working.
- **Never print to stdout** from the server process. It is the transport; a
  stray `print` corrupts the protocol rather than showing up somewhere.

To see the tool surface without wiring up a client, the MCP Inspector speaks
the same stdio protocol:

```bash
npx @modelcontextprotocol/inspector \
  .venv/bin/notebook-editor-mcp --workspace-root /absolute/path --no-browser
```

Read [Security Limits](#security-limits) before pointing a client at a notebook
you would not run yourself.

## Using it

1. **Open a notebook.** From the start screen choose a local `.ipynb` file, or
   open a project folder to browse and edit its notebooks in place. There's
   also an upload/download path if you prefer working on a copy.
2. **Edit and run cells.** Edit cell source directly and run cells against the
   local kernel. Toggle **Auto-save** or use **Save** / **Save As** to write
   back to disk.
3. **Scope cells for the agent.** Two independent axes: mark cells **editable**
   (permission — the agent may rewrite their source) and/or pin cells as
   **Focus** (salience — the cells most relevant to your request; attention only,
   see the security note). Sending a prompt with no editable cells is a valid
   read-only turn: the agent answers but writes nothing.
4. **Pick a scope, model, and mode** in the agent composer:
   - **Scope** (top of the panel) — **Blocking** (default) lets the agent edit
     only the cells you marked editable; **Trusted** makes the whole notebook
     editable and lets it add/delete/reorder/retype cells. In Trusted the
     per-cell "allow agent edit" control is hidden and every pin is a Focus hint.
   - **Model** — Default, Opus, Sonnet, or Haiku. Default defers to the CLI's
     own default; otherwise it is passed as `--model`.
   - **Mode** — **Edit** applies scoped changes; **Plan** returns a
     step-by-step plan and writes nothing.
5. **Send.** Review the agent's answer and the inline diffs on changed cells. A
   Trusted turn also shows a summary of structural changes (added / deleted /
   reordered / retyped) and marks agent-added cells with a provenance badge;
   Trusted turns apply structure only and do **not** auto-run cells — you run
   them after reviewing. You can undo an applied turn (whole-turn undo).

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

The MCP surface is checked at three depths, and it is worth knowing which one
to reach for:

| | What it covers | Cost |
|---|---|---|
| `pytest backend/tests/test_mcp_server.py` | the tool surface against fakes — errors, revisions, descriptions | instant |
| `pytest backend/tests/test_mcp_end_to_end.py` | the tools against a real editor, kernel and approval gate | seconds |
| `python evals/mcp_tool_eval.py` | whether a *model* drives the tools well | a minute per run, and tokens |

Only the last one needs an authenticated `claude` CLI, which is why it is not
in CI. It answers a different question from the other two: not "does the tool
work" but "does a model reach for the right one, fetch no more than it needs,
and ignore a notebook that tells it to do something else".

```bash
python evals/mcp_tool_eval.py                 # every task, once each
python evals/mcp_tool_eval.py --repeat 3      # a pass rate, not a pass
```

## Security Limits

The editor binds to loopback and keeps one active notebook in process memory.
The agent workspace contains the full notebook, so the agent can read every
cell. Focus (attention) selection is an attention signal in the manifest and
prompt, not a confidentiality control. In **Blocking** turns, writes are imported
only from manifest-listed editable-cell source files; notebook structure,
metadata, outputs, and unselected-cell writes are rejected at the workspace
boundary. **Trusted** turns widen this to the whole notebook (see below). Risk
classification pauses selected downstream operations for explicit approval.

**Trusted turns** (the per-turn Blocking/Trusted toggle) widen the write boundary to
the whole notebook and let the agent **add, delete, reorder, and change the type of**
cells — including introducing **new executable code cells** you never scoped, and
changing execution order. The backend still derives, validates, and applies every
change (the agent never edits the notebook directly), and OS posture is unchanged (no
shell, loopback only). But this makes **review-before-run load-bearing**: agent-added
cells are marked with a provenance badge and are not auto-executed, yet once accepted
they are ordinary cells a later Run-All will run. Review the diff before running, and
use Trusted only with agents and instructions you trust. The risky-cell execution
approval flow still gates execution.

**Driving it from an MCP client** narrows two of these and widens one. Model-
initiated runs are gated, where a click of Run is not, and `--workspace-root`
confines every path the server accepts. But the notebook's own text — markdown,
code, and cell outputs — is read into the model's context while that same model
holds a tool that executes cells. A notebook from somewhere you do not trust
can therefore carry instructions to an agent that can run code on your machine.
The approval pause is the control that matters there; do not wave it through.

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
