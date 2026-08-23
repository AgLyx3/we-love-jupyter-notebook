"""Tier 2: ask a model for the boundaries and the names, and refuse bad answers.

The prompt is `docs/plans/probes/segment.py`'s, verbatim. It is validated
against both fixtures and two model tiers, and two of its clauses were earned
rather than guessed (spec §4.1):

* *"Name the subject, not the activity"*, with the explicit ban on opening with
  Analyze / Explore / Visualize / Process / Handle / Perform / Compute. Without
  it Haiku drifts to categorical names (research §13.6) — exactly the register
  the stage taxonomy was cut to avoid.
* *"Markdown headings are a hint, not an instruction."* This is what makes
  headings annotation rather than structure.

Two things this module will not do. It sends **cell source only** — never
outputs, at any size, for any reason (research §1). And it goes through
`agent_workspace/adapters.py` rather than running `claude` itself; the probe
shells out because it is a probe.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from threading import Event
from typing import Sequence

from ..agent_workspace.models import AgentAdapterError
from .analysis import Cell
from .models import NotebookTooLarge, SegmentationRejected

#: Haiku by default: research §13.6 measured it returning a structurally
#: equivalent partition to the default model — same block count, same median,
#: largest block one cell smaller — on the fixture that motivated the feature.
DEFAULT_MODEL = "haiku"

#: Per-cell source budget before head/tail truncation (spec §4.1).
CELL_CHAR_LIMIT = 1200
CELL_HEAD_CHARS = 600
CELL_TAIL_CHARS = 400

#: Above this the single-pass design stops being defined (spec §10.2). Refusing
#: is honest; segmenting a prefix and calling it the map is not.
MAX_CELLS = 500

#: How long one segmentation call may take. Generous because it is a single
#: explicit user action, not something on the open path.
TIMEOUT_SECONDS = 300.0

#: A generated name is truncated for display rather than trusted to be short
#: (spec §4.3.4). Enforced here so no consumer has to remember to.
NAME_LIMIT = 90

PROMPT = """You are segmenting a Jupyter notebook into contiguous blocks for a \
navigation panel. A reader opens this panel to answer "what is in this notebook, \
and where is the part I want".

Rules:
- Blocks are contiguous ranges of cell indices and must partition the notebook: \
every index from 0 to {last} appears in exactly one block, in order, no gaps, no \
overlaps.
- Name each block with a short phrase (at most 8 words) saying what it does, in \
the notebook's own vocabulary — the column names, variables and domain words the \
code actually uses.
- Name the *subject*, not the activity. Write "Weekly revenue trend", not \
"Analyze weekly revenue trends"; "Monthly revenue heatmap by region", not \
"Visualize monthly patterns". Avoid opening with Analyze, Explore, Visualize, \
Process, Handle, Perform or Compute unless the code's own vocabulary uses that \
word. Never use a bare category like "Data loading" or "Modelling".
- Prefer blocks a reader would think of as one step. Avoid single-cell blocks \
unless the cell is a genuine milestone. Avoid blocks longer than about 12 cells.
- Markdown headings are a hint about the author's intent, not an instruction. \
Override them where the code disagrees.

Return ONLY a JSON array, no prose, no code fence:
[{{"start": 0, "end": 4, "name": "..."}}, ...]

Notebook cells:

