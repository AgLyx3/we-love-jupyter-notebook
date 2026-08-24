# Baseline — 2026-08-23

`python3 docs/plans/probes/evaluate.py --model --json corpus/baseline.json`

Nine notebooks, 21–286 cells. Deterministic pass is `extract.segment()`; model
pass is `segment.py` through the Claude CLI at its default model, one call per
notebook. Raw block ranges and every generated name are in `baseline.json`.

```
notebook              cells  head  blocks  min  med  max    cv  1-cell  >12   hdep  no-md  valid
madewithml              243    36      44    1    5   22  0.70       3    2    84%      8     ok
  └ model                              36    3    6   13  0.31       0    1    83%            ok
handson-unsupervised    286    27      39    1    5   25  0.82       3    7    71%     12     ok
  └ model                              33    3    9   17  0.39       0    4    72%            ok
orie4741-eda             54     6       7    1    4   22  0.96       1    2    83%      2     ok
  └ model                               8    4    7    9  0.21       0    0    43%            ok
revenue-recovery-eda     87    20      22    1    3   15  0.79       5    1    90%      3     ok
  └ model                              12    4    7   11  0.25       0    0    91%            ok
fraud-eda                21     4       6    1    2   12  1.09       1    0    60%      3     ok
  └ model                               5    2    4    7  0.41       0    0    75%            ok
messy-exploration        57     0       5    1    4   44  1.43       1    1     0%      5     ok
  └ model                              12    2    5    8  0.35       0    0     0%            ok
simulation-sweep         21     1       3    2    4   15  0.82       0    1     0%      3     ok
  └ model                               5    2    5    6  0.35       0    0     0%            ok
messy-adspend           154     2       5    1   10   99  1.20       1    2    25%      4     ok
  └ model                              28    3    5    9  0.32       0    0     4%            ok
tidy-phased             122    19      23    1    5   10  0.46       2    0    82%      5     ok
  └ model                              19    4    6   10  0.26       0    0   100%            ok
```

## 1. The deterministic pass is a heading-follower

`hdep` is 71–90% on every notebook that has headings. Demote the headings to
prose and the same pass collapses: 44 blocks → 8, 39 → 12, 23 → 5. It is not
finding structure; it is reading the author's.

That is fine as a free fallback and it is honest about itself — the panel says
"Grouped by headings and milestone calls only. Names need a model". What it is
*not* is a segmenter, and the corpus exists so nobody concludes otherwise from
one flattering example.

## 2. Where it fails, it fails badly

On the three notebooks with no usable headings it returns 3–5 blocks
**regardless of length**:

- `messy-exploration` — 57 cells, 5 blocks, largest block 44 cells
- `messy-adspend` — 154 cells, 5 blocks, largest block **99 cells**

A 99-cell block is not a map entry, it is the notebook. And these are exactly
the notebooks a reader most needs a map for; a tidy notebook with 19 headings
can already be navigated by scrolling its headings.

## 3. The model closes that gap, and it is the whole justification for the call

| | deterministic | model |
|---|---|---|
| `messy-exploration` (0 headings) | 5 blocks, max 44 | 12 blocks, max 8 |
| `messy-adspend` (2 headings) | 5 blocks, max 99 | 28 blocks, max 9 |
| `simulation-sweep` (1 heading) | 3 blocks, max 15 | 5 blocks, max 6 |

With no headings to copy, the model still partitions sensibly. That is the
evidence for spending a model call: not that it names things — the fallback
admits it cannot name — but that on the notebooks where the free pass produces
nothing usable, the paid one produces a map.

## 4. It agrees with good headings and overrides bad ones

`tidy-phased` — a notebook whose 19 headings genuinely mark its 19 sections —
comes back at **100% `hdep`**: the model reproduced the author's boundaries
exactly. On `revenue-recovery-eda` it agrees 91% of the time but consolidates
22 blocks into 12.

