# Design brief — Local Notebook Agent Editor

Hand this file, plus `docs/screenshots/`, to a design agent or designer asked to
restyle the frontend. It describes what the product is, what each surface is
for, which visual signals carry meaning, and what may not change.

Everything below reflects the app as built. The screenshots are captured from a
live session by `scripts/capture-screenshots.mjs` — re-run it after visual
changes, because nothing checks them automatically.

---

## 1. What the product is

A **local** desktop-in-a-browser editor for Jupyter notebooks with an AI agent
attached. The user opens a real `.ipynb` from their own disk, edits and runs
cells, and saves in place. The agent (the Claude CLI, running locally) can
rewrite cells — but only inside a **scope** the user grants it, and every change
it makes lands as a reviewable diff the user must Keep or Undo.

Frontend: React 19 + TypeScript + Vite. CodeMirror 6 for code cells,
`react-markdown` for markdown cells, Material Symbols Outlined for icons. No CSS
framework, no component library — one hand-written stylesheet,
`frontend/src/styles.css`, built on a Material 3 token layer with a light and a
dark theme.

The audience is a data scientist or ML engineer working alone on their own
machine. The tone is a professional tool, not a consumer app: dense, quiet,
Jupyter-adjacent, closer to VS Code than to a marketing site.

## 2. The central idea the design has to serve

**The user must always be able to tell who wrote what, and always be able to
undo it.** That is the product's whole argument, and most of the visual system
exists to carry it.

**How it is carried, since the 2026-08-23 redesign:** *in words and markers, not
in colour.* This changed deliberately — see the "Approved Visual Redesign" entry
in the spec's decision log, and `stitch-diff.md` §H caveat 1.

- **The review strip names its author.** Every settled-or-not change carries a
  persistent strip that reads **"Agent Suggestion"** or **"You tuned this
  cell"**, with a `smart_toy` or `tune` glyph beside it. That sentence is the
  provenance signal; the strip's colour is the same either way.
- **An agent-added cell carries a marker in the gutter** — a filled `smart_toy`,
  labelled "Added by the agent". It sits above the execution count, where the
  old `AI` badge was.
- **A dotted underline means a model wrote this text.** It appears on exactly
  one thing — generated block names in the outline panel — and it must not be
  reused for decoration. This one is unchanged, and it is the last piece of
  provenance still encoded in the mark rather than in words.
- **Every change is reversible from where it is shown.** Keep/Discard per hunk,
  per cell, and Accept All/Reject All in the review bar — persistent, never
  hover-revealed, never icon-only, and identical at every anchor.

Colour now carries *state and severity*, not authorship:

- `--primary` is the accent: focus, selection, active tabs, the write-scope
  control, the counter. `#005087` in light, VS Code's `#007acc` in dark.
- `--secondary` (green) means **kept / added / healthy** — the Keep button,
  added diff rows, an idle kernel, auto-save on.
- `--tertiary` (amber) means **stale or unsaved** — an unsaved notebook, outputs
  from code that was discarded, an outline map built before the last edit, and
  Trusted mode's warnings.
- `--error` (red) means **discard / reject / failure** — Discard, Reject All,
  removed diff rows, tracebacks.

**What this costs, stated plainly.** Before the redesign, blue meant *the agent*
and green meant *the user*: a tuned cell got a green review strip and an agent
edit a blue one, so authorship was legible at a glance without reading. It is
not any more. The words are still there and still correct, but they have to be
read. If a redesign wants to put that back, the cheapest honest place is the
review strip's left rule and its glyph colour — one rule in `.cell-review`.

If a redesign blurs agent-vs-user *in the words as well*, or makes a change look
permanent, it has broken the product regardless of how it looks.

## 3. Layout

