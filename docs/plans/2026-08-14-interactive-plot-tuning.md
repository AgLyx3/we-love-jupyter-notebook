# Interactive Plot Tuning

Status: design agreed, not built
Branch: `interactive-plot-knobs`
Date: 2026-08-14

Read alongside `docs/notebook-agent-editor-spec.md` (product/architecture
authority) and `docs/engineering-handoff.md` (implemented state).

---

## 1. Understanding Summary

**What is being built.** A tuning panel attached to a plot cell's output. It
scans the cell for the upstream variables its plot depends on, offers those as
typed controls, and re-renders a **real** plot per value you try — without
touching your live kernel or your notebook. A single Apply then writes the
chosen values back into the source literals and re-runs the live chain for real.

**Why it exists.** Notebook plots are frozen at whatever value was last
executed. Exploring "what does this look like at 50 bins / 0.3 threshold / 5000
samples" today means edit → run → look → edit again, destroying kernel state and
notebook outputs on every guess. The panel makes exploration free and
reversible, and makes committing the one deliberate, expensive act.

**Who it is for.** A single local user, on their own notebook, in one panel at a
time.

**The governing rule (user's words).** *"The upstream doesn't change while
you're just dragging for a different value — it only changes when you decide on
one value and confirm."* Playing is free. Confirming is what costs.

**Key constraints discovered in the codebase.**

- Output iframes are `sandbox=""` (`frontend/src/notebook/NotebookCell.tsx:110`).
  No JS runs in outputs. ipywidgets, Plotly interactivity, and Bokeh are
  therefore not available without weakening a deliberate security boundary. This
  is why the preview is server-rendered images.
- All kernel execution today is **cell-bound and committing**
  (`/execution/cells/{id}/run` → `KernelExecutionService.start_cell`), takes a
  mutation lease, and writes outputs into the document. Non-committing preview
  execution does not exist and is the main new backend concept.
- The operation ledger has two layers. `agent_turns/operations.py` is fully
  generic — `TurnOperation` is `(id, cell_id, ordinal, hunk, kind, state)` and
  `compose()`/`with_state()`/`is_stale()` are pure functions with no agent
  concepts. `AgentTurn` (`agent_turns/service.py:101`) is very agent-shaped:
  `prompt`, `model`, `mode`, `write_scope`, `attempts`, `final_output`,
  `cancel_event`.
- `RiskyCellClassifier` already detects `network_client`, `file_write`,
  `database_write`, `shell_escape`, `credential_access`, `process_execution`,
  and `RiskyExecutionDialog.tsx` is already the UI for approving them.

**Non-goals.**

- ipywidgets, Plotly/Bokeh interactivity, or any JS in outputs. Plotly in
  particular emits `application/vnd.plotly.v1+json`, which the output renderer
  has no branch for, so it falls through to the text fallback — and since the
  Tune button is gated on an image output, a Plotly cell never offers tuning at
  all. Its arguments *are* discovered correctly; it is the render path that
  stops. Pinned in `test_plot_tuning_render.py`.
- Pre-computed value sweeps and instant scrubbing. This is a later caching
  policy on the same machinery, not a different design (see D3).
- Forking the live kernel process.
- Knobs that are not AST-resolvable literal assignments — `params['bins']`,
  computed values, inline call kwargs.
- Any agent involvement. This feature contains no agent turn.
- Windows (already out of scope repo-wide).

---

## 2. Assumptions

Agreed as reasonable defaults; each is a decision that can be revisited.

- **Performance.** No latency SLA. Preview cost is one upstream chain run, shown
  with progress and cancellable. Warm-up cost is a full upstream replay, paid
  once per panel session.
- **Memory.** The shadow kernel duplicates resident data. One shadow at a time,
  torn down on panel close, notebook close, and ~10 min idle.
- **Reliability.** A shadow crash must never reach the live kernel or the
  document. Process isolation makes this structural, not merely intended.
- **Determinism.** If anything upstream uses unseeded randomness, the shadow's
  plot will not reproduce pixel-for-pixel after Apply. The promise is "real code,
  real values," not pixel identity. Worth surfacing in the UI copy eventually.
- **Security.** The shadow runs the same notebook code at the same trust level on
  loopback. No new privilege surface. It does double *side-effect* exposure,
  which the warm-up prompt (D5) is what mitigates.
- **Product invariant preserved.** Agent integration still does not own notebook
  mutation. This is a user-driven edit path with no agent in it; mutation still
  goes through the Notebook Document domain.

---

## 3. Design

### 3.1 Preview execution — the one genuinely new concept

New backend module `backend/app/plot_tuning/`, owning a `ShadowSession`.
Everything it does is the inverse of today's execution path: **execute, return
outputs, commit nothing, touch no document.**

**Warm-up.** On panel open for cell *C*: resolve the upstream chain (§3.2),
classify every cell in it with `RiskyCellClassifier`, and return the flagged set
to the client *before starting anything*. Client confirms via
`RiskyExecutionDialog`. Only then spawn a second `KernelSession` and replay the
chain from the top.

**Preview.** `POST /tuning/{shadow_id}/preview` with a `{name: value}` map. The
server rewrites the literals in its own in-memory copy of the chain source,
executes from the earliest rewritten cell through *C*, and returns only *C*'s
outputs. Nothing is persisted. The live document is never opened for mutation,
so no lease is taken and the live kernel is never blocked.

**Cache.** Keyed on the full knob-value tuple. Revisiting a value is instant.
This is what makes lazy previewing dominate a pre-computed sweep (D3).

**Lifecycle.** One shadow at a time. Dies on panel close, notebook close, ~10
min idle, and on any document revision change (it replayed source that no longer
exists). It **survives a live-kernel restart** — having replayed from the top, it
does not depend on live state. See §3.6 for what a restart *does* invalidate.

`plot_tuning` reads the notebook document and never mutates it. Only Apply
mutates, through the existing domain.

### 3.2 Discovery

**The scan is transitive.** The motivating case is `n_samples = 1000` →
`df = generate(n_samples)` → plot cell uses `df`. The plot cell never mentions
`n_samples`, so a direct-read scan misses precisely the knob that matters.

Algorithm: collect names *C* reads; resolve each to its binding in an upstream
cell; collect the names that binding's RHS reads; recurse. Any name in the
closure bound to a literal becomes a knob. The re-run chain is "earliest cell
holding a changed knob → *C*".

**Three refinements found by running the scan against a real notebook**
(`examples/ml-pipeline.ipynb`). The first version found 1 of its 4
hyperparameters; all three of these were needed to reach 4 of 4.

1. **The closure must follow in-place mutation, not just binding.** The ordinary
   imperative training loop — `for _ in range(EPOCHS): weights[i] -=
   LEARNING_RATE * error` — never *rebinds* `weights`, so following assignment
   right-hand-sides alone never learns that `EPOCHS` and `LEARNING_RATE` decide
   it. A subscript or attribute write is a dependency edge from its base name.
2. **Rejecting a name must not sever the graph.** `train, test =
   scaled[:n], scaled[n:]` is unrewritable and correctly rejected, but dropping
   it from the graph hides every literal reachable *through* it. Rejected names
   still carry their dependencies.
3. **Names bound to a function, class or lambda are suppressed entirely**, not
   rejected. "`sigmoid` is computed rather than set to a fixed value" is true,
   useless, and buries the rejections describing something actually tunable.
   This cut the real notebook's rejection list from 25 to 17.

**Rejection rules — the correctness boundary.** A name is *not* a knob if it is:

- bound more than once before *C*
- bound by augmented assignment (`x += 1`)
- bound by tuple unpacking (`a, b = 1, 2`) or chained assignment
- a loop, comprehension, walrus or `with ... as` target
- bound anywhere below the top level of a cell (inside `if`/`for`/`with`/`try`)
- reassigned inside a function via an explicit `global`

That last rule replaces the original "bound inside a function or class body",
which was too coarse: a name assigned inside a `def` is *local* and cannot
affect the module-level name of the same spelling. Rejecting on it would kill a
good global knob because some helper happened to reuse the name. Function and
class bodies are therefore opaque to the scan — for bindings, for mutation
edges, and for rejection reporting — except for `global` declarations, which do
reach out.

Each means rewriting one literal does not determine the runtime value. Silently
offering a knob that does not control what it claims is this feature's worst
possible failure, so these are enforced, tested individually, and *explained* in
the UI rather than causing a silent omission.

Discovery and write-back are the same problem: a value is offerable as a knob
exactly when its AST source range (`col_offset`/`end_col_offset`) lets us
rewrite it confidently. The boundary is not a v1 shortcut.

### 3.3 Controls and bounds

| Literal type | Control |
|---|---|
| `int` | slider, integer step, plus numeric input |
| `float` | slider, step = range/100, plus numeric input |
| `bool` | toggle |
| `str` | text input — no dropdown, we have no vocabulary to populate one |
| tuple/list of numbers | one numeric input per element, no slider |
| anything else | not a knob |

**Bounds.** Default `[v/5, v*5]`, so `bins=30` → 6–150. Negative values flip to
`[5v, v/5]` to keep min < max. Zero has no scale, so it falls back to `[0, 10]`
for ints and `[0, 1]` for floats.

Both bounds are directly editable, and typing a value outside them **widens the
range rather than clamping**. The heuristic is a starting point, not a cage:
`alpha=0.5` will suggest 0.1–2.5, which is wrong for alpha, and nothing in a
type-only scan can know that. One keystroke fixes it.

### 3.4 Apply and write-back

**Flow.** Apply sends the changed knob set. The backend rewrites each literal at
its AST source range, diffs old→new with the existing `diff_hunks` /
`build_operations`, applies all affected cells **atomically** through the
notebook document domain guarded by `expectedDocumentRevision`, then starts live
downstream execution from the earliest changed cell.

Undo is the existing move — flip operation state to `rejected` and recompose
from pre-edit source. No inverse patches, no new undo semantics.

**Record ownership.** A separate small `TuningService` reusing the generic
pieces: `operations.py` primitives, the document apply path, the downstream
execution path. It gets its own thin routes. See D6 for why not `AgentTurn`.

**Labelling.** The frontend gets an `origin` discriminator so the review bar at
`NotebookCell.tsx:228` renders **"You tuned this cell"**, with the same per-hunk
Keep/Undo, rather than "Agent changed this cell."

### 3.5 UI

**Entry point.** Not the `cell-actions` cluster — that file's own comment
(`NotebookCell.tsx:223-226`) records that the revert control there was
"effectively undiscoverable" because the cluster is hover-revealed, unlabelled,
and shared with scope/run actions. Reuse the better precedent in the same file:
`output-add-chat` (line 28), a labelled button overlaid on an output. Show a
**"Tune"** button on the output only when the cell has an image output *and* the
scan found knobs.

**Layout.** The panel opens inside the output region, below the editor. Knobs
stacked under a tall plot would mean scrolling between cause and effect, so:
**preview left, knob rail right (~280px), stacking on narrow viewports.** The
output region stops being a single column — this is the redesign.

**Preview must never be mistaken for the notebook's output.** While any knob
differs from its committed value, the preview carries a band — *"Preview — not in
your notebook yet"* — plus a distinct border. All knobs at committed values: no
band, real output.

**States.** closed · risky-cell confirm · warming (progress + cancel) ·
ready-clean · dirty-previewing (spinner **over the last preview**, never blank —
losing the picture you are comparing against defeats the feature) · dirty-ready ·
preview-failed (`bins=0` will crash; show the traceback inline, keep the knobs,
keep the shadow alive) · applying.

**Two labels doing real work.** Apply reads **"Apply 3 changes & re-run"**,
because it spends a live chain re-run and that should not be a surprise. The
empty state says *why* — "`threshold` is assigned twice before this cell" — not
"no tunable variables found." Given §3.2's rejection rules, that explanation is
the difference between a feature that seems broken and one that is honest.

### 3.6 Edge cases

- **Live kernel restarted (the subtle one).** The shadow survives, but Apply's
  hidden assumption does not. Apply re-runs "from the earliest changed cell,"
  which only works if the live kernel already executed everything before it. On a
  fresh or restarted kernel it has not, and Apply would fail with a `NameError`
  that reads as the feature being broken. **Apply must guard on live prefix
  state** — extend the re-run to the top, or say so before spending it.
- Document revision change invalidates the shadow. Re-warm; never silently serve
  a stale preview.
- *C* edited while the panel is open → re-scan knobs.
- Concurrent previews supersede; latest wins; the client debounces.
- A cell with no image output never offers Tune.
- A tune Apply onto a cell with pending agent hunks makes them `is_stale` via the
  existing mechanism. Warn before doing it.
- `ast.parse` failure → no knobs, with the reason shown.

### 3.7 Testing

- **Rejection rules get one test each** (§3.2). They are the correctness
  boundary.
- **Literal rewriting**: repeated literals on one line, non-ASCII column offsets
  (AST offsets are byte-sensitive), idempotence.
- **Bounds heuristic**: zero, negatives, int flooring.
- **The load-bearing integration test: "preview mutates nothing."** Assert the
  document revision is unchanged and the live kernel is untouched across a
  preview.
- Shadow lifecycle: cache hit, invalidation on revision change, risky-chain
  gating, idle teardown.
- Apply: multi-cell atomicity, undo recomposes, live re-run triggered, and the
  §3.6 prefix guard.
- Frontend: panel states, preview band present/absent, empty-state explanation,
  "Apply N changes & re-run" label.
- E2E: open a plot notebook, tune, preview, apply, assert both source and outputs
  changed.
- Per standing project rule, every regression test is verified by disabling the
  fix and watching it fail first.

---

## 4. Decision Log

### D1 — Knob scope: any upstream variable, not just same-cell literals

**Decided.** Knobs may live several cells upstream of the plot.

*Alternatives:* same-cell literals only (`bins=30`, `alpha`) — cheap, safe, but
cosmetic; or a full "tool figures out where it lives" dependency engine.

*Why:* the valuable knobs are parameters like `n_samples` and `threshold` that
sit upstream. Restricting to same-cell literals would ship a styling tweaker.
Cost accepted: every knob change drags real computation, which forces D2–D5.

### D2 — Playing is free; confirming is what costs

**Decided.** Dragging never mutates the live kernel or notebook. Only Apply does.

*Alternatives considered and rejected in favour of this rule:* explicit
Preview-per-value against the live kernel; debounced live scrubbing against the
live kernel.

*Why:* this is the user's own framing and it is a stronger invariant than any of
the offered options. It converts an open-ended safety question into a structural
one, and it is what makes the shadow kernel necessary rather than optional.

### D3 — Lazy memoized previews, not a pre-computed sweep

**Decided.** Run the chain per value actually examined; cache by knob-value
tuple.

*Alternative:* pre-compute N values over a range, then scrub cached images
instantly.

*Why:* the sweep is not an alternative architecture — it needs the same shadow
kernel, so it is an *eager caching policy* on the same machinery. It pays for
every unexamined grid point, requires you to choose a range before seeing
anything, and delays first plot by N runs. Memoized lazy previewing absorbs its
only real advantage (instant revisits) for free. *Revisit if:* users routinely
examine more values than a grid would hold — then add prefetch-neighbours on top,
which is a pure addition.

### D4 — Warm the shadow by replaying upstream cells

**Decided.** Fresh kernel, re-execute the chain from the top.

*Alternatives:* `os.fork()` the live kernel for exact copy-on-write state (no
replay, no re-fired side effects, provably matching state — but forking a live
process with threads, sockets, or CUDA is genuinely hairy); or retreat to
same-cell knobs and skip warm-up entirely (kills D1).

*Why:* simplest, no new failure modes, reuses existing execution machinery. Cost
accepted: full upstream cost once per session, and side effects re-fire — which
D5 addresses. *Revisit if:* warm-up latency proves intolerable on real notebooks;
fork is the high-payoff escape hatch and the repo is already POSIX-only.

### D5 — Prompt once per warm-up when the replay chain is risky

**Decided.** Show which upstream cells will re-fire and what each does, reusing
`RiskyExecutionDialog`, and approve the whole warm-up once.

*Alternatives:* block the feature entirely behind any flagged cell (strictest
reading of D2, but a notebook that fetches its data over the network loses the
feature — that is a lot of real notebooks); or replay silently (a slider drag
could re-POST to an API).

*Why:* matches the product's existing pattern exactly, keeps the feature usable,
and makes the promise precise: free unless we tell you otherwise.

### D6 — A separate `TuningService`, not an `origin` field on `AgentTurn`

**Decided.** Reuse the generic `operations.py` primitives, the document apply
path, and the downstream execution path, behind a small dedicated service with
its own thin routes.

*Alternative:* add `origin: "agent" | "tune"` to `AgentTurn` and reuse
`AgentTurnService`, inheriting accept/reject/undo/revert routes and
`execution_operation_id` for free.

*Why not:* that injects a record with a synthetic `prompt`, no `model`, no
`write_scope`, no adapter run, and a meaningless `cancel_event` into a 1305-line
service whose entire lifecycle assumes an adapter ran. Every method becomes "does
this assume an agent?" That is a large audit surface to save roughly 55 lines of
route delegation (`agent_turn_routes.py:247-301`).

*Revisit if:* a third edit origin appears. Two is a coincidence; three means
extract a shared `EditRecord` that `AgentTurn` also rides on — the correct end
state either way.

### D7 — AST auto-scan, no AI in the loop

**Decided.** Deterministic AST scan decides what is tunable.

*Alternatives:* AST scan plus one Claude turn to propose ranges and salience;
manual "mark this literal as a knob."

*Why:* deterministic, instant, no agent turn in the hot path, and every knob is
rewritable by construction. Cost accepted: dict entries, computed values and
inline kwargs are not tunable, and bounds come from a dumb heuristic (§3.3)
rather than semantic understanding. *Revisit if:* the bounds heuristic proves
annoying often enough to justify a turn.

### D8 — Apply writes code *and* re-runs the live chain

**Decided.** Apply rewrites the literals, then re-executes the upstream chain in
the live kernel so code, kernel, and outputs agree.

*Alternatives:* write code and mark outputs stale using the existing
`outputsStale` banner (`NotebookCell.tsx:200`), leaving the re-run to the user; or
transplant the shadow's rendered image straight into the cell's outputs.

*Why:* it is the direct consequence of D2 — confirm is the moment you pay, and
paying should leave everything consistent. Transplanting was rejected outright:
the live kernel would still hold the old value, so every downstream cell would
silently disagree with the plot above it.

### D9 — Several knobs at once, one atomic Apply

**Decided.** Change several knobs, preview once, Apply writes all literals —
possibly across several cells — as one all-or-nothing edit with one live re-run.

*Alternative:* one knob at a time, where atomicity is free.

*Why:* matches how parameters are actually explored (interactions between two
parameters are the interesting case), and costs one live re-run instead of three.
The multi-cell atomicity requirement maps cleanly onto the existing ledger, which
already spans cells within one record.

### D10 — Panel lives under the cell, not in a separate tab

**Decided.** The panel opens in the cell's own output region, with a
preview-left / knob-rail-right layout.

*Alternative:* a separate tab or dock, as originally sketched.

*Why:* cause and effect belong in one place; a separate tab means losing sight of
the notebook context the plot lives in. Cost accepted: the output region needs a
real layout redesign rather than remaining a single column.

---

### D11 — Line magics are neutralised; non-Python cell magics block the chain

**Decided.** `%matplotlib inline` and `!cmd` lines are replaced by `pass`
(preserving line count, so every source range stays valid) and analysis
continues. A cell magic whose body is still Python (`%%time`, `%%capture`,
`%%timeit`, `%%prun`, `%%debug`) has its magic line dropped the same way.
Any other cell magic (`%%bash`, `%%html`, `%%script`) makes the cell
un-analysable, and the whole chain is blocked with that reason.

*Why:* `%matplotlib inline` is near-universal in plotting notebooks, so failing
on it would disable the feature almost everywhere. But a `%%bash` cell could
bind anything, and guessing would risk offering a knob that does not control
what it claims — the one failure mode this design refuses.

---

## 5. Implementation Status

**Stage 0 (discovery, rewrite, bounds) — complete.** 88 tests, all rejection
rules covered individually, verified by mutation: each fix was disabled in turn
and the corresponding tests watched to fail. Two of those mutation runs found
real gaps — two "non-ASCII" tests that never exercised the UTF-8 byte-offset
conversion because the multibyte text sat *after* the literal, and an untested
guard that let a nested `def`'s locals become module bindings.

Code review of the branch found and fixed three further defects:

- `df["col"] = ...` and `obj.attr = ...` were reported as *"assigned by
  unpacking several values at once"* — false, and one of the most common lines
  in a real notebook. They now reject as `mutated`.
- Magic-stripping ran before parsing, so a line inside a triple-quoted string
  starting with `%` or `!` was rewritten to `pass` and the panel showed a value
  the file does not contain. Cells are now parsed as-is first; only a genuine
  `SyntaxError` triggers stripping.
- Dependency-only graph edges were counted toward the "assigned more than once"
  check. Fixing that opened a new hole — `n = 5; del n` would have been offered
  as a knob for a name that no longer exists — so `del` is now its own rejection
  rule.

One guard is knowingly uncovered: the dependency-edge exclusion above is
unreachable today, because every module-level store that is not a simple
assignment is already disqualified by another rule. It is kept as a guard on
that unstated cross-pass invariant, and is commented as such.

Against `examples/ml-pipeline.ipynb`: 4 knobs (`N_SAMPLES`, `TEST_FRACTION`,
`LEARNING_RATE`, `EPOCHS`), 17 explained rejections, nothing wrongly offered.
Against the new `examples/plot-tuning.ipynb` fixture: 7 knobs covering all five
control types.

`matplotlib` added to the `[test]` extra — test-only; the app never imports it.

**Stages 1–5 — complete.** 576 backend tests, 165 frontend tests, 2 end-to-end
tests passing against a real kernel and dev server, `tsc -b` clean.

- **§3.1 shadow** (`plot_tuning/shadow.py`) — owns its own `KernelSession` and
  imports nothing from `KernelExecutionService`; the load-bearing test asserts a
  preview leaves both the document revision and the live kernel untouched.
- **§3.4 apply** (`plot_tuning/apply.py`) — atomic multi-cell write-back through
  `apply_source_changes_under_lease`, ledger reuse for per-hunk Keep/Undo, and
  the `tune` origin. The §3.6 prefix guard resolves by *extending* the re-run to
  the top when the live kernel has not executed the prefix, and reports having
  done so (`reRanFromTop`) — "re-ran the whole notebook" is a different bill from
  "re-ran from your edit down", and the user should be told which they got.
- **§3.1 panel service** (`plot_tuning/panel.py`) — the analysis pass and the
  one-shadow-at-a-time rule. `open_panel` starts nothing, which is what makes D5
  real rather than decorative.
- **§3.5 UI** (`frontend/src/plotTuning/`) — `dirty` is derived, never stored, so
  the preview band cannot be present in one state and missing in another.

Three defects were found by adversarial review of the frontend and fixed:

- **A settled tuning record permanently captured its cells.** Routing keyed off
  "does this cell appear in the record", but `with_state` keeps accepted and
  rejected operations for ever and the record only clears on a session change.
  After keeping a tune, the next *agent* edit to that cell rendered no review
  bar, no diff and no per-cell Keep/Undo. This was a regression in the existing
  agent flow, not the new feature. Routing now requires an unsettled operation.
- **An in-flight warm-up leaked its kernel on unmount.** The panel is swapped out
  wholesale rather than re-rendered closed, and at that moment the shadow id is
  still unknown, so the warm resolved into a component nobody was watching and a
  duplicate kernel survived to the idle timeout.
- **A superseded preview could pin `preview-failed`** after a re-scan had already
  reset every knob to its committed value.

Both of the first two have regression tests, each verified by reverting the fix
and watching them fail. The third is covered by the same token-retirement change
but has no dedicated test.

## 6. Open Questions

None blocking. Deferred until implementation:

- Exact idle-timeout value for shadow teardown (10 min is a placeholder).
- Whether the determinism caveat (§2) needs UI copy or only documentation.
- Whether preview should render *all* of *C*'s outputs or only image outputs.
