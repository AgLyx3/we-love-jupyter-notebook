"""Tier 0/1: the five *computed* Block fields, with no model involved.

Ported from `docs/plans/probes/extract.py`, which is working code measured
against both probe fixtures (research §13.4). Three scope bugs were found and
fixed there before this port, and each one looked correct until it was run
against a real notebook. They are called out at their call sites below because
every one of them is a bug this file would otherwise reintroduce:

1. `produces` leaked function locals — a flat `ast.walk` treats `S, I, R = y`
   inside a `def` as a module-level binding, so a block advertised producing
   names no other cell can see.
2. Dead-function detection false-positived on callbacks — `rhs` passed to
   `solve_ivp(rhs, ...)` is never *called* by name, and an `ast.Call`-only scan
   reported it dead. Usage has to count name references.
3. Comprehension targets are separately scoped in Python 3 — omitting them from
   the nested-scope set re-admitted exactly the loop counters that `produces`
   filters out.

This module is also the deterministic fallback (spec §6). Be honest about what
that is: on the hard fixture it yields five blocks with a 44-cell lump, which is
not navigation. It keeps the panel functional without a CLI; it is not a
second-class-but-fine experience.
"""

from __future__ import annotations

from statistics import mean

import ast
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from typing import Any, Iterable, Sequence

from ..kernel_execution.risky_cell_classifier import RiskyCellClassifier
from ..plot_tuning.discovery import parse_cell
from .models import Block, Def

#: Milestone calls, per stage. Deliberately small, and deliberately only used by
#: the deterministic fallback: research §13.1 measured this segmenting the hard
#: fixture into a 44-cell block, and §13.2 measured import-family notebook-kind
#: detection reporting a 41-cell pandas exploration as ML because `sklearn`
#: arrives at cell 42. Neither is good enough to draw boundaries the panel
#: presents as the map — which is why the model draws them (spec §1).
MILESTONES = {
    "ingest": {"read_csv", "read_parquet", "read_json", "read_sql", "open", "load", "loadtxt"},
    "model": {"fit", "train", "solve_ivp", "odeint", "minimize", "sample"},
    "evaluate": {"predict", "score", "mean_absolute_percentage_error", "accuracy_score"},
    "export": {"to_csv", "to_parquet", "save", "dump", "savefig"},
}

#: How many `produces` names a block reports. Research §13.7: a block correctly
#: reporting `beta, ip, peaks, r0, results, s` buries the two that matter.
PRODUCES_LIMIT = 4


@dataclass
class Cell:
    """One notebook cell with everything the AST can say about it."""

    index: int
    kind: str
    source: str
    execution_count: int | None
    binds: set[str] = field(default_factory=set)
    reads: set[str] = field(default_factory=set)
    defines: list[str] = field(default_factory=list)
    imports: set[str] = field(default_factory=set)
    milestone: str | None = None
    #: Names bound *only* as a loop target. Python leaks these to module scope,
    #: so they are genuinely visible later — and genuinely noise. See produces().
    loop_binds: set[str] = field(default_factory=set)
    risky: bool = False

    @property
    def is_code(self) -> bool:
        return self.kind == "code"

    @property
    def is_heading(self) -> bool:
        return self.kind == "markdown" and self.source.lstrip().startswith("#")

    @property
    def heading(self) -> str:
        """The heading line itself, for `marks`. Empty when this is not one."""
        return self.source.strip().split("\n")[0] if self.is_heading else ""


def cell_source(cell: dict[str, Any]) -> str:
    source = cell.get("source", "")
    return "".join(source) if isinstance(source, list) else str(source)


def read_cells(
    notebook: dict[str, Any], *, classifier: RiskyCellClassifier | None = None,
) -> list[Cell]:
    """Parse every cell of a notebook document into an analysed `Cell`."""
    classifier = classifier or RiskyCellClassifier()
    cells = [
        Cell(
            index=index,
            kind=str(raw.get("cell_type", "code")),
            source=cell_source(raw),
            execution_count=raw.get("execution_count"),
        )
        for index, raw in enumerate(notebook.get("cells", []))
    ]
    for cell in cells:
        analyse(cell, classifier=classifier)
    return cells


def analyse(cell: Cell, *, classifier: RiskyCellClassifier | None = None) -> None:
    """Fill in everything the AST knows about one cell."""
    if not cell.is_code:
        return

    # `risky` is free here: the execution path already classifies exactly this,
    # and spec §3.3 says to reuse it rather than grow a second opinion about
    # what a dangerous cell looks like.
    classification = (classifier or RiskyCellClassifier()).classify(cell.source)
    cell.risky = classification.level == "confirm"

    # parse_cell rather than the probe's line filter. The probe blanked every
    # line starting with `!` or `%`, which also rewrites those lines inside a
    # triple-quoted string; parse_cell parses first and only strips magics if
    # that fails, so a cell holding a string full of `%`s is read as written.
    tree = parse_cell(cell.source)
    if tree is None:
        return

    _collect_names(cell, tree)
    _collect_loop_targets(cell, tree)
    _collect_imports_and_calls(cell, tree)


