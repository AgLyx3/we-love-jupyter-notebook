# Prompts for a design agent

For a design agent that **cannot see this repository**. Everything it needs has
to travel in the prompt itself, so each block below is self-contained — paste
it, don't link to it.

## How to use

1. Attach the images from `docs/screenshots/` (eight files). Say which is which;
   the filenames are descriptive but attachments often arrive unnamed.
2. Paste **Block A — Context** first. It is the same every time.
3. Paste **one** task block after it.

Do not paste `design-brief.md` — it is written for someone with the code open
and refers to file paths the agent cannot follow. Block A is its portable
equivalent.

If the agent will return CSS you intend to apply, keep the selector inventory in
Block A. If you only want direction and mockups, you can drop it.

---

## Block A — Context

> ## The product
>
> A **local** desktop-in-a-browser editor for Jupyter notebooks with an AI agent
> attached. The user opens a real `.ipynb` from their own disk, edits and runs
> cells, and saves in place. The agent can rewrite cells — but only inside a
> **scope** the user grants it, and every change it makes lands as a reviewable
> diff the user must Keep or Undo.
>
> Audience: a data scientist or ML engineer working alone on their own machine.
> The tone is a professional tool, not a consumer app — dense, quiet,
> Jupyter-adjacent, closer to VS Code than to a marketing site.
>
> Stack: React + TypeScript, CodeMirror 6 for code cells, `lucide-react` icons.
> No CSS framework and no component library — one hand-written 355-line
> stylesheet. No dark mode.
>
> ## The one idea the design has to serve
>
> **The user must always be able to tell who wrote what, and always be able to
> undo it.** That is the product's whole argument. The colour system carries it:
>
> - **Blue `#1769aa` = the agent.** Agent-authored cells, the agent panel, the
>   review bar, the "AI" gutter badge, the per-cell review strip.
> - **Green `#20744a` = the user.** The same review strip in green means "you did
>   this with the tuning panel, not the agent." Also scope marks, auto-save on.
> - **Amber `#a15c00` = stale or unsaved.** Unsaved notebook, outputs from code
>   that was undone, a notebook map built before the last edit.
> - **Red `#b42318` = destructive or error.** Undo all, tracebacks.
> - **A dotted underline means a model wrote this text.** It appears on exactly
>   one thing — generated block names in the outline panel. It is provenance,
>   not decoration, and must not be reused.
>
> If a redesign blurs agent-vs-user, or makes a change look permanent, it has
> broken the product regardless of how it looks.
>
> ## Layout
>
> ```
> ┌──────────────────────────────────────────────────────────────────┐
> │ topbar  52px, sticky                                             │
> │  filename · Clean/Unsaved · Revision N │ auto-save · kernel · file│
> ├──────────┬───────────────────────────────┬───────────────────────┤
> │ sidebar  │ notebook pane                 │ agent panel           │
> │ 240px    │  ┌ review bar (during review) │  header + turn scope  │
> │ fixed,   │  ├ scrolling cell list        │  ───────────────────  │
> │ collaps- │  │   cell                     │  conversation         │
> │ ible     │  │   cell …                   │  ───────────────────  │
> │          │  │                            │  composer             │
> │ Files /  │  │                            │  300–760px, drag-     │
> │ Outline  │  │                            │  resizable            │
> └──────────┴──┴────────────────────────────┴───────────────────────┘
>                                              ↑ 6px drag handle
> ```
>
> - Middle + right are a CSS grid: `minmax(0,1fr) 6px var(--agent-width)`.
> - A notebook cell is itself a grid: 52px gutter + body, max-width 1000px,
>   centred.
> - One breakpoint, `max-width: 800px`: stacks to a single column, hides the
>   resizer, narrows the gutter to 42px, and forces the hover-only controls
>   permanently visible.
>
> ## Current visual language
>
> ```
> page background  #eef1f3      body text   #20262e
> surface (cards)  #ffffff      muted text  #63707c
> border           #cbd2d8      focus ring  2px #2780bd, 2px offset
> blue #1769aa   green #20744a   amber #a15c00   red #b42318
>
> tinted states: #dceef8 active blue · #dff2e8 context green
>                #fdf6ea amber warning · #fff3f2 error
> hover (neutral button): background #eef5fa, border #92aec2
>
> type   Inter, system-ui fallback; code: ui-monospace/SFMono-Regular/Menlo
> sizes  9, 9.5, 10, 10.5, 11, 11.5, 12, 12.5, 13, 16, 17, 19, 21, 24px
> radii  2, 3, 4, 5, 6, 7, 8, 999px  (5px is the default)
> icons  lucide; 17px in buttons, 13–14px inline
> shadow almost none — 0 1px 2px #1a26300d on cells; real shadows only on
>        dialogs and the context menu
> ```
>
> The type scale and radius set are **not** a system — they accreted. Fourteen
> font sizes and eight radii for one 355-line stylesheet.
>
> ## Hard constraints
>
> 1. Every visible, enabled control stays **at least 28×28px**. An automated test
>    walks the live DOM and fails on any smaller one.
> 2. The review controls (Keep / Undo) are **persistent — never hover-revealed,
>    never icon-only** — and look identical everywhere they appear: per diff
>    hunk, per cell, and in the review bar. Same affordance, different anchor.
> 3. The review bar's action buttons keep a **fixed width** so the group cannot
>    reflow as the "N of M reviewed" counter drops. A destructive control that
>    moves is one that gets mis-clicked.
> 4. The dotted underline stays exclusive to model-generated text.
> 5. Accessibility already in place, to be preserved: every icon button has an
>    `aria-label`; dialogs are `role="dialog" aria-modal`; the conversation is
>    `aria-live="polite"`; the pane resizer is a keyboard-operable
>    `role="separator"`; tabs use real `role="tab"`.
> 6. Buttons carry a global `min-width: 34px`, and flex items shrink by default,
>    so a labelled button inside a narrow flex row will squeeze to its min-width
>    and **clip its own label**. Any labelled button in a constrained flex row
>    needs `flex: 0 0 auto`.
>
> ## Known rough edges — fair game
>
> - No dark mode, despite an audience that mostly works in dark editors.
> - One breakpoint at 800px and nothing between 800–1100px, which is exactly
>   where a half-screen laptop window lands.
> - Cells can be dragged onto the agent panel to scope them, but there is **no
>   drop affordance at all** — no target, no hint the gesture exists.
> - Gutter and cell action buttons are `opacity: 0` until hover on desktop, so
>   they are nearly undiscoverable.
> - Density is uneven: the tuning panel is roomy, the outline panel is very
>   tight. They do not read as one system.
> - Long numeric values overflow the tuning panel's min/max inputs.
>
> ## The screenshots attached
>
> 1. **App shell** — the whole editor at rest.
> 2. **App shell, mid-review** — review bar, diffed cells, agent panel together.
> 3. **A code cell with output** — the densest component.
> 4. **A cell under review** — blue strip, inline diff, per-hunk Keep/Undo.
> 5. **The agent panel** after a completed turn.
> 6. **The outline panel** — generated names (dotted) vs computed facts.
> 7. **The plot tuning panel** — the user-driven green counterpart to a turn.
> 8. **Mobile, 390px** — below the breakpoint.

