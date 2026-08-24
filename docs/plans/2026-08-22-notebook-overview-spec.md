# Notebook Overview Panel — Implementation Spec

Status: **ready to build.** Design settled, key assumptions measured.
Branch: `claude/notebook-overview-research-qbb1tw`
Date: 2026-08-22

Companion to `docs/plans/2026-08-22-notebook-overview-research.md` (the research
and the reasoning behind every decision here). Read alongside
`docs/notebook-agent-editor-spec.md` (product/architecture authority) and
`docs/engineering-handoff.md` (implemented state).

**If you are picking this up cold, read in this order:** §1 below, then research
§12 (the data model) and §13 (what was measured), then come back here. Do not
re-open the decisions in §9 — they were each argued to a conclusion.

---

## 1. What this is

A left-rail panel that segments the open notebook into named **blocks** and
jumps you to the cells. It exists because a notebook that has grown large is
unnavigable, and every existing outline is a rendering of markdown headings the
author never wrote.

The map is built from **code**. A model segments and names it; markdown headings
are annotation, not structure. Nothing is written to the `.ipynb`.

**Measured, not assumed** (research §13): on a 57-cell notebook with no headings
and no function definitions, deterministic segmentation yields 5 blocks with a
44-cell lump; the model yields 14–17 blocks, largest 6–9, valid partition on
first attempt, with both the default model and Haiku.

---

## 2. The data model

One unit. Six fields. Five computed, one generated.

```python
@dataclass(frozen=True)
class Block:
    start: int            # cell index, inclusive        generated (boundaries)
    end: int              # cell index, inclusive        generated (boundaries)
    name: str             # short phrase                 generated
    state: str            # ok | never-run | out-of-order | risky   computed
    produces: list[str]   # variables later cells read   computed
    defines: list[Def]    # (name, call_sites)           computed
    marks: list[str]      # markdown headings inside     computed
```

Blocks partition the notebook: contiguous, ordered, every cell in exactly one.

`Def` is `(name: str, call_sites: list[int])`; an empty `call_sites` means the
function is never used and should render as such.

---

## 3. Backend

### 3.1 Module layout

Follow the `plot_tuning` shape — a package per feature, service on `app.state`,
router under `api/`:

```
backend/app/notebook_overview/
    __init__.py
    analysis.py     # Tier 0/1: the five computed fields. Port from
                    # docs/plans/probes/extract.py — it is working code with
                    # the bug fixes already reasoned through in its comments.
    segmenter.py    # Tier 2: prompt, CLI call, response validation
    service.py      # orchestration + cache
    models.py       # Block, Def, domain errors (mirror notebook_document/models.py)
backend/app/api/overview_routes.py
```

Wire in `main.py` beside the existing services (`main.py:64-83`), and
`app.include_router(overview_router)` with the others.

### 3.2 Endpoints

Mirror `tuning_routes.py`, including its split between free analysis and costly
work — that separation is what lets the client decide when to spend.

| Route | Does | Cost |
|---|---|---|
| `GET /overview` | Computed fields + cached blocks if fresh | free |
| `POST /overview/generate` | Runs the model pass, caches the result | one CLI call |

Both take `sessionId` and `expectedDocumentRevision` as the other routes do, and
raise `RevisionConflict` on mismatch. `GET` never calls the model: opening a
notebook must not spend anything.

`GET` returns blocks plus a `stale: bool` and, when no cache exists, the
deterministic fallback (§6) so the panel is never empty.

### 3.3 Reuse, and one caution

- `kernel_execution/risky_cell_classifier.py` gives the `risky` state free.
- `plot_tuning/discovery.py` records dependency edges separately from knob
  candidacy (`discovery.py:475`), so its graph half is reusable. **Its rejection
  logic is not** — it is deliberately conservative because a wrong knob is
  silent and dangerous, whereas here a missing edge just loses a `produces`
  entry. Do not inherit the bias; see research §9.5.

---

## 4. The model pass

### 4.1 Prompt

Use the prompt in `docs/plans/probes/segment.py` verbatim as the starting point.
It is validated, and two of its clauses were earned rather than guessed:

