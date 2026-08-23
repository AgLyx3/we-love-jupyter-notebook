# Stitch redesign — every difference, for review

Source: Stitch project **"Design Agent Instructions"** (`17558291239200577194`),
7 screens, captured 2026-08-23. Compared against `frontend/src/styles.css` and
the current components.

Evidence is the generated HTML for each screen, not the screenshots — values
below are quoted from the markup. Screens without HTML (the standalone plot) are
judged from the image only and marked as such.

Tick these off as you go. Nothing here has been applied.

**Review status (2026-08-23):** decisions recorded in §H below. Items not listed
there are still open.

---

## A. Design language / tokens

| # | Change | Now | Proposed |
|---|---|---|---|
| A1 | **Icon library swapped** | `lucide-react` (SVG components) | Material Symbols Outlined, loaded from Google Fonts as a webfont (`play_arrow`, `smart_toy`, `auto_awesome`, `attach_file`, `keyboard_tab_rtl`, …) |
| A2 | **Code font changed** | `ui-monospace, SFMono-Regular, Menlo` (system) | **JetBrains Mono**, loaded from Google Fonts |
| A3 | **UI font kept, weights narrowed** | Inter + system fallback | Inter at 400/500/600 only, from Google Fonts |
| A4 | **Whole colour system replaced** | 4 CSS vars + ~40 ad-hoc hexes | Full **Material 3** token set — `surface-container-{lowest,low,high,highest}`, `on-primary`, `inverse-surface`, `outline-variant`, etc. |
| A5 | **Page background** | `#eef1f3` | `#f8f9ff` (cooler, bluer) |
| A6 | **Agent blue demoted to a container token** | `--blue: #1769aa` is the accent | `#1769aa` becomes `primary-container`; `primary` is `#005087`. In dark screens the accent is **`#007acc`** (VS Code blue), not your blue |
| A7 | **Green retained but re-keyed** | `--green: #20744a` | `#20744a` present in the theme but **unused in markup**; the rendered green is `#146c43` with an `#a2f4c0/20` hover |
| A8 | **Two font-loading network dependencies added** | zero — all fonts are system or already bundled | Google Fonts for Inter, JetBrains Mono, and Material Symbols. Note this app is offline/local-first |

## B. Layout and structure

| # | Change | Now | Proposed |
|---|---|---|---|
| B1 | **Review bar moves top → bottom** | `.review-bar` sits above the notebook | A persistent **bottom status bar** spanning the notebook column |
| B2 | **Review bar contents recast** | `0 of 2 changes reviewed` + `‹ ›` nav + Keep all + Undo all | `2 Pending Reviews` + `Reviewing changes from Agent task "Clean dataset"` + Reject All + Accept All |
| B3 | **Per-change navigation removed** | Paired `‹ ›` buttons step through unsettled cells, wrapping | No stepper in any screen |
| B4 | **Agent panel becomes tabbed** | One scrolling column; history is a list inside it | Two tabs: **Agent** / **History** |
| B5 | **Composer restyled** | Textarea + Model select + Mode select + Send | Textarea + `attach_file` + circular `arrow_upward` send + a **Models** control in a footer row |
| B6 | **Tuning panel becomes a floating overlay** | Opens *inside the cell*, below the plot: preview left, knob rail right | A small **draggable "Tuning Controls" popover** floating over the notebook, detached from the cell |
| B7 | **Tuning preview column removed** | Dedicated live-preview pane with a "Preview — not in your notebook yet" band | No preview pane; you watch the cell's own output |
| B8 | **Knob min/max editors dropped** | Each knob: slider + value + editable `min` / `max` | Slider + value only |
| B9 | **Non-numeric knobs missing** | COLOR (text) and GRID (toggle) render alongside numeric knobs | Only the 4 numeric knobs appear |
| B10 | **Sidebar gains a footer** | Tree/outline fills the rail | A pinned **Settings** row at the bottom |
| B11 | **Cell gutter simplified** | Number pill + exec count + AI badge + hover scope buttons | Exec count only (`[1]`, `[*]`, `[14]`) |
| B12 | **Diff presentation changed** | CodeMirror inline diff, per-hunk widget with a left rule | Classic `-` / `+` line markers with red/green row fills |