```
┌──────────────────────────────────────────────────────────────────────┐
│ .topbar  52px, sticky                                                │
│  brand · filename · Clean/Unsaved · Revision N   │  auto-save ·      │
│                                                     kernel · file    │
├──────────┬───────────────────────────────────────┬───────────────────┤
│          │ .notebook-pane                        │ .agent-panel      │
│ .work-   │  ┌─ .review-bar (only during review)  │  header + scope   │
│  space-  │  ├─ .notebook-surface (scrolls)       │  ───────────────  │
│  sidebar │  │    .notebook-cell                  │  .conversation    │
│          │  │    .notebook-cell                  │   (turn history,  │
│ 240px    │  │    …                               │    status, output)│
│ fixed    │  │                                    │  ───────────────  │
│          │  │                                    │  .prompt-form     │
│ Files /  │  │                                    │   (composer)      │
│ Outline  │  │                                    │                   │
│          │  │                                    │  300–760px,       │
│          │  │                                    │  user-resizable   │
└──────────┴──┴────────────────────────────────────┴───────────────────┘
                                                    ↑ .editor-resizer 6px
```

- `.workspace-layout` is a flex row of height `calc(100vh - 52px)`.
- `.editor-layout` is a CSS grid: `minmax(0,1fr) 6px var(--agent-width)`.
- `--agent-width` is set inline from React state, clamped 300–760px, persisted
  to `localStorage`. The 6px column is the drag handle.
- The sidebar is a fixed 240px and can be collapsed entirely.
- A notebook cell is itself a grid: `52px` gutter + `minmax(0,1fr)` body,
  `max-width: 1000px`, centred.

There is exactly one breakpoint, `@media (max-width: 800px)` at the very bottom
of `styles.css`. Below it the layout stacks to a single column, the resizer is
hidden, the cell gutter narrows to 42px, and the hover-only gutter and cell
actions are forced to `opacity: 1`. Between roughly 800 and 1100px there is no
adaptation at all — three columns, all cramped. `08-mobile.png` shows the
stacked layout below the breakpoint; the 800–1100px band is not photographed,
because the only thing to see there is the desktop layout squeezed.

## 4. The surfaces, in the order they matter

### Notebook cell — `frontend/src/notebook/NotebookCell.tsx`
The densest and most important component. One cell can be showing, at once: a
number pill, an execution count, a filled `smart_toy` agent-authored marker,
hover-revealed gutter scope buttons, hover-revealed cell actions, a persistent
review strip reading "Agent Suggestion" or "You tuned this cell" with
Keep/Discard, an inline CodeMirror diff with `-`/`+` rows and per-hunk
Keep/Discard widgets, a stale-outputs warning, outputs (text, image, HTML
iframe, error traceback), a "Tune" button on plot outputs, and a text-selection
toolbar.

Rules that are load-bearing:
- The review strip is **persistent, never hover-revealed, never icon-only**. An
  applied agent change must be visible and reversible without hunting.
- Keep/Discard look identical everywhere they appear (per hunk, per cell, and as
  Accept All/Reject All in the review bar). Same affordance, different anchor.
- Every visible, enabled control is **at least 28×28px**. The e2e suite walks
  every `button` and `.icon-button` on screen and fails on any smaller one
  (`e2e/notebook-editor.spec.ts`, `e2e/plot-tuning.spec.ts`).

### Agent panel — `frontend/src/agentChat/AgentChatPanel.tsx`
Right rail. Title header, **Agent / History tabs** (History is a placeholder —
nothing behind it is wired), the turn scope section, a conversation area (turn
history, live state, markdown final output, and the "Cell Edit Proposed" card),
and the composer. The composer footer holds three controls: **Write scope**
(Blocking/Trusted — moved here from the panel header, and deliberately heavier
than its neighbours because it is the write boundary), Model, and Mode, plus the
send button. They wrap to a second row rather than shrink. Cells can be
**dragged from the notebook onto this panel** to add them to scope — the drop
target is the whole panel, and today there is no visible drop affordance. That
is a real gap worth designing.

### Turn scope — `frontend/src/turnScope/TurnScopePanel.tsx`
The permission fence, rendered as counts (`N editable`, `N focus`) plus one
compact row per scoped cell — index, type, and the first of its source, with the
editable/focus split carried by the left rule. In Trusted mode the counts are
replaced by a banner saying the marks are attention hints, not permissions. This
distinction must survive any restyle.