- **"Name the subject, not the activity"**, with the explicit ban on opening with
  *Analyze / Explore / Visualize / Process / Handle / Perform / Compute*. Without
  it, Haiku drifts to categorical names (research §13.6) — exactly the register
  the stage taxonomy was cut to avoid.
- **"Markdown headings are a hint, not an instruction. Override them where the
  code disagrees."** This is what makes headings annotation rather than
  structure.

Send **cell source only**. No outputs, ever (research §1). Truncate cells over
~1200 chars head/tail.

### 4.2 Invocation

Go through `agent_workspace/adapters.py` rather than shelling out to `claude`
directly — the CLI version check and flag discipline already live there, and a
second path to the CLI is a maintenance trap. This is a read-only call that
writes nothing to any workspace; if the adapter cannot express that shape
without a workspace, add the narrow path rather than bypassing it.

Model: **Haiku by default** (research §13.6 — structurally equivalent output).

### 4.3 Validation — non-negotiable

Reject and fall back rather than render a broken map. Port `validate()` from
`segment.py`:

1. Every index `0..N-1` covered exactly once — no gaps, no overlaps.
2. All indices in range.
3. Blocks in document order.
4. Names non-empty, and truncated for display rather than trusted to be short.

A response failing any check is discarded; serve the deterministic fallback and
surface that generation failed. Three probe runs across two notebooks and two
models each produced a valid partition first try, so this should be rare — but
"rare" is why it must be handled rather than assumed away.

---

## 5. Caching and refresh — simplified

**This supersedes research §7.1's per-block hashing.** Two probe findings
collapsed that design:

1. **Cost is ~10× lower than estimated** — about 22 tokens per cell, so a
   200-cell notebook is ~4.5k tokens per full pass (research §13.7).
2. **Partial invalidation assumed deterministic boundaries.** Once segmentation
   is model-generated, you cannot re-label one block in isolation: an edit may
   move a boundary, so the unit of caching is the whole notebook.

So:

- **One cache entry per notebook**, keyed on a hash of all cell **sources**
  concatenated (not the document revision — `_revision` also increments on
  `set_execution_count`, `service.py:308`, so it bumps on every cell *run*).
- Source hash unchanged → cached blocks stand. **Running cells never
  invalidates the map**, which is the frequent case.
- Source hash changed → the whole map is marked `stale: true`. Keep showing it,
  visibly stale, with a refresh affordance. Never silently regenerate.
- Regeneration is **always explicit** — a button, never automatic on open or on
  edit.
- The computed fields (state, produces, defines, marks) recompute on every
  request; they are an AST parse and effectively free.

Cache lives in memory on the service, keyed by notebook path. Losing it on
restart costs one button press.

---

## 6. Fallback when the CLI is unavailable

The app treats the Claude CLI as required *only* for agent turns (README), and
the panel must not break that. With no CLI:

- Serve deterministic blocks — markdown headings where they exist, milestone
  calls where they do not (`extract.py:segment`).
- All five computed fields work normally.
- Say plainly that names are unavailable, and why.

Be honest internally about what this is: research §13.1 measured it producing a
44-cell block on the hard case. It is a degraded mode that keeps the panel
functional, not a second-class-but-fine experience.

---

## 7. Frontend

### 7.1 Placement

**One left rail, two tabs — Files | Outline — toggled from the top-left button.**

- The toggle **extends the existing `sidebar-reveal` button** (`App.tsx:496`,
  a `PanelLeft` icon in `.brand`). Do not add a second button beside it.
- The rail is the existing `.workspace-sidebar`, 240px (`styles.css:41`). Do not
  add a region: `.workspace-layout` is already sidebar + notebook + 350px agent
  panel, and a third rail squeezes the notebook body.
- File tree is **workspace-scoped**, outline is **notebook-scoped**. With no
  folder open there is no tree, so Outline takes the whole rail and no tabs
  render.
- **Tab memory is per notebook**, in `localStorage` keyed by notebook path. Save
  As changes the path (treat as a new notebook); the upload path has no path, so
  it falls back to the default tab.
- Mobile is out of scope; the breakpoint at `styles.css:303` already collapses
  the layout.

### 7.2 Behaviour

- Click a block → focus its `start` cell. **Use the existing path**: the
  `focusRequest` effect at `NotebookView.tsx:43` already scrolls an arbitrary
  cell into view against a `refs` map keyed by cell id. No new scroll machinery.