def _collect_names(cell: Cell, tree: ast.Module) -> None:
    """Walk the tree scope-aware, separating module-level binds from reads.

    Bug 1 lived here. `ast.walk` is flat, so `S, I, R = y` inside a `def` looked
    like a module-level binding and the block advertised producing `I` and `r0`
    — names nothing outside that function can see. Descend into nested scopes
    for *reads*, never for *binds*.
    """

    def collect(node: ast.AST, *, top: bool) -> None:
        for child in ast.iter_child_nodes(node):
            # Bug 3: comprehensions carry their own scope in Python 3, so
            # `{r0: f(s) for r0, s in ...}` binds nothing at module level.
            # Leaving them out of this set re-admitted the loop counters that
            # produces() exists to filter.
            nested = isinstance(child, (
                ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda, ast.ClassDef,
                ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp,
            ))
            if isinstance(child, ast.Name):
                if isinstance(child.ctx, ast.Store):
                    if top:
                        cell.binds.add(child.id)
                else:
                    cell.reads.add(child.id)
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) and top:
                cell.defines.append(child.name)
                cell.binds.add(child.name)
            collect(child, top=top and not nested)

    collect(tree, top=True)


def _collect_loop_targets(cell: Cell, tree: ast.Module) -> None:
    for node in ast.walk(tree):
        if isinstance(node, (ast.For, ast.AsyncFor)):
            for target in ast.walk(node.target):
                if isinstance(target, ast.Name):
                    cell.loop_binds.add(target.id)


def _collect_imports_and_calls(cell: Cell, tree: ast.Module) -> None:
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            cell.imports |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                cell.imports.add(node.module.split(".")[0])
        elif isinstance(node, ast.Call):
            func = node.func
            name = (
                func.id if isinstance(func, ast.Name)
                else func.attr if isinstance(func, ast.Attribute)
                else None
            )
            if not name:
                continue
            cell.reads.add(name)
            if cell.milestone is None:
                for label, names in MILESTONES.items():
                    if name in names:
                        cell.milestone = label
                        break


def produces(
    members: Sequence[Cell], later: Sequence[Cell], limit: int = PRODUCES_LIMIT,
) -> tuple[str, ...]:
    """Variables this block makes that something later reads, most-read first.

    Two filters, both learned from running this against real notebooks (§13.7):

    * A name bound *only* as a loop target is dropped. `for r0 in grid:` really
      does bind `r0` at module scope, so reporting it is correct — and useless.
      A block advertising `produces beta, ip, r0, s` buries the two names that
      matter. Correct and useless is still useless.
    * The rest are ranked by how many later cells read them and capped, so the
      block leads with what the notebook actually depends on it for.
    """
    code = [cell for cell in members if cell.is_code]
    if not code:
        return ()
    bound: set[str] = set().union(*(cell.binds for cell in code))
    # Bound as a loop target *and never any other way* — a name that is also
    # assigned normally somewhere in the block is a real product of it.
    loop_only = set().union(*(cell.loop_binds for cell in code)) - set().union(
        *((cell.binds - cell.loop_binds) for cell in code)
    )
    demand: Counter[str] = Counter()
    for cell in later:
        for name in cell.reads:
            demand[name] += 1
    candidates = [
        name for name in bound - loop_only
        if demand[name] and not name.startswith("_")
    ]
    return tuple(sorted(candidates, key=lambda name: (-demand[name], name))[:limit])


def call_sites(cells: Sequence[Cell]) -> dict[str, tuple[int, ...]]:
    """Every function definition in the notebook, and the cells referencing it.

    Bug 2 lived here. This keys off `reads`, not calls: a function passed as a
    value — `solve_ivp(rhs, ...)`, a `key=` argument, a decorator — is used, and
    counting `ast.Call` nodes alone marked every callback in the fixture dead.
    """
    code = [cell for cell in cells if cell.is_code]
    definitions = {name: cell.index for cell in code for name in cell.defines}
    sites: dict[str, list[int]] = defaultdict(list)
    for cell in code:
        for name in cell.reads:
            if name in definitions and cell.index != definitions[name]:
                sites[name].append(cell.index)
    return {name: tuple(sorted(set(found))) for name, found in sites.items()}


def is_out_of_order(cells: Iterable[Cell]) -> bool:
    """Whether execution counts decrease anywhere down the document."""
    counts = [
        cell.execution_count for cell in cells
        if cell.is_code and cell.execution_count is not None
    ]
    return any(later < earlier for earlier, later in zip(counts, counts[1:]))


