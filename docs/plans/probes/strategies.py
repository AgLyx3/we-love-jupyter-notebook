"""Candidate ways of splitting a notebook into blocks.

Every strategy has the same signature — `(cells) -> [(lo, hi), ...]` — and must
return a contiguous partition covering every index exactly once. evaluate.py
checks that rather than trusting it.

The point of having more than one is that `headings` (the shipped deterministic
pass) scores 71–90% heading-dependence and collapses to 3–5 blocks without
headings, so "can anything free do better on a heading-free notebook" is an open
question, not a rhetorical one. `fixed` is in here to keep the answer honest: a
strategy that cannot beat cutting every N cells has earned nothing.
"""

from __future__ import annotations

import statistics

import extract

WINDOW = 3          # cells either side of a gap, for cohesion
MAX_BLOCK = 12      # the research doc's "avoid blocks longer than about 12"
MIN_BLOCK = 2


def _spans(starts: list[int], last: int, first: int = 0) -> list[tuple[int, int]]:
    """Turn cut points into spans over [first, last].

    `first` is not always 0: hybrid() runs a strategy over a *slice* of the
    notebook, and a floor hard-coded to 0 silently anchored those spans at the
    top of the document instead of at the slice.
    """
    starts = sorted(set(s for s in starts if first <= s <= last))
    if not starts or starts[0] != first:
        starts = [first] + starts
    return [(s, (starts[i + 1] - 1) if i + 1 < len(starts) else last)
            for i, s in enumerate(starts)]


def _terms(cell: extract.Cell) -> set[str]:
    """What a cell is 'about', for overlap purposes.

    Identifiers only — not raw tokens. Two cells that both say `df` and
    `revenue` are working on the same thing; two that both say `for` and `print`
    are not, and a bag-of-words would score those the same.
    """
    if cell.kind != "code":
        return set()
    return set(cell.binds) | set(cell.reads) | set(cell.calls) | set(cell.imports)


# ── strategies ──────────────────────────────────────────────────────────────

def headings(cells: list[extract.Cell]) -> list[tuple[int, int]]:
    """The shipped deterministic pass, unchanged. The control."""
    return extract.segment(cells)


def fixed(cells: list[extract.Cell], size: int = 8) -> list[tuple[int, int]]:
    """Cut every `size` cells. The floor everything else has to clear."""
    first, last = cells[0].idx, cells[-1].idx
    return _spans(list(range(first, last + 1, size)), last, first)


def milestones(cells: list[extract.Cell]) -> list[tuple[int, int]]:
    """Milestone transitions only — headings deliberately ignored.

    This is what the shipped pass degrades to on a heading-free notebook, run
    directly so the degradation is a measurement rather than an inference.
    """
    first, last = cells[0].idx, cells[-1].idx
    starts = [first]
    seen: str | None = None
    for cell in cells:
        if cell.kind == "code" and cell.milestone and cell.milestone != "evaluate":
            if cell.milestone != seen:
                starts.append(cell.idx)
            seen = cell.milestone
    return _spans(starts, last, first)


def cohesion(cells: list[extract.Cell], window: int = WINDOW) -> list[tuple[int, int]]:
    """Cut where the vocabulary changes — TextTiling over identifiers.

    For each gap, compare the identifiers used in the `window` cells before it
    with those in the `window` after. A low overlap means the notebook stopped
    talking about one thing and started on another. Cut at the *valleys*: gaps
    whose depth (how far the score falls below the local peaks either side)
    clears mean + stdev, which adapts the threshold per notebook instead of
    hard-coding a similarity anyone would have to tune.
    """
    first, last = cells[0].idx, cells[-1].idx
    if len(cells) < 2 * window:
        return [(first, last)]

    terms = [_terms(c) for c in cells]
    scores: list[float] = []
    for gap in range(len(cells) - 1):             # gap g sits between cells[g] and cells[g+1]
        before: set[str] = set().union(*terms[max(0, gap - window + 1):gap + 1]) or set()
        after: set[str] = set().union(*terms[gap + 1:gap + 1 + window]) or set()
        union = before | after
        scores.append(len(before & after) / len(union) if union else 0.0)

    depths: list[float] = []
    for gap, score in enumerate(scores):
        left = score
        for i in range(gap - 1, -1, -1):
            if scores[i] < left:
                break
            left = scores[i]
        right = score
        for i in range(gap + 1, len(scores)):
            if scores[i] < right:
                break
            right = scores[i]
        depths.append((left - score) + (right - score))

    positive = [d for d in depths if d > 0]
    if not positive:
        return _split_oversized([(first, last)], cells, depths)
    # mean, not mean + stdev: the stricter threshold left 85-cell blocks, which
    # is the failure this strategy exists to avoid.
    cut = statistics.mean(positive)
    starts = [first] + [cells[gap + 1].idx for gap, depth in enumerate(depths) if depth >= cut]
    blocks = _spans(starts, last, first)
    return _enforce_sizes(_split_oversized(blocks, cells, depths), cells)


