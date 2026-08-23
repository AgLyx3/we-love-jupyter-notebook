# Competitive Landscape

Last reviewed: 2026-07-26. Sources are linked inline; this space moves fast, so
re-verify before relying on any specific claim.

## Our position

This project is a **local, notebook-first `.ipynb` editor with a backend-enforced
agent permission boundary**. The differentiators, in the order they matter:

1. **Local-first.** Your notebook and data never leave the machine.
2. **Turn-scoped, cell-scoped write permission.** An agent may only write to the
   cells in the current turn's editable set.
3. **The agent does not own mutation.** The CLI agent produces candidate cell
   sources; the Notebook Document domain applies or rejects them, and backend
   validation is authoritative.

See `docs/notebook-agent-editor-spec.md` for the full model.

Nearly every competitor below is **cloud-hosted** and **grants the agent broad,
unscoped authority over the whole notebook** — including creating, deleting, and
restructuring cells. None of them documents a per-turn, per-cell permission
boundary. That gap is our wedge.

---

## Hex — Notebook Agent

Cloud, collaborative analytics platform for data teams; the closest "agentic
notebook" incumbent.

- Notebook combines SQL, Python, no-code, and AI in one multiplayer workspace.
- The Notebook Agent takes natural-language requests and generates the cells to
  satisfy them: Python, SQL, Markdown, Pivot, Input parameter, Single value, and
  Chart cells.
