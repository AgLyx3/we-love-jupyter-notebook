"""Tier 2: the naming prompt, and the validation that decides what gets rendered.

The model no longer draws the boundaries (#36 measured its partition as
indistinguishable from the deterministic one), so most of what this file used to
assert has moved rather than gone:

* The **partition guarantee** — every cell covered exactly once, in order, no
  gaps or overlaps — used to be enforced here, adversarially, because the model
  could return anything. `analysis.segment()` is now its sole guarantor, and it
  is tested there (`test_segment_always_partitions_*`) more strictly than the
  validator ever tested it, because nothing downstream checks it any more.
* What is left here is the one thing the model can still get wrong: returning
  the wrong number of names, or a blank one.

Nothing here asserts a generated *name*. Names are model output; the tests
assert the shape (spec §8).
"""

from __future__ import annotations

import json
from pathlib import Path
from threading import Event

import pytest

from backend.app.agent_workspace.models import AdapterResult, AgentAdapterError
from backend.app.notebook_overview import analysis, segmenter
from backend.app.notebook_overview.models import NotebookTooLarge, SegmentationRejected

FIXTURES = Path(__file__).parent / "fixtures"


def load(name: str) -> list[analysis.Cell]:
    return analysis.read_cells(json.loads((FIXTURES / name).read_text(encoding="utf-8")))


class StubAdapter:
    """Returns a canned response, and records what it was asked."""

    def __init__(self, response: str = "[]", error: Exception | None = None) -> None:
        self.response = response
        self.error = error
        self.prompts: list[str] = []
        self.models: list[str | None] = []

    def run_prompt(self, prompt, *, timeout, cancel_event, model=None):
        self.prompts.append(prompt)
        self.models.append(model)
        if self.error:
            raise self.error
        return AdapterResult(self.response)


def names_for(cells) -> str:
    """A well-formed answer: one name per block the analyser drew."""
    return json.dumps([f"block {i}" for i, _ in enumerate(analysis.segment(cells))])


# ----------------------------------------------------------------- validation


def test_one_name_per_block_has_no_problems():
    assert segmenter.validate(["alpha", "beta"], 2) == []


def test_too_few_names_is_rejected():
    problems = segmenter.validate(["alpha"], 3)
    assert any("1 names for 3 blocks" in problem for problem in problems)


def test_too_many_names_is_rejected():
    problems = segmenter.validate(["a", "b", "c"], 2)
    assert any("3 names for 2 blocks" in problem for problem in problems)


def test_a_blank_name_is_rejected():
    """The fallback renders the range when a name is absent; a blank string
    would render as a block that looks named and says nothing."""
    problems = segmenter.validate(["alpha", "   "], 2)
    assert any("Blank names" in problem for problem in problems)


def test_an_empty_response_is_rejected_when_blocks_were_expected():
    assert segmenter.validate([], 4) != []


def test_no_blocks_and_no_names_is_consistent():
    assert segmenter.validate([], 0) == []


# --------------------------------------------------------------------- parse


def test_parse_extracts_the_array_from_surrounding_prose():
    assert segmenter.parse('Sure! Here you go:\n["alpha", "beta"]\nHope that helps.') == ["alpha", "beta"]


def test_parse_rejects_a_response_with_no_array():
    with pytest.raises(SegmentationRejected):
        segmenter.parse("I could not name these blocks.")


def test_parse_rejects_malformed_json():
    with pytest.raises(SegmentationRejected):
        segmenter.parse('["alpha", ]')


def test_parse_rejects_a_list_that_is_not_names():
    """The old prompt returned objects. A model still answering that shape is
    wrong now, and saying so beats coercing it into strings."""
    with pytest.raises(SegmentationRejected):
        segmenter.parse('[{"start": 0, "end": 1, "name": "a"}]')


# --------------------------------------------------------------------- prompt


def test_prompt_sends_source_but_never_outputs():
    """Research §1: outputs are out of scope, at any size, for any reason."""
    notebook = {
        "cells": [{
            "cell_type": "code",
            "source": ["df = load()\n"],
            "execution_count": 1,
            "outputs": [{
                "output_type": "stream", "name": "stdout",
                "text": ["SECRET-CUSTOMER-ROW-12345\n"],
            }],
        }],
    }
    cells = analysis.read_cells(notebook)
    prompt = segmenter.build_prompt(cells, analysis.segment(cells))
    assert "df = load()" in prompt
    assert "SECRET-CUSTOMER-ROW-12345" not in prompt
    assert "stdout" not in prompt


def test_prompt_keeps_the_naming_clauses_that_were_earned_not_guessed():
    """Spec §4.1: both of these were measured, not assumed, and both survive
    the change of job — the model still names, it just no longer partitions."""
    cells = load("simulation-sweep.ipynb")
    prompt = segmenter.build_prompt(cells, analysis.segment(cells))
    # Without the ban, Haiku drifts to categorical names (research §13.6).
    for banned in ("Analyze", "Explore", "Visualize", "Process", "Handle", "Perform", "Compute"):
        assert banned in prompt
    assert "Name the *subject*, not the activity" in prompt
    # This is what keeps a heading from dictating a name the code contradicts.
    assert "Markdown headings are a hint" in prompt


