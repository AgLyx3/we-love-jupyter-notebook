# Notebook Overview ("Roadmap") — Prior-Art Research

Status: research only, nothing designed or built
Branch: `claude/notebook-overview-research-qbb1tw`
Date: 2026-08-22

Read alongside `docs/notebook-agent-editor-spec.md` (product/architecture
authority) and `docs/engineering-handoff.md` (implemented state). This document
answers one question: **has anyone built "an overview of the notebook that looks
like a roadmap", and what did they learn?** It ends with the gap worth building
into and a recommended first slice — but it does not commit to a design.

---

## 1. The user and the moment

The overview is not for the person who just wrote the notebook. It is for the
person who **opens a notebook they did not write** (or wrote three weeks ago)
and needs to answer, in under a minute:

1. What is this notebook *for*? What does it produce?
2. What are its stages, in order?
3. Which cells matter, and which are scratch/dead ends?
4. What state is it in — what has run, what is stale, what will break?
5. Where is the one chart / the model / the number I came for?

Every prior-art cluster below answers **one** of these and ignores the rest.
That is the gap.

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

Nobody has combined the four signals that a notebook actually carries:

|  | narrative (md) | dataflow (vars) | artifacts (outputs) | state (exec) |
|---|---|---|---|---|
| TOC / Outline | ✅ | ❌ | ❌ | ❌ |
| marimo / Hex / Observable minimap | partial | ✅ | ❌ | partial |
| ipyflow | ❌ | ✅ | ❌ | ✅ (stale) |
| gather | ❌ | ✅ | ✅ (as target) | ✅ (log) |
| Themisto / Cell2Doc | ✅ (generated) | ❌ | ❌ | ❌ |
| CodeBoarding / Swark | ✅ (generated) | ✅ (module-level) | ❌ | ❌ |
| Albireo | partial | ✅ | partial | partial |

**The unclaimed square: a single overview that segments the notebook into named
stages, says what each stage produces, shows what flows between them, indexes
the artifacts, and marks what is stale or never-run — for a plain `.ipynb` on a
normal IPython kernel.**

Two further things nobody does, both of which need a model:

- **Meaning without markdown.** Every structural tool is parasitic on headings
  the author already wrote. The notebooks that most need an overview are exactly
  the ones with no headings. An LLM segmenting and naming stages from code +
  outputs inverts that dependency.
- **Intent vs. reality.** The markdown says "train the model"; the code
  `.fit()`s on the *uncleaned* frame. Only a model that reads both the prose and
  the code can flag the divergence. This is a class of finding no existing
  notebook tool produces.

---

## 4. Signal inventory — what we can actually compute

Ordered by cost. The design principle this implies is in §6.

**Tier 0 — free, from the `.ipynb` alone. No kernel, no AI.**

- Markdown headings and their level → the skeleton, when it exists.
- `execution_count`: `null` → never run; non-monotonic across cells → the
  notebook was not run top-to-bottom, so linear reading is a lie.
- Output types per cell: `image/png` → a chart; `text/html` table → a frame
  preview; `error` output → a broken cell sitting in the file.
- Cell magics, `!` escapes, imports → environment/setup cells.

**Tier 1 — static AST. Deterministic, offline, cheap.**

- **Defs/uses per cell → the cell DAG.** `plot_tuning/discovery.py` already
  walks transitive assignment chains across cells conservatively (see its
  module docstring). That is most of a dependency graph already, built and
  tested, in this repo.
- Function/class definitions → the reusable spine.
- "Milestone" call detection: `read_csv`/`read_parquet`/`open` (ingest),
  `.fit(` / `train` (model), `.score`/`accuracy_score` (evaluate),
  `to_csv`/`joblib.dump`/`torch.save` (export).
- Risk/side-effect classification: **already implemented** in
  `kernel_execution/risky_cell_classifier.py` (shell escapes, package changes,
  DB writes, credential access, file deletes, process execution).
- Dead-end detection: cells whose bindings nothing downstream reads (the
  complement of a gather slice).

**Tier 2 — model, one scoped pass over the notebook.**