### Optional: selector inventory

Append this to Block A when you want CSS you can paste straight in.

> ## Selectors, so your CSS lands
>
> ```
> .app-shell .topbar .brand .toolbar-actions .kernel-controls .file-toolbar
> .workspace-layout .editor-layout .editor-resizer .workspace-placeholder
> .workspace-sidebar .workspace-sidebar-head .rail-tabs .workspace-tree
>   .tree-row(.notebook/.other/.active) .tree-empty .sidebar-reveal
> .outline-panel .outline-generate .outline-note(.stale/.error) .outline-list
>   .outline-block .outline-jump .outline-name(.generated/.unnamed)
>   .outline-range .outline-state(.never-run/.out-of-order/.risky)
>   .outline-detail .outline-label .outline-mark
> .notebook-pane .notebook-surface .notebook-cell(.is-focused/.is-selected/
>   .is-outlined) .cell-gutter .cell-number .execution-count
>   .agent-authored-badge .gutter-actions .cell-main .cell-actions
>   .cell-review(.tuned) .cell-review-label .cell-review-stale
>   .cell-stale-outputs .cell-retyped .cell-outputs .output-error-block
>   .output-error .output-add-chat .output-tune .image-output .html-output
>   .markdown-preview .cm-editor .cm-gutters .cm-hunk-widget .cm-hunk-actions
> .review-bar .review-bar-count .review-bar-actions .review-bar-nav
>   .review-bar-confirm .review-bar-danger .review-action(.keep/.undo)
> .agent-panel .agent-panel-title .scope-panel .section-heading .scope-counts
>   .scope-items .scope-item-row .scope-remove .scope-trusted-banner
>   .conversation .turn-history .turn-status .turn-state .turn-output
>   .turn-actions .empty-conversation .prompt-form .prompt-select
>   .prompt-controls .prompt-mode(.trusted) .chat-attachments .attachment-chip
>   .mention-menu .mention-option
> .tuning-panel .tuning-panel-head .tuning-close .tuning-panel-body
>   .tuning-preview-column .tuning-preview-frame .tuning-preview-band
>   .tuning-rail .tuning-knobs .tuning-apply .tuning-message .tuning-error
> .dialog-backdrop .file-picker .file-picker-head .file-picker-path
>   .file-picker-root .file-picker-list .file-picker-row .file-picker-foot
> .cell-context-menu .context-menu-heading .marquee-box .notice(.error)
> .upload-state .loading-screen .spinner
> ```
>
> Colour variables are `--border --muted --surface --blue --green --amber --red`
> on `:root`. `--agent-width` is set inline by JS and must keep working.

