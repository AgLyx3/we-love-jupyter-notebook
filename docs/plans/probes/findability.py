"""Score a map by whether it helps someone find a cell — the thing the panel is for.

The previous metric compared a strategy's boundaries to the model's. That is
agreement with a sampled partition: it drifts 12% between runs, and it is
structurally blind to whether the reference is any good. This does not use a
reference partition at all.

    1. Pick K target cells. Ask a model to write, for each, the question a
       reader would arrive with — phrased in the reader's words, not the cell's.
       Generated once per notebook and cached, so every strategy is scored on
       exactly the same questions.
    2. For a strategy: name its blocks (one call), then answer the questions
       from **the block list alone** (one call).
    3. Score: did the answer's block contain the target cell, and how many cells
       would you then have to read.

Step 2 is deliberately two calls, and the second one never sees code. Naming and
answering in one pass would let the model answer from the cells it was just
shown, which measures nothing — the reader only ever has the rail.

Naming is part of the map, so every strategy gets named by the same prompt. A
partition cannot be scored for findability without names; comparing an unnamed
partition to a named one would be scoring the names, not the split.

Run: python3 docs/plans/probes/findability.py                 # 3 notebooks x 5 strategies
     python3 docs/plans/probes/findability.py --questions 6
     python3 docs/plans/probes/findability.py --only messy-adspend
"""

from __future__ import annotations

import argparse
import json
import pathlib
import random
import re
import statistics
import sys

sys.path.insert(0, str(pathlib.Path(__file__).parent))
import extract  # noqa: E402
import segment as segment_probe  # noqa: E402
import strategies  # noqa: E402

HERE = pathlib.Path(__file__).parent
CORPUS = HERE / "corpus"
CACHE = CORPUS / "findability.json"

DEFAULT_NOTEBOOKS: list[str] | None = None   # None = every notebook in the corpus
DEFAULT_STRATEGIES = ["headings", "fixed8", "cohesion", "hybrid"]

QUESTION_PROMPT = """Below is a Jupyter notebook, one cell per entry.

For each of these cell indices — {targets} — write the question a reader would \
arrive at this notebook already wanting answered, whose answer is in that cell.

Rules:
- Write it as the reader would ask it, not as the cell describes itself. \
"Where do they decide how many bins the histogram uses?" not "Where is BINS set?".
- Do not quote identifiers, function names or literals that appear in the cell. \
The question must be answerable by someone who has not seen the code.
- One sentence. No preamble.

Return ONLY a JSON object mapping each index (as a string) to its question:
{{"12": "...", "40": "..."}}

Notebook:

{body}
"""

NAME_PROMPT = """Below is a Jupyter notebook, one cell per entry, followed by a \
segmentation of it into contiguous blocks.

Name each block with a short phrase (at most 8 words) saying what it does, in \
the notebook's own vocabulary — the column names, variables and domain words the \
code actually uses. Name the subject, not the activity: "Weekly revenue trend", \
not "Analyze weekly revenue trends". Never a bare category like "Data loading".

Return ONLY a JSON array of names, one per block, in order. No prose.

Blocks:
{blocks}

Notebook:

{body}
"""

ANSWER_PROMPT = """You are using a notebook's navigation panel. You cannot see \
the notebook — only this list of blocks.

{blocks}

For each question below, say which block you would open. Answer with the block \
number only. If genuinely nothing fits, answer -1.

Return ONLY a JSON object mapping each question number (as a string) to a block \
number: {{"1": 3, "2": 7}}

Questions:
{questions}
"""


def load(path: pathlib.Path) -> list[extract.Cell]:
    raw = json.loads(path.read_text())
    cells = [extract.Cell(i, c["cell_type"], "".join(c["source"]), c.get("execution_count"))
             for i, c in enumerate(raw["cells"])]
    for cell in cells:
        extract.analyse(cell)
    return cells


def parse_json(text: str, opener: str) -> object:
    closer = "}" if opener == "{" else "]"
    match = re.search(re.escape(opener) + r".*" + re.escape(closer), text, re.S)
    if not match:
        raise ValueError(f"no JSON {opener}{closer} in response: {text[:200]}")
    return json.loads(match.group(0))


