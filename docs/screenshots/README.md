# Screenshots

Reference shots of the frontend, for design work. Captured from a live session
against real notebooks in `examples/`, with a real kernel and the real Claude
CLI — not the test agent, so the agent panel shows a genuine transcript.

**All eight are the light theme.** The app has a dark theme too (the toggle is
the first control in the topbar, and it follows the OS by default); it is not
photographed here, on the same "no near-duplicates" grounds as everything in
"Not captured" below.

Deliberately minimal: **eight shots, one per distinct visual system**, not one
per screen. Nothing compares these images to anything, so every extra
near-duplicate is one more thing that goes stale silently.

**Re-capture after any visual change:**

```bash
.venv/bin/python scripts/dev.py --backend-port 8010 --frontend-port 5183
node scripts/capture-screenshots.mjs
```

Env: `SHOT_URL` (default `http://127.0.0.1:5183`), `SHOT_ONLY=<substring>` to
capture a subset, `SHOT_SKIP_AGENT=1` to skip the four shots that need a real
agent turn (02, 04, 05, 06) and finish in about a minute.

All shots are 1440×900 at 2× device scale, except `08-mobile` (390×844, full
page).

Two things that will otherwise strand a run:

- **Interrupted a previous run?** Restart the backend first. A killed run leaves
  an operation lease held, and every subsequent close returns `409 Conflict`, so
  the script fails at the first notebook open.
- **Risky-execution pauses.** `examples/ml-pipeline.ipynb` writes a file, so
  execution stops for approval. The script answers that modal with **Skip cell**
  — deliberately not Approve, since a screenshot run should not write files into
  the repo. If you add an example notebook that touches the network or the
  shell, expect the same gate.

Read `../design-brief.md` alongside these — it explains what the colours and
controls *mean*, which is not recoverable from the images.

| # | File | What it shows |
|---|------|---------------|
| 01 | `01-app-shell.png` | **Start here.** The whole editor at rest: sidebar, notebook, agent panel, topbar. Establishes the layout, the type scale, and the resting palette. |
| 02 | `02-app-shell-reviewing.png` | The same view mid-review — the top review bar ("N Pending Reviews", ‹ ›, Accept All, Reject All), diffed cells, and agent panel reading together. This is the state the product exists for. |
| 03 | `03-cell-with-output.png` | One code cell: gutter number, execution count, CodeMirror source, output region. The densest component in the app. |
| 04 | `04-cell-under-review.png` | **The key interaction.** A cell with an applied agent change: the "Agent Suggestion" review strip, the inline `-`/`+` diff, per-hunk Keep/Discard. |
| 05 | `05-agent-panel.png` | The right rail after a completed turn — Agent/History tabs, the compacted turn scope, agent output with the "Cell Edit Proposed" card, and the composer footer's Write scope / Model / Mode row. |
| 06 | `06-outline-panel.png` | The left rail's Outline tab after **Build map**, plus the pinned Settings footer. Generated names carry a dotted underline; every other fact is computed. (The never-run / out-of-order / risky chips were dropped in the redesign.) |
| 07 | `07-tuning-panel.png` | Plot tuning. The knobs are a draggable popover floating over the notebook; the picture keeps the full width of the cell's output region. Whole viewport, because cropping to the panel would leave the controls out of frame. |
| 08 | `08-mobile.png` | 390px, full page — below the single 800px breakpoint. Single column, resizer gone, 42px gutter, hover-only actions pinned visible. |

## Not captured

The script drives the app through most of these but does not photograph them.
Add a `shot()` call if you need one.

- The cold-start empty state and the file picker dialog (`.dialog-backdrop`)
- Topbar, sidebar Files tree, and notebook pane in isolation — all legible in `01`
- Cell multi-select, the right-click scope menu, the turn scope panel pre-turn
- A rendered markdown cell (`.markdown-preview`)
- The review bar alone (`.review-bar`) — visible in `02`
- The 800–1100px band, where the desktop layout is simply squeezed
- The dark theme, in any of these eight states
- Error output with "Add to chat" (`.output-error-block`), the risky-execution
  and close-unsaved dialogs, notices (`.notice`), the `@`-mention menu, the
  selection toolbar, and Trusted mode
