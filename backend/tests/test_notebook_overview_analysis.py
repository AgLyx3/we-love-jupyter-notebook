"""Tier 0/1: the five computed fields, against the two probe fixtures.

The fixtures are copies of `docs/plans/probes/`, and the numbers asserted here
are the ones research §13 measured — so a regression in the port shows up as a
disagreement with the recorded evidence, not just as a failing test.

Three of these cover bugs that were found by running the probe against real
notebooks and would otherwise have shipped as design (research §13.3, §13.9).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from backend.app.notebook_overview import analysis

FIXTURES = Path(__file__).parent / "fixtures"


def load(name: str) -> list[analysis.Cell]:
    notebook = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
    return analysis.read_cells(notebook)


def cells_from(sources: list[tuple[str, str]], *, counts=None) -> list[analysis.Cell]:
    """Build cells from (kind, source) pairs, with optional execution counts."""
    counts = counts or [None] * len(sources)
    cells = [
        analysis.Cell(index=index, kind=kind, source=source, execution_count=count)
        for index, ((kind, source), count) in enumerate(zip(sources, counts))
    ]
    for cell in cells:
        analysis.analyse(cell)
    return cells


# --------------------------------------------------------------- `produces`


def test_produces_drops_loop_only_bindings():
    """`for r0 in grid:` binds r0 at module scope — correct, and useless.

    Research §13.7: a block reporting `beta, ip, peaks, r0, results, s` buries
    the two names that matter.
    """
    cells = cells_from([
        ("code", "grid = [1, 2]\nresults = []\nfor r0 in grid:\n    results.append(r0)\n"),
        ("code", "print(results, r0, grid)\n"),
    ])
    made = analysis.produces(cells[:1], cells[1:])
    assert "r0" not in made
    assert "results" in made and "grid" in made


def test_produces_keeps_a_name_assigned_in_one_cell_and_looped_in_another():
    """Only *loop-only* names are dropped; a real assignment is a real product.

    The filter subtracts per cell, so this holds when the assignment and the
    loop are in different cells — the realistic shape, and the one the fixtures
    exercise. A name assigned *and* used as a loop target inside a single cell
    still reads as loop-only and is dropped; that is the ported probe's
    behaviour and it errs toward less noise, so it is left as measured.
    """
    cells = cells_from([
        ("code", "total = 0\n"),
        ("code", "for total in range(3):\n    pass\n"),
        ("code", "print(total)\n"),
    ])
    assert "total" in analysis.produces(cells[:2], cells[2:])


def test_produces_excludes_comprehension_targets():
    """Comprehensions are separately scoped in Python 3 (research §13.9).

    Omitting them from the nested-scope set re-admitted exactly the loop
    counters the filter exists to remove.
    """
    cells = cells_from([
        ("code", "grid = [1, 2]\ntable = {r0: r0 * 2 for r0 in grid}\n"),
        ("code", "print(table, r0)\n"),
    ])
    made = analysis.produces(cells[:1], cells[1:])
    assert "r0" not in made
    assert "table" in made


def test_produces_excludes_function_locals():
    """Bug 1: a flat ast.walk reported `S, I, R = y` inside a def as produced."""
    cells = cells_from([
        ("code", "def sir(y):\n    S, I, R = y\n    return S + I + R\n"),
        ("code", "print(sir([1, 2, 3]), S, I)\n"),
    ])
    made = analysis.produces(cells[:1], cells[1:])
    assert "S" not in made and "I" not in made
    assert "sir" in made


def test_produces_only_reports_names_something_later_reads():
    cells = cells_from([
        ("code", "kept = 1\nignored = 2\n"),
        ("code", "print(kept)\n"),
    ])
    assert analysis.produces(cells[:1], cells[1:]) == ("kept",)


def test_produces_is_ranked_by_demand_and_capped():
    cells = cells_from([
        ("code", "a = 1\nb = 2\nc = 3\nd = 4\ne = 5\n"),
        ("code", "print(e)\n"), ("code", "print(e, d)\n"),
        ("code", "print(e, d, c)\n"), ("code", "print(e, d, c, b)\n"),
        ("code", "print(e, d, c, b, a)\n"),
    ])
    made = analysis.produces(cells[:1], cells[1:])
    # Demands are 5,4,3,2,1 for e,d,c,b,a — most-read first, capped at four, so
    # the least-depended-on name is the one that falls off.
    assert made == ("e", "d", "c", "b")


# ---------------------------------------------------------------- `defines`


def test_call_sites_count_references_not_calls():
    """Bug 2: `solve_ivp(rhs, ...)` never calls rhs by name, but it uses it.

    An ast.Call-only scan reported every callback, key function and decorator
    argument dead.
    """
    cells = cells_from([
        ("code", "def rhs(t, y):\n    return y\n"),
        ("code", "solve_ivp(rhs, [0, 1], [1])\n"),
    ])
    assert analysis.call_sites(cells)["rhs"] == (1,)


def test_call_sites_omit_a_genuinely_unused_function():
    cells = cells_from([("code", "def unused():\n    pass\n"), ("code", "x = 1\n")])
    assert "unused" not in analysis.call_sites(cells)


def test_definition_cell_is_not_its_own_call_site():
    cells = cells_from([("code", "def f():\n    return f\n"), ("code", "f()\n")])
    assert analysis.call_sites(cells)["f"] == (1,)


# ------------------------------------------------------------------ `state`


def test_never_run_requires_every_code_cell_unrun():
    cells = cells_from([("code", "a = 1\n"), ("code", "b = 2\n")], counts=[None, None])
    assert analysis.block_state(cells, out_of_order=False) == "never-run"
    partly = cells_from([("code", "a = 1\n"), ("code", "b = 2\n")], counts=[1, None])
    assert analysis.block_state(partly, out_of_order=False) != "never-run"


def test_markdown_only_block_is_ok_not_never_run():
    """Prose was never going to have an execution count."""
    cells = cells_from([("markdown", "# Heading\n")])
    assert analysis.block_state(cells, out_of_order=False) == "ok"


def test_out_of_order_detection():
    cells = cells_from(
        [("code", "a = 1\n"), ("code", "b = 2\n"), ("code", "c = 3\n")],
        counts=[3, 1, 2],
    )
    assert analysis.is_out_of_order(cells) is True
    assert analysis.block_state(cells, out_of_order=True) == "out-of-order"


def test_in_order_execution_is_not_flagged():
    cells = cells_from(
        [("code", "a = 1\n"), ("code", "b = 2\n")], counts=[1, 2],
    )
    assert analysis.is_out_of_order(cells) is False
    assert analysis.block_state(cells, out_of_order=False) == "ok"


def test_out_of_order_does_not_smear_across_untouched_blocks():
    """A notebook-wide flag must not mark every block on one transposition."""
    cells = cells_from(
        [("code", "a = 1\n"), ("code", "b = 2\n"), ("code", "c = 3\n"), ("code", "d = 4\n")],
        counts=[1, 2, 9, 4],
    )
    blocks = analysis.annotate(cells, [(0, 1), (2, 3)], ["first", "second"])
    assert blocks[0].state == "ok"
    assert blocks[1].state == "out-of-order"


def test_risky_state_comes_from_the_execution_classifier():
    cells = cells_from([("code", "import subprocess\nsubprocess.run(['ls'])\n")], counts=[1])
    assert analysis.block_state(cells, out_of_order=False) == "risky"


def test_run_state_outranks_risk():
    """A block that was never run says so, even when it also touches the shell."""
    cells = cells_from([("code", "import subprocess\nsubprocess.run(['ls'])\n")], counts=[None])
    assert analysis.block_state(cells, out_of_order=False) == "never-run"


# ------------------------------------------------------------------ `marks`


def test_headings_are_collected_as_marks():
    cells = cells_from([
        ("markdown", "## Load the data\nsome prose\n"),
        ("code", "df = 1\n"),
        ("markdown", "plain prose, not a heading\n"),
    ])
    assert analysis.marks(cells) == ("## Load the data",)


def test_no_heading_is_dropped_when_a_block_spans_several():
    """Spec §7.3: a heading always surfaces in whichever block contains it."""
    cells = cells_from([
        ("markdown", "# One\n"), ("code", "a = 1\n"),
        ("markdown", "# Two\n"), ("code", "b = 2\n"),
    ])
    blocks = analysis.annotate(cells, [(0, 3)], ["everything"])
    assert blocks[0].marks == ("# One", "# Two")


def test_every_heading_in_the_notebook_survives_arbitrary_boundaries():
    cells = cells_from([
        ("markdown", "# One\n"), ("code", "a = 1\n"), ("markdown", "## Two\n"),
        ("code", "b = 2\n"), ("markdown", "### Three\n"), ("code", "c = 3\n"),
    ])
    # Boundaries deliberately cutting across where the headings sit.
    blocks = analysis.annotate(cells, [(0, 0), (1, 4), (5, 5)], ["a", "b", "c"])
    seen = [mark for block in blocks for mark in block.marks]
    assert seen == ["# One", "## Two", "### Three"]


# ------------------------------------------------------------------- parsing


def test_magics_and_shell_escapes_do_not_break_analysis():
    cells = cells_from([("code", "%matplotlib inline\n!ls -la\ndf = load()\n"), ("code", "print(df)\n")])
    assert analysis.produces(cells[:1], cells[1:]) == ("df",)


def test_a_percent_inside_a_string_is_not_rewritten():
    """parse_cell parses before stripping, so a string full of `%` is safe."""
    cells = cells_from([
        ("code", 'BANNER = """\n%complete\n"""\n'),
        ("code", "print(BANNER)\n"),
    ])
    assert analysis.produces(cells[:1], cells[1:]) == ("BANNER",)


def test_unparseable_cell_is_skipped_rather_than_raising():
    cells = cells_from([("code", "this is not python ((\n"), ("code", "x = 1\n")])
    assert analysis.produces(cells[:1], cells[1:]) == ()


# ------------------------------------------------------------------ fixtures


def test_messy_exploration_matches_the_measured_shape():
    """Research §13.1: 57 cells, 0 headings, 0 defs, out of order.

    This used to assert 5 blocks with a 44-cell lump — "no navigation at all" —
    so that the heading pass's known weakness stayed visible rather than
    drifting quietly. The weakness is what got fixed: `segment()` no longer
    consults headings, so a notebook with none is no longer a single lump.
    The assertion is inverted rather than deleted, because a regression here
    would be silent otherwise.
    """
    cells = load("messy-exploration.ipynb")
    assert len(cells) == 57
    assert sum(1 for cell in cells if cell.is_heading) == 0
    assert analysis.call_sites(cells) == {}
    assert analysis.is_out_of_order(cells) is True
    blocks = analysis.fallback_blocks(cells)
    assert len(blocks) == 11
    # Nothing over the size rule the panel states for itself.
    assert max(block.end - block.start + 1 for block in blocks) <= analysis.MAX_BLOCK


def test_simulation_sweep_function_layer():
    """Research §13.4: sir() used in [4, 14]; sensitivity() genuinely dead."""
    cells = load("simulation-sweep.ipynb")
    assert len(cells) == 21
    sites = analysis.call_sites(cells)
    assert sites["sir"] == (4, 14)
    assert sites["peak_of"] == (8, 17)
    assert sites["attack_rate"] == (12, 17)
    assert "sensitivity" not in sites

    # Ranges follow from the segmenter, so they are asserted as a shape rather
    # than a list: a partition, none oversized. The function layer below is
    # what this test is actually about, and it is boundary-independent.
    blocks = analysis.fallback_blocks(cells)
    covered = [i for block in blocks for i in range(block.start, block.end + 1)]
    assert covered == list(range(len(cells)))
    assert max(block.end - block.start + 1 for block in blocks) <= analysis.MAX_BLOCK
    assert any("# SIR outbreak sweep" in block.marks for block in blocks)
    dead = [d for block in blocks for d in block.defines if not d.call_sites]
    assert [d.name for d in dead] == ["sensitivity"]

    # A consequence of the segmenter change, asserted so it stays visible.
    # `block_state` reports "never-run" only when *every* code cell in the block
    # is unrun. The heading pass happened to leave cells 19-20 as their own
    # block, so the tail was flagged; cohesion groups them with 14-18, which did
    # run, and the flag is gone. Nothing renders block state today (stitch-diff
    # D10 dropped the chips) but the API still carries it, and this is where the
    # loss would show up if a marker returns.
    tail = [cell for cell in cells if cell.index >= 19]
    assert all(cell.execution_count is None for cell in tail if cell.is_code)
    holder = next(b for b in blocks if b.start <= 19 <= b.end)
    assert holder.state == "ok"


def test_fallback_blocks_partition_the_notebook():
    for name in ("messy-exploration.ipynb", "simulation-sweep.ipynb"):
        cells = load(name)
        blocks = analysis.fallback_blocks(cells)
        covered = [i for block in blocks for i in range(block.start, block.end + 1)]
        assert covered == list(range(len(cells))), name


def test_fallback_leaves_names_empty_rather_than_inventing_them():
    """Spec §6: say plainly that names are unavailable."""
    blocks = analysis.fallback_blocks(load("messy-exploration.ipynb"))
    assert all(block.name == "" for block in blocks)


@pytest.mark.parametrize("name", ["messy-exploration.ipynb", "simulation-sweep.ipynb"])
def test_produces_never_leaks_a_function_local_on_the_fixtures(name):
    """The end-to-end form of bug 1, on the notebooks that exposed it."""
    cells = load(name)
    locals_in_defs = {"S", "I", "R", "t", "y"}
    for block in analysis.fallback_blocks(cells):
        assert not set(block.produces) & locals_in_defs


# ------------------------------------------------------- the partition itself
#
# `segmenter.validate()` used to prove every cell was covered exactly once,
# adversarially, because the model could return anything. The model no longer
# draws boundaries, so `segment()` is the sole guarantor and nothing downstream
# re-checks it. These are stricter than that validator was, and they are here
# rather than there for that reason.


def _is_partition(blocks, count: int) -> bool:
    covered = [i for block in blocks for i in range(block[0], block[1] + 1)]
    return covered == list(range(count))


@pytest.mark.parametrize("name", ["messy-exploration.ipynb", "simulation-sweep.ipynb"])
def test_segment_always_partitions_the_fixtures(name):
    cells = load(name)
    assert _is_partition(analysis.segment(cells), len(cells))


@pytest.mark.parametrize("count", [0, 1, 2, 3, 5, 6, 7, 13, 40, 97])
def test_segment_always_partitions_a_uniform_notebook(count):
    """Sizes around COHESION_WINDOW and MAX_BLOCK, where the windowing, the
    oversize split and the singleton absorb all change behaviour."""
    cells = analysis.read_cells({"cells": [
        {"cell_type": "code", "source": [f"x{i} = {i}\n"], "execution_count": None}
        for i in range(count)
    ]})
    assert _is_partition(analysis.segment(cells), count)


def test_segment_partitions_a_notebook_of_only_markdown():
    """No identifiers anywhere, so every cohesion score is zero — the case that
    divides by an empty union if the guard is missing."""
    cells = analysis.read_cells({"cells": [
        {"cell_type": "markdown", "source": [f"some prose {i}\n"]} for i in range(30)
    ]})
    assert _is_partition(analysis.segment(cells), 30)


def test_segment_partitions_when_every_cell_is_identical():
    """Perfectly cohesive: no valley anywhere, so the only cuts are the ones
    the size rule forces."""
    cells = analysis.read_cells({"cells": [
        {"cell_type": "code", "source": ["df = df.dropna()\n"], "execution_count": 1}
        for _ in range(50)
    ]})
    blocks = analysis.segment(cells)
    assert _is_partition(blocks, 50)
    assert max(end - start + 1 for start, end in blocks) <= analysis.MAX_BLOCK


def test_segment_partitions_unparsable_source():
    """A cell that does not parse contributes no identifiers; it must still be
    covered exactly once."""
    cells = analysis.read_cells({"cells": [
        {"cell_type": "code", "source": ["this is not python(((\n"], "execution_count": None}
        for _ in range(20)
    ]})
    assert _is_partition(analysis.segment(cells), 20)


def test_segment_never_returns_an_empty_or_reversed_block():
    for name in ("messy-exploration.ipynb", "simulation-sweep.ipynb"):
        for start, end in analysis.segment(load(name)):
            assert start <= end, name


def test_a_nested_scopes_own_names_are_not_module_reads():
    """#37: descending into a nested scope for reads is right — a function
    reading a module-level `df` really does read it — but only for names that
    scope does not bind itself."""
    cell, = analysis.read_cells({"cells": [{
        "cell_type": "code",
        "source": [
            "def outer(alpha):\n",
            "    def inner(t, y):\n",
            "        local = t + y + alpha\n",
            "        return local + module_level\n",
            "    return inner\n",
        ],
        "execution_count": None,
    }]})
    # The free variable is a genuine read.
    assert "module_level" in cell.reads
    # Parameters and locals of the nested scopes are not.
    for shadowed in ("t", "y", "alpha", "local"):
        assert shadowed not in cell.reads, shadowed


def test_a_comprehension_target_is_not_a_module_read():
    cell, = analysis.read_cells({"cells": [{
        "cell_type": "code",
        "source": ["totals = {key: source[key] for key in keys}\n"],
        "execution_count": None,
    }]})
    assert {"source", "keys"} <= cell.reads
    assert "key" not in cell.reads


def test_a_function_that_rebinds_an_outer_name_does_not_read_it():
    """Python makes it a local, so the module binding is not read at all."""
    cell, = analysis.read_cells({"cells": [{
        "cell_type": "code",
        "source": ["def f():\n", "    df = 1\n", "    return df\n"],
        "execution_count": None,
    }]})
    assert "df" not in cell.reads


def test_out_of_order_outranks_risky_as_models_declares():
    """#33: the upgrade was gated on `state == "ok"`, so a risky block that
    also ran out of order reported only `risky` — the inverse of the
    precedence BLOCK_STATES states in its own comment."""
    notebook = {"cells": [
        # Ran late.
        {"cell_type": "code", "source": ["a = 1\n"], "execution_count": 9},
        {"cell_type": "code", "source": ["b = a + 1\n"], "execution_count": 10},
        # Ran earlier *and* touches the shell: risky, and out of order.
        {"cell_type": "code", "source": ["import os\n", "os.system('ls')\n"], "execution_count": 2},
        {"cell_type": "code", "source": ["c = 3\n"], "execution_count": 3},
    ]}
    cells = analysis.read_cells(notebook)
    blocks = analysis.annotate(cells, [(0, 1), (2, 3)], ["first", "second"])
    assert blocks[1].state == "out-of-order"


def test_never_run_still_outranks_out_of_order():
    """The other side of the same ranking: never-run is above out-of-order, so
    an unrun block is not relabelled by the ordering check."""
    notebook = {"cells": [
        {"cell_type": "code", "source": ["a = 1\n"], "execution_count": 9},
        {"cell_type": "code", "source": ["b = 2\n"], "execution_count": None},
    ]}
    cells = analysis.read_cells(notebook)
    blocks = analysis.annotate(cells, [(0, 0), (1, 1)], ["first", "second"])
    assert blocks[1].state == "never-run"
