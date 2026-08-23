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
