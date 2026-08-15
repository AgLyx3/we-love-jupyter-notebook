"""Rewriting a knob's literal in place.

The offset maths is the part that fails quietly, so the awkward cases — non-ASCII
text earlier on the same line, two knobs on one line, a literal that also appears
in a comment — get explicit tests.
"""

from __future__ import annotations

import pytest

from backend.app.plot_tuning.discovery import ChainCell, discover
from backend.app.plot_tuning.rewrite import RewriteError, render, rewrite_cell, rewrite_chain


def knob_for(source: str, name: str):
    cells = (
        ChainCell(cell_id="setup", index=0, source=source),
        ChainCell(cell_id="plot", index=1, source=f"plt.plot({name})\n"),
    )
    result = discover(cells, "plot")
    return next(knob for knob in result.knobs if knob.name == name)


def test_replaces_an_int():
    source = "bins = 30\n"
    assert rewrite_cell(source, [(knob_for(source, "bins"), 55)]) == "bins = 55\n"


def test_replaces_a_float():
    source = "alpha = 0.5\n"
    assert rewrite_cell(source, [(knob_for(source, "alpha"), 0.8)]) == "alpha = 0.8\n"


def test_replaces_a_string_with_quotes():
    source = "color = 'red'\n"
    assert rewrite_cell(source, [(knob_for(source, "color"), "blue")]) == "color = 'blue'\n"


def test_replaces_a_bool():
    source = "grid = False\n"
    assert rewrite_cell(source, [(knob_for(source, "grid"), True)]) == "grid = True\n"


def test_replaces_a_tuple():
    source = "xlim = (0, 10)\n"
    assert rewrite_cell(source, [(knob_for(source, "xlim"), (2, 8))]) == "xlim = (2, 8)\n"


def test_preserves_the_rest_of_the_cell():
    source = "import numpy as np\n\nbins = 30  # how many buckets\n\nprint(bins)\n"
    updated = rewrite_cell(source, [(knob_for(source, "bins"), 7)])
    assert updated == "import numpy as np\n\nbins = 7  # how many buckets\n\nprint(bins)\n"


def test_non_ascii_before_the_literal_on_the_same_line_does_not_shift_the_edit():
    # `ast` reports col_offset in UTF-8 *bytes*. `é` is two bytes, so the byte
    # offset of `30` here is one past its character index and naive `str`
    # slicing lands mid-literal. The non-ASCII must precede the literal *on the
    # same line* to exercise this — text after it, or on an earlier line, leaves
    # the sliced prefix pure ASCII and proves nothing.
    source = "label = 'café'; bins = 30\n"
    updated = rewrite_cell(source, [(knob_for(source, "bins"), 12)])
    assert updated == "label = 'café'; bins = 12\n"


def test_multibyte_run_before_the_literal_does_not_shift_the_edit():
    source = "label = 'température ≈ 30°'; bins = 30\n"
    updated = rewrite_cell(source, [(knob_for(source, "bins"), 7)])
    assert updated == "label = 'température ≈ 30°'; bins = 7\n"


def test_non_ascii_on_an_earlier_line_does_not_shift_the_edit():
    # Line accumulation is in characters and `lineno` is a line count, so this
    # is a different mechanism from the byte offset above. Worth pinning too.
    source = "label = 'température'\nbins = 30\n"
    updated = rewrite_cell(source, [(knob_for(source, "bins"), 12)])
    assert updated == "label = 'température'\nbins = 12\n"


def test_two_knobs_on_the_same_line():
    source = "bins = 30; alpha = 0.5\n"
    cells = (
        ChainCell(cell_id="setup", index=0, source=source),
        ChainCell(cell_id="plot", index=1, source="plt.hist(d, bins=bins, alpha=alpha)\n"),
    )
    knobs = {knob.name: knob for knob in discover(cells, "plot").knobs}
    updated = rewrite_cell(source, [(knobs["bins"], 100), (knobs["alpha"], 0.9)])
    assert updated == "bins = 100; alpha = 0.9\n"


def test_a_matching_number_in_a_comment_is_untouched():
    source = "bins = 30  # was 30 before\n"
    updated = rewrite_cell(source, [(knob_for(source, "bins"), 4)])
    assert updated == "bins = 4  # was 30 before\n"


def test_a_matching_number_in_an_earlier_string_is_untouched():
    source = "title = '30 day window'\nbins = 30\n"
    updated = rewrite_cell(source, [(knob_for(source, "bins"), 4)])
    assert updated == "title = '30 day window'\nbins = 4\n"


def test_rewriting_is_idempotent_for_the_same_value():
    source = "bins = 30\n"
    assert rewrite_cell(source, [(knob_for(source, "bins"), 30)]) == source


def test_no_edits_returns_the_source_unchanged():
    assert rewrite_cell("bins = 30\n", []) == "bins = 30\n"


def test_line_magic_above_the_knob_does_not_shift_the_edit():
    source = "%matplotlib inline\nbins = 30\n"
    updated = rewrite_cell(source, [(knob_for(source, "bins"), 9)])
    assert updated == "%matplotlib inline\nbins = 9\n"


def test_verification_catches_a_bad_source_range():
    # Simulate an offset bug by moving the knob's range onto the variable name.
    knob = knob_for("bins = 30\n", "bins")
    broken = type(knob)(**{**knob.__dict__, "col_offset": 0, "end_col_offset": 4})
    with pytest.raises(RewriteError):
        rewrite_cell("bins = 30\n", [(broken, 55)])


def test_overlapping_edits_are_refused():
    knob = knob_for("bins = 30\n", "bins")
    wide = type(knob)(**{**knob.__dict__, "col_offset": 0, "end_col_offset": 9})
    with pytest.raises(RewriteError):
        rewrite_cell("bins = 30\n", [(knob, 1), (wide, 2)])


def test_rewrite_chain_groups_edits_by_cell_and_returns_only_changes():
    sources = {"a": "n = 1\n", "b": "m = 2\n", "c": "untouched = 3\n"}
    cells = (
        ChainCell(cell_id="a", index=0, source=sources["a"]),
        ChainCell(cell_id="b", index=1, source=sources["b"]),
        ChainCell(cell_id="c", index=2, source=sources["c"]),
        ChainCell(cell_id="plot", index=3, source="plt.plot(n, m)\n"),
    )
    knobs = {knob.name: knob for knob in discover(cells, "plot").knobs}
    updated = rewrite_chain(sources, [(knobs["n"], 10), (knobs["m"], 20)])
    assert updated == {"a": "n = 10\n", "b": "m = 20\n"}


@pytest.mark.parametrize(("kind", "value", "expected"), [
    ("int", 5, "5"),
    ("float", 0.5, "0.5"),
    ("bool", True, "True"),
    ("str", "red", "'red'"),
    ("tuple", (1, 2), "(1, 2)"),
    ("tuple", (1,), "(1,)"),
    ("list", (1, 2), "[1, 2]"),
])
def test_render(kind, value, expected):
    assert render(kind, value) == expected