## C. Labels and terminology

| # | Now | Proposed |
|---|---|---|
| C1 | `Undo` (per hunk / per cell) | **Discard** |
| C2 | `Undo all` | **Reject All** |
| C3 | `Keep all` | **Accept All** |
| C4 | `Agent changed this cell` | **Agent Suggestion** |
| C5 | `0 of 2 changes reviewed` | **2 Pending Reviews** |
| C6 | `Reset all` (tuning) | **Reset** |
| C7 | — | New string: `Cell Edit Proposed` / `Review the changes inline in the notebook editor.` |

`Keep`, `Rebuild map`, `Tune`, `Apply and re-run`, `Notebook Agent`,
`Scoped local edits`, `Unsaved`, `Revision N` are unchanged.

## D. Present in the app, absent from the design

These are the ones I'd look at hardest — several are load-bearing per the brief.

| # | Missing | Why it matters |
|---|---|---|
| D1 | **The entire Turn scope panel** | No `Turn scope`, no `N editable` / `N focus` counts, no cell chips, no clear-scope control. This is the permission fence the product rests on |
| D2 | **The Blocking / Trusted scope select** | No `Blocking`, no `Trusted` anywhere. The write-boundary control is gone |
| D3 | **Trusted-mode affordances** | No trusted banner, no amber trusted send button, no `Trusted turn` note |
| D4 | **`Read-only turn` note** | The composer hint explaining the agent can answer but not write |
| D5 | **Kernel state and controls labelling** | `play_arrow / stop / refresh` icons exist, but no `Kernel Idle`/`Busy` text or status dot |
| D6 | **Auto-save toggle** | No `Auto-save on/off` switch |
| D7 | **`Undo entire turn`** | Whole-turn reversal, the only remedy for a Trusted turn |
| D8 | **The `AI` agent-authored badge** | Provenance marker on agent-added cells |
| D9 | **Mode select (Edit / Plan)** | Plan mode has no entry point |
| D10 | **Outline state chips** | `never-run`, `out-of-order`, `risky` are not rendered on blocks |
| D11 | **Stale-map / stale-output warnings** | The amber "cells changed since this map was built" and "outputs are from code you undid" notes |
| D12 | **Green user-authored review strip** | No `.cell-review.tuned` equivalent — no screen shows the post-Apply state, so the user-vs-agent split is unverified |

## E. Additions the design makes

| # | Addition |
|---|---|
| E1 | Dark theme, VS Code-flavoured (`#1e1e1e` / `#252526` / `#3c3c3c`, accent `#007acc`) — closes a real gap |
| E2 | Named agent task in the review bar (`Agent task "Clean dataset"`) — better provenance than a bare counter |
| E3 | `Cell Edit Proposed` card in the agent panel, linking the turn to the inline diff |
| E4 | Attachment affordance (`attach_file`) surfaced in the composer |
| E5 | Settings entry point in the sidebar |
| E6 | Agent/History split, which would unclutter the conversation column |

## F. Constraint compliance

| # | Constraint | Verdict |
|---|---|---|
| F1 | Controls ≥ 28×28px | **Held.** `h-[28px]` / `min-w-[28px]` used throughout (12 in the shell, 9 in tuning, 2 per cell screen) |
| F2 | Review controls persistent, never icon-only | **Held.** `Discard` / `Keep` always show icon + text |
| F3 | Same Keep/Undo affordance at every anchor | **Partly.** Per-cell is `Discard`/`Keep`; the bar is `Reject All`/`Accept All` — different words for the same act (see C1–C3) |
| F4 | Review-bar buttons fixed-width so the group can't reflow | **Not evident.** Bottom bar uses `flex-1`; nothing pins widths |
| F5 | Dotted underline reserved for model-generated text | **Held.** `border-dotted` appears only on outline block names |
| F6 | Destructive action not the visual default | **Broken.** `Accept All` is the solid primary button, bottom-right — the most prominent control on screen |
| F7 | ARIA roles/labels preserved | **Unknown.** Static mockup markup; no `role`/`aria-*` to inspect |