### Review bar — `frontend/src/notebook/ReviewBar.tsx`
Appears above the notebook while a turn has unsettled changes — at the **top**,
not as a bottom status bar. "N Pending Reviews" (which is also a
jump-to-first-change button; its title still gives the reviewed/total ratio),
paired `‹ ›` navigation, Accept All, Reject All. Accept All is deliberately not
the solid primary button the mockups made it. The action buttons hold a fixed
`min-width: 108px` **on purpose** so the
group never reflows as the counter drops — a destructive control that moves is a
destructive control that gets mis-clicked.

### Workspace sidebar — `frontend/src/fileOperations/WorkspaceSidebar.tsx`
Two tabs, Files and Outline, and a pinned Settings row in the footer (disabled —
there is no settings screen). Files is a lazy folder tree; non-notebook files
are shown greyed and unopenable, for orientation.

### Outline panel — `frontend/src/notebookOverview/OutlinePanel.tsx`
The notebook map: blocks of cells with a name, a cell range, and an expandable
detail showing what the block produces and defines. Names come from a model;
**everything else is computed deterministically**, and the dotted underline is
what tells them apart. Hovering a block highlights the cells it covers in the
notebook. The never-run / out-of-order / risky chips were dropped in the
redesign; the state still classes the row but nothing renders it (§7).

### Plot tuning panel — `frontend/src/plotTuning/TuningPanel.tsx`
Opens on a cell's plot output. The picture takes the whole width of the output
region and the preview stands in for it there; the extracted knobs (slider plus
value, text inputs, toggles — no min/max fields) live in a **draggable
fixed-position popover** floating over the notebook, with Apply / Reset in its
footer. Preview state is signalled by the "Preview — not in your notebook yet"
band over the picture. Applying produces a review strip reading "You tuned this
cell" — the same strip an agent change gets, distinguished by its words rather
than by colour (§2).

### Dialogs
`.file-picker` (open/save browser), `CloseNotebookDialog`,
`RiskyExecutionDialog`. All are `role="dialog"` over a `.dialog-backdrop`.

## 5. Current visual language

A Material 3 token set, defined twice at the top of `styles.css` — `:root` for
light, `[data-theme="dark"]` for dark — and referenced by every rule below it.
A literal colour further down the sheet is a mistake.

```css
light                                dark (VS Code flavoured)
--primary:            #005087        #007acc
--primary-container:  #1769aa        #007acc
--secondary:          #146c43        #89d185
--tertiary:           #754100        #fabc45
--error:              #ba1a1a        #f48771
--background:         #f8f9ff        #1e1e1e
--surface-container-lowest: #ffffff  #1e1e1e   (cells, menus, dialogs)
--surface-container-low:    #f2f3f9  #252526   (topbar, gutters, headers)
--surface-container:        #eceef4  #252526   (both rails, review bar)
--surface-container-highest:#e1e2e8  #333333   (hover)
--outline-variant:    #c1c7d2        #3c3c3c   (every border)
--on-surface:         #191c20        #cccccc
--on-surface-variant: #414750        #a3a3a3
```

Tinted states are `color-mix(in srgb, var(--token) N%, transparent)` rather than
fixed hexes, so one token list drives both themes.

- Fonts: **Inter** 400/500/600 for UI, **JetBrains Mono** for code, both from
  Google Fonts. Icons are **Material Symbols Outlined**, a ligature webfont,
  rendered by `frontend/src/ui/Icon.tsx`.
- Type scale: `--text-tiny` 11px, `--text-small` 12px, `--text-base` 13px,
  `--text-large` 14px, `--text-heading` 16px, `--text-display` 20px.
- Radii: `--radius` 2px default, `--radius-lg` 4px for cards and cells,
  `--radius-xl` 8px for dialogs and the tuning popover, `--radius-pill` for the
  cell-number pill and nothing else.
- Elevation: `--shadow-1/2/3`, softened in dark. Cells carry `--shadow-1`; only
  menus, dialogs and the tuning popover carry more.
- Focus is `outline: 2px solid var(--focus-ring); outline-offset: 2px` —
  uniform, and it should stay uniform.
