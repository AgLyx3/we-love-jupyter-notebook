"""Tier 2: the prompt, and the validation that decides what gets rendered.

Validation is the safety net spec §4.3 makes non-negotiable — a response that is
not a valid partition is discarded rather than rendered — so it is tested
directly and adversarially rather than only through the happy path.

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


def partition(ranges) -> str:
    return json.dumps([
        {"start": start, "end": end, "name": f"block {start}"} for start, end in ranges
    ])


# ----------------------------------------------------------------- validation


def test_a_correct_partition_has_no_problems():
    assert segmenter.validate(
        [{"start": 0, "end": 2, "name": "a"}, {"start": 3, "end": 4, "name": "b"}], 4,
    ) == []


def test_gaps_are_rejected():
    problems = segmenter.validate(
        [{"start": 0, "end": 1, "name": "a"}, {"start": 3, "end": 4, "name": "b"}], 4,
    )
    assert any("uncovered cells" in problem for problem in problems)


def test_overlaps_are_rejected():
    problems = segmenter.validate(
        [{"start": 0, "end": 3, "name": "a"}, {"start": 2, "end": 4, "name": "b"}], 4,
    )
    assert any("overlapping cells" in problem for problem in problems)


def test_out_of_range_indices_are_rejected():
    problems = segmenter.validate([{"start": 0, "end": 9, "name": "a"}], 4)
    assert any("out of range" in problem for problem in problems)


def test_a_negative_index_is_rejected():
    problems = segmenter.validate([{"start": -1, "end": 4, "name": "a"}], 4)
    assert any("out of range" in problem for problem in problems)


def test_reversed_range_is_rejected():
    problems = segmenter.validate([{"start": 3, "end": 1, "name": "a"}], 4)
    assert any("out of range" in problem for problem in problems)


def test_unordered_blocks_are_rejected():
    problems = segmenter.validate(
        [{"start": 3, "end": 4, "name": "b"}, {"start": 0, "end": 2, "name": "a"}], 4,
    )
    assert any("document order" in problem for problem in problems)


def test_an_empty_name_is_rejected():
    problems = segmenter.validate(
        [{"start": 0, "end": 4, "name": "   "}], 4,
    )
    assert any("no name" in problem for problem in problems)


def test_a_missing_name_is_rejected():
    problems = segmenter.validate([{"start": 0, "end": 4}], 4)
    assert any("no name" in problem for problem in problems)


def test_non_integer_indices_are_rejected():
    problems = segmenter.validate([{"start": "0", "end": 4, "name": "a"}], 4)
    assert any("non-integer" in problem for problem in problems)


def test_a_boolean_is_not_accepted_as_an_index():
    """`True` is an int in Python; it is not a cell index."""
    problems = segmenter.validate([{"start": True, "end": 4, "name": "a"}], 4)
    assert any("non-integer" in problem for problem in problems)


def test_an_empty_response_is_rejected():
    assert "no blocks returned" in segmenter.validate([], 4)


# --------------------------------------------------------------------- parse


def test_parse_extracts_the_array_from_surrounding_prose():
    blocks = segmenter.parse('Sure! Here you go:\n[{"start": 0, "end": 1, "name": "a"}]\nHope that helps.')
    assert blocks == [{"start": 0, "end": 1, "name": "a"}]


def test_parse_rejects_a_response_with_no_array():
    with pytest.raises(SegmentationRejected):
        segmenter.parse("I could not segment this notebook.")


def test_parse_rejects_malformed_json():
    with pytest.raises(SegmentationRejected):
        segmenter.parse('[{"start": 0, "end": }]')


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
    prompt = segmenter.build_prompt(analysis.read_cells(notebook))
    assert "df = load()" in prompt
    assert "SECRET-CUSTOMER-ROW-12345" not in prompt
    assert "stdout" not in prompt


def test_prompt_keeps_the_clauses_that_were_earned_not_guessed():
    """Spec §4.1: both of these were measured, not assumed."""
    prompt = segmenter.build_prompt(load("simulation-sweep.ipynb"))
    # Without the ban, Haiku drifts to categorical names (research §13.6).
    for banned in ("Analyze", "Explore", "Visualize", "Process", "Handle", "Perform", "Compute"):
        assert banned in prompt
    assert "Name the *subject*, not the activity" in prompt
    # This is what makes headings annotation rather than structure.
    assert "Markdown headings are a hint" in prompt


def test_prompt_states_the_real_last_index():
    cells = load("messy-exploration.ipynb")
    assert f"every index from 0 to {len(cells) - 1}" in segmenter.build_prompt(cells)


def test_long_cells_are_truncated_head_and_tail():
    body = "x = 1\n" * 500
    notebook = {"cells": [
        {"cell_type": "code", "source": [f"FIRST = 1\n{body}LAST = 2\n"], "execution_count": None},
    ]}
    prompt = segmenter.build_prompt(analysis.read_cells(notebook))
    assert "# ... truncated ..." in prompt
    # Head and tail both survive: the tail is where the result gets assigned.
    assert "FIRST = 1" in prompt
    assert "LAST = 2" in prompt


def test_markdown_cells_are_labelled_for_the_model():
    notebook = {"cells": [
        {"cell_type": "markdown", "source": ["# Title\n"]},
        {"cell_type": "code", "source": ["a = 1\n"], "execution_count": None},
    ]}
    prompt = segmenter.build_prompt(analysis.read_cells(notebook))
    assert "[0] (markdown)" in prompt
    assert "[1] (code)" in prompt


def test_an_oversized_notebook_is_refused_rather_than_truncated():
    """Spec §10.2: chunking is undefined, so segmenting a prefix would lie."""
    cells = analysis.read_cells({"cells": [
        {"cell_type": "code", "source": ["a = 1\n"], "execution_count": None}
        for _ in range(segmenter.MAX_CELLS + 1)
    ]})
    with pytest.raises(NotebookTooLarge):
        segmenter.build_prompt(cells)


# -------------------------------------------------------------------- segment


def test_segment_returns_ranges_and_names_on_a_valid_partition():
    cells = load("simulation-sweep.ipynb")
    adapter = StubAdapter(partition([(0, 9), (10, 20)]))
    result = segmenter.segment(cells, adapter)
    assert result.ranges == ((0, 9), (10, 20))
    assert all(name for name in result.names)


def test_segment_defaults_to_haiku():
    """Research §13.6 measured Haiku as structurally equivalent."""
    adapter = StubAdapter(partition([(0, 20)]))
    segmenter.segment(load("simulation-sweep.ipynb"), adapter)
    assert adapter.models == ["haiku"]


def test_segment_discards_an_invalid_partition():
    cells = load("simulation-sweep.ipynb")
    adapter = StubAdapter(partition([(0, 5)]))  # covers 6 of 21 cells
    with pytest.raises(SegmentationRejected) as caught:
        segmenter.segment(cells, adapter)
    assert any("uncovered" in problem for problem in caught.value.problems)


def test_segment_lets_adapter_errors_through_unchanged():
    """"No model available" and "the model answered badly" are different facts."""
    adapter = StubAdapter(error=AgentAdapterError("Claude CLI is unavailable"))
    with pytest.raises(AgentAdapterError):
        segmenter.segment(load("simulation-sweep.ipynb"), adapter)


def test_a_long_name_is_truncated_rather_than_trusted():
    """Spec §4.3.4: names are truncated for display, not trusted to be short."""
    cells = load("simulation-sweep.ipynb")
    adapter = StubAdapter(json.dumps([{"start": 0, "end": 20, "name": "word " * 200}]))
    result = segmenter.segment(cells, adapter)
    assert len(result.names[0]) <= segmenter.NAME_LIMIT


def test_segment_passes_a_cancel_event_through():
    cells = load("simulation-sweep.ipynb")
    seen: list[Event] = []

    class Recorder(StubAdapter):
        def run_prompt(self, prompt, *, timeout, cancel_event, model=None):
            seen.append(cancel_event)
            return AdapterResult(partition([(0, 20)]))

    event = Event()
    segmenter.segment(cells, Recorder(), cancel_event=event)
    assert seen == [event]