- Positioned around iterating on complex analysis end-to-end ("ask a question,
  get a complete analysis, keep going").

**Overlap:** agentic editing inside a notebook surface — the same core job.
**Divergence:** cloud/multiplayer, team-analytics oriented, and the agent freely
authors and restructures cells. No local mode, no scoped write boundary.

Sources: [Hex AI capability](https://hex.tech/capability/ai/) ·
[Introducing the Notebook Agent](https://hex.tech/blog/introducing-notebook-agent/) ·
[Notebook agent docs](https://learn.hex.tech/docs/explore-data/notebook-view/notebook-agent)

---

## Deepnote — Deepnote Agent (+ open-source local runtime)

**The most direct threat**, because Deepnote is moving toward local.

- **Deepnote Agent** (Beta on Pro/Team/Enterprise) is a unified chat that treats
  the notebook as a canvas. It can **create, edit, and remove SQL, Python, or
  text blocks anywhere in the notebook**, with full project context, across
  multi-step agentic workflows — not single-cell completions.
- Earlier "auto notebook" capability materializes an entire notebook (code, SQL,
  text) from a prompt.
- **Open-source local runtime** (Apache-2.0): a self-described "drop-in
  replacement for Jupyter," with extensions for **VS Code, Cursor, and
  Windsurf**. Converts Jupyter notebooks to a `.deepnote` **YAML** format,
  arguing human-readable YAML beats `.ipynb` JSON. Adds block-based architecture
  and reactive execution.

**Why this matters to us:** the local open-source piece attacks our local-first
advantage directly. Two mitigating facts as of this review:

- The **AI agent is cloud-only**. A *local* AI agent and bring-your-own-keys are
  on the **roadmap, not shipped**. Today, local Deepnote = editing/conversion;
  agentic editing requires the cloud.
- **No permission or safety model for AI editing is documented**, locally or in
  the cloud. The agent's authority to add/remove blocks anywhere is the opposite
  of our scoped boundary.

Also note the format divergence: Deepnote's answer to "`.ipynb` JSON is bad for
agents" is *replace the format with YAML*. Ours is *keep `.ipynb` and extract
per-cell plain text into an isolated workspace*. Ours preserves ecosystem
compatibility; theirs requires conversion.

**Watch item:** if Deepnote ships a local agent with BYO keys, the overlap
becomes near-total except for our permission model. That makes the boundary
guarantees — not "local" alone — the durable differentiator.

Sources: [Deepnote Agent docs](https://deepnote.com/docs/deepnote-agent) ·
[Deepnote AI](https://deepnote.com/ai) ·
[deepnote/deepnote on GitHub](https://github.com/deepnote/deepnote)

---

## Google Colab — Data Science Agent

Free/consumer-scale distribution; the widest reach of anything here.

- Gemini-powered agent that removes setup toil — imports, data loading,
  boilerplate — and generates working notebooks from a natural-language
  description.
- The **next-generation agent runs autonomous analytical workflows**: it creates
  a plan, executes code, reasons about results, and presents findings, with the
  user able to give feedback mid-flight. Reimagined AI-first Colab is GA, with an
  agentic collaborator on Gemini 2.5 Flash.
- **Custom Instructions** and **Learn Mode** give users some control over agent
  behavior and pedagogy.
- Available to users 18+ in select countries/languages; expanding university
  partnerships.

**Overlap:** notebook-native agentic editing and execution.
**Divergence:** fully cloud, Google-account bound, tied to Colab's runtime. Custom
Instructions are *soft steering* (prompt-level), not an *enforced* write
boundary — an important distinction to make in any comparison we publish.

Sources: [Data Science Agent in Colab](https://developers.googleblog.com/en/data-science-agent-in-colab-with-gemini/) ·
[AI-first Colab now available to everyone](https://developers.googleblog.com/new-ai-first-google-colab-now-available-to-everyone/) ·
[Customize your Gemini agent in Colab](https://blog.google/innovation-and-ai/technology/developers-tools/colab-updates/) ·
[Colab Enterprise docs](https://docs.cloud.google.com/colab/docs/use-data-science-agent)

---

## Comparison

| | Hex | Deepnote | Colab | This project |
|---|---|---|---|---|
| Deployment | Cloud | Cloud (+ OSS local editing) | Cloud | **Local** |
| Agentic cell editing | Yes | Yes (cloud only) | Yes | Yes |
| Agent may add/delete/restructure cells | Yes | Yes | Yes | **No** |
| Per-turn, per-cell write scope | No | No | No | **Yes** |
| Mutation applied by a validating backend | Not documented | Not documented | Not documented | **Yes** |
| Native `.ipynb` | Yes | Converts to `.deepnote` YAML | Yes | **Yes** |
| Data leaves the machine | Yes | Yes (for agent) | Yes | **No** |

---

## Interactive plot tuning — a different competitive set

Added 2026-08-22, after the plot tuning feature shipped (PR #17). **Not
source-verified in this pass** — the claims below come from general knowledge of
these tools, not a live review. Re-check before publishing any of it.

Worth separating from the sections above, because "drag a variable and see the
plot change" competes with a different set of tools than "an agent edits my
notebook safely". Plenty of things do interactive plots. Almost nothing writes
the value back into your source.

### The notebook incumbents already parameterize

Both of the closest competitors ship parameter widgets today:

- **Hex — Input parameter cells.** The Notebook Agent even authors them (already
  noted in the Hex section above). Sliders, dropdowns and inputs bound to
  variables that downstream cells read.
- **Deepnote — input blocks.** Same idea in block form.

**Overlap:** the end-user gesture is identical — move a control, see the analysis
update.
**Divergence, and it is the whole point:** those widgets *are* the notebook.
Adding one means authoring a parameter cell, and the value lives in the widget's
state, not in your code. Ours goes the other way — it reads literals that are
**already in your source**, requires no authoring step, and Apply **rewrites the
literal in place**. You end up with an ordinary notebook containing
`BINS = 12`, not a notebook containing a widget.

### marimo — reactive by construction

Listed under "adjacent" above, but for *this* feature it is the closest analog in
the whole landscape. marimo re-runs dependent cells automatically when a value
changes; that is the same job the tuning panel does.

**Divergence:**
- marimo re-runs your **live** kernel. The governing rule here is the opposite —
  playing is free, only confirming costs — which is why previews run in a shadow
  kernel and the live one is untouched until Apply.
- marimo requires `.py`. Same compatibility trade as the `.deepnote` YAML point
  above.

### ipywidgets `@interact` — the in-notebook classic

The obvious "I already have this" objection, and the honest answer is that it
does less:

- **No write-back.** The value dies with the kernel session; your source still
  says `bins=30`.
- **Needs JS in outputs.** Our output iframes are `sandbox=""` by deliberate
  security decision, which is precisely why previews are server-rendered images
  rather than live widgets. Same reason Plotly figures render but are inert.
- Requires wrapping your plotting code in a decorated function.

### App builders — adjacent, different job

**Panel / Voilà / Streamlit / Gradio** turn a notebook or script into a
parameterized app. They compete for "let someone else explore this analysis",
not for "help me find the right value while I am writing it". They also do not
edit your notebook.

### Comparison

| | Hex / Deepnote inputs | marimo | ipywidgets | This project |
|---|---|---|---|---|
| Change a value, see the plot update | Yes | Yes | Yes | Yes |
| Works on literals already in your code | No — author a widget | Yes | No — wrap in a function | **Yes** |
| Writes the chosen value back to source | No | No | No | **Yes** |
| Live kernel untouched while exploring | No | No | No | **Yes** |
| Reviewable / undoable like any edit | n/a | n/a | n/a | **Yes** |
| Native `.ipynb` | Hex yes / Deepnote no | No (`.py`) | Yes | **Yes** |

### What this means for positioning

The feature is **not** "interactive plots" — that is table stakes and several
competitors have it. Two things are unoccupied:

1. **Write-back.** Turning a knob produces a source edit, reviewed and undone
   through the same per-hunk ledger an agent turn uses. Nothing above edits your
   code from the widget.
2. **Exploration that costs nothing.** A shadow kernel means dragging never
   touches your session — no lost state, no half-mutated dataframes, no
   re-running an expensive load because you guessed wrong.

Both are consequences of the same architecture as the permission model: mutation
is applied by a validating backend, never by the thing proposing it. That is
worth saying explicitly — it is one wedge, not two.

---

## Adjacent / worth monitoring

Not researched in this pass — listed so the next review has a starting point.

- **JupyterLab-native AI** — `jupyter-ai` (official Project Jupyter), **Mito AI**.
  Architecturally our closest neighbors: local, open-source, agent layered onto
  Jupyter. Neither emphasizes a scoped permission boundary.
- **marimo** — open-source reactive Python notebook stored as `.py`; sidesteps
  `.ipynb` JSON entirely. Same "escape the JSON" instinct as Deepnote's YAML.
- **Platform incumbents** — Databricks (Assistant, Genie; acquired Einblick),
  Microsoft Fabric / Data Wrangler.
- **Chat-first analysts** — Julius AI, ChatGPT Advanced Data Analysis, Claude
  analysis tool. Compete for the *job* ("analyze my data"), not the *form factor*
  (they don't edit your `.ipynb`).
- **Code-agent forks** — Cursor, Windsurf, Positron (Posit). General-purpose, but
  they edit notebooks via notebook-aware tooling and now host Deepnote's
  extension.

---

## Takeaways

1. **"Local" alone is not a moat.** Deepnote is already local for editing and has
   a local agent on the roadmap. Lead with the **permission model**.
2. **No competitor documents an enforced write boundary.** Turn-scoped,
   cell-scoped, backend-authoritative mutation is genuinely unoccupied ground —
   and it is the thing that is hard to retrofit onto a "let the agent rewrite the
   notebook" architecture.
3. **Everyone else is expanding agent authority** (create/delete/restructure
   anywhere, autonomous multi-step plans). We are deliberately constraining it.
   Frame that as trust and reviewability, not as a missing feature.
4. **Staying native to `.ipynb`** is a compatibility advantage over format
   replacements (`.deepnote` YAML, marimo `.py`).
5. **Plot tuning is the permission model applied to a second surface.** The
   differentiator is not the slider — it is that the slider produces a
   *reviewable source edit* and that exploring costs nothing. Pitch it as one
   architectural wedge with two visible consequences, not as a separate feature.
