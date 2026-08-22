"""Tier 0/1 probe for the notebook overview panel (docs/plans/...-overview-research.md §12).

Computes, with no model involved, the five *computed* fields of a Block:
range, state, produces, defines, marks. The sixth — `name` — is the generated
one, and its absence here is the point: this script shows exactly what the
model would be handed, and what the deterministic fallback looks like alone.

Run: python3 docs/plans/probes/extract.py docs/plans/probes/*.ipynb
"""

from __future__ import annotations

import ast
import json
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass, field

# Import families → the notebook's kind. §4 argues this generalises where a
# per-domain keyword list does not.
KIND_IMPORTS = {
    "ml": {"sklearn", "torch", "xgboost", "lightgbm", "keras", "tensorflow"},
    "sim": {"scipy", "sympy", "pymc", "numba", "networkx"},
    "data": {"pandas", "polars", "pyarrow", "duckdb"},
    "viz": {"matplotlib", "seaborn", "plotly", "altair", "bokeh"},
}

# Milestone calls, per kind. Deliberately small — §4 concedes this is the weak part.
MILESTONES = {
    "ingest": {"read_csv", "read_parquet", "read_json", "read_sql", "open", "load", "loadtxt"},
    "model": {"fit", "train", "solve_ivp", "odeint", "minimize", "sample"},
    "evaluate": {"predict", "score", "mean_absolute_percentage_error", "accuracy_score"},
    "export": {"to_csv", "to_parquet", "save", "dump", "savefig"},
}


@dataclass
class Cell:
    idx: int
    kind: str
    src: str
    exec_count: int | None
    binds: set[str] = field(default_factory=set)
    reads: set[str] = field(default_factory=set)
    defines: list[str] = field(default_factory=list)
    calls: Counter = field(default_factory=Counter)
    imports: set[str] = field(default_factory=set)
    milestone: str | None = None
    #: Names bound *only* as a loop target. Python leaks these to module scope,
    #: so they are genuinely visible later — and genuinely noise. See produces().
    loop_binds: set[str] = field(default_factory=set)


def analyse(cell: Cell) -> None:
    if cell.kind != "code":
        return
    src = "\n".join(
        "" if line.lstrip().startswith(("!", "%")) else line
        for line in cell.src.split("\n")
    )
    try:
        tree = ast.parse(src)
    except SyntaxError:
        return

    # Bindings are collected scope-aware: a name assigned inside a function body
    # is a local, not something the block "produces". Walking with ast.walk
    # flattened that distinction and filled `produces` with loop counters and
    # unpacked locals (`I`, `r0`) that no other cell can see.
    def collect(node, *, top):
        for child in ast.iter_child_nodes(node):
            # Comprehensions carry their own scope in Python 3, so their targets
            # are not module-level either — `{r0: f(s) for r0, s in ...}` leaks
            # nothing. Omitting them here re-admitted the loop counters that
            # produces() exists to filter.
            nested = isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef,
                                        ast.Lambda, ast.ClassDef, ast.ListComp,
                                        ast.SetComp, ast.DictComp, ast.GeneratorExp))
            if isinstance(child, ast.Name):
                if isinstance(child.ctx, ast.Store):
                    if top:
                        cell.binds.add(child.id)
                else:
                    cell.reads.add(child.id)
                    # A function passed as a value (`solve_ivp(rhs, ...)`) is a
                    # use. Counting only ast.Call marked every callback dead.
                    cell.calls[child.id] += 0
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) and top:
                cell.defines.append(child.name)
                cell.binds.add(child.name)
            collect(child, top=top and not nested)

    collect(tree, top=True)

    for node in ast.walk(tree):
        if isinstance(node, (ast.For, ast.AsyncFor)):
            for t in ast.walk(node.target):
                if isinstance(t, ast.Name):
                    cell.loop_binds.add(t.id)

    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            cell.imports |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                cell.imports.add(node.module.split(".")[0])
        elif isinstance(node, ast.Call):
            fn = node.func
            name = fn.id if isinstance(fn, ast.Name) else fn.attr if isinstance(fn, ast.Attribute) else None
            if name:
                cell.calls[name] += 1
                cell.reads.add(name)
                for label, names in MILESTONES.items():
                    if name in names and cell.milestone is None:
                        cell.milestone = label