def block_state(members: Sequence[Cell], *, out_of_order: bool) -> str:
    """One of BLOCK_STATES, in that precedence order.

    A block with no code cells is `ok` rather than `never-run`: prose was never
    going to have an execution count, and reporting a markdown-only block as
    unrun would be a warning about nothing.
    """
    code = [cell for cell in members if cell.is_code]
    if not code:
        return "ok"
    if all(cell.execution_count is None for cell in code):
        return "never-run"
    if out_of_order and _spans_a_regression(code):
        return "out-of-order"
    if any(cell.risky for cell in code):
        return "risky"
    return "ok"


def _spans_a_regression(code: Sequence[Cell]) -> bool:
    """Whether this block is where an out-of-order run actually shows up.

    The notebook-wide flag says *somewhere* a later cell ran earlier; marking
    every block with it would make the state meaningless on a 57-cell notebook
    with one transposition. A block is out-of-order when its own counts fall, or
    when it starts below where the previous block left off — the latter is
    checked by the caller, which knows the neighbours.
    """
    counts = [cell.execution_count for cell in code if cell.execution_count is not None]
    return any(later < earlier for earlier, later in zip(counts, counts[1:]))


def marks(members: Sequence[Cell]) -> tuple[str, ...]:
    """Markdown headings inside the range.

    Spec §7.3: a heading is *never* silently dropped. The model draws boundaries
    from code and may disagree with where the author put a heading; when it
    does, the heading still surfaces inside whichever block contains it. A user
    who wrote a heading and cannot find it reads the panel as broken.
    """
    return tuple(cell.heading for cell in members if cell.is_heading)


def annotate(
    cells: Sequence[Cell], ranges: Sequence[tuple[int, int]], names: Sequence[str],
) -> tuple[Block, ...]:
    """Attach the five computed fields to a set of generated boundaries.

    The boundaries and the names come from the model (or from `segment` in the
    fallback); everything else is derived here, on every request, because it is
    an AST parse and effectively free (spec §5).
    """
    code = [cell for cell in cells if cell.is_code]
    sites = call_sites(cells)
    out_of_order = is_out_of_order(cells)
    blocks: list[Block] = []
    previous_max: int | None = None
    for (start, end), name in zip(ranges, names):
        members = [cell for cell in cells if start <= cell.index <= end]
        member_code = [cell for cell in members if cell.is_code]
        state = block_state(members, out_of_order=out_of_order)
        counts = [c.execution_count for c in member_code if c.execution_count is not None]
        # The cross-block half of the out-of-order check: this block ran before
        # the one above it finished. `_spans_a_regression` cannot see that.
        if state == "ok" and previous_max is not None and counts \
                and min(counts) < previous_max:
            state = "out-of-order"
        if counts:
            previous_max = max(counts) if previous_max is None else max(previous_max, max(counts))
        blocks.append(Block(
            start=start,
            end=end,
            name=name,
            state=state,
            produces=produces(member_code, [c for c in code if c.index > end]),
            defines=tuple(
                Def(name=defined, call_sites=sites.get(defined, ()))
                for cell in member_code for defined in cell.defines
            ),
            marks=marks(members),
        ))
    return tuple(blocks)


#: The research doc's own size rule, now enforced rather than stated.
MAX_BLOCK = 12
MIN_BLOCK = 2
#: Cells either side of a seam whose vocabulary is compared.
COHESION_WINDOW = 3


def _terms(cell: Cell) -> set[str]:
    """What a cell is *about*, for the purpose of comparing two of them.

    Identifiers, not tokens. Two cells that both say `df` and `revenue` are
    working on the same thing; two that both say `for` and `print` are not, and
    a bag-of-words scores those the same.
    """
    if not cell.is_code:
        return set()
    # binds | reads | imports, and deliberately not `defines`: a def's *name*
    # is already in binds, and adding it twice weights a definition cell above
    # the cells that use it. The probe this was ported from has a `calls`
    # field too; every name it records is also added to `reads`, so leaving it
    # out here changes nothing. Verified block-for-block against the evaluated
    # strategy on all nine corpus notebooks.
    return set(cell.binds) | set(cell.reads) | set(cell.imports)


def _spans(starts: Sequence[int], first: int, last: int) -> list[tuple[int, int]]:
    ordered = sorted({start for start in starts if first <= start <= last} | {first})
    return [
        (start, (ordered[i + 1] - 1) if i + 1 < len(ordered) else last)
        for i, start in enumerate(ordered)
    ]