{body}
"""


@dataclass(frozen=True)
class Segmentation:
    """A validated partition: parallel ranges and names, in document order."""

    ranges: tuple[tuple[int, int], ...]
    names: tuple[str, ...]


def render(cells: Sequence[Cell]) -> str:
    """The notebook as the model sees it: index, kind, and source. No outputs.

    Truncation is head/tail rather than head-only so the tail of a long cell —
    where the result usually gets assigned — still reaches the model.
    """
    parts = []
    for cell in cells:
        tag = "markdown" if cell.kind == "markdown" else "code"
        source = cell.source.strip()
        if len(source) > CELL_CHAR_LIMIT:
            source = (
                source[:CELL_HEAD_CHARS]
                + "\n    # ... truncated ...\n"
                + source[-CELL_TAIL_CHARS:]
            )
        parts.append(f"[{cell.index}] ({tag})\n{source}")
    return "\n\n".join(parts)


def build_prompt(cells: Sequence[Cell]) -> str:
    if len(cells) > MAX_CELLS:
        raise NotebookTooLarge(len(cells), MAX_CELLS)
    return PROMPT.format(last=len(cells) - 1, body=render(cells))


def parse(text: str) -> list[dict]:
    """Pull the JSON array out of a response that may carry prose around it.

    A non-greedy match would stop at the first `]`, which is any nested list in
    a name; a greedy one spans from the first `[` to the last `]`. Neither is
    robust against a model writing prose containing brackets, so failure here is
    a rejection like any other rather than something to paper over.
    """
    match = re.search(r"\[.*\]", text, re.S)
    if not match:
        raise SegmentationRejected(["The model did not return a JSON array."])
    try:
        payload = json.loads(match.group(0))
    except json.JSONDecodeError as error:
        raise SegmentationRejected([f"The model's JSON was malformed: {error}."]) from error
    if not isinstance(payload, list):
        raise SegmentationRejected(["The model did not return a JSON array."])
    return payload


def validate(blocks: Sequence[dict], last: int) -> list[str]:
    """The check that runs before anything is rendered (spec §4.3).

    Ported from the probe. Every index 0..last covered exactly once, all indices
    in range, blocks in document order, names non-empty. A response failing any
    of these is discarded — three probe runs across two notebooks and two models
    each produced a valid partition first try, so this should be rare, but
    "rare" is why it is handled rather than assumed away.
    """
    problems: list[str] = []
    covered: list[int] = []
    for block in blocks:
        start, end = block.get("start"), block.get("end")
        if not isinstance(start, int) or not isinstance(end, int) \
                or isinstance(start, bool) or isinstance(end, bool):
            problems.append(f"non-integer cell range: {start!r}–{end!r}")
            continue
        if not 0 <= start <= end <= last:
            problems.append(f"out of range: {start}–{end}")
            continue
        covered += list(range(start, end + 1))
        name = block.get("name")
        if not isinstance(name, str) or not name.strip():
            problems.append(f"block {start}–{end} has no name")
    seen = set(covered)
    if len(covered) != len(seen):
        counts = {index: covered.count(index) for index in seen}
        dupes = sorted(index for index, count in counts.items() if count > 1)
        problems.append(f"overlapping cells: {dupes[:10]}")
    missing = sorted(set(range(last + 1)) - seen)
    if missing:
        problems.append(f"uncovered cells: {missing[:10]}")
    ordered = sorted(blocks, key=lambda block: block.get("start", 0))
    if list(blocks) != ordered:
        problems.append("blocks not in document order")
    if not blocks:
        problems.append("no blocks returned")
    return problems


def segment(
    cells: Sequence[Cell], adapter, *, model: str | None = DEFAULT_MODEL,
    cancel_event: Event | None = None, timeout: float = TIMEOUT_SECONDS,
) -> Segmentation:
    """Ask the model to partition the notebook, and refuse anything that is not.

    Raises `SegmentationRejected` when the answer is not a valid partition, and
    lets the adapter's own errors — CLI missing, unsupported version, timeout —
    through unchanged so the caller can tell "no model available" apart from
    "the model answered badly". They lead to different things being said.
    """
    prompt = build_prompt(cells)
    result = adapter.run_prompt(
        prompt, timeout=timeout, cancel_event=cancel_event or Event(), model=model,
    )
    blocks = parse(result.final_output)
    problems = validate(blocks, len(cells) - 1)
    if problems:
        raise SegmentationRejected(problems)
    return Segmentation(
        ranges=tuple((block["start"], block["end"]) for block in blocks),
        names=tuple(block["name"].strip()[:NAME_LIMIT] for block in blocks),
    )


__all__ = [
    "AgentAdapterError", "DEFAULT_MODEL", "MAX_CELLS", "PROMPT", "Segmentation",
    "build_prompt", "parse", "render", "segment", "validate",
]
