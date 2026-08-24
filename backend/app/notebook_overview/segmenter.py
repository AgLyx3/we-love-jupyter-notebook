"""Tier 2: ask a model to *name* blocks the analyser has already drawn.

It used to ask for the boundaries too. Measured (#36), that was not worth
buying: a model partition and the deterministic cohesion pass are
indistinguishable on what a reader pays to find a cell — paired Δ 0.0 cells,
95% CI [-1.5, +2.2] over nine notebooks. What the model is uniquely good at is
the naming, which the fallback cannot do at all and says so.

Handing it fixed ranges is worth more than the call it saves:

* An invalid partition becomes structurally impossible. There is no longer a
  gap, overlap or out-of-order block to detect, so the whole class of answer
  that used to be discarded after a full-notebook call cannot occur.
* The map stops moving. Two identical runs of the old prompt returned block
  counts differing by 12% on average and 40% on one notebook; blocks are now
  deterministic and only the names vary.
* The task gets easier. Naming N given ranges is a smaller ask than
  partitioning and naming at once — and the notebook where the model's own
  partition was worst (`madewithml`, 243 cells) is exactly where partitioning
  is hardest.

The naming rules below are `docs/plans/probes/segment.py`'s, verbatim. It is validated
against both fixtures and two model tiers, and two of its clauses were earned
rather than guessed (spec §4.1):

* *"Name the subject, not the activity"*, with the explicit ban on opening with
  Analyze / Explore / Visualize / Process / Handle / Perform / Compute. Without
  it Haiku drifts to categorical names (research §13.6) — exactly the register
  the stage taxonomy was cut to avoid.
* *"Markdown headings are a hint, not an instruction."* This is what makes
  headings annotation rather than structure. It survives the change of job:
  the model no longer draws boundaries, but it still must not let a heading
  dictate a name that the code inside the block contradicts.

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
from . import analysis
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

PROMPT = """You are naming the blocks of a Jupyter notebook for a navigation \
panel. A reader opens this panel to answer "what is in this notebook, and where \
is the part I want".

The blocks are already decided. Do not change them, merge them or split them.

Rules:
- Name each block with a short phrase (at most 8 words) saying what it does, in \
the notebook's own vocabulary — the column names, variables and domain words the \
code actually uses.
- Name the *subject*, not the activity. Write "Weekly revenue trend", not \
"Analyze weekly revenue trends"; "Monthly revenue heatmap by region", not \
"Visualize monthly patterns". Avoid opening with Analyze, Explore, Visualize, \
Process, Handle, Perform or Compute unless the code's own vocabulary uses that \
word. Never use a bare category like "Data loading" or "Modelling".
- Markdown headings are a hint about the author's intent, not an instruction. \
Where the code inside a block disagrees with a heading it contains, name what \
the code does.

Return ONLY a JSON array of {count} strings, one per block, in order. No prose, \
no code fence, no object keys:
["...", "..."]

Blocks:
{blocks}

Notebook cells:

{body}
"""


@dataclass(frozen=True)
class Segmentation:
    """Ranges and names, in document order.

    The ranges come from `analysis.segment()` and the names from the model, so
    this is no longer "a validated partition" — the partition is not the
    model's to invalidate.
    """

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


def describe(ranges: Sequence[tuple[int, int]]) -> str:
    """The blocks as the model sees them: one line each, 1-based like the UI."""
    lines = []
    for position, (start, end) in enumerate(ranges, 1):
        span = f"cell {start + 1}" if start == end else f"cells {start + 1}-{end + 1}"
        lines.append(f"{position}. {span}")
    return "\n".join(lines)


def build_prompt(cells: Sequence[Cell], ranges: Sequence[tuple[int, int]]) -> str:
    if len(cells) > MAX_CELLS:
        raise NotebookTooLarge(len(cells), MAX_CELLS)
    return PROMPT.format(count=len(ranges), blocks=describe(ranges), body=render(cells))


def parse(text: str) -> list[str]:
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
    if not all(isinstance(name, str) for name in payload):
        raise SegmentationRejected(["The model returned something other than a list of names."])
    return payload


def validate(names: Sequence[str], expected: int) -> list[str]:
    """The check that runs before anything is rendered (spec §4.3).

    Much smaller than it was. It used to prove the model's answer partitioned
    the notebook — every index covered exactly once, in order, no gaps or
    overlaps — because a map that skips cells is worse than no map. The blocks
    are no longer the model's to get wrong, so the only thing left to check is
    that it returned one name per block.

    An empty name is a rejection rather than a silent gap: the fallback renders
    the cell range when a name is absent, and a blank string would render as
    a block that looks named and says nothing.
    """
    problems: list[str] = []
    if len(names) != expected:
        problems.append(
            f"The model returned {len(names)} names for {expected} blocks."
        )
    blank = [i for i, name in enumerate(names) if not name.strip()]
    if blank:
        problems.append(f"Blank names at positions: {blank[:10]}.")
    return problems


def segment(
    cells: Sequence[Cell], adapter, *, model: str | None = DEFAULT_MODEL,
    cancel_event: Event | None = None, timeout: float = TIMEOUT_SECONDS,
) -> Segmentation:
    """Draw the blocks here, and ask the model only what to call them.

    Raises `SegmentationRejected` when the answer is not one usable name per
    block, and lets the adapter's own errors — CLI missing, unsupported
    version, timeout — through unchanged so the caller can tell "no model
    available" apart from "the model answered badly". They lead to different
    things being said.
    """
    ranges = [tuple(span) for span in analysis.segment(cells)]
    if not ranges:
        return Segmentation(ranges=(), names=())
    prompt = build_prompt(cells, ranges)
    result = adapter.run_prompt(
        prompt, timeout=timeout, cancel_event=cancel_event or Event(), model=model,
    )
    names = parse(result.final_output)
    problems = validate(names, len(ranges))
    if problems:
        raise SegmentationRejected(problems)
    return Segmentation(
        ranges=tuple(ranges),
        names=tuple(name.strip()[:NAME_LIMIT] for name in names),
    )


__all__ = [
    "AgentAdapterError", "DEFAULT_MODEL", "MAX_CELLS", "PROMPT", "Segmentation",
    "build_prompt", "describe", "parse", "render", "segment", "validate",
]
