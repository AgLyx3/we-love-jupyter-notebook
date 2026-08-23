# Map evaluation corpus

A fixed set of notebooks for measuring the overview map, and a harness that
scores the segmenter across all of them at once
(`python3 docs/plans/probes/evaluate.py`).

It exists because of a specific mistake. While demoing the panel I hand-wrote a
122-cell notebook as 19 tidy phases, each under its own markdown heading, and
the resulting map looked excellent — 19 evenly-sized named blocks, a clean
partition. It was excellent about nothing. `extract.segment()` opens a block at
every markdown heading, so that notebook had already been segmented by its
author before the segmenter saw it. The reviewer noticed the block sizes were
suspiciously even; the number that proves it is `hdep`, below.

One notebook can flatter. A corpus with a heading-free end cannot.

## What is in it, and why each one

Nine notebooks spanning 21–286 cells, from four headings-per-cell down to none.

**Remote** — real notebooks from GitHub, referenced rather than vendored.
`fetch.py` pulls each at a pinned commit into `.cache/` (gitignored). We do not
have redistribution rights for all of them, and a pinned sha is the same bytes
forever where a branch name is not.

| id | cells | source |
|---|---|---|
| `madewithml` | 243 | Real end-to-end ML project. Headings exist, nowhere near one per section. |
| `handson-unsupervised` | 286 | The heading-rich control — 142 markdown cells. If the deterministic pass ever looks good, it should look good here. |
| `orie4741-eda` | 54 | Real course EDA, 6 headings. Heading-poor, mid-sized. |
| `revenue-recovery-eda` | 87 | Personal-project EDA — the most common shape on GitHub, and the one the panel will actually meet. |
| `fraud-eda` | 21 | The small end. Guards against a segmenter that only behaves on long inputs. |

**Local** — committed here, no licensing question.

| id | cells | why |
|---|---|---|
| `messy-exploration` | 57 | The existing hard probe: one markdown cell, no defs, out-of-order execution. |
| `simulation-sweep` | 21 | Non-ML — a parameter sweep, not a data pipeline. |
| `messy-adspend` | 154 | Synthetic and deliberately nasty: 2 headings, copy-paste-tweak runs, abandoned attempts interleaved rather than grouped, a topic resumed 40 cells later. |
| `tidy-phased` | 122 | The flattering fixture above, kept on purpose so its flattery shows up as a number beside the others. |

## The columns

| column | meaning |
|---|---|
| `blocks` | how many the pass produced |
| `min` / `median` / `max` | block size in cells |
| `cv` | stdev/mean of block size. Read it *with* `min` and `max`, never alone: the deterministic pass scores a high `cv` not because it tracks real structure but because it emits a 1-cell block next to a 99-cell one. Uniformity is neither good nor bad by itself |
| `1-cell` | blocks of one cell — the research doc says avoid unless a genuine milestone |
| `>12` | blocks longer than 12 cells — same doc says avoid |
| `hdep` | **the important one.** Fraction of block boundaries that land on a markdown heading. High means the author segmented the notebook, not us |
| `no-md` | blocks produced by the same pass with every heading demoted to prose. The drop from `blocks` to `no-md` is how much structure the pass finds on its own |
| `valid` | the partition covers every cell exactly once, in order |

`hdep` and `no-md` are the pair to read together. A pass scoring 85% `hdep` and
collapsing from 23 blocks to 5 without headings is a heading-follower wearing a
segmenter's clothes.

## Running it

```bash
python3 docs/plans/probes/corpus/fetch.py      # once; --force to re-pull
python3 docs/plans/probes/evaluate.py          # deterministic only, free, instant
python3 docs/plans/probes/evaluate.py --model  # adds one model call per notebook
python3 docs/plans/probes/evaluate.py --only madewithml --model
```

`--json <path>` writes every block range and generated name, which is what to
diff when a prompt or a heuristic changes.

## Adding to it

Add the shape that is missing, not another of a shape already here. The gaps
worth filling: a notebook with headings that *lie* (copied from a template and
never updated), one that is mostly markdown prose, one with `%%` cell magics
throughout, and a non-Python kernel.