- Hover a block → highlight its cells in the gutter.
- Progressive disclosure: blocks by default, details on expand. One level at a
  time.
- Subscribe to `/events` (`api/event_routes.py`, SSE with a sequence cursor)
  rather than polling.

### 7.3 Provenance must be visible

The name is generated; everything else is computed. Render that difference —
dotted underline on the name, plain text elsewhere — and make every block cite
its cell range so a wrong name is checkable in one click. This is what keeps a
bad name a correctable annoyance instead of a reason to distrust the panel.

**Never silently drop a markdown heading.** If the model's boundaries disagree
with a heading, the heading still renders as a mark inside whichever block
contains it. A user who wrote a heading and cannot find it reads the feature as
broken, and would be right to.

---

## 8. Tests

Match existing conventions — `backend/tests/test_*.py`, frontend `*.test.tsx`
beside the component, Playwright in `e2e/`.

Fixtures already exist: `docs/plans/probes/messy-exploration.ipynb` (57 cells, 0
headings, 0 defs, out-of-order) and `simulation-sweep.ipynb` (21 cells, 7 defs).
Copy them into `backend/tests/fixtures/` rather than referencing `docs/`.

Cover at least:

- **Analysis.** `produces` excludes loop-only bindings and comprehension targets
  (both were real bugs — see the comments in `extract.py`). `defines` counts
  name *references*, not just calls, so a callback passed to `solve_ivp` is not
  reported dead. Never-run and out-of-order detection.
- **Validation.** Gaps, overlaps, out-of-range indices, and unordered blocks are
  each rejected. This is the safety net; test it directly.
- **Cache.** Running a cell does not invalidate. Editing a cell marks stale.
  Nothing regenerates without an explicit request.
- **Fallback.** No CLI → deterministic blocks, no crash, names absent.
- **E2E.** Open a notebook, toggle the rail, click a block, assert the right
  cell is focused.

Do not test exact generated names — they are model output. Test the *shape*:
valid partition, non-empty names, block count within a sane band.

---

## 9. Decided — do not re-open

| Decision | Where argued |
|---|---|
| Purpose is navigation, not comprehension or cleanup | research §1 |
| Outputs entirely out of scope (costs the error-in-file signal) | research §1 |
| Map built from code; markdown is annotation, never structure | research §11.1 |
| One map, not two (code map + markdown marks) | research §11.1 |
| Model does segmentation *and* naming; both in V1 | research §11, §13.1 |
| Blocks contiguous and read-only in V1; editing is V2 | research §7.3 |
| No fixed stage taxonomy (load/clean/model/…) | research §12.2 |
| Variables and call sites render as text, not wires | research §12.2 |
| No graph/DAG view in V1 | research §11 |
| Haiku is sufficient | research §13.6 |
| Left rail, two tabs, top-left toggle, per-notebook tab memory | research §5.1 |

---

## 10. Still open

1. **Where V2 stores user edits to the map** — notebook metadata (travels when
   shared, but writes to the `.ipynb`) versus a sidecar (read-only, does not
   travel, and introduces a file this app has never written). Leaning sidecar.
   Does not block V1, which stores nothing.
2. **Chunking for notebooks too large for one pass.** Much less pressing given
   §5's cost finding, but undefined above roughly 500 cells.
3. **Import-based notebook-kind detection is regional, not global** (research
   §13.2) — `sklearn` imported at cell 42 makes a 41-cell pandas exploration
   report as ML. Only affects the deterministic fallback; needs windowing if
   used at all.
4. **Block-count sensitivity.** The same model gave 17 blocks on one prompt
   revision and 14 on another for the same notebook. Both are usable, but if
   stability across regenerations matters, it needs pinning down.

---

## 11. Running the probes

```bash
python3 docs/plans/probes/extract.py docs/plans/probes/*.ipynb        # Tier 0/1, no model
python3 docs/plans/probes/segment.py docs/plans/probes/messy-exploration.ipynb
python3 docs/plans/probes/segment.py --model haiku <notebook>
```

`segment.py` shells out to `claude` directly. That is fine for a probe and
**wrong for the app** — see §4.2.