def hybrid(cells: list[extract.Cell]) -> list[tuple[int, int]]:
    """Trust headings, then fix what they leave behind.

    Headings are real evidence where they exist — the model agreed with them
    100% of the time on a notebook whose headings were good. The failures are at
    the two ends: blocks far too long (a 99-cell block on a heading-free
    notebook) and 1-cell blocks from two consecutive headings. So: take the
    heading boundaries, subdivide anything oversized by cohesion, and merge
    anything undersized into its neighbour.
    """
    first, last = cells[0].idx, cells[-1].idx
    blocks: list[tuple[int, int]] = []
    for lo, hi in extract.segment(cells):
        if hi - lo + 1 <= MAX_BLOCK:
            blocks.append((lo, hi))
            continue
        # cohesion() works in the notebook's own index space, so the slice needs
        # no offset arithmetic — it just needs a strategy that honours `first`.
        blocks.extend(cohesion([c for c in cells if lo <= c.idx <= hi]))
    return _enforce_sizes(_spans([b[0] for b in blocks], last, first), cells)


def _split_oversized(blocks: list[tuple[int, int]], cells: list[extract.Cell],
                     depths: list[float] | None = None) -> list[tuple[int, int]]:
    """Recursively halve any block over MAX_BLOCK at its weakest internal seam.

    The size rule is the segmenter's own, and a strategy that states it and then
    returns a 99-cell block has not applied it. Falls back to the midpoint when
    no cohesion signal is available, which is still better than one huge block.
    """
    by_pos = {c.idx: i for i, c in enumerate(cells)}
    out: list[tuple[int, int]] = []
    queue = list(blocks)
    while queue:
        lo, hi = queue.pop(0)
        if hi - lo + 1 <= MAX_BLOCK:
            out.append((lo, hi))
            continue
        inner = [i for i in range(lo + MIN_BLOCK, hi - MIN_BLOCK + 2)]
        if not inner:
            out.append((lo, hi))
            continue
        if depths and all(i in by_pos and by_pos[i] - 1 < len(depths) for i in inner):
            cut = min(inner, key=lambda i: depths[by_pos[i] - 1])
        else:
            cut = lo + (hi - lo + 1) // 2
        queue = [(lo, cut - 1), (cut, hi)] + queue
    return sorted(out)


def _enforce_sizes(blocks: list[tuple[int, int]], cells: list[extract.Cell]) -> list[tuple[int, int]]:
    """Fold blocks below MIN_BLOCK into the neighbour they share more with.

    A 1-cell block is only worth keeping when the cell is a genuine milestone;
    otherwise it is a rail entry that costs a line and says nothing.
    """
    if len(blocks) < 2:
        return blocks
    by_idx = {c.idx: c for c in cells}
    out = list(blocks)
    changed = True
    while changed and len(out) > 1:
        changed = False
        for i, (lo, hi) in enumerate(out):
            if hi - lo + 1 >= MIN_BLOCK:
                continue
            cell = by_idx.get(lo)
            if cell is not None and cell.kind == "code" and cell.milestone:
                continue                          # a genuine milestone earns its line
            if i == 0:
                out[1] = (out[0][0], out[1][1]); out.pop(0)
            elif i == len(out) - 1:
                out[-2] = (out[-2][0], out[-1][1]); out.pop()
            else:
                mine = _terms(by_idx[lo]) if lo in by_idx else set()
                prev = set().union(*[_terms(by_idx[j]) for j in range(*out[i - 1]) if j in by_idx]) or set()
                nxt = set().union(*[_terms(by_idx[j]) for j in range(*out[i + 1]) if j in by_idx]) or set()
                if len(mine & prev) >= len(mine & nxt):
                    out[i - 1] = (out[i - 1][0], hi); out.pop(i)
                else:
                    out[i + 1] = (lo, out[i + 1][1]); out.pop(i)
            changed = True
            break
    return out