---

## Task 1 — Audit before redesigning

Cheap, and it tells you whether the agent has actually read the screenshots
before you let it change anything.

> Before proposing any changes, audit what you see.
>
> Go surface by surface through the eight screenshots and tell me:
> 1. Where the visual system is **inconsistent** — the same kind of thing styled
>    two ways, or two different things styled the same way.
> 2. Where the **semantics leak** — anywhere agent-vs-user, or
>    reversible-vs-permanent, is ambiguous from the pixels alone.
> 3. What a first-time user would **misread**, and what they would fail to find.
> 4. Which problems are cosmetic and which are structural.
>
> Be specific and cite the screenshot. No redesign yet, no CSS. If something
> looks wrong but you cannot tell from a static image, say so and tell me what
> interaction you would need to see.

## Task 2 — Visual direction and a real token system

The main event. Ask for the system, not a skin.

> Propose a redesign of this app's visual style.
>
> Deliver, in order:
>
> **1. Direction.** Two or three sentences on the character you are aiming for
> and why it fits a local, single-user, professional tool. Then say what you are
> deliberately *not* doing.
>
> **2. A token system.** The current scale accreted — fourteen font sizes, eight
> radii, ad-hoc greys. Replace it with a real one: a colour ramp, a type scale, a
> spacing scale, a radius set, and elevation levels. Justify the step ratios.
> Keep the four semantic colours meaningful — you may re-pick the hues, but blue
> must still mean agent, green user, amber stale, red destructive, and they must
> stay distinguishable to someone with deuteranopia.
>
> **3. Component specs** for: the notebook cell (resting, focused, selected,
> under review), the review bar, the agent panel, the outline panel, and the
> tuning panel. Say what changes and what stays.
>
> **4. CSS.** Concrete declarations against the selectors given above, as a
> stylesheet I can apply. Where you are replacing a rule, show the replacement,
> not a description of it.
>
> **5. A "what this changes semantically" section.** For every place you altered
> what a colour, weight, or control *means* rather than how it looks — call it
> out explicitly. If you changed nothing semantically, say that.
>
> Respect every hard constraint above. If a constraint blocks something you think
> is clearly right, propose it anyway but flag the conflict and its cost rather
> than silently breaking it.

## Task 3 — Dark mode

Worth a separate pass; the semantic colours are the whole difficulty.

> Design a dark theme for this app.
>
> The four semantic colours are the hard part: on a dark surface, `#1769aa` blue
> and `#20744a` green lose the separation they have on white, and the tinted
> state backgrounds (`#dceef8`, `#dff2e8`, `#fdf6ea`, `#fff3f2`) stop working
> entirely. Solve that first and show your reasoning.
>
> Deliver: the full dark palette as CSS custom properties; the light palette
> restated in the same variable names so the two are swappable; a rule for how
> tinted state backgrounds are derived in each mode; contrast ratios for every
> text-on-surface pair, meeting WCAG AA; and a recommendation on whether to key
> this off `prefers-color-scheme`, an explicit toggle, or both.
>
> Note that the code editor is CodeMirror 6, which brings its own theme — say
> how yours should meet it at the boundary.

## Task 4 — One surface, in depth

Template. Swap the surface and the question.

> Focus only on **‹the notebook cell›** (screenshots 3 and 4).
>
> This one component carries, at various times: a cell number, an execution
> count, an AI-authorship badge, hover-revealed scope buttons, hover-revealed
> cell actions, a persistent review strip in blue or green, an inline diff with
> per-hunk Keep/Undo, a stale-outputs warning, outputs of four different kinds,
> a Tune button on plots, and a text-selection toolbar.
>
> It is overloaded. Redesign it so that a user scanning a notebook can tell at a
> glance which cells need their attention, without losing any of the above.
>
> Give me: an information hierarchy for the component, the resting/hover/focus/
> selected/under-review states, and CSS. Tell me explicitly what you demoted, and
> what a user loses by that.