- The global `button` rule sets `min-width: 34px`, and flex items shrink by
  default, so a button with a text label inside a narrow flex row will squeeze
  to its `min-width` and **clip its own label rather than overflow the row**.
  Every labelled button in a constrained container carries
  `width: auto; flex: 0 0 auto` for this reason. Any new one needs the same.
- **The CodeMirror boundary.** The editor carries its own theme, so
  `CellEditor.tsx` defines one written against these same tokens — including
  the syntax colours, which are `--cm-*` variables (VS Code Light+ / Dark+).
  There is exactly one place where the editor and its surroundings can drift,
  and it is that token list.

## 6. Constraints for a redesign

1. **Do not lose who-did-what** (§2). Since the 2026-08-23 redesign it is
   carried by the review strip's wording and the gutter's agent marker rather
   than by colour. It is semantics, not decoration — wherever it is carried.
2. **28×28px minimum on every visible, enabled control.** Enforced by e2e tests
   that enumerate the live DOM, so this is not advisory.
3. **Keep the review affordances persistent and identical across anchors.**
   Keep/Discard per hunk and per cell, Accept All/Reject All in the bar.
4. **Keep the fixed-width review-bar actions** so the group cannot reflow.
5. **The dotted underline stays exclusive to model-generated text.**
6. Accessibility already in place and worth preserving: every icon button has an
   `aria-label`, dialogs are `role="dialog" aria-modal`, the conversation is
   `aria-live="polite"`, the resizer is a keyboard-operable `role="separator"`,
   tabs use real `role="tab"`.
7. It is one stylesheet on a Material 3 token layer, with a light and a dark
   block at the top and no build-time theming. A colour anywhere below those two
   blocks is a bug. Introducing a component library or a CSS framework is a
   legitimate proposal — just make it an explicit one rather than a side effect.
8. **The CodeMirror boundary is part of the theme.** The editor has its own
   theming system; `CellEditor.tsx` drives it from the same tokens, syntax
   colours included. Change one side and the other has to follow.

## 7. Known rough edges (fair game)

- **Provenance is no longer colour-encoded** (§2). The words are right; the
  glance is gone.
- **One breakpoint, and a gap above it.** The 800px stacked layout works, but
  800–1100px gets no adaptation and is where a laptop half-screen lands.
- **The drag-cells-to-the-agent-panel gesture is invisible** — no drop zone, no
  hint that it exists.
- **Hover-only affordances on desktop.** Gutter scope buttons and cell actions
  are `opacity: 0` until hover. The 800px breakpoint pins them visible, so touch
  is covered — but discovery on desktop is not.
- **The outline no longer marks never-run, out-of-order or risky blocks.** The
  state still arrives from the backend and still classes the row
  (`.outline-block.state-risky`); nothing renders it.
- **The History tab is a placeholder.** It reads no state and calls no endpoint.
- **The sidebar's Settings entry is disabled.** There is no settings screen.
- **The tuning popover overlaps the plot it tunes** on a narrow notebook pane.
  It is draggable, which is the mitigation, not a fix.
- **Three Google Fonts requests on load.** Accepted (§A8 in `stitch-diff.md`)
  because this ships as an MCP, but the app is otherwise loopback-only.

## 8. Screenshots

See `docs/screenshots/README.md` for the annotated index, and re-capture with:

```bash
.venv/bin/python scripts/dev.py --backend-port 8010 --frontend-port 5183
node scripts/capture-screenshots.mjs
```

---

## Handing this to a design agent

**This file assumes you have the code open.** It cites file paths, component
names, and line-level rules a design agent working from screenshots alone cannot
follow. Do not paste it into one.

Use `design-prompts.md` instead. It carries the same substance — the semantics,
the layout, the tokens, the constraints — in a self-contained form, plus
ready-made task prompts for an audit, a full restyle, dark mode, a single-surface
deep dive, and one-off fixes. Attach the eight images from `screenshots/`
alongside it.

Keep this file for yourself, and for anyone reviewing what the design agent
sends back: it is the reference for whether a proposed change preserves the
meanings in §2 or quietly breaks them.