def _split_oversized(
    blocks: Sequence[tuple[int, int]], depths: Sequence[float], offset: int,
) -> list[tuple[int, int]]:
    """Halve anything over MAX_BLOCK at its weakest internal seam.

    The size rule is the panel's own; a segmenter that states it and then
    returns a 99-cell block has not applied it.
    """
    out: list[tuple[int, int]] = []
    queue = list(blocks)
    while queue:
        low, high = queue.pop(0)
        if high - low + 1 <= MAX_BLOCK:
            out.append((low, high))
            continue
        inner = range(low + MIN_BLOCK, high - MIN_BLOCK + 2)
        if not inner:
            out.append((low, high))
            continue
        cut = min(inner, key=lambda i: depths[i - offset - 1]) if depths else low + (high - low + 1) // 2
        queue = [(low, cut - 1), (cut, high)] + queue
    return sorted(out)


def _absorb_singletons(
    blocks: Sequence[tuple[int, int]], cells: Sequence[Cell],
) -> list[tuple[int, int]]:
    """Fold a lone cell into the neighbour it shares more vocabulary with.

    A one-cell block is worth a rail line only when the cell is a milestone;
    otherwise it costs a line and says nothing.
    """
    by_index = {cell.index: cell for cell in cells}
    out = list(blocks)
    changed = True
    while changed and len(out) > 1:
        changed = False
        for i, (low, high) in enumerate(out):
            if high - low + 1 >= MIN_BLOCK:
                continue
            cell = by_index.get(low)
            if cell is not None and cell.is_code and cell.milestone:
                continue
            if i == 0:
                out[1] = (out[0][0], out[1][1]); out.pop(0)
            elif i == len(out) - 1:
                out[-2] = (out[-2][0], out[-1][1]); out.pop()
            else:
                mine = _terms(by_index[low]) if low in by_index else set()
                before = set().union(*(_terms(by_index[j]) for j in range(*out[i - 1]) if j in by_index)) or set()
                after = set().union(*(_terms(by_index[j]) for j in range(*out[i + 1]) if j in by_index)) or set()
                if len(mine & before) >= len(mine & after):
                    out[i - 1] = (out[i - 1][0], high); out.pop(i)
                else:
                    out[i + 1] = (low, out[i + 1][1]); out.pop(i)
            changed = True
            break
    return out


def segment(cells: Sequence[Cell]) -> list[tuple[int, int]]:
    """Deterministic boundaries: cut where the notebook's vocabulary changes.

    Replaces the heading-and-milestone pass this used to be. That version was
    measured (docs/plans/probes/corpus/RESULTS.md) as a heading *follower* — 71
    to 90% of its boundaries sat on a markdown heading, and with headings
    removed it collapsed to three to five blocks regardless of length, putting
    154 cells in five blocks with one of 99. It cost a reader 21.5 cells to find
    something against 9.3 for this, and lost to cutting blindly every eight
    cells.

    This compares the identifiers used in the `COHESION_WINDOW` cells before
    each seam with those after it, and cuts at the valleys — gaps whose depth
    below the local peaks clears the mean, which adapts per notebook instead of
    hard-coding a similarity nobody can tune. Headings are not consulted at
    all — which is the point: the pass this replaces was 71-90% heading-driven,
    and the notebooks a map is *for* are the ones without them.

    On the corpus this is indistinguishable from what the model returns
    (paired Δ 0.0 cells, 95% CI [-1.5, +2.2]), which is why the model call is
    spent on naming instead.
    """
    if not cells:
        return []
    first, last = cells[0].index, cells[-1].index
    if len(cells) < 2 * COHESION_WINDOW:
        return [(first, last)]

    terms = [_terms(cell) for cell in cells]
    scores: list[float] = []
    for gap in range(len(cells) - 1):
        before: set[str] = set().union(*terms[max(0, gap - COHESION_WINDOW + 1):gap + 1]) or set()
        after: set[str] = set().union(*terms[gap + 1:gap + 1 + COHESION_WINDOW]) or set()
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

    positive = [depth for depth in depths if depth > 0]
    if not positive:
        return _split_oversized([(first, last)], depths, first)
    cut = mean(positive)
    starts = [first] + [cells[gap + 1].index for gap, depth in enumerate(depths) if depth >= cut]
    blocks = _split_oversized(_spans(starts, first, last), depths, first)
    return _absorb_singletons(blocks, cells)


def fallback_blocks(cells: Sequence[Cell]) -> tuple[Block, ...]:
    """The deterministic map, with names left empty rather than invented.

    Spec §6: say plainly that names are unavailable. A synthesised name like
    "Cells 1–44" would look like the generated field and claim something the
    panel does not know, so the field stays empty and the client renders the
    range instead.
    """
    ranges = segment(cells)
    return annotate(cells, ranges, [""] * len(ranges))
