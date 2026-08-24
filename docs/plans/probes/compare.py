"""Score every candidate split against the model's partition.

There is no ground truth for "the right blocks", so this uses the closest thing
we have: the map the model returns, recorded in corpus/baseline.json. That is
not a claim the model is correct — it is that the model's partition is what the
panel actually shows when someone presses Build map, so a free strategy is
useful exactly to the extent it lands in the same places.

Boundaries, not blocks, are the unit: two partitions that agree everywhere but
one cut should score near 1, and block-level exact match would score them 0.
Tolerance ±1 cell, because a boundary one cell early is a judgement call about
where a markdown cell belongs, not a different reading of the notebook.

Run: python3 docs/plans/probes/compare.py
     python3 docs/plans/probes/compare.py --tolerance 0
"""

from __future__ import annotations

import argparse
import json
import pathlib
import statistics
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent))
import extract  # noqa: E402
import strategies  # noqa: E402

HERE = pathlib.Path(__file__).parent
CORPUS = HERE / "corpus"


def load(path: pathlib.Path) -> list[extract.Cell]:
    raw = json.loads(path.read_text())
    cells = [extract.Cell(i, c["cell_type"], "".join(c["source"]), c.get("execution_count"))
             for i, c in enumerate(raw["cells"])]
    for cell in cells:
        extract.analyse(cell)
    return cells


def boundaries(blocks: list[tuple[int, int]]) -> set[int]:
    """Where a block starts, excluding cell 0 — that one is not a decision."""
    return {lo for lo, _ in blocks if lo > 0}


def f1(predicted: set[int], reference: set[int], tolerance: int) -> tuple[float, float, float]:
    if not predicted and not reference:
        return 1.0, 1.0, 1.0
    hit = lambda b, other: any(abs(b - o) <= tolerance for o in other)  # noqa: E731
    tp_p = sum(1 for b in predicted if hit(b, reference))
    tp_r = sum(1 for b in reference if hit(b, predicted))
    precision = tp_p / len(predicted) if predicted else 0.0
    recall = tp_r / len(reference) if reference else 0.0
    return precision, recall, (2 * precision * recall / (precision + recall)) if precision + recall else 0.0


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tolerance", type=int, default=1)
    parser.add_argument("--baseline", default=str(CORPUS / "baseline.json"))
    args = parser.parse_args()

    recorded = {row["notebook"]: row for row in json.loads(pathlib.Path(args.baseline).read_text())}
    corpus = json.loads((CORPUS / "corpus.json").read_text())
    entries = [(e["id"], CORPUS / ".cache" / f"{e['id']}.ipynb") for e in corpus["remote"]]
    entries += [(e["id"], (CORPUS / e["path"]).resolve()) for e in corpus["local"]]
    entries = [(n, p) for n, p in entries if p.exists() and n in recorded
               and "model" in recorded[n] and "error" not in recorded[n]["model"]]

    names = list(strategies.STRATEGIES)
    print(f"F1 of block boundaries against the model's partition (±{args.tolerance} cell)\n")
    print("notebook              cells  head  " + "  ".join(f"{n:>10}" for n in names))
    print("-" * (30 + 12 * len(names)))

    totals: dict[str, list[float]] = {n: [] for n in names}
    hardest: dict[str, list[float]] = {n: [] for n in names}

    for name, path in entries:
        cells = load(path)
        head = sum(1 for c in cells if c.kind == "markdown" and c.src.lstrip().startswith("#"))
        model = recorded[name]["model"]
        if "ranges" not in model:
            print(f"{name:22s}  (baseline has no ranges — re-run evaluate.py --model)")
            continue
        ref = boundaries([tuple(r) for r in model["ranges"]])
        row = []
        for strategy in names:
            blocks = strategies.STRATEGIES[strategy](cells)
            _, _, score = f1(boundaries(blocks), ref, args.tolerance)
            totals[strategy].append(score)
            if head <= 2:
                hardest[strategy].append(score)
            row.append(f"{score:>10.2f}")
        print(f"{name[:22]:22s} {len(cells):>5} {head:>5}  " + "  ".join(row))

    print("-" * (30 + 12 * len(names)))
    print(f"{'mean':22s} {'':>5} {'':>5}  " +
          "  ".join(f"{statistics.mean(totals[n]):>10.2f}" if totals[n] else f"{'—':>10}" for n in names))
    print(f"{'mean, ≤2 headings':22s} {'':>5} {'':>5}  " +
          "  ".join(f"{statistics.mean(hardest[n]):>10.2f}" if hardest[n] else f"{'—':>10}" for n in names))


if __name__ == "__main__":
    main()