## G. Judgement calls worth making explicitly

1. **D1–D3 together remove the scope model from the UI.** Possibly the design
   agent simply wasn't shown it — the screenshot set has no dedicated turn-scope
   image, and Block A describes it in prose only. That's a gap in what I sent,
   not necessarily a design decision.
2. **F6 plus C2/C3** shift the whole review posture from cautious to
   accept-by-default. Worth deciding deliberately rather than inheriting.
3. **A8** conflicts with local-first: three Google Fonts requests on a tool that
   otherwise binds to loopback and needs no network.
4. **A6** means the dark theme's identity colour is VS Code's blue, not yours.
   Fine as a choice, but it decouples agent-blue from the brand.


---

## H. Review decisions

Recorded 2026-08-23. `REJECT` = don't take the design's change, keep current
behaviour. `MODIFY` = keep the capability but change it from what the app does
today. `ACCEPT` = take the design's proposal.

| # | Decision | Detail |
|---|---|---|
| B1 | **REJECT** | Review bar stays at the top of the notebook pane. No bottom status bar. |
| B3 | **REJECT** | Keep the paired `‹ ›` per-change stepper, wrapping at both ends. |
| B4 | **ACCEPT** | Take the Agent / History tabs. History is not implemented — build the UI now and wire it later. |
| B5 | **REJECT** | Composer keeps the existing **Model** and **Mode** selects. |
| B9 | **REJECT** | Tuning must keep rendering non-numeric knobs (COLOR text input, GRID toggle) alongside numeric ones. |
| B11 | **REJECT** | Keep the current gutter cell-number pill. |
| D1 | **MODIFY** | Keep the Turn scope panel, but **make it more compact** than it is today. |
| D2 | **MODIFY** | Keep the Blocking/Trusted control, but **move it out of the agent-panel header** into the composer footer, beside Model and Mode. |
| D3 | **KEEP** | Trusted-mode affordances: banner, amber send button, trusted note. |
| D4 | **KEEP** | `Read-only turn` composer note. |
| D5 | **KEEP** | Kernel state text and status dot. |
| D6 | **KEEP** | Auto-save toggle. |
| D7 | **KEEP** | `Undo entire turn`. |
| D9 | **KEEP** | Mode select (Edit / Plan). |
| D11 | **KEEP** | Stale-map and stale-output warnings. |
| A8 | **ACCEPT** | Google Fonts dependency is fine — shipping as an MCP, so the offline-first objection doesn't apply. |

### Everything else — ACCEPTED by default

Reviewed 2026-08-23: anything not listed in the table above is **approved**.
That is:

- **A1–A7** — Material Symbols icons, JetBrains Mono for code, Inter 400/500/600,
  the Material 3 token set, the `#f8f9ff` background, `#1769aa` demoted to
  `primary-container` with `#007acc` as the dark-theme accent, and green re-keyed
  to `#146c43`.
- **B2** review-bar contents, **B6** tuning as a floating overlay, **B7** no
  preview pane, **B8** no min/max editors, **B10** sidebar Settings footer,
  **B12** `-`/`+` diff rows.
- **C1–C7** — the relabelling: Discard, Reject All, Accept All, Agent Suggestion,
  N Pending Reviews, Reset, Cell Edit Proposed.
- **D8** AI authorship badge dropped, **D10** outline state chips dropped,
  **D12** no green user-authored review strip.
- **E1–E6** — all additions.

Two caveats worth knowing, since they were not individually argued:

1. **A6 + A7 + D12 together dissolve the agent-vs-user colour split.** Blue stops
   being the agent's identity (it becomes a container token, and the dark accent
   is VS Code's `#007acc`), green is re-keyed and no longer marks user-authored
   changes, and the tuned review strip is gone. The brief called that split the
   one thing a redesign must not break. Accepting it is a legitimate call — but
   it is a product decision, not a styling one.
2. **C1–C3 plus F6** move the review posture from cautious to accept-by-default.
   F6 is still being fixed as a constraint violation, so the visual weight will
   come back to neutral even though the labels change.
