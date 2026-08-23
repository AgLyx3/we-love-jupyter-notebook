"""Score the notebook overview map across the corpus (docs/plans/probes/corpus/).

Two passes, and the interesting number is the gap between them:

  deterministic  extract.segment() — free, no model
  model          segment.py — one call per notebook, costs real tokens (--model)

The metric this exists for is **heading dependence**. extract.segment() opens a
new block at every markdown heading, so on a notebook whose author already wrote
one heading per section it will look excellent while having decided nothing. The
`hdep` column is the fraction of block boundaries that sit on a heading, and
`no-md` re-runs the same segmentation with every heading stripped: the drop
between `blocks` and `no-md` is how much structure the pass finds on its own.

The other columns are the segmenter's own rules from the research doc, turned
into numbers so a change can be seen rather than argued about:
  1-cell   blocks of a single cell        ("avoid unless a genuine milestone")
  >12      blocks longer than 12 cells    ("avoid")
  cv       stdev/mean of block size       (evenness — 0 means uniform)
  valid    the partition covers every cell exactly once, in order

Run: python3 docs/plans/probes/corpus/fetch.py
     python3 docs/plans/probes/evaluate.py
     python3 docs/plans/probes/evaluate.py --model            # spends tokens
     python3 docs/plans/probes/evaluate.py --only madewithml
"""

from __future__ import annotations

import argparse
import copy
import json
import pathlib
import statistics
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).parent))
import extract  # noqa: E402
import segment as segment_probe  # noqa: E402

HERE = pathlib.Path(__file__).parent
CORPUS = HERE / "corpus"


def load(path: pathlib.Path) -> list[extract.Cell]:
    raw = json.loads(path.read_text())
    cells = [
        extract.Cell(i, c["cell_type"], "".join(c["source"]), c.get("execution_count"))
        for i, c in enumerate(raw["cells"])
    ]
    for cell in cells:
        extract.analyse(cell)
    return cells


def strip_headings(cells: list[extract.Cell]) -> list[extract.Cell]:
    """The same notebook with its markdown demoted to prose.

    Not deleted — deleting would renumber every cell and change the answer for
    reasons that have nothing to do with segmentation. The heading marker is
    what segment() keys on, so removing just that isolates it.
    """
    out = copy.deepcopy(cells)
    for cell in out:
        if cell.kind == "markdown":
            cell.src = cell.src.lstrip().lstrip("#").lstrip()
    return out


def shape(blocks: list[tuple[int, int]], last: int, cells: list[extract.Cell]) -> dict:
    sizes = [hi - lo + 1 for lo, hi in blocks]
    heading_at = {c.idx for c in cells
                  if c.kind == "markdown" and c.src.lstrip().startswith("#")}
    boundaries = [lo for lo, _ in blocks][1:]  # cell 0 is not a decision
    covered: list[int] = []
    for lo, hi in blocks:
        covered.extend(range(lo, hi + 1))
    return {
        "blocks": len(blocks),
        "min": min(sizes) if sizes else 0,
        "median": statistics.median(sizes) if sizes else 0,
        "max": max(sizes) if sizes else 0,
        "cv": (statistics.pstdev(sizes) / statistics.mean(sizes)) if len(sizes) > 1 else 0.0,
        "one_cell": sum(1 for s in sizes if s == 1),
        "over_12": sum(1 for s in sizes if s > 12),
        "hdep": (sum(1 for b in boundaries if b in heading_at) / len(boundaries)) if boundaries else 0.0,
        "valid": covered == list(range(last + 1)),
    }


ROW = "{name:24s} {cells:>5} {heads:>6} {blocks:>7} {min:>4} {median:>7} {max:>4} {cv:>6} {one:>7} {over:>5} {hdep:>6} {nomd:>6} {valid:>6}"


def header() -> None:
    print(ROW.format(name="notebook", cells="cells", heads="head", blocks="blocks",
                     min="min", median="median", max="max", cv="cv",
                     one="1-cell", over=">12", hdep="hdep", nomd="no-md", valid="valid"))
    print("-" * 118)


