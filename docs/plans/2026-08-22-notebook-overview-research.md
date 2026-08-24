# Notebook Overview ("Roadmap") — Prior-Art Research

Status: **design settled, key assumptions measured, ready to build**
Branch: `claude/notebook-overview-research-qbb1tw`
Date: 2026-08-22

Read alongside `docs/notebook-agent-editor-spec.md` (product/architecture
authority) and `docs/engineering-handoff.md` (implemented state). This document
began as one question — **has anyone built "an overview of the notebook that
looks like a roadmap", and what did they learn?** — and ends with a design that
was argued to a conclusion and then tested against real notebooks.

**To build this, read `docs/plans/2026-08-22-notebook-overview-spec.md`** — the
implementation spec. This document is the research and the reasoning behind it.

**Start at §12** for the consolidated statement of what the panel captures and
shows, and §13 for what was measured. Both supersede anything earlier that
contradicts them.

**Scope decided 2026-08-22** (§1): navigation of a large notebook, click a block
to jump to its cells. The map is built from **code**, segmented and named by the
model, with markdown headings as annotation rather than structure (§11.1).
Outputs are out of scope; the map is read-only in V1 and editable in V2. §9
lists the assumptions still standing.

---

## 1. The purpose

**Scope decided 2026-08-22.** The job is navigation of a notebook that has grown
large: *keep track of what is in it, and click a block to jump straight to the
cells it stands for.* Not comprehension of someone else's analysis, not
storytelling, not cleanup. Who wrote the notebook does not matter — a notebook
you wrote yourself becomes unnavigable at the same size a stranger's does.

That makes the panel an **index with meaning**, and sets the bar accordingly:

1. What blocks is this notebook made of, in order?
2. What is each block, in a phrase I recognize?
3. Click a block → land on its cells.
4. What state is each block in — run, never run, out of order?

Everything below is measured against those four. Questions the research raised
but this scope does *not* buy — what the notebook produces, which cells are dead
ends, where intent and code diverge — are recorded where they arise and left
out of the build.

Two consequences worth stating early, because they cut real work:

- **Click-to-jump already exists.** `NotebookView.tsx:43` scrolls an arbitrary
  cell into view from a `focusRequest`, against a `refs` map keyed by cell id.
  The panel emits a focus request; it does not need new scroll machinery.
- **Outputs are out of scope entirely** (decided). No chart thumbnails, no
  artifact chips, no find-that-chart lane, and nothing output-derived in the
  model input. §3 and §4 are adjusted accordingly. Note the boundary: run state
  comes from `execution_count`, which is *not* an output, so never-run and
  out-of-order survive the cut. What is lost is the error-in-file signal — a
  cell carrying a saved traceback now looks like any other cell.

Notebook kinds in scope: **ML/data pipelines, exploratory analysis, and
simulation/research code**. Teaching and narrative notebooks — the
heavily-marked-up case — are explicitly *not* the target, which matters because
those are the only ones a heading-based outline already serves.

Evidence that this is a real, studied problem, not a hunch:

