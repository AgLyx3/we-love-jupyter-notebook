# Local Notebook Agent Editor

A local FastAPI and React editor for uploading, editing, executing, and downloading Jupyter notebooks. Agent turns operate on an explicit set of editable cells; context selection highlights cells for the agent's attention but is not a read-isolation boundary.

## Setup

Requires Python 3.11+, Node.js 20.19+ or 22.12+, a local Python kernel, and npm.

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

The combined launcher and Playwright server cleanup use POSIX process groups and signals and currently support macOS and Linux. Windows process management is outside the v1 local target.

The app uses the `claude` executable. It requires Claude CLI `>=2.1.203,<2.2.0` with support for `--safe-mode`, `--disable-slash-commands`, `--strict-mcp-config`, `--tools`, and `--permission-mode`. The adapter verifies the CLI version before every turn and rejects unsupported or unavailable installations.

## Verify

```bash
.venv/bin/python -m pytest backend/tests -q
npm test -- --run
npm run build
npm run test:e2e
```

The Playwright suite uploads `examples/sample.ipynb` and covers desktop and mobile workflows. Screenshots and failure traces are written under `test-results/`.

## Security Limits

The editor binds to loopback and keeps one active notebook in process memory. The agent workspace contains the full notebook, so the agent can read every cell. Context selection is an attention signal in the manifest and prompt, not a confidentiality control. Writes are imported only from manifest-listed editable-cell source files; notebook structure, metadata, outputs, and unselected-cell writes are rejected at the workspace boundary. Risk classification pauses selected downstream operations for explicit approval.

This is not an operating-system sandbox. The CLI and executed notebook code run with the current user's permissions. Risk classification is heuristic, approval does not make code safe, and notebook execution can read files, use credentials, access the network, or start processes. Use the editor only with notebooks and agent instructions you trust.

## Contributing

Contributions are welcome — see [CONTRIBUTING.md](CONTRIBUTING.md) for setup, the checks CI runs, and pull-request guidelines. Participation is governed by our [Code of Conduct](CODE_OF_CONDUCT.md). To report a vulnerability, see [SECURITY.md](SECURITY.md).

## License

Released under the [MIT License](LICENSE).
