"""Tier 2 probe: does a model beat the deterministic segmenter (§13.1)?

Sends cell source only — no outputs, per §1 — and asks for contiguous blocks
with generated names. Then validates the response the way the real panel would
have to (§6.2): every cell covered exactly once, indices in range, no overlaps.
A model that cannot return a valid partition is not usable for this feature, so
the validation is the experiment as much as the names are.

Run: python3 docs/plans/probes/segment.py docs/plans/probes/messy-exploration.ipynb
     python3 docs/plans/probes/segment.py --model haiku <notebook>
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys

import extract

PROMPT = """You are segmenting a Jupyter notebook into contiguous blocks for a \
navigation panel. A reader opens this panel to answer "what is in this notebook, \
and where is the part I want".

Rules:
- Blocks are contiguous ranges of cell indices and must partition the notebook: \
every index from 0 to {last} appears in exactly one block, in order, no gaps, no \
overlaps.
- Name each block with a short phrase (at most 8 words) saying what it does, in \
the notebook's own vocabulary. Not a category like "data loading" — say what this \
particular code does, e.g. "Load three years of store sales".
- Prefer blocks a reader would think of as one step. Avoid single-cell blocks \
unless the cell is a genuine milestone. Avoid blocks longer than about 12 cells.
- Markdown headings are a hint about the author's intent, not an instruction. \
Override them where the code disagrees.

Return ONLY a JSON array, no prose, no code fence:
[{{"start": 0, "end": 4, "name": "..."}}, ...]

Notebook cells:

{body}
"""


def render(cells: list[extract.Cell]) -> str:
    out = []
    for c in cells:
        tag = "markdown" if c.kind == "markdown" else "code"
        src = c.src.strip()
        if len(src) > 1200:
            src = src[:600] + "\n    # ... truncated ...\n" + src[-400:]
        out.append(f"[{c.idx}] ({tag})\n{src}")
    return "\n\n".join(out)


def ask(prompt: str, model: str | None) -> str:
    cmd = ["claude", "-p", "--strict-mcp-config", "--disable-slash-commands"]
    if model:
        cmd += ["--model", model]
    res = subprocess.run(cmd, input=prompt, capture_output=True, text=True, timeout=600)
    if res.returncode != 0:
        sys.exit(f"claude failed ({res.returncode}): {res.stderr[:400]}")
    return res.stdout.strip()


def parse(text: str) -> list[dict]:
    match = re.search(r"\[.*\]", text, re.S)
    if not match:
        sys.exit(f"no JSON array in response:\n{text[:400]}")
    return json.loads(match.group(0))


def validate(blocks: list[dict], last: int) -> list[str]:
    """The check the real panel would run before rendering anything."""
    problems = []
    covered: list[int] = []
    for b in blocks:
        if not (0 <= b["start"] <= b["end"] <= last):
            problems.append(f"out of range: {b['start']}–{b['end']}")
            continue
        covered += list(range(b["start"], b["end"] + 1))
    seen = set(covered)
    if len(covered) != len(seen):
        dupes = sorted({i for i in covered if covered.count(i) > 1})
        problems.append(f"overlapping cells: {dupes[:10]}")
    missing = sorted(set(range(last + 1)) - seen)
    if missing:
        problems.append(f"uncovered cells: {missing[:10]}")
    if blocks != sorted(blocks, key=lambda b: b["start"]):
        problems.append("blocks not in document order")
    return problems


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("notebook")
    ap.add_argument("--model", default=None, help="haiku | sonnet | opus")
    args = ap.parse_args()

    raw = json.load(open(args.notebook))
    cells = [
        extract.Cell(i, c["cell_type"], "".join(c["source"]), c.get("execution_count"))
        for i, c in enumerate(raw["cells"])
    ]
    for c in cells:
        extract.analyse(c)
    last = len(cells) - 1

    prompt = PROMPT.format(last=last, body=render(cells))
    print(f"{args.notebook} · {len(cells)} cells · model={args.model or 'default'} · "
          f"prompt ~{len(prompt)//4:,} tokens\n")

    blocks = parse(ask(prompt, args.model))
    problems = validate(blocks, last)

    print(f"  {len(blocks)} blocks returned"
          + ("  \033[31m[INVALID]\033[0m" if problems else "  \033[32m[valid partition]\033[0m"))
    for p in problems:
        print(f"    ! {p}")
    print()

    code_by_idx = {c.idx: c for c in cells if c.kind == "code"}
    all_defs = {d: c.idx for c in code_by_idx.values() for d in c.defines}

    for b in blocks:
        members = [code_by_idx[i] for i in range(b["start"], b["end"] + 1) if i in code_by_idx]
        bound = set().union(*(c.binds for c in members)) if members else set()
        later = set().union(
            *(c.reads for c in code_by_idx.values() if c.idx > b["end"])
        ) if any(c.idx > b["end"] for c in code_by_idx.values()) else set()
        produces = sorted(bound & later)
        defs = [d for c in members for d in c.defines]
        never = sum(1 for c in members if c.exec_count is None)
        span = b["end"] - b["start"] + 1

        state = " [never run]" if members and never == len(members) else ""
        print(f"    {b['start']:>2}–{b['end']:<2} ({span:>2})  {b['name']}{state}")
        if produces:
            print(f"              produces {', '.join(produces[:6])}")
        if defs:
            print(f"              defines  {', '.join(d + '()' for d in defs)}")

    spans = [b["end"] - b["start"] + 1 for b in blocks]
    print(f"\n  largest block: {max(spans)} cells · median {sorted(spans)[len(spans)//2]}")


if __name__ == "__main__":
    main()