## Task 5 — Fix a specific rough edge

For the discrete problems, one at a time.

> ‹Cells can be dragged onto the agent panel to grant the agent permission to
> edit them, but there is no drop target, no hover affordance, and no hint the
> gesture exists. Most users never discover it.›
>
> Propose a fix. Give me the resting state, the during-drag state, and the
> post-drop confirmation. Keep it consistent with the rest of the visual system
> shown in the screenshots, and give me the CSS.

---

## Task 6 — Revision round 1 (Stitch, 2026-08-23)

Paste after Block A. Send with the same eight screenshots plus the design
agent's own current screens, so it can see what it is revising.

> Revise the screens you produced. The direction and the dark theme are good —
> keep them. What follows are corrections, not a restart.
>
> **Put back, unchanged from the original app:**
>
> - The **review bar stays at the top** of the notebook column. Do not move it to
>   a bottom status bar.
> - Keep the **paired `‹ ›` per-change navigation** in that bar. It steps through
>   the cells that still have something unreviewed, wrapping at both ends. It is
>   how a user works through a multi-cell turn.
> - The composer keeps its **Model** and **Mode** selects. Mode is Edit / Plan —
>   Plan mode has no other entry point, so removing the select removes the
>   feature.
> - The cell gutter keeps its **cell-number pill**, not just the execution count.
> - **Trusted-mode affordances**: the trusted banner on the scope panel, the amber
>   send button, and the "Trusted turn — the agent may add, delete, reorder, and
>   edit any cell" note.
> - The **`Read-only turn`** note under the composer.
> - **Kernel state** as text plus a coloured status dot — not icons alone.
> - The **Auto-save on/off** toggle in the top bar.
> - **`Undo entire turn`** in the agent panel. It is the only remedy for a
>   Trusted turn, which carries no per-cell ledger.
> - The **stale warnings**: "cells changed since this map was built" on the
>   outline, and "outputs are from code you undid" on a cell.
>
> **Tuning panel — restore capability:**
>
> - Knobs are not all numeric. Design the **text input** (e.g. a colour name) and
>   the **toggle** (e.g. grid on/off) variants alongside the slider, as one
>   coherent set.
>
> **Two things to change from the original, not restore:**
>
> - **Turn scope panel: keep it, but make it more compact.** It currently takes a
>   large block at the top of the agent panel — heading, counts, and one row per
>   scoped cell. It must still show how many cells are editable versus focus, let
>   a cell be removed, and let the whole scope be cleared. Make it denser without
>   losing those.
> - **Move the Blocking / Trusted control** out of the agent-panel header and into
>   the **composer footer, beside Model and Mode**, so all three per-turn settings
>   sit together. This is the write-boundary control: Blocking means the agent may
>   only rewrite cells the user marked editable; Trusted means the whole notebook
>   is writable. It must not read as a minor preference.
>
> **Keep as you had them:** the Agent / History tabs (History is not built yet —
> design the UI, we will wire it later), the dark theme, the named agent task in
> the review bar, the Cell Edit Proposed card, the attachment affordance, and the
> Settings entry point. Google Fonts are fine; ignore the offline concern.
>
> **Two constraint violations to fix:**
>
> - `Accept All` is currently the solid primary button, bottom-right — the most
>   prominent control on the screen. Bulk-accepting unreviewed agent edits must
>   not be the visual default. Rebalance so the cautious path reads as default.
> - The review bar's action buttons need **fixed widths**, so the group cannot
>   reflow as the "N reviewed" counter changes. A destructive control that shifts
>   under the cursor is one that gets mis-clicked.
>
> Deliver the revised screens plus a short note on anything above you disagree
> with and why.

---

## Getting better output

- **Give it the "why", not just the "what".** The one thing a codebase-blind
  agent cannot infer is that the colours are load-bearing. Block A leads with it
  for that reason — don't trim that section to save space.
- **Run Task 1 first.** If the audit misreads the screenshots, nothing built on
  top of it will be worth applying.
- **Make it commit.** "Give me three options" produces three half-designs. Ask
  for one direction with an explicit list of what was rejected.
- **Demand the semantic diff.** Item 5 of Task 2 is what catches a redesign that
  quietly turns the agent's blue into a generic accent.
- **Re-attach the screenshots after you apply changes** and re-run the audit.
  The agent has no repo access and no memory of what you shipped.
