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