def evaluate(name: str, path: pathlib.Path, use_model: bool, model: str | None) -> dict:
    cells = load(path)
    last = cells[-1].idx
    headings = sum(1 for c in cells if c.kind == "markdown" and c.src.lstrip().startswith("#"))

    det = shape(extract.segment(cells), last, cells)
    stripped = strip_headings(cells)
    no_md = len(extract.segment(stripped))

    print(ROW.format(
        name=name[:24], cells=len(cells), heads=headings, blocks=det["blocks"],
        min=det["min"], median=f"{det['median']:g}", max=det["max"], cv=f"{det['cv']:.2f}",
        one=det["one_cell"], over=det["over_12"], hdep=f"{det['hdep']:.0%}", nomd=no_md,
        valid="ok" if det["valid"] else "BROKEN"))

    row = {"notebook": name, "cells": len(cells), "headings": headings,
           "deterministic": det, "deterministic_no_headings": no_md}

    if use_model:
        started = time.time()
        try:
            # PROMPT takes both fields — {body} is where the cells go. Passing
            # only `last` and concatenating leaves {body} unformatted, which
            # fails as a KeyError before the CLI is ever reached.
            prompt = segment_probe.PROMPT.format(last=last, body=segment_probe.render(cells))
            text = segment_probe.ask(prompt, model)
            blocks = segment_probe.parse(text)
            problems = segment_probe.validate(blocks, last)
            ranges = [(b["start"], b["end"]) for b in sorted(blocks, key=lambda b: b["start"])]
            mod = shape(ranges, last, cells)
            mod["problems"] = problems
            mod["seconds"] = round(time.time() - started, 1)
            mod["names"] = [b["name"] for b in sorted(blocks, key=lambda b: b["start"])]
        except Exception as error:
            mod = {"error": f"{type(error).__name__}: {error}",
                   "seconds": round(time.time() - started, 1)}
        row["model"] = mod
        if "error" in mod:
            print(ROW.format(name="  └ model", cells="", heads="", blocks="—", min="", median="",
                             max="", cv="", one="", over="", hdep="", nomd="", valid="ERROR"))
            print(f"      {mod['error']}")
        else:
            print(ROW.format(
                name="  └ model", cells="", heads="", blocks=mod["blocks"], min=mod["min"],
                median=f"{mod['median']:g}", max=mod["max"], cv=f"{mod['cv']:.2f}",
                one=mod["one_cell"], over=mod["over_12"], hdep=f"{mod['hdep']:.0%}", nomd="",
                valid="ok" if not mod["problems"] else "BROKEN"))
    return row


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--model", action="store_true", help="also run the model pass (spends tokens)")
    parser.add_argument("--model-name", default=None, help="passed through to segment.py")
    parser.add_argument("--only", default=None, help="one corpus id")
    parser.add_argument("--json", default=None, help="write the full result to this path")
    args = parser.parse_args()

    corpus = json.loads((CORPUS / "corpus.json").read_text())
    entries: list[tuple[str, pathlib.Path]] = []
    for entry in corpus["remote"]:
        entries.append((entry["id"], CORPUS / ".cache" / f"{entry['id']}.ipynb"))
    for entry in corpus["local"]:
        entries.append((entry["id"], (CORPUS / entry["path"]).resolve()))
    if args.only:
        entries = [e for e in entries if e[0] == args.only]

    missing = [name for name, path in entries if not path.exists()]
    if missing:
        print(f"missing (run corpus/fetch.py): {', '.join(missing)}\n", file=sys.stderr)
    entries = [(n, p) for n, p in entries if p.exists()]

    header()
    rows = [evaluate(name, path, args.model, args.model_name) for name, path in entries]

    if args.json:
        pathlib.Path(args.json).write_text(json.dumps(rows, indent=2) + "\n")
        print(f"\nwrote {args.json}")


if __name__ == "__main__":
    main()
