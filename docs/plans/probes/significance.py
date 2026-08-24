"""Is the difference between two maps real, or is it 24 questions of noise?

The previous round reported `hybrid` 12.6 against `cohesion` 14.6 and then said,
correctly, that the gap was not resolved. This answers that properly rather than
by hedging.

Every strategy answers the *same* questions, so the comparison is **paired**:
for each question, the cost under A minus the cost under B. Comparing two
independent means throws away exactly the variance that pairing removes — the
per-question difficulty, which dominates here (a question about a cell in a
99-cell block is expensive under every map).

Reported per pair:
  Δ         mean paired difference in cells, negative = the first is cheaper
  95% CI    bootstrap over questions, 10k resamples, clustered by notebook
  wins      questions where the first strategy cost strictly less

Clustered because questions from one notebook are not independent — they share
its map, its length and its author's headings. Resampling notebooks rather than
questions is the conservative choice and roughly doubles the interval.

Run: python3 docs/plans/probes/significance.py
"""

from __future__ import annotations

import argparse
import json
import pathlib
import random
import statistics

CORPUS = pathlib.Path(__file__).parent / "corpus"


def paired(cache: dict, a: str, b: str) -> tuple[list[tuple[str, float]], list[float]]:
    """(notebook, difference) per question, and the per-notebook means."""
    rows: list[tuple[str, float]] = []
    per_nb: list[float] = []
    for nb, entry in cache.items():
        qa = entry.get(a, {}).get("score", {}).get("per_question")
        qb = entry.get(b, {}).get("score", {}).get("per_question")
        if not qa or not qb:
            continue
        diffs = [qa[k] - qb[k] for k in qa.keys() & qb.keys()]
        if not diffs:
            continue
        rows += [(nb, d) for d in diffs]
        per_nb.append(statistics.mean(diffs))
    return rows, per_nb


def bootstrap(per_nb: list[float], rounds: int, seed: int) -> tuple[float, float]:
    if len(per_nb) < 2:
        return (float("nan"), float("nan"))
    rng = random.Random(seed)
    means = []
    for _ in range(rounds):
        sample = [rng.choice(per_nb) for _ in per_nb]
        means.append(statistics.mean(sample))
    means.sort()
    return means[int(0.025 * rounds)], means[int(0.975 * rounds)]


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--rounds", type=int, default=10_000)
    parser.add_argument("--seed", type=int, default=11)
    args = parser.parse_args()

    cache = json.loads((CORPUS / "findability.json").read_text())
    names = ["headings", "fixed8", "cohesion", "hybrid", "model"]
    have = [n for n in names
            if any(n in e and "per_question" in e.get(n, {}).get("score", {}) for e in cache.values())]

    print("mean cost per strategy (cells a reader pays)\n")
    for n in have:
        costs = [c for e in cache.values()
                 for c in (e.get(n, {}).get("score", {}).get("per_question") or {}).values()]
        if costs:
            print(f"  {n:10s} {statistics.mean(costs):6.1f}   ({len(costs)} questions)")

    print("\npaired differences, 95% CI bootstrapped over notebooks\n")
    print(f"  {'A vs B':24s} {'Δ':>7} {'95% CI':>18} {'A wins':>8} {'n':>5}  verdict")
    print("  " + "-" * 78)
    for i, a in enumerate(have):
        for b in have[i + 1:]:
            rows, per_nb = paired(cache, a, b)
            if not rows:
                continue
            diffs = [d for _, d in rows]
            delta = statistics.mean(diffs)
            lo, hi = bootstrap(per_nb, args.rounds, args.seed)
            wins = sum(1 for d in diffs if d < 0)
            verdict = ("A cheaper" if hi < 0 else "B cheaper" if lo > 0 else "not resolved")
            print(f"  {a + ' vs ' + b:24s} {delta:>7.1f} {f'[{lo:+.1f}, {hi:+.1f}]':>18} "
                  f"{wins:>4}/{len(diffs):<4} {len(per_nb):>4}  {verdict}")
    print("\n  Δ is A − B in cells; negative means A is cheaper.")
    print("  A CI straddling zero means this corpus cannot separate them — which is")
    print("  a finding, not a failure to find one.")


if __name__ == "__main__":
    main()
