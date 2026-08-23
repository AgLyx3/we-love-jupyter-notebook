"""End-to-end over the committed fixture notebook, still without a kernel.

Unit tests use hand-written snippets, which is exactly the shape of source I
also wrote the scanner against — so they can agree with each other and still be
wrong about real notebooks. Pinning the fixture keeps discovery, the bounds
heuristic and rewriting honest against a file that looks like something a person
would actually open.
"""

from __future__ import annotations

import json
import pathlib

import pytest

from backend.app.plot_tuning.bounds import bounds_for
from backend.app.plot_tuning.discovery import ChainCell, discover
from backend.app.plot_tuning.rewrite import rewrite_chain

FIXTURE = pathlib.Path(__file__).resolve().parents[2] / "examples" / "plot-tuning.ipynb"


@pytest.fixture
def chain_and_target():
    notebook = json.loads(FIXTURE.read_text())
    cells = tuple(
        ChainCell(cell_id=cell["id"], index=index, source="".join(cell["source"]))
        for index, cell in enumerate(notebook["cells"])
        if cell["cell_type"] == "code"
    )
    return cells, "plot"


def test_fixture_exposes_every_control_type(chain_and_target):
    cells, target = chain_and_target
    result = discover(cells, target)
    found = {knob.name: knob for knob in result.knobs}
    assert result.blocked is None
    assert {knob.kind for knob in result.knobs} == {"int", "float", "str", "bool", "tuple"}
    assert (found["N_POINTS"].kind, found["N_POINTS"].value) == ("int", 800)
    assert (found["COLOR"].kind, found["COLOR"].value) == ("str", "steelblue")
    assert (found["GRID"].kind, found["GRID"].value) == ("bool", True)
    assert (found["FIGSIZE"].kind, found["FIGSIZE"].value) == ("tuple", (7, 4))


def test_fixture_reaches_a_knob_the_plot_cell_never_mentions(chain_and_target):
    # The plot cell reads `samples`, not `N_POINTS` or `NOISE`. Those are only
    # reachable through the generating cell.
    cells, target = chain_and_target
    plot_source = next(cell.source for cell in cells if cell.cell_id == target)
    assert "N_POINTS" in plot_source  # used in the title…
    assert "NOISE" in plot_source
    generated = next(cell.source for cell in cells if cell.cell_id == "generate")
    assert "N_POINTS" in generated and "NOISE" in generated
    assert {"N_POINTS", "NOISE"} <= {knob.name for knob in discover(cells, target).knobs}


def test_fixture_line_magic_does_not_block_discovery(chain_and_target):
    # The setup cell opens with `%matplotlib inline`.
    cells, target = chain_and_target
    assert discover(cells, target).blocked is None


def test_alpha_default_bounds_exceed_the_valid_range(chain_and_target):
    # Documents a known limit rather than pretending it away: a type-only scan
    # cannot know alpha must stay under 1, so the heuristic overshoots and the
    # user narrows it by hand. If this ever starts passing, the bounds logic
    # gained semantics and the design note should be revisited.
    cells, target = chain_and_target
    alpha = next(knob for knob in discover(cells, target).knobs if knob.name == "ALPHA")
    assert bounds_for(alpha.kind, alpha.value).maximum > 1.0


def test_rewriting_several_knobs_across_the_fixture(chain_and_target):
    cells, target = chain_and_target
    knobs = {knob.name: knob for knob in discover(cells, target).knobs}
    sources = {cell.cell_id: cell.source for cell in cells}
    updated = rewrite_chain(sources, [
        (knobs["BINS"], 12),
        (knobs["COLOR"], "crimson"),
        (knobs["GRID"], False),
        (knobs["FIGSIZE"], (10, 6)),
    ])
    # All four live in one cell, so exactly one cell changes.
    assert set(updated) == {"params-cell"}
    changed = updated["params-cell"]
    assert "BINS = 12" in changed
    assert "COLOR = 'crimson'" in changed
    assert "GRID = False" in changed
    assert "FIGSIZE = (10, 6)" in changed
    # Untouched knobs keep their original literals.
    assert "N_POINTS = 800" in changed
    assert "ALPHA = 0.75" in changed


def test_rewritten_fixture_still_discovers_the_new_values(chain_and_target):
    cells, target = chain_and_target
    knobs = {knob.name: knob for knob in discover(cells, target).knobs}
    sources = {cell.cell_id: cell.source for cell in cells}
    updated = rewrite_chain(sources, [(knobs["BINS"], 12)])
    rescanned = tuple(
        ChainCell(cell_id=cell.cell_id, index=cell.index,
                  source=updated.get(cell.cell_id, cell.source))
        for cell in cells
    )
    again = {knob.name: knob.value for knob in discover(rescanned, target).knobs}
    assert again["BINS"] == 12
