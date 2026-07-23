# Contributing

Thanks for your interest in improving the Local Notebook Agent Editor!

## Development setup

Requires Python 3.11+, Node.js 20.19+ or 22.12+, a local Python kernel, and npm.

```bash
python -m venv .venv
.venv/bin/pip install -e '.[test]'
npm install
npx playwright install chromium
```

Run both servers with one command:

```bash
.venv/bin/python scripts/dev.py
```

The app uses the real Claude CLI (`>=2.1.203,<2.2.0`) for agent turns. The
`--test-agent` flag swaps in a deterministic in-process adapter and exists
**only** for the automated test suites — it is not a user-facing mode.

## Before you open a pull request

Run the same checks CI runs and make sure they pass:

```bash
.venv/bin/python -m pytest backend/tests -q   # backend
npm run build                                 # typecheck + bundle
npx vitest run                                # frontend unit tests
```

(The Playwright end-to-end suite, `npm run test:e2e`, is optional locally.)

## Pull request guidelines

- Branch off `main` and open a PR against `main`; direct pushes to `main` are
  disabled.
- Keep changes focused and include tests for new behavior.
- CI (`backend` + `frontend` jobs) must be green before a PR can merge.
- Preserve the core invariant: agent integration produces *candidate* cell-source
  changes; the notebook-document domain applies or rejects them, and agent write
  permission is turn-scoped. Changes to that boundary need explicit discussion.

## Reporting bugs and ideas

Open a GitHub issue with steps to reproduce (for bugs) or the problem you're
trying to solve (for features). For security-sensitive reports, see
[SECURITY.md](SECURITY.md).