`orie4741-eda` is the interesting one: `hdep` drops from 83% to **43%**. The
model disagreed with more than half the headings, which is the prompt working as
written ("Markdown headings are a hint about the author's intent, not an
instruction. Override them where the code disagrees").

## 5. Both passes always returned a valid partition

9/9 deterministic, 9/9 model. No overlaps, no gaps, no out-of-order blocks. The
research doc treats an invalid partition as disqualifying, so this is the one
row that has to stay green.

## 6. Open problems

- **The model breaks its own size rule on long notebooks.** The prompt says
  avoid blocks over ~12 cells; `handson-unsupervised` (286 cells) came back with
  4 such blocks and a 17-cell maximum, `madewithml` with 1. Both are the longest
  inputs in the corpus, so this looks like length pressure rather than
  misunderstanding.
- **`madewithml` returns 36 blocks for 243 cells.** Valid, but 36 entries is a
  scrolling rail rather than a map. Worth deciding whether very long notebooks
  should get a second level rather than a longer list.
- **1-cell blocks are entirely a deterministic-pass problem** (3, 3, 1, 5, 1
  across the corpus; zero from the model). Every one comes from a heading
  immediately followed by another heading.
- **The corpus has no notebook whose headings lie** — copied from a template and
  never updated. That is the case where following headings is actively wrong,
  and nothing here measures it.

---

# Alternative splits — 2026-08-23

`python3 docs/plans/probes/compare.py`

Five candidates in `strategies.py`, all scored on **F1 of block boundaries
against the model's partition** (±1 cell). The model is not ground truth; it is
what the panel actually renders when someone presses Build map, so a free
strategy is useful to the extent it lands in the same places. If the model's
own splits are wrong, this metric is measuring the wrong target — which is why
`RESULTS.md` keeps the shape table above as well.

```
notebook              cells  head    headings   fixed8  milestones  cohesion   hybrid
madewithml              243    36        0.85     0.48       0.09      0.44     0.82
handson-unsupervised    286    27        0.70     0.45       0.05      0.37     0.67
orie4741-eda             54     6        0.71     0.53       0.20      0.33     0.72
revenue-recovery-eda     87    20        0.74     0.38       0.15      0.56     0.76
fraud-eda                21     4        0.77     0.00       0.33      0.33     0.75
messy-exploration        57     0        0.12     0.42       0.13      0.70     0.55
simulation-sweep         21     1        0.25     0.25       0.25      0.71     0.91
messy-adspend           154     2        0.09     0.50       0.08      0.49     0.53
tidy-phased             122    19        0.95     0.36       0.18      0.61     0.95
------------------------------------------------------------------------------------
mean                                     0.58     0.38       0.16      0.50     0.74
mean, ≤2 headings                        0.15     0.39       0.16      0.63     0.66
```

## The uncomfortable one

**On heading-poor notebooks, cutting blindly every 8 cells beats the shipped
segmenter — 0.39 against 0.15.** `fixed8` is in the corpus as a floor nothing
should fall below, and the current pass falls below it by a factor of 2.6 on
exactly the notebooks a map is for. That is the strongest single argument in
this document for changing it.

## What each one showed

- **`milestones`** (0.16) — the shipped pass with headings taken away, run
  directly. 5 blocks for 286 cells, a 167-cell block. There is no salvaging the
  milestone signal on its own; it is not doing the work.
- **`cohesion`** (0.50 overall, **0.63** on ≤2 headings) — TextTiling over
  identifiers, cutting at vocabulary valleys. Beats headings by 4× where
  headings are absent and loses to them where they are present, which is
  exactly the complementary shape you would want.
- **`hybrid`** (**0.74** overall, 0.66 on ≤2 headings) — headings first, then
  subdivide anything over 12 cells by cohesion and fold 1-cell blocks into the
  neighbour they share more vocabulary with. It gives up almost nothing where
  headings are good (0.82 / 0.95 against headings' 0.85 / 0.95) and rescues the
  case where they are not.

## Recommendation

Replace `extract.segment()` with `hybrid`. It is strictly better on the corpus
mean, materially better on the notebooks the panel exists for, and costs nothing
extra — same inputs, no model call. The heading signal is kept where it is
genuinely evidence; the difference is that it is no longer the *only* signal.

That is a change to the overview feature, not to this evaluation, so it belongs
on the panel's own branch with this table as the justification.

## Caveats worth carrying

- **One reference, one model, one run.** Nothing here is averaged over repeated
  samples, so small differences between neighbouring strategies are noise. The
  gaps being relied on above (0.15 vs 0.66) are not small.
- **Tolerance ±1 flatters everyone.** At exact-match the ordering survives but
  the numbers halve: hybrid 0.52, headings 0.51 overall, and on ≤2 headings
  hybrid 0.40 against headings' 0.11.
- **`fixed8` scores 0.00 on `fraud-eda`** — 21 cells, so cutting at 8 and 16
  misses every real boundary. Small notebooks punish a fixed grid, which is the
  other half of why it is only a floor.
- **`cohesion` needs identifiers.** A notebook that is mostly prose, or mostly
  shell magics, gives it nothing to work with. Untested here; the corpus has no
  such notebook.

---

# Borrowed from codebase navigation, and what happened — 2026-08-23

Agent codebase-navigation tooling solves a bigger version of this problem, so
two ideas were lifted from it and one deliberately not.

**Taken: a reference graph instead of lexical overlap.** Aider's repo map builds
a graph whose nodes are files and whose edges are symbol references, then ranks
*that* rather than comparing text. The cell-scale version is def→use dataflow —
an edge from the cell that last bound a name to each cell that reads it, weighted
1/distance so a config constant read ninety cells later is not a reason to refuse
a boundary. `cohesion` compares vocabulary, which scores two cells that both
mention `df` as related even when one rebinds it and the other never sees that
value; a dataflow edge does not.

**Taken: budget the map, do not threshold it.** Aider fits its repo map to a
token budget rather than a similarity cutoff. The rail holds roughly a dozen
entries before it stops being a map and becomes a second thing to scroll, so
`dataflow` treats block count as a *constraint* derived from length (~1 entry
per 10 cells, floored at 4 and capped at 18) and finds the best cuts that fit
it. Every other strategy here picks a threshold and accepts whatever count
falls out.

**Not taken: PageRank.** Aider ranks nodes to decide *what to show* inside a
fixed budget. Segmentation has to cover every cell, so there is nothing to omit
and nothing for an importance score to do. It would matter for a second-level
map — deciding which blocks get expanded — which is a different feature.

**Not taken as such: cAST.** Its contribution — recursively split structure to a
size limit rather than cutting on a fixed grid — was already in
`_split_oversized` before the reading. Worth recording that it is the same idea
independently, not a borrowing.

## The result: it lost

```
                      mean   mean, ≤2 headings
hybrid                0.74        0.66
cohesion              0.50        0.63
dataflow              0.55        0.48
headings  (shipped)   0.58        0.15
```

Better-motivated did not mean better. And the obvious excuse — that the
scannability budget was starving it against a reference with 22–37 blocks —
does not hold: re-running `dataflow` with the model's own block count moves the
mean from **0.55 to 0.57**. The budget is not what is costing it. The seams
themselves land in different places.

Where the budget *did* matter is the heading-free end, and in both directions:
`messy-exploration` 0.32 → 0.67 and `simulation-sweep` 0.60 → 0.91 when allowed
more blocks, but `revenue-recovery-eda` 0.74 → 0.46 and `fraud-eda` 0.75 → 0.50
when forced to take them. A single length-derived budget is wrong for both ends.

## The reference moves, which is worse news than the loss

The baseline was recorded twice (the first run saved block shape but not ranges,
so it had to be re-run). Same corpus, same prompt, same model — different
partitions:

```
notebook                run1  run2   drift
handson-unsupervised      33    37    12%
orie4741-eda               8    10    25%
simulation-sweep           5     7    40%
messy-adspend             28    22    21%
mean                                  12%
```

A reference that shifts 12% on average — 40% on one notebook — cannot support
the precision the table above implies. `hybrid` at 0.74 against `dataflow` at
0.55 is a real gap. Anything under about 0.1 between neighbouring strategies is
not.

This is the strongest argument yet for changing the objective. Agreement with a
single sampled partition is measuring a moving target, and it structurally
cannot tell whether the target is any good — the metric was silent about
`handson-unsupervised` returning a 17-cell block, because that was the
reference.

The replacement should score what the panel is for: given a question, can a
reader find the right cell from the block names alone, and how much do they have
to read after landing. That needs no reference partition, so it does not drift,
and it can score the model's own map — which this metric structurally cannot.

---

# Findability — 2026-08-23

`python3 docs/plans/probes/findability.py`

The objective, at last, is the one the panel is for: *given a question, can a
reader find the cell from the block names alone, and what does it cost them.*
No reference partition, so nothing drifts, and the model's own map is scored
rather than assumed correct.

Three notebooks spanning the corpus (243 / 154 / 57 cells, heading-rich to
heading-free), eight questions each, five maps.

```
notebook             cells  strategy    rail   hit   cost
messy-exploration       57  headings       5   80%   30.1
                            fixed8         8   80%   15.0
                            cohesion      12   80%    5.1
                            hybrid        11   80%    5.4
                            model         13  100%    4.6
madewithml             243  headings      44   88%   17.1
                            fixed8        31   62%   21.1
                            cohesion      42   75%   13.8
                            hybrid        49   75%    8.6
                            model         37   25%   18.4
messy-adspend          154  headings       5   88%   84.9
                            fixed8        20  100%    8.0
                            cohesion      35   50%   25.0
                            hybrid        33   75%   23.9
                            model         22  100%    7.5
--------------------------------------------------------
mean                        headings      18   85%   44.0
                            fixed8        20   81%   14.7
                            cohesion      30   68%   14.6
                            hybrid        31   77%   12.6
                            model         24   75%   10.2
```

`cost` is cells a reader pays: the block they open, plus the distance they then
travel if the answer was not in it.

## How it is scored, and two things that had to be fixed to make it honest

Questions are generated once per notebook and cached, so every map is scored on
the identical set. Naming and answering are **separate calls and the answering
call never sees code** — one pass would let the model answer from the cells it
was just shown, and the reader only ever has the rail. Every map is named by the
same prompt, because a partition cannot be scored for findability without names
and comparing a named map to an unnamed one scores the naming.

**Accuracy alone is not comparable across rail lengths.** `headings` scores 88%
on `messy-adspend` with five blocks, one of which covers 99 cells — high
accuracy because there were five things to choose between, not because anything
was found. Reported alone it would have made the shipped pass look best.

**Strict containment of one sampled cell was too harsh.** The model's
`madewithml` map scored 25% while choosing blocks whose names were plainly
right — "train_loop_config, scaling, checkpoint configs" for a question about
how much hardware the job uses — because the sampled cell sat one block over. A
map that points at the right region has done its job. Grading by distance moves
that result from 185.5 to **18.4**, and it is the difference between a metric
that flags a real failure and one that flags a sampling artefact.

## What it says

**The shipped heading pass costs about 3.5× more reading than anything else
here** (44.0 against 10–15). Almost all of that is `messy-adspend`: 84.9,
because 88% of the time the reader lands correctly in a block covering most of
the notebook. That is the 99-cell block finally showing up as a user cost rather
than as a rule violation.

**Everything else is within noise of everything else.** `hybrid` 12.6,
`cohesion` 14.6, `fixed8` 14.7, model 10.2 — on 24 questions per strategy. The
gap that supports a decision is headings-vs-the-rest. The ordering inside the
rest does not.

Notably **`fixed8` — cutting blindly every 8 cells — beats the shipped
segmenter three times over.** Same finding as the boundary metric, now in terms
of what a reader pays.

## Caveats

- **24 questions per strategy.** Differences under roughly 5 cells of cost are
  not resolved. `hybrid` is not established as better than `cohesion` here.
- **An LLM reads the rail, not a human.** A proxy — but a proxy for the *task*,
  where the previous metric was a proxy for a drifting reference.
- **Heading-aligned maps may be flattered.** The question generator saw the
  notebook including its markdown headings, and a heading-aligned block can be
  named with the author's own words. `headings` still loses badly, so this does
  not change the conclusion, but it would matter for a closer race.
- **One run, one model, one seed.** The seed is fixed so the question set is
  stable; nothing is averaged over resamples.

---

# Findability at scale — 2026-08-23

`python3 docs/plans/probes/findability.py --questions 16`
`python3 docs/plans/probes/significance.py`

The previous round said the gap between the alternatives was "not resolved" at
24 questions per strategy. This is the same experiment at **138 questions** —
all nine notebooks, sixteen questions each — with the comparison done properly.

Every strategy answers the *same* questions, so the statistic is the **paired**
per-question difference, not two independent means: question difficulty
dominates the variance (a question whose answer sits in a 99-cell block is
expensive under every map) and pairing removes exactly that. The bootstrap
resamples **notebooks, not questions** — sixteen questions from one notebook
share its map, its length and its author's headings, so treating 138 questions
as 138 independent samples would manufacture significance. Clustering roughly
doubles the interval.

```
mean cost (cells a reader pays)        paired Δ, 95% CI over notebooks

  headings   21.5                  headings vs fixed8    +10.3  [ +0.7, +24.4]  fixed8 cheaper
  fixed8     11.2                  headings vs cohesion  +12.2  [ +0.9, +27.0]  cohesion cheaper
  cohesion    9.3                  headings vs hybrid    +12.5  [ +1.8, +27.1]  hybrid cheaper
  hybrid      9.1                  headings vs model     +12.5  [ +2.1, +26.8]  model cheaper
  model       9.0                  fixed8   vs cohesion   +1.9  [ -1.1,  +5.0]  not resolved
                                   fixed8   vs hybrid     +2.2  [ -0.4,  +4.4]  not resolved
                                   fixed8   vs model      +2.2  [ +0.3,  +4.5]  model cheaper
                                   cohesion vs hybrid     +0.3  [ -1.0,  +1.5]  not resolved
                                   cohesion vs model      +0.3  [ -1.3,  +2.6]  not resolved
                                   hybrid   vs model       0.0  [ -1.5,  +2.2]  not resolved
```

## 1. The shipped pass is out, conclusively

Worse than every alternative, every interval strictly positive, by 10–12 cells.
Including worse than cutting blindly every eight cells. This is no longer a
suggestive mean; it is settled.

## 2. cohesion and hybrid are the same thing

Δ 0.3 cells, CI [-1.0, +1.5]. At six times the data the difference did not grow
— it shrank. This is not "needs more evidence"; it is a measured equivalence,
and the interval is tight enough (±1.25 cells) to say so.

Pick on other grounds. **`cohesion`** is the recommendation: it is about forty
lines, depends on nothing but the cells' own identifiers, and is the only
candidate that cannot be fooled by headings that lie — the one corpus gap still
open. `hybrid` is defensible on the same evidence and is marginally better on
notebooks whose headings are good.

## 3. The model's *segmentation* buys nothing

**Δ 0.0 cells, CI [-1.5, +2.2], against a free strategy.**

This is the result worth acting on. Every map in this table — including the
model's — is named by the same prompt, so the comparison isolates *where the
boundaries go* from *what the blocks are called*. On boundaries, a forty-line
lexical heuristic matches the model call.

It does not follow that the model call is pointless: naming still needs one, and
the deterministic fallback still cannot name. What it means is that the call
should be spent on naming rather than on segmenting, and that the free map a
user sees before pressing Build map can be genuinely useful rather than the
5-blocks-for-154-cells it is today.

## 4. Two things that argue against over-reading this

- **Ordering is not perfectly transitive.** `fixed8` loses to `model`
  (CI [+0.3, +4.5]) but is not separated from `cohesion` or `hybrid`. With
  overlapping intervals that is expected, and it is a reminder that "not
  resolved" is not "equal" for the pairs where the interval is wide.
- **The model's own map is the weakest link on the longest notebook.**
  `madewithml`: 12% found, cost 18.3 — worse than three of the four free
  strategies on that notebook. Consistent with the 25% seen at 8 questions, so
  it is not a sampling artefact. Whatever is wrong there is in the model's
  boundaries, and the old boundary-F1 metric could never have seen it because
  the model *was* the reference.

## Recommendation

Replace `extract.segment()` with `cohesion`. Keep the model call for naming.
Re-run this file after the change; the corpus and the harness are the point, not
this particular table.