def pick_targets(cells: list[extract.Cell], k: int, seed: int) -> list[int]:
    """Substantive code cells, spread across the notebook.

    Fixed seed so the question set is stable across runs; spread so a strategy
    is not scored entirely on the part of the notebook it happens to handle well.
    """
    candidates = [c.idx for c in cells
                  if c.kind == "code" and len(c.src.strip()) > 40 and (c.binds or c.calls)]
    if len(candidates) <= k:
        return candidates
    rng = random.Random(seed)
    buckets = [candidates[i * len(candidates) // k:(i + 1) * len(candidates) // k] for i in range(k)]
    return sorted(rng.choice(b) for b in buckets if b)


def block_list(blocks: list[tuple[int, int]], names: list[str] | None = None) -> str:
    out = []
    for i, (lo, hi) in enumerate(blocks, 1):
        label = f" — {names[i - 1]}" if names and i - 1 < len(names) else ""
        span = f"cell {lo + 1}" if lo == hi else f"cells {lo + 1}–{hi + 1}"
        out.append(f"{i}. {span}{label}")
    return "\n".join(out)


def name_blocks(cells, blocks, model) -> list[str]:
    text = segment_probe.ask(NAME_PROMPT.format(
        blocks=block_list(blocks), body=segment_probe.render(cells)), model)
    names = parse_json(text, "[")
    return [str(n) for n in names]


def score(cells, blocks, names, questions: dict[int, str], model) -> dict:
    ordered = sorted(questions)
    numbered = "\n".join(f"{i}. {questions[t]}" for i, t in enumerate(ordered, 1))
    text = segment_probe.ask(ANSWER_PROMPT.format(
        blocks=block_list(blocks, names), questions=numbered), model)
    answers = parse_json(text, "{")

    total = cells[-1].idx + 1
    hits, sizes, misses, costs = 0, [], [], []
    for i, target in enumerate(ordered, 1):
        choice = int(answers.get(str(i), -1))
        if not 1 <= choice <= len(blocks):
            misses.append((target, choice, questions[target]))
            costs.append(total)                    # no answer: the map saved nothing
            continue
        lo, hi = blocks[choice - 1]
        size = hi - lo + 1
        # Graded by distance, not binary. Strict containment of one sampled cell
        # punishes a map that points at the right region: the model's map for
        # madewithml was marked 25% wrong while choosing blocks whose names were
        # plainly correct, because the sampled cell sat one block over. Cost is
        # what a reader actually pays — the block they read, plus the distance
        # they then travel.
        distance = 0 if lo <= target <= hi else (lo - target if target < lo else target - hi)
        costs.append(size + distance)
        if distance == 0:
            hits += 1
            sizes.append(size)
        else:
            misses.append((target, choice, questions[target]))
    # Accuracy alone is not comparable across rail lengths — a one-block map
    # scores 100% and is useless, and `headings` scores 88% on a notebook where
    # one of its five blocks covers 99 cells. `cost` is the honest single
    # number; accuracy is kept because it is what makes cost interpretable.
    return {
        "accuracy": hits / len(ordered),
        "cells_to_read": statistics.mean(sizes) if sizes else None,
        "cost": statistics.mean(costs),
        # Per question, keyed by target cell. Every strategy answers the same
        # questions, so the comparison that matters is paired — two independent
        # means throw away exactly the variance that pairing removes.
        "per_question": {str(t): c for t, c in zip(ordered, costs)},
        "total_cells": total,
        "rail": len(blocks),
        "answers": {str(t): int(answers.get(str(i), -1)) for i, t in enumerate(ordered, 1)},
        "misses": misses,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--questions", type=int, default=16)
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--only", default=None)
    parser.add_argument("--model-name", default=None)
    parser.add_argument("--strategies", default=",".join(DEFAULT_STRATEGIES))
    args = parser.parse_args()

    corpus = json.loads((CORPUS / "corpus.json").read_text())
    paths = {e["id"]: CORPUS / ".cache" / f"{e['id']}.ipynb" for e in corpus["remote"]}
    paths |= {e["id"]: (CORPUS / e["path"]).resolve() for e in corpus["local"]}
    recorded = {r["notebook"]: r for r in json.loads((CORPUS / "baseline.json").read_text())}

    every = [e["id"] for e in corpus["remote"]] + [e["id"] for e in corpus["local"]]
    wanted = [args.only] if args.only else (DEFAULT_NOTEBOOKS or every)
    cache = json.loads(CACHE.read_text()) if CACHE.exists() else {}
    names = args.strategies.split(",")

    print(f"{'notebook':20s} {'strategy':12s} {'rail':>5} {'found':>7} {'read':>6} {'cost':>6}")
    print("-" * 66)
    results = {}

    for nb in wanted:
        path = paths.get(nb)
        if not path or not path.exists():
            print(f"{nb:20s} (missing — run corpus/fetch.py)")
            continue
        cells = load(path)
        entry = cache.setdefault(nb, {})

        if "questions" not in entry:
            targets = pick_targets(cells, args.questions, args.seed)
            text = segment_probe.ask(QUESTION_PROMPT.format(
                targets=", ".join(str(t) for t in targets),
                body=segment_probe.render(cells)), args.model_name)
            entry["questions"] = {str(k): v for k, v in parse_json(text, "{").items()}
            CACHE.write_text(json.dumps(cache, indent=2) + "\n")
        questions = {int(k): v for k, v in entry["questions"].items()}

        candidates: dict[str, list[tuple[int, int]]] = {
            n: strategies.STRATEGIES[n](cells) for n in names if n in strategies.STRATEGIES
        }
        if nb in recorded and "ranges" in recorded[nb].get("model", {}):
            candidates["model"] = [tuple(r) for r in recorded[nb]["model"]["ranges"]]

        for sname, blocks in candidates.items():
            slot = entry.setdefault(sname, {})
            if "names" not in slot:
                slot["names"] = name_blocks(cells, blocks, args.model_name)
                CACHE.write_text(json.dumps(cache, indent=2) + "\n")
            outcome = score(cells, blocks, slot["names"], questions, args.model_name)
            slot["score"] = {k: v for k, v in outcome.items() if k not in ("misses", "answers")}
            slot["answers"] = outcome["answers"]
            slot["misses"] = outcome["misses"]
            CACHE.write_text(json.dumps(cache, indent=2) + "\n")
            results[(nb, sname)] = outcome
            read = f"{outcome['cells_to_read']:.1f}" if outcome["cells_to_read"] else "—"
            print(f"{nb[:20]:20s} {sname:12s} {outcome['rail']:>5} "
                  f"{outcome['accuracy']:>6.0%} {read:>6} {outcome['cost']:>6.1f}")

    print("-" * 66)
    for sname in names + ["model"]:
        rows = [v for (nb, s), v in results.items() if s == sname]
        if not rows:
            continue
        acc = statistics.mean(r["accuracy"] for r in rows)
        reads = [r["cells_to_read"] for r in rows if r["cells_to_read"]]
        rail = statistics.mean(r["rail"] for r in rows)
        exp = statistics.mean(r["cost"] for r in rows)
        print(f"{'mean':20s} {sname:12s} {rail:>5.0f} {acc:>6.0%} "
              f"{statistics.mean(reads) if reads else 0:>6.1f} {exp:>6.1f}")


if __name__ == "__main__":
    main()