def dataflow(cells: list[extract.Cell], target: int | None = None) -> list[tuple[int, int]]:
    """Cut where the fewest variables flow across the seam, to a size budget.

    Two borrowed ideas, both from agent codebase-navigation work, shrunk to cell
    scale:

    * **A reference graph, not lexical overlap** (Aider's repo map builds a graph
      of files joined by symbol references and ranks that, rather than comparing
      text). Here an edge runs from the cell that last bound a name to each cell
      that reads it — real def→use dataflow. `cohesion` compares vocabulary,
      which counts two cells that both mention `df` as related even when one
      rebinds it and the other never sees that value. This does not.
      Edges are weighted 1/distance: a config constant read ninety cells later
      is a real dependency but a terrible reason to refuse a boundary.

    * **Budget the map, do not threshold it** (Aider fits its repo map to a token
      budget rather than a similarity cutoff). The rail has room for roughly a
      dozen entries before it stops being a map and becomes a second thing to
      scroll, so the block count is a *constraint* — derived from length — and
      the segmentation is whatever best fits it. Every other strategy here picks
      a threshold and accepts whatever count falls out.

    cAST's contribution is already in `_split_oversized`: split structure
    recursively to a size limit rather than cutting on a fixed grid.
    """
    first, last = cells[0].idx, cells[-1].idx
    n = len(cells)
    if n < 2 * MIN_BLOCK:
        return [(first, last)]
    if target is None:
        # ~10 cells an entry, floored and capped so the rail stays scannable at
        # both ends of the corpus (21 cells → 4 entries, 286 → 18).
        target = max(4, min(18, round(n / 10)))

    pos = {c.idx: i for i, c in enumerate(cells)}
    writer: dict[str, int] = {}
    crossing = [0.0] * (n - 1)                    # crossing[g] spans cells g..g+1
    for i, cell in enumerate(cells):
        if cell.kind != "code":
            continue
        for name in cell.reads:
            src = writer.get(name)
            if src is None or src == i:
                continue
            weight = 1.0 / (i - src)
            for gap in range(src, i):
                crossing[gap] += weight
        for name in cell.binds:
            writer[name] = i

    # A markdown cell is a heading for what follows, so a seam just *before* one
    # is cheap and a seam just after it is not: never strand a heading at the
    # end of the block it introduces.
    for gap in range(n - 1):
        if cells[gap + 1].kind == "markdown":
            crossing[gap] *= 0.5
        if cells[gap].kind == "markdown":
            crossing[gap] += 1.0

    chosen: list[int] = []
    for gap in sorted(range(n - 1), key=lambda g: crossing[g]):
        if len(chosen) >= target - 1:
            break
        if all(abs(gap - c) >= MIN_BLOCK for c in chosen) and gap + 1 >= MIN_BLOCK \
           and (n - 1 - gap) >= MIN_BLOCK:
            chosen.append(gap)

    starts = [first] + [cells[g + 1].idx for g in sorted(chosen)]
    return _enforce_sizes(_split_oversized(_spans(starts, last, first), cells), cells)


STRATEGIES = {
    "headings": headings,
    "fixed8": fixed,
    "milestones": milestones,
    "cohesion": cohesion,
    "hybrid": hybrid,
    "dataflow": dataflow,
}