def produces(members: list[Cell], later: list[Cell], limit: int = 4) -> list[str]:
    """Variables this block makes that something later reads, most-read first.

    Two filters, both learned from running this against real notebooks (§13.7):

    * A name bound *only* as a loop target is dropped. `for r0 in grid:` really
      does bind `r0` at module scope, so reporting it is correct — and useless.
      A block advertising `produces beta, ip, r0, s` buries the two names that
      matter. Correct and useless is still useless.
    * The rest are ranked by how many later cells read them and capped, so the
      block leads with what the notebook actually depends on it for.
    """
    if not members:
        return []
    bound = set().union(*(c.binds for c in members))
    loop_only = set().union(*(c.loop_binds for c in members)) - set().union(
        *((c.binds - c.loop_binds) for c in members)
    )
    demand = Counter()
    for c in later:
        for name in c.reads:
            demand[name] += 1
    candidates = [b for b in bound - loop_only if demand[b] and not b.startswith("_")]
    return sorted(candidates, key=lambda n: (-demand[n], n))[:limit]


def notebook_kind(cells: list[Cell]) -> str:
    seen = set().union(*(c.imports for c in cells)) if cells else set()
    for kind in ("ml", "sim"):
        if seen & KIND_IMPORTS[kind]:
            return kind
    return "data" if seen & KIND_IMPORTS["data"] else "unknown"


def segment(cells: list[Cell]) -> list[tuple[int, int]]:
    """Boundaries: markdown headings if any, else milestone transitions."""
    starts = [0]
    for c in cells[1:]:
        if c.kind == "markdown" and c.src.lstrip().startswith("#"):
            starts.append(c.idx)
        elif c.milestone and c.milestone != "evaluate":
            prev = [p for p in cells[: c.idx] if p.kind == "code" and p.milestone]
            if not prev or prev[-1].milestone != c.milestone:
                starts.append(c.idx)
    starts = sorted(set(starts))
    return [(s, (starts[i + 1] - 1) if i + 1 < len(starts) else cells[-1].idx)
            for i, s in enumerate(starts)]


def run(path: str) -> None:
    raw = json.load(open(path))
    cells = [
        Cell(i, c["cell_type"], "".join(c["source"]), c.get("execution_count"))
        for i, c in enumerate(raw["cells"])
    ]
    for c in cells:
        analyse(c)

    code = [c for c in cells if c.kind == "code"]
    counts = [c.exec_count for c in code if c.exec_count is not None]
    out_of_order = any(b < a for a, b in zip(counts, counts[1:]))
    headings = [c for c in cells if c.kind == "markdown" and c.src.lstrip().startswith("#")]
    all_defs = {d: c.idx for c in code for d in c.defines}
    call_sites = defaultdict(list)
    for c in code:
        # `reads` rather than `calls`: a function referenced without being called
        # is still used. See the note in analyse().
        for name in c.reads:
            if name in all_defs and c.idx != all_defs[name]:
                call_sites[name].append(c.idx)

    print(f"\n\033[1m{path}\033[0m")
    print(f"  {len(cells)} cells ({len(code)} code) · kind={notebook_kind(code)} · "
          f"{len(headings)} headings · {len(all_defs)} defs · "
          f"{'OUT OF ORDER' if out_of_order else 'in order'} · "
          f"{sum(1 for c in code if c.exec_count is None)} never run")

    blocks = segment(cells)
    print(f"  \033[1m{len(blocks)} blocks\033[0m from the deterministic pass:\n")

    for lo, hi in blocks:
        members = [c for c in cells if lo <= c.idx <= hi]
        mcode = [c for c in members if c.kind == "code"]
        made = produces(mcode, [c for c in code if c.idx > hi])
        defs = [d for c in mcode for d in c.defines]
        marks = [c.src.strip().split("\n")[0] for c in members
                 if c.kind == "markdown" and c.src.lstrip().startswith("#")]
        never = sum(1 for c in mcode if c.exec_count is None)

        state = "ok"
        if never == len(mcode) and mcode:
            state = "never-run"
        elif never:
            state = f"{never} never run"

        print(f"    cells {lo:>2}–{hi:<2}  [{state}]")
        print(f"      produces  {', '.join(made) or '—'}")
        if defs:
            print(f"      defines   " + ", ".join(
                f"{d}() used in {call_sites[d] or 'nowhere'}" for d in defs))
        if marks:
            print(f"      marks     {' | '.join(marks)}")
        print()


if __name__ == "__main__":
    for p in sys.argv[1:]:
        run(p)