- **Managing Messes in Computational Notebooks** (Head, Hohman, Barik et al.,
  CHI'19) — establishes the "mess" as the normal state of an exploratory
  notebook, not a failure case.
- **Albireo** (Wenskovitch, Zhao, Carter, Cooper, North, VDS'19) — "in practice
  they are loose collections of scripts, charts, and tables that rarely tell a
  story or clearly represent the analysis process".
- **"I Need to Find That One Chart"** (Gu, Palani, Setlur, CHI'26) — data
  workers navigating long linear analysis transcripts; scrolling + keyword
  search are the only affordances they have, and both fail. Their design probe
  added structure, filtering, multi-level navigation, and detail-on-demand.
- **Visualising data science workflows to support third-party notebook
  comprehension** (Empirical Software Engineering, 2023) — the closest framing
  to our target user: *third-party* comprehension, explicitly.
- **How Scientists Use Jupyter Notebooks** (arXiv 2503.12309) — goals and
  quality attributes, including the ones nobody serves.

---

## 2. What exists today

### 2.1 Structure view — markdown headings only

| Tool | What it shows |
|---|---|
| JupyterLab built-in TOC | markdown headers, nested, click-to-scroll |
| `toc2` (nbextensions) | floating/dockable TOC, numbering, collapsible sections |
| VS Code Outline view | notebook cells; `Notebook > Outline: Show Code Cells` adds code cells (first line only); run-cell/run-section from the outline |
| `xelad0m.jupyter-toc` | generates a TOC *into* the notebook |
| jupyterlab-jupyterbook-navigation | Jupyter-Book TOC in a side panel |

**What they all are:** a rendering of the markdown headings the author already
wrote. Zero inference.

**Where they fail — and this is the whole opening:**

- A notebook with **no markdown headings gets an empty or useless outline.**
  This is the common case for exploratory work.
- Code cells appear as their **first line of source** — `df = pd.read_csv(...)`,
  or worse `# %%`. That is a label, not a meaning.
- No notion of **importance**. A one-line scratch cell and the model-training
  cell are the same size in the outline.
- No notion of **state**. Nothing says "this section never ran" or "this is
  stale relative to what it depends on".
- No notion of **output**. The chart you are looking for is invisible in a TOC.

VS Code's own issue tracker carries this as a long-standing ask
(microsoft/vscode-jupyter #1348, #1349), and the answer shipped was the
heading-level outline described above — the shallow version.

### 2.2 Dataflow view — the good stuff, but only in reactive-native tools

This is the closest existing thing to "a visualization of the code inside".

- **Observable minimap** — the design origin. Lists cells and draws wires to
  their dependencies; left-to-right reading order *is* dataflow order, and the
  wires are independent of vertical cell position. Deliberately shows **one
  level of connections at a time** — a restraint worth stealing.
- **marimo** — a Dependencies panel with two tabs: **Minimap** (default) and
  **Graph**. Marimo models the notebook as a DAG of cells where edge `(u,v)`
  means "v reads a variable u defines", built by **static analysis, not runtime
  tracing**. The minimap shows local context — direct vs. transitive
  dependencies, with transitives shown as unconnected-but-offset cells. Recent
  iterations added *cell index labels* and *showing real markdown/SQL content
  instead of boilerplate* — i.e. they converged toward "make each node say what
  it is", which is exactly the AI opening.
- **Hex 2.0** — infers dependencies per cell, models the project as a DAG behind
  a linear notebook UI, and exposes a **Graph view**.
- **dfnotebook / Dataflow Notebooks** (Koop, TaPP'17) — makes dependencies
  explicit at the language level by referencing other cells' outputs.
- **ipyflow / nbsafety** — the only way to get this inside plain Jupyter:
  a replacement kernel with a runtime tracer plus a static checker. It
  highlights **stale** cells (red) and **resolving/updated** cells (turquoise).

**Where they fail for us:** every one of these either (a) requires abandoning
`.ipynb`/IPython for a different runtime (marimo, Hex, Observable, dfnotebook),
or (b) requires swapping the kernel (ipyflow). And all of them show *variable
plumbing* — the graph tells you `df` flows from cell 3 to cell 9, never that
cells 3–9 are "loading and cleaning the sales data".

### 2.3 Slicing / cleanup

- **microsoft/gather** (from the CHI'19 mess paper) — program slicing over the
  execution log to produce the **ordered, minimal subset of code** that
  reproduces a chosen output. Framed as cleanup, but note what it actually
  computes: *the answer to "which cells actually matter for this result"* — the
  importance signal a TOC lacks, and the inverse of it (cells in no slice =
  dead-end scratch).

### 2.4 AI documentation for notebooks

- **Themisto** — generates per-cell documentation; triggered on cell focus,
  combines retrieved API docs with a generated summary, plus a prompt-the-human
  mode. Reported reduced documentation time and higher satisfaction.
- **HAConvGNN**, **Cell2Doc**, **Graph-Augmented Code Summarization in
  Computational Notebooks** — model work on notebook-specific summarization;
  the graph-augmented framing (use the dataflow graph as context for the
  summarizer) is directly reusable.
- **Jupyter AI** — chat/magics: explain, generate, fix, summarize. Chat-shaped,
  not map-shaped.

**Where they fail:** all of them are **per-cell** and **write-into-the-notebook**
(or chat). None produce a *document-level artifact you navigate*. A notebook
with a generated docstring on all 60 cells is still a 60-cell wall.

### 2.5 AI codebase maps — right idea, wrong altitude

- **CodeBoarding** — static analyzer + LLM agent core + incremental cache →
  layered architecture diagrams, component docs, Mermaid for PRs, VS Code
  extension, GitHub Action. The most complete example of the hybrid pattern.
- **Swark** — LLM-only, language-agnostic, Mermaid architecture diagrams.
- **Code2UML** (arXiv 2605.24453) — agentic, five specialized agents on the
  Claude Agent SDK, one per cognitive subtask.
- **CodeWiki** (arXiv 2510.24428) — benchmarks holistic repo documentation.
- **AI-Guided Exploration of Large-Scale Codebases** (arXiv 2508.05799).

**Where they fail for us:** they map **repos of modules and classes**. A
notebook has none of that structure — it is a flat sequence of statements with
implicit global state, outputs, and an execution history. A module-dependency
diagram of a notebook is one node. Nothing here handles *outputs* or
*execution state*, which are half of what a notebook actually is.

### 2.6 Research prototypes closest to "roadmap"

- **Albireo** (VDS'19) — the single closest prior work. Summarizes notebook
  structure as a **dynamic graph** of cells and their dependencies, explicitly
  aimed at comprehension, exploration, and *communicating the analysis
  narrative*. Evaluated by case study + expert interviews; found useful both for
  the author's self-reflection during exploratory programming and for
  communication/collaboration. It is a 2019 research prototype — no AI labels,
  not maintained as a product.
- **Enhancing Comprehension and Navigation in Jupyter Notebooks with Static
  Analysis** (Venkatesh et al./Bodden, 2023) — static analysis that
  **auto-annotates** a notebook with structural, commentary, and navigational
  text, plus **annotated cell folding**. This is "generate the missing headings"
  — done statically, before LLMs made it good.
- **InterLink** (arXiv 2502.16114) — links text with code and output.
- **NotePlayer** (UIST'24) — turns the notebook into a dynamic *presentation* of
  the analytical process. The "story" end of the same axis.

---

## 3. The gap

A notebook carries four signals — narrative (markdown), dataflow (variables),
artifacts (outputs), and state (execution). With outputs cut from scope (§1),
three remain in play, and nobody combines those three either:

|  | narrative (md) | dataflow (vars) | state (exec) |
|---|---|---|---|
| TOC / Outline | ✅ | ❌ | ❌ |
| marimo / Hex / Observable minimap | partial | ✅ | partial |
| ipyflow | ❌ | ✅ | ✅ (stale) |
| gather | ❌ | ✅ | ✅ (log) |
| Themisto / Cell2Doc | ✅ (generated) | ❌ | ❌ |
| CodeBoarding / Swark | ✅ (generated) | ✅ (module-level) | ❌ |
| Albireo | partial | ✅ | partial |

**The unclaimed square: a single navigable index that segments the notebook into
named blocks, shows what flows between them, marks what has and has not run, and
jumps you to the cells — for a plain `.ipynb` on a normal IPython kernel.**

Cutting outputs weakens this argument and it is worth being honest about how
much: the artifacts column was the one where prior art is thinnest, so it was
the easiest column to win. What remains is a narrower claim — the combination is
still unclaimed, but the margin is smaller, and the win now rests mostly on
*naming without markdown* (below) rather than on breadth of signal.

The one thing nobody does that survives the cut, and it needs a model:

- **Meaning without markdown.** Every structural tool is parasitic on headings
  the author already wrote. The notebooks that most need an index — the
  exploratory and simulation ones named in §1 — are exactly the ones without
  them. A model segmenting and naming blocks from code alone inverts that
  dependency. This is now the *whole* differentiator versus a plain outline —
  which is why §11 pulls it into V1 rather than deferring it.

Recorded but out of scope: **intent vs. reality** (the markdown says "train the
model", the code `.fit()`s the uncleaned frame). No notebook tool produces
findings of that class, and it needs both prose and code — but it answers a
comprehension question, not a navigation one, so it is not part of this build.

---

## 4. Signal inventory — what we can actually compute

Ordered by cost. The design principle this implies is in §6.

**Tier 0 — free, from the `.ipynb` alone. No kernel, no AI.**

- Markdown headings and their level → the skeleton, when it exists.
- `execution_count`: `null` → never run; non-monotonic across cells → the
  notebook was not run top-to-bottom, so linear reading is a lie. Not an
  output, so it survives the §1 cut.
- Cell magics, `!` escapes, imports → environment/setup cells.
- ~~Output types per cell~~ — cut with outputs (§1).

**Tier 1 — static AST. Deterministic, offline, cheap.**

- **Defs/uses per cell → the cell DAG.** `plot_tuning/discovery.py` already
  walks transitive assignment chains across cells conservatively (see its
  module docstring). That is most of a dependency graph already, built and
  tested, in this repo.
- **Function/class definitions → the notebook naming itself.** Elevated from a
  minor bullet: this is the best anchor available, because a `def` is
  author-committed meaning that costs nothing to read, and it is *code* rather
  than prose (§11.1). Crucially it is **domain-agnostic**, which is the way out
  of the vocabulary problem below — `moving_average`, `solve_step`,
  `compute_loss` describe the work without anyone maintaining a keyword list per
  field. Ranking is computable: call count across cells, how many distinct
  blocks call it, definition length. The inverse is free and useful — a `def`
  nothing ever calls is dead scratch, the gather signal (§2.3) without needing
  slicing. See §11.4 for how it renders.
- "Milestone" call detection — **but the vocabulary does not generalize across
  the three notebook kinds in scope.** The obvious list is pandas/sklearn-shaped
  (`read_csv` ingest, `.fit(` model, `.score` evaluate, `to_csv` export) and
  covers ML pipelines well. Exploratory analysis needs reshaping verbs
  (`groupby`, `merge`, `pivot`); simulation/research needs another set again
  (`seed`, `odeint`/`solve_ivp`, `sample`, sweep loops, `np.save`) and often has
  no ingest step at all — the data is generated, not loaded. Maintaining three
  keyword lists is a losing game. **Better signal: imports.** `import torch` /
  `sklearn` says ML; `scipy.integrate` / `pymc` says simulation; `seaborn` with
  no model import says exploratory. Detect the notebook's *kind* from imports
  first, then apply the matching milestone vocabulary. This is the weakest part
  of Tier 1 and the strongest argument for Tier 2 doing segmentation.
- Risk/side-effect classification: **already implemented** in
  `kernel_execution/risky_cell_classifier.py` (shell escapes, package changes,
  DB writes, credential access, file deletes, process execution).
- Dead-end detection: cells whose bindings nothing downstream reads (the
  complement of a gather slice).

**Tier 2 — model, one scoped pass over the notebook.**

- Segment into stages and **name them** (works with zero markdown).
- ~~Classify each stage into a fixed taxonomy~~ — **cut in §12.** The
  load/clean/explore/model/evaluate/export vocabulary is well established in the
  literature, but it has exactly the domain problem the milestone keywords have
  (§4, Tier 1): it describes ML pipelines and mislabels simulation and
  exploratory work. The generated *name* is the label; a forced category on top
  of it is a chip that is wrong a third of the time.
- One-line "what this does, in the notebook's own vocabulary" per stage.
- "What is this notebook for" — the single sentence at the top.
- Intent/reality divergence, as above.
- What the notebook *produces* — the deliverables list, in user terms.

---

## 5. Product concepts considered

**A. Roadmap rail (recommended).** A vertical spine beside the notebook: one
node per *block*, not per cell. Each node carries a name, its cell range, and a
state chip (never-run / out-of-order / stale / risky). Click → focus the first
cell of the range, which `NotebookView`'s existing `focusRequest` path already
does. Hover → the block's cells highlight in the gutter. Degrades to a plain
outline if no model is available.

**B. Minimap / DAG.** marimo/Observable-style wires. Highest information
density, and the most honest picture of a non-linear notebook — but it is the
one thing prior art *does* do well, and it answers "what depends on what", not
"what is this notebook".

**C. Generated notebook README / architecture card.** One AI-written page:
purpose, inputs, stages, outputs, caveats. Cheap, very legible, but static prose
— not navigation, and it rots on the next edit.

**D. Artifact index.** ~~"Find that one chart": every output as a thumbnail
grid.~~ **Cut** — outputs are out of scope (§1). Recorded because it is the
best-evidenced concept here (it is the literal finding of the CHI'26 paper), so
if outputs ever come back into scope, this is where they go.

**Recommendation: A, with B available as a secondary tab** — the same two-tab
shape marimo converged on (map first, graph second), which is weak evidence it
is the right decomposition.

### 5.1 Where it lives — decided

**A toggle button in the top-left corner opens and closes the panel, and the
panel shares the existing left rail with the file tree as two tabs: Files |
Outline.**

The corner is already spoken for, which is what makes this work rather than
collide. `App.tsx:496` renders a `sidebar-reveal` button (a `PanelLeft` icon)
inside `.brand`, shown when a workspace folder is open and the file tree is
collapsed. That is already a top-left left-panel toggle. **Extend it; do not add
a second button beside it** — two adjacent toggles with near-identical icons in
the same corner is the confusing outcome.

Sharing the rail rather than adding one matters because of the current widths.
`.workspace-layout` is a flex row: a fixed 240px `.workspace-sidebar`, then
`.editor-layout`, itself a grid of notebook / 6px resizer / 350px agent panel
(`styles.css:37-41`). A second left rail would put three panels around a
notebook body that is already the squeezed middle.

**The two panels are never both mandatory**, which is what makes one rail
sufficient:

- The file tree is **workspace-scoped** — it exists only when a folder was
  opened. Open a single `.ipynb` directly (the README's first path) and there is
  no rail at all, so the outline takes the whole 240px and needs no tabs.
- The overview is **notebook-scoped** — it applies whenever a notebook is open,
  folder or not.

So: rail present → tabs **Files | Outline**. No folder → Outline alone. One
button, one region, no new competition for width.

**The rail remembers its tab per notebook** (decided). Switching between two
notebooks in a workspace restores whichever tab each was left on, so a notebook
you navigate by outline stays that way while one you navigate by file tree does
too.

That needs somewhere to remember it, which brushes against "V1 stores nothing"
(§7.3) — so be precise about the distinction. V1 stores no *notebook* state and
writes nothing to the `.ipynb`; a UI preference is a different kind of thing.
**`localStorage`, keyed by notebook path**, survives a reload, needs no backend
change, and does not touch §6.6's read-only rule. Two edges worth handling: Save
As changes the path, so the memory does not follow it (acceptable — treat the
new path as a new notebook), and the upload path has no notebook path to key on,
so it falls back to the default tab.

**Mobile is out of scope** (decided). The breakpoint at `styles.css:303`
already collapses `.editor-layout` to one column and stacks the agent panel; the
rail is simply not designed for that width in V1.

---

## 6. Design principles the research implies

1. **Deterministic skeleton, AI labels.** Tier 0+1 build the structure; the
   model only *names and explains* it. This is exactly CodeBoarding's split
   (static analyzer → LLM interprets), and it means the panel still works with
   no `claude` CLI — which matters here, since this app already treats the CLI
   as required *only* for agent turns (README, "Claude CLI"). An overview that
   dies without it would break that promise.
2. **Every generated claim is anchored to cell ids.** A label that cannot be
   clicked back to the cells it came from is unverifiable, and unverifiable
   summaries of your own code are worse than none. This also gives a cheap
   hallucination check: reject any label whose cited cells do not exist.
3. **One level of detail at a time** (Observable's restraint; the CHI'26
   "multi-level navigation and detail-on-demand" finding). Stages by default,
   cells on expand, wires on focus.
4. **Cache on per-cell source hash, not on document revision.** The document
   revision (`expectedDocumentRevision`) is the right *wake-up signal* but the
   wrong *cache key*: `NotebookDocumentService` increments `_revision` on
   `set_execution_count` (`service.py:308`) as well as on source edits, so
   revision bumps every time a cell is **run**. Keyed on it, every run would
   discard and regenerate every label — the wrong behavior and the expensive
   one. Key each label on the hashes of its stage's member-cell sources; see
   §7. Stale Tier-2 labels should be visibly stale rather than silently wrong —
   the same posture `plot_tuning` takes toward stale knobs.
5. **Show state, not just structure.** Never-run, out-of-order, error-in-file,
   and risky cells are Tier 0/1 facts that no competitor's overview surfaces,
   and they are the first thing a third-party reader needs.
6. **Read-only by construction.** The overview must never mutate the notebook
   (unlike Themisto, which writes docs into cells). If the user wants the
   generated headings committed, that is a separate, explicit, diffable action —
   which in this app means an agent turn, reviewed as a diff.

---

## 7. Refresh, cost, and control

Three questions decide whether this is usable or annoying: what happens when the
notebook changes, what it costs, and whether the user can correct it.

### 7.1 What happens when the notebook changes

The three tiers get deliberately different refresh rules.

**Tier 0/1 — always live.** An AST parse of a few hundred cells is well under
100ms, so recompute on every change, debounced ~300ms after typing stops. Never
stale, never wrong. The push channel already exists: `/events`
(`api/event_routes.py`) is an SSE stream with a sequence cursor, so the panel
subscribes rather than polls.

**Tier 2 — never auto-regenerates.** Each stage's label is keyed on the hashes
of its member cells' *source*. That derives the behavior we want:

| Change | Effect |
|---|---|
| Run a cell | Revision bumps; no source hash changes. **Labels untouched**, state chips flip. |
| Edit cell 7 | Invalidates only the stage containing cell 7. Its label greys to dotted-stale; every other stage keeps its label. |
| Undo | Hashes return to a previous value → cache hit, free. |
| Trusted-mode structural turn | Boundaries recompute deterministically; labels follow their cells by id wherever stage membership is unchanged. |

The run-a-cell row is the important one, and the one a document-revision key
gets backwards (§6.4). It is also the most frequent event in the app.

Stale labels stay visible but visibly stale, with a refresh affordance. Never
silently wrong, never silently expensive.

### 7.2 Cost

Cost is real and user-visible here — agent turns shell out to the CLI on the
user's own subscription — so it cannot be hand-waved.

- **Opt-in.** Tier 2 never runs on notebook open. Opening gets the free
  skeleton; naming is a button.
- **Incremental.** Only dirty stages are re-labelled. A typical edit dirties one
  stage → one small call, not a full pass.
- **Content-addressed cache**, persisted per notebook path, keyed on
  `(member cell source hashes, prompt version)`. Survives close/reopen.
- **Input shaping — the largest lever.** Send source, not outputs. Charts are
  the strongest semantic signal a notebook has *and* the expensive,
  privacy-loaded one; excluding them by default is both cheaper and the safer
  default given outputs render in a `sandbox=""` iframe deliberately
  (`NotebookCell.tsx`). Long cells truncated head/tail; outputs reduced to type
  and shape.
- **One call per notebook**, structured JSON out — not one call per stage.
- **Model tier.** Naming is a Haiku job — **confirmed by probe** (§13.6), not
  just plausible. The composer already exposes model choice.
- **Ceiling.** Past N cells, segment deterministically and label only the
  largest stages, with "label the rest" as an explicit action.

### 7.3 User control over the flow

**Decided: V1 read-only, V2 editable.** Segmentation is subjective and two
passes will disagree, so the map must eventually be correctable — but editing is
V2, not the first build. V1 ships whatever the deterministic pass produces.

**V1 — blocks are contiguous cell ranges, not editable.** A block is a range,
which keeps rendering and click-to-jump trivial (jump = focus the first cell in
the range). The known cost: a notebook where you went back and added a cell to
an earlier block will show that cell in the wrong block. §9.5 tracks this.

**V2 — the user can adjust ranges and merge blocks.** Rename, merge two, split
one, drag a boundary. This is also when the sidecar-vs-metadata question below
has to be answered; V1 stores nothing, so it does not arise yet.

Two rules keep V2 coherent, recorded now so V1 does not paint them out:

1. **Edits are sticky, and become context.** A manual rename is never
   overwritten by a later pass; it becomes a pin, and is fed into subsequent
   passes as an example of the user's own vocabulary. Corrections improve later
   labels instead of being steamrolled by them.
2. **The map is a view, not the notebook.** Nodes may be reordered on a map;
   cells may not be reordered *through* one. Observable users asked for
   drag-to-reorder on their minimap (observablehq/feedback#40) — that is a
   different feature, and mutating execution order through a summary view is
   exactly the kind of thing this app makes explicit and reviewable everywhere
   else.

**Open decision, deferred to V2 — where do user edits live?** Notebook metadata
means they travel with the file when shared, but the overview then writes to the
notebook, breaking §6.6. A sidecar keyed on notebook path keeps it read-only but
does not travel — and introduces a file this app has never written, which is a
larger change than it looks. Lean: sidecar, plus an explicit **"write these as
markdown headings"** action routed through a normal agent turn, landing as a
reviewable, undoable diff.

---

## 8. Fit with this codebase

Real touchpoints, not hand-waving:

- `backend/app/plot_tuning/discovery.py` — transitive cross-cell AST chain
  analysis with an explicit conservative-rejection discipline. The DAG builder
  should be extracted from / modeled on this rather than written fresh.
- `backend/app/kernel_execution/risky_cell_classifier.py` — the risk chips,
  free.
- `backend/app/notebook_document/` — document + revision; the cache key.
- `backend/app/agent_workspace/` — `workspace_builder.py` already renders the
  notebook for a model, and `adapters.py` + Plan mode already give a
  **read-only, writes-nothing** turn shape. A Tier-2 overview pass is close to a
  Plan-mode turn with a fixed prompt and a structured (JSON) return, and should
  reuse the boundary rather than open a second path to the CLI.
- `frontend/src/plotTuning/TuningPanel.tsx`, `turnScope/TurnScopePanel.tsx` —
  the established side-panel patterns; `NotebookView.tsx` for scroll/highlight.
- **Compounding with existing features:** the overview is a natural *scope
  picker*. "Send this stage to the agent" → set the turn scope to that stage's
  cells; "focus this stage" → Focus pins. Selecting scope cell-by-cell is the
  clunkiest part of the current flow, and stages are the missing unit.

---

## 9. Assumptions this document makes

Surfaced deliberately: these were choices made while writing, not findings, and
each one could be wrong.

1. **"Roadmap" means a linear spine of blocks.** The user's word was "roadmap";
   it was read as a pipeline. It could equally have meant a milestone/progress
   view or a mind-map. §1's navigation framing supports the spine reading, but
   it was an interpretation.
2. **The block, not the cell, is the unit.** A per-cell minimap (concept B) is a
   coherent alternative that needs no segmentation at all — and segmentation is
   the hardest and least reliable part of the design.
3. **Document order is the display order.** The spine reads top-to-bottom in
   document order. §9.5 questions whether that stays honest when execution order
   contradicts it.
4. ~~**The panel is a third side panel.**~~ **Resolved** — a top-left toggle
   opens a left rail shared with the file tree; see §5.1.
5. **`plot_tuning/discovery.py` is reusable for the DAG.** Verified in part: it
   records dependency edges separately from knob candidacy (`discovery.py:475`),
   so its aggressive rejection logic applies to offering a control, not to the
   edge. The graph half looks genuinely reusable; that it generalizes beyond the
   plot-cell case it was written for is still an assumption.
6. **A single active notebook.** True today (the app keeps one in process
   memory), so no cross-notebook or multi-user concerns are considered.
7. **Segmentation quality is the make-or-break.** Everything downstream — names,
   jump targets, state rollup — inherits the boundaries. This is asserted, not
   measured, and it is the first thing a prototype should test.

---

## 10. Risks and open questions

1. **Stage segmentation is subjective.** Two models, two answers; the user
   disagrees with both. Mitigation in §7.3: segments are adjustable and manual
   edits are sticky. Headings, when present, win over inference.
2. **Where user edits to the map live** — notebook metadata (travels, but
   writes to the file) versus a sidecar (read-only, does not travel). Leaning
   sidecar; see §7.3. **Deferred to V2**, since V1 stores nothing.
3. **Truncation.** Big notebooks exceed a single pass; needs a chunking story
   (CodeWiki/Code2UML are the references for hierarchical context engineering).
   §7.2 sets a ceiling but not the chunking strategy.
4. ~~**Outputs in the prompt.**~~ **Closed** — outputs are out of scope
   entirely (§1). The cost and privacy arguments in §7.2 no longer need to be
   made; the loss is the error-in-file signal.
5. **Does the roadmap metaphor survive a genuinely non-linear notebook?**
   Sharpened by the V1 contiguity decision (§7.3): a block is a cell range, so a
   cell added later to an earlier block renders in the wrong place. If
   execution order contradicts document order, a linear spine may lie. The
   honest fallback is B (the DAG). Worth prototyping against a real messy
   notebook before committing.
6. **Scope creep into "clean my notebook"** (gather's territory). Deliberately
   out for a first slice: overview reads, it does not refactor.

---

## 11. First slice — decided

**Naming is in V1, and the map is built from code, not from prose.** The
ordering question this section previously left open is settled: Tier-2 naming
moves into the first build, paid for by dropping the DAG tab (concept B) out of
it. Naming is the differentiator (§3); the wires are the part prior art already
does well, and they are not what the panel is for.

### 11.1 One map, not two

Markdown sections and code structure are genuinely two different maps — the
author's declared narrative versus the actual computational structure. They can
disagree. The design question was whether to show both.

**Decided: one map, from the code. Markdown becomes annotation on it.**

Two maps would make the user choose which to look at *before* navigating — a
cost paid on every use for a benefit paid rarely. The economics are worse still
for the notebooks in scope: exploratory and simulation code is where markdown is
sparsest, so a markdown map would often be empty or three items long. And the
rail is 240px with a graph view already contemplated; a third view makes it a
menu rather than a map.

Instead, **markdown headings render as marks on the code spine** — ticks showing
where the author declared a section begins.

- Agreement: the mark sits on a block boundary. Quiet reassurance, no work.
- Divergence: a heading lands mid-block, or one block spans two headings. The
  disagreement becomes a *visual property* rather than a comparison task —
  comparing two lists is work; a misaligned tick on one spine is perception.

This is the intent-vs-reality signal §3 recorded as out of scope, arriving
through the navigation door instead of the comprehension one, at no extra cost.
It also degrades correctly: no markdown → no marks → the map is unaffected,
because it never depended on them.

### 11.2 What that makes the model responsible for

**Segmentation and naming in one pass, from code.** Markdown headings enter the
prompt as a *hint* — "the author marked sections here" — which the model may
follow or override. They are reference, not label.

**The rule that keeps the override safe:** a markdown heading is **never
silently dropped**. It is always at least a mark on the spine. Without this, a
user who wrote a heading and cannot find it in the panel reads the feature as
broken — and they would be right to. Nothing the author wrote disappears; it
just may not be the block's name.

### 11.3 The slice

- Contiguous blocks, segmented and named by the model from code (§11.2).
- Markdown headings as marks, never as the sole source of structure.
- A state chip per block (never-run / out-of-order / risky) — Tier 0/1, free.
- Click-to-jump through the existing `focusRequest` path.
- Left rail, Files | Outline tabs, per-notebook tab memory (§5.1).
- Read-only. No editing (V2), no outputs (§1), no DAG tab (deferred).

**Deterministic fallback still required** (§6.1): with no `claude` CLI
available, the panel falls back to headings-plus-milestone blocks rather than
disappearing. That fallback is now the degraded path, not the first release.

**What V1 is meant to answer:** does a named block map make a large notebook
navigable? Nothing cheaper answers it, which is why naming could not stay
deferred.

### 11.4 Functions as the second annotation layer

A notebook typically has a few functions that matter, and they are the strongest
anchor the file offers — for the reasons in §4: the notebook names them itself,
the names are domain-agnostic, and importance is computable from call counts.

**They do not become the primary anchor**, for two reasons.

*Coverage.* Measured against this repo's own examples, four of five notebooks
define **zero** functions and the fifth defines one, called once. Small demos, so
weak evidence — but it shows the variance that matters: exploratory and plotting
notebooks are often entirely top-level statements over a dataframe, while
simulation/research code is usually def-heavy. Functions are absent in roughly
the same notebooks markdown is absent from, so they cannot carry the map alone.

*The definition-site trap.* Definition site is not where the work happens.
Notebooks routinely define a helper near the top and call it fifteen cells
later. A map anchored on defs would jump the user to the definition when they
wanted the sweep that calls it — a navigation failure, which is the one kind
this panel cannot afford (§1).

**So they land as a second annotation layer, alongside markdown marks:**

- Each block lists the functions it defines; the important ones (by call count)
  surface in the collapsed view.
- Cross-references are **text, not wires** — "defined here, used in 5, 7, 9".
  This is deliberate: the graph view was cut from V1 (§11), and drawing call
  edges would reintroduce it through the side door.
- A `def` is a strong **segmentation hint** for the model — function boundaries
  are natural block boundaries.
- Never-called defs are flagged as dead scratch.
- Zero-def notebook → the layer is simply absent, the same graceful degradation
  markdown marks have.

---

## Sources

- Managing Messes in Computational Notebooks — https://dl.acm.org/doi/fullHtml/10.1145/3290605.3300500 · https://microsoft.github.io/gather/ · https://github.com/microsoft/gather
- Albireo — https://ieeexplore.ieee.org/document/8973385/
- Enhancing Comprehension and Navigation in Jupyter Notebooks — https://arxiv.org/pdf/2301.04419v2 · https://www.bodden.de/pubs/vwlb23enhancing.pdf
- "I Need to Find That One Chart" (CHI'26) — https://arxiv.org/abs/2603.00485
- Visualising data science workflows / third-party comprehension — https://link.springer.com/article/10.1007/s10664-023-10289-9
- How Scientists Use Jupyter Notebooks — https://arxiv.org/pdf/2503.12309
- InterLink — https://arxiv.org/html/2502.16114 · NotePlayer — https://dl.acm.org/doi/abs/10.1145/3654777.3676410
- marimo dataflow / minimap — https://docs.marimo.io/guides/editor_features/dataflow/ · https://marimo.io/blog/dataflow
- Observable minimap — https://observablehq.com/@observablehq/minimap · https://observablehq.com/@observablehq/introducing-visual-dataflow
- Hex 2.0 graph view — https://hex.tech/blog/hex-two-point-oh/
- Dataflow Notebooks (Koop, TaPP'17) — https://www.usenix.org/system/files/conference/tapp2017/tapp17_paper_koop.pdf · https://github.com/dataflownb/dfnotebook
- ipyflow / nbsafety — https://github.com/ipyflow/ipyflow · https://nbsafety.org/docs/index.html
- VS Code notebook outline asks — https://github.com/microsoft/vscode-jupyter/issues/1348 · https://github.com/microsoft/vscode-jupyter/issues/1349
- JupyterLab minimap request — https://github.com/jupyterlab/jupyterlab/issues/3738
- toc2 — https://jupyter-contrib-nbextensions.readthedocs.io/en/latest/nbextensions/toc2/README.html · Jupyter TOC — https://marketplace.visualstudio.com/items?itemName=xelad0m.jupyter-toc
- Themisto / notebook doc generation — https://arxiv.org/pdf/2104.01002 · https://www.ijcai.org/proceedings/2021/0717.pdf
- CodeBoarding — https://github.com/CodeBoarding/CodeBoarding · Swark — https://github.com/swark-io/swark
- Code2UML — https://arxiv.org/pdf/2605.24453 · CodeWiki — https://arxiv.org/pdf/2510.24428 · AI-Guided Exploration of Large-Scale Codebases — https://arxiv.org/pdf/2508.05799

---

## 12. Consolidated — what the panel captures

Written after eight rounds of scope decisions, and **authoritative over anything
earlier in this document that contradicts it.** Everything the panel shows is
one of the six fields below. Nothing else is captured.

### 12.1 The unit is a Block

```
Block
  range      cells [i..j], contiguous              computed
  name       a phrase describing what it does      generated
  state      ok | never-run | out-of-order | risky  computed
  produces   variables bound here, read later      computed
  defines    functions, with their call sites      computed
  marks      markdown headings falling inside      computed
```

That is the whole model. One generated field, five computed ones.

### 12.2 Two things resolved in this pass

**Variables are text, not wires.** A block says `produces: df, stores`. It does
not draw an edge. This keeps the rule set consistent — §11.4 already said
function cross-references render as text so the cut graph view could not return
through the side door, and variables are the same case. "What does this block
make" is the single most useful fact about it, and it survives as a label.

**The fixed stage taxonomy is cut** (was §4, Tier 2). Classifying blocks into
load / clean / explore / model / evaluate / export reproduces exactly the domain
problem that sank the milestone keyword lists: it fits ML pipelines and mislabels
simulation and exploratory work — two of the three notebook kinds in scope. The
generated name already says what the block does, in the notebook's own terms. A
category chip on top of it adds a second claim that is wrong often enough to
cost trust, and buys nothing navigation needs.

### 12.3 Three layers, three independent degradations

| Layer | Source | Absent when |
|---|---|---|
| Blocks (spine) | model, from code | never — always present |
| Markdown marks | headings in range | notebook has no headings |
| Function annotations | `def`s + call sites | notebook defines no functions |

Each layer disappears cleanly without affecting the others. The honest catch,
worth keeping in view: **markdown and functions go missing from the same
notebooks** — large exploratory analyses of the kind that motivated the feature.
There, the generated name carries the map alone. That is the case a prototype
must be pointed at (§9.7).

### 12.4 Provenance is visible

One generated field and five computed ones, and the interface must say which is
which — dotted underline on the name, plain text everywhere else (§6.2). Every
name cites the cells it came from, so it is checkable in one click. This is what
makes a wrong name a correctable annoyance rather than a reason to distrust the
panel.

### 12.5 Not captured, and why

| Not captured | Why |
|---|---|
| Outputs — charts, tables, saved errors | Out of scope (§1) |
| Dataflow wires / graph view | Cut from V1 (§11); text labels instead |
| Dead-end cells | Cleanup, not navigation (§1) |
| Intent vs. reality divergence | Comprehension, not navigation — but §11.1's marks surface it free |
| Block category / stage taxonomy | Cut in §12.2 |
| Cross-notebook or project-level views | One active notebook (§9.6) |

---

## 13. Probe results — the deterministic pass, measured

`docs/plans/probes/` holds two purpose-built notebooks and `extract.py`, which
computes the five *computed* Block fields (§12.1) with no model involved. Run:

```
python3 docs/plans/probes/extract.py docs/plans/probes/*.ipynb
```

The fixtures are the two cases the design keeps arguing about:
`messy-exploration.ipynb` (57 cells, **0 headings, 0 defs**, out-of-order
execution — the hard case where both annotation layers go dark) and
`simulation-sweep.ipynb` (21 cells, 1 heading, 7 defs — the def-heavy contrast).

### 13.1 The deterministic segmenter fails the hard case

| Notebook | Cells | Blocks | Largest block |
|---|---|---|---|
| messy-exploration | 57 | 5 | **cells 1–44** |
| simulation-sweep | 21 | 3 | cells 4–18 |

A 44-cell block is not a coarse label, it is **no navigation at all** — 79% of
the notebook in one node. §11 argued the no-model fallback would be "less
eloquent"; that was too generous. On a notebook with no headings and one
`read_csv`, milestone segmentation has almost nothing to fire on, and the whole
load → clean → merge → plot → re-clean → feature-engineer arc collapses into a
single node.

**This settles the §11 ordering question with evidence rather than judgement.**
Naming had to move into V1, and so does model-driven *segmentation* — the
boundaries are as generated as the names. The deterministic pass is a fallback
for when no CLI is available, and its output on the hard case should be
understood as "degraded", not "adequate".

### 13.2 Notebook-level kind detection is too coarse

The probe reports `kind=ml` for `messy-exploration`, because `sklearn` is
imported — **at cell 42**. Cells 1–41 are pandas exploration. §4 proposed
detecting notebook kind from imports as the way out of the per-domain keyword
problem; the probe shows kind is **regional, not global**. Imports arrive
mid-notebook, exactly where the work changes character. If import-family
detection is used at all it must be windowed, which is another argument for
letting the model draw the boundaries.

### 13.3 Two bugs found that would have shipped as design

Both were in the first version of the probe, and both are the kind of thing that
looks correct until run against real code:

1. **`produces` leaked function locals.** Walking the AST flat treats
   `S, I, R = y` inside a `def` as a module-level binding, so the block
   advertised producing `I` and `r0` — names no other cell can see. Binding
   collection has to be **scope-aware**: descend into function bodies for
   *reads*, not for *binds*.
2. **Dead-function detection false-positived on callbacks.** `rhs` is passed to
   `solve_ivp(rhs, ...)` as a value, never called by name, so a `ast.Call`-only
   scan reported `rhs() used in nowhere`. Any callback, key function, or
   decorator argument would have been flagged dead. Usage must count **name
   references**, not calls.

After both fixes, `sir()` correctly shows `used in [4, 14]` and the only
never-used definition is `sensitivity()`, which genuinely is a stub.

### 13.4 What the probe confirms

The **function annotation layer is as good as §11.4 hoped** where it applies —
`peak_of() used in [8, 17]`, `attack_rate() used in [12, 17]`, and a correctly
identified dead stub, all free from the AST. `produces` is likewise clean once
scoped. The computed half of the model is sound.

It is the **boundaries** that the deterministic pass cannot supply, which is
precisely the field everything else hangs off (§9.7).

### 13.5 Model segmentation, measured against the same notebook

`segment.py` sends cell source only (no outputs, §1), asks for a contiguous
partition with generated names, and validates the response the way the panel
would have to: every cell covered exactly once, indices in range, document
order. A model that cannot return a valid partition is unusable here, so the
validation is as much the experiment as the names are.

| | Deterministic (§13.1) | Model (default) | Model (Haiku) |
|---|---|---|---|
| Blocks | 5 | **17** | **17** |
| Largest block | **44 cells** | 7 cells | 6 cells |
| Median block | 3 | 3 | 3 |
| Valid partition | — | ✅ first try | ✅ first try |

The 44-cell lump becomes seventeen navigable blocks, and the names are specific
rather than categorical: *"Compute revenue and drop return transactions"*,
*"Remove outlier unit sales above 1000"*, *"Compare North vs South revenue
distributions"*. On the simulation notebook the model produced 8 blocks and
correctly labelled the stub cell *"Stubbed-out sensitivity analysis (not
implemented)"*.

**This is the §9.7 assumption discharged.** Segmentation quality was the
make-or-break, it was never measured, and it holds up on both fixtures.

### 13.6 Haiku is enough (§7.2 confirmed)

Haiku returned a structurally equivalent partition — same block count, same
median, largest block one cell smaller — differing only in where it placed two
boundaries (it folded the import cell into the first block; the default model
kept it separate, and both are defensible).

The one visible difference is register. Haiku reaches for categorical verbs —
*"Analyze weekly revenue trends"*, *"Visualize monthly patterns by region"* —
where the default model says *"Weekly revenue trend"* and *"Monthly revenue
heatmap by region"*. Since §12.2 cut the stage taxonomy precisely to avoid
category labels, that drift is worth watching, but it is a prompt problem, not a
model-tier problem.

### 13.7 Two corrections this produced

**`produces` needs a relevance filter, not just correctness.** On the simulation
notebook a block reports `produces beta, ip, peaks, r0, results, s`. Every one of
those is genuinely module-level — Python loop targets leak to global scope, so
`for r0 in R0_grid:` really does bind `r0` — but half of them are throwaways and
the useful answer is `results, peaks`. Correct and useless is still useless.
Filter names bound only as loop or comprehension targets, rank the rest by how
many later blocks read them, and cap the display at three or four.

**The cost estimate in §7.2 was an order of magnitude pessimistic.** The 57-cell
notebook renders to roughly 1,200 tokens of prompt, and the 21-cell one to
roughly 800 — about 22 tokens per cell, not the 150–300 assumed. Extrapolating,
a 200-cell notebook is ~4.5k tokens, not 30–60k. Cells in real exploratory
notebooks are mostly one-liners. This substantially weakens the case for a size
ceiling: at these sizes, a full re-segmentation is cheap enough that
incrementality is a nicety rather than a necessity.

### 13.8 Caching, resolved — §7.1 was over-engineered

Two probe findings collapse the per-block hashing scheme in §7.1.

**Cost.** At ~22 tokens per cell, a full re-segmentation of a 200-cell notebook
is ~4.5k tokens. Incrementality was justified by a cost that is not there.

**Coherence, which is the real argument.** Per-block invalidation assumed
*deterministic* boundaries — you could re-label block 2 because block 2's extent
was a fixed fact. Now that segmentation is model-generated (§13.1), an edit may
move a boundary, so there is no stable "block 2" to invalidate. The unit of
caching is necessarily the whole notebook.

**Resolved design:** one cache entry per notebook, keyed on a hash of all cell
sources concatenated. Source unchanged → cached map stands, so **running cells
never invalidates it**. Source changed → the whole map is marked stale, stays
visible, and regenerates only when the user asks. Nothing automatic, ever.

Note this keeps §6.4's substance — revision is the wake-up signal, not the cache
key, because `_revision` bumps on `set_execution_count` — while dropping the
per-block machinery built on top of it.

### 13.9 Fixes applied to the probes

Both findings in §13.7 are now fixed in `docs/plans/probes/`, with the reasoning
recorded at the call sites so the port to `notebook_overview/analysis.py` keeps
it:

- `produces()` drops names bound only as loop targets and ranks the rest by how
  many later cells read them, capped at four. A third scope bug surfaced while
  testing the fix — **comprehension targets are separately scoped in Python 3**,
  so `{r0: f(s) for r0, s in ...}` binds nothing at module level, and omitting
  comprehensions from the nested-scope set re-admitted exactly the loop counters
  the filter existed to remove.
- The segmentation prompt now bans opening a name with *Analyze / Explore /
  Visualize / Process / Handle / Perform / Compute* and asks for the subject
  rather than the activity. Re-running Haiku afterwards: *"Revenue by region"*,
  *"Monthly revenue heatmap by region"*, *"Unit outlier filtering"* — the
  categorical register is gone.

One observation worth carrying forward: the tightened prompt returned **14
blocks where the previous revision returned 17** for the same notebook, largest
9 vs 6. Both are usable maps. But it shows block granularity is sensitive to
prompt wording, so if stability across regenerations matters, it needs pinning
down deliberately (spec §10.4).