def test_prompt_states_the_blocks_and_forbids_changing_them():
    cells = load("messy-exploration.ipynb")
    ranges = analysis.segment(cells)
    prompt = segmenter.build_prompt(cells, ranges)
    assert f"JSON array of {len(ranges)} strings" in prompt
    assert "already decided" in prompt
    # 1-based in the prompt, as in the UI, so a name and a rail entry agree.
    start, end = ranges[1]
    assert f"2. cells {start + 1}-{end + 1}" in prompt


def test_a_single_cell_block_reads_as_one_cell_not_a_range():
    assert segmenter.describe([(0, 0), (1, 3)]).splitlines() == ["1. cell 1", "2. cells 2-4"]


def test_long_cells_are_truncated_head_and_tail():
    body = "x = 1\n" * 500
    notebook = {"cells": [
        {"cell_type": "code", "source": [f"FIRST = 1\n{body}LAST = 2\n"], "execution_count": None},
    ]}
    cells = analysis.read_cells(notebook)
    prompt = segmenter.build_prompt(cells, analysis.segment(cells))
    assert "# ... truncated ..." in prompt
    # Head and tail both survive: the tail is where the result gets assigned.
    assert "FIRST = 1" in prompt
    assert "LAST = 2" in prompt


def test_markdown_cells_are_labelled_for_the_model():
    notebook = {"cells": [
        {"cell_type": "markdown", "source": ["# Title\n"]},
        {"cell_type": "code", "source": ["a = 1\n"], "execution_count": None},
    ]}
    cells = analysis.read_cells(notebook)
    prompt = segmenter.build_prompt(cells, analysis.segment(cells))
    assert "[0] (markdown)" in prompt
    assert "[1] (code)" in prompt


def test_an_oversized_notebook_is_refused_rather_than_truncated():
    """Spec §10.2: chunking is undefined, so segmenting a prefix would lie."""
    notebook = {"cells": [
        {"cell_type": "code", "source": ["a = 1\n"], "execution_count": None}
        for _ in range(segmenter.MAX_CELLS + 1)
    ]}
    cells = analysis.read_cells(notebook)
    with pytest.raises(NotebookTooLarge):
        segmenter.build_prompt(cells, analysis.segment(cells))


# ------------------------------------------------------------------- segment


def test_segment_returns_the_analysers_ranges_and_the_models_names():
    cells = load("simulation-sweep.ipynb")
    expected = [tuple(span) for span in analysis.segment(cells)]
    result = segmenter.segment(cells, StubAdapter(names_for(cells)))
    assert list(result.ranges) == expected
    assert len(result.names) == len(expected)


def test_the_model_cannot_move_a_boundary():
    """The point of the change: boundaries are not the model's to return, so
    an answer that tries to move one cannot."""
    cells = load("simulation-sweep.ipynb")
    expected = [tuple(span) for span in analysis.segment(cells)]
    result = segmenter.segment(cells, StubAdapter(names_for(cells)))
    assert list(result.ranges) == expected


def test_segment_is_deterministic_across_calls():
    """Two runs of the old prompt returned block counts differing by 12% on
    average. The blocks no longer depend on the model at all."""
    cells = load("messy-exploration.ipynb")
    first = segmenter.segment(cells, StubAdapter(names_for(cells)))
    second = segmenter.segment(cells, StubAdapter(names_for(cells)))
    assert first.ranges == second.ranges


def test_segment_defaults_to_haiku():
    cells = load("simulation-sweep.ipynb")
    adapter = StubAdapter(names_for(cells))
    segmenter.segment(cells, adapter)
    assert adapter.models == ["haiku"]


def test_segment_discards_a_wrong_number_of_names():
    cells = load("simulation-sweep.ipynb")
    with pytest.raises(SegmentationRejected) as caught:
        segmenter.segment(cells, StubAdapter('["only one"]'))
    assert "names for" in str(caught.value)


def test_segment_lets_adapter_errors_through_unchanged():
    cells = load("simulation-sweep.ipynb")
    with pytest.raises(AgentAdapterError):
        segmenter.segment(cells, StubAdapter(error=AgentAdapterError("no CLI")))


def test_a_long_name_is_truncated_rather_than_trusted():
    cells = load("simulation-sweep.ipynb")
    count = len(analysis.segment(cells))
    response = json.dumps(["x" * 400] * count)
    result = segmenter.segment(cells, StubAdapter(response))
    assert all(len(name) == segmenter.NAME_LIMIT for name in result.names)


def test_segment_passes_a_cancel_event_through():
    cells = load("simulation-sweep.ipynb")
    seen = {}

    class Recording(StubAdapter):
        def run_prompt(self, prompt, *, timeout, cancel_event, model=None):
            seen["event"] = cancel_event
            return AdapterResult(names_for(cells))

    event = Event()
    segmenter.segment(cells, Recording(), cancel_event=event)
    assert seen["event"] is event


def test_an_empty_notebook_asks_the_model_nothing():
    adapter = StubAdapter("[]")
    result = segmenter.segment([], adapter)
    assert result.ranges == () and result.names == ()
    assert adapter.prompts == []