- Segment into stages and **name them** (works with zero markdown).
- Classify each stage: load / clean / explore / feature / model / evaluate /
  export / scratch. (The pipeline-stage taxonomy is well established in the
  notebook-comprehension literature.)
- One-line "what this does, in the notebook's own vocabulary" per stage.
- "What is this notebook for" — the single sentence at the top.
- Intent/reality divergence, as above.
- What the notebook *produces* — the deliverables list, in user terms.

---

## 5. Product concepts considered

**A. Roadmap rail (recommended).** A vertical spine beside the notebook: one
node per *stage*, not per cell. Each node carries a generated name, a stage
badge, the artifacts it produced (chart thumbnail, metric, file written), and a
state chip (never-run / stale / error / risky). Click → scroll to the first cell
of the stage; hover → the stage's cells highlight in the gutter. Reads as a
roadmap because the analysis genuinely is a pipeline; it degrades to a plain TOC
if the model is unavailable.

**B. Minimap / DAG.** marimo/Observable-style wires. Highest information
density, and the most honest picture of a non-linear notebook — but it is the
one thing prior art *does* do well, and it answers "what depends on what", not
"what is this notebook".

**C. Generated notebook README / architecture card.** One AI-written page:
purpose, inputs, stages, outputs, caveats. Cheap, very legible, but static prose
— not navigation, and it rots on the next edit.

**D. Artifact index.** "Find that one chart": every output as a thumbnail grid
with generated captions, filterable. Directly serves the CHI'26 finding. Narrow
but unusually well-evidenced, and a natural *lane* inside A rather than a rival
to it.

**Recommendation: A, with D as a lane inside each node, and B available as a
secondary tab** — the same two-tab shape marimo converged on (map first, graph
second), which is weak evidence it is the right decomposition.

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
4. **Cache on document revision; invalidate on edit.** The document already
   carries a revision (`expectedDocumentRevision` throughout the API). Stale
   Tier-2 labels should be visibly stale rather than silently wrong — the same
   posture `plot_tuning` takes toward stale knobs.
5. **Show state, not just structure.** Never-run, out-of-order, error-in-file,
   and risky cells are Tier 0/1 facts that no competitor's overview surfaces,
   and they are the first thing a third-party reader needs.
6. **Read-only by construction.** The overview must never mutate the notebook
   (unlike Themisto, which writes docs into cells). If the user wants the
   generated headings committed, that is a separate, explicit, diffable action —
   which in this app means an agent turn, reviewed as a diff.

---

## 7. Fit with this codebase

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

## 8. Risks and open questions

1. **Stage segmentation is subjective.** Two models, two answers; the user
   disagrees with both. Mitigation: segments must be adjustable (merge/split),
   and headings, when present, should win over inference.
2. **Cost and latency of Tier 2** on a 200-cell notebook, and whether it runs on
   open, on demand, or incrementally per revision. Undecided.
3. **Truncation.** Big notebooks exceed a single pass; needs a chunking story
   (CodeWiki/Code2UML are the references for hierarchical context engineering).
4. **Outputs in the prompt.** Charts are the strongest semantic signal a
   notebook has, and this app renders outputs in a `sandbox=""` iframe for good
   reason (`NotebookCell.tsx`). Whether images are sent to the model at all is a
   privacy/cost decision, not a technical one.
5. **Does the roadmap metaphor survive a genuinely non-linear notebook?** If
   execution order contradicts document order, a linear spine may lie. The
   honest fallback is B (the DAG). Worth prototyping against a real messy
   notebook before committing.
6. **Scope creep into "clean my notebook"** (gather's territory). Deliberately
   out for a first slice: overview reads, it does not refactor.

---

## 9. Suggested first slice

A **Tier 0+1 only, zero-AI overview panel**: sections from markdown headings
where they exist, otherwise segmented at milestone calls; per-section artifact
chips (chart / table / error), state chips (never-run / out-of-order / risky),
and click-to-scroll. Ship that, use it on real notebooks, and *then* add the
Tier-2 naming pass on top of a structure that is already proven useful — because
if the deterministic skeleton is not useful, no amount of generated prose on top
of it will be.

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
