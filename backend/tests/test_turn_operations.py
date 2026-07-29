from __future__ import annotations

import pytest

from backend.app.agent_turns.operations import (
    ACCEPTED, MAX_DIFF_LINES, PENDING, REJECTED, TurnOperation,
    build_operations, compose, diff_hunks, is_stale, operation_id, with_state,
)


def build(previous: str, following: str, *, turn_id="turn", cell_id="cell"):
    return build_operations(
        turn_id=turn_id, cell_id=cell_id,
        previous_source=previous, next_source=following,
    )


def composed(previous: str, following: str, operations) -> str:
    return compose(
        previous_source=previous, next_source=following, operations=operations
    )


def reject_all(operations):
    result = operations
    for operation in operations:
        result = with_state(result, operation.operation_id, REJECTED)
    return result


class TestDiffHunks:
    def test_identical_sources_produce_no_operations(self):
        assert diff_hunks("a\nb", "a\nb") == ()

    def test_separate_edits_become_separate_hunks(self):
        previous = "a\nb\nc\nd\ne"
        following = "a\nB\nc\nd\nE"
        hunks = diff_hunks(previous, following)
        assert len(hunks) == 2
        assert [hunk.ordinal for hunk in hunks] == [0, 1]

    def test_adjacent_edits_collapse_into_one_hunk(self):
        hunks = diff_hunks("a\nb\nc", "a\nX\nY\nc")
        assert len(hunks) == 1

    def test_pure_insertion_has_an_empty_previous_range(self):
        (hunk,) = diff_hunks("a\nc", "a\nb\nc")
        assert hunk.prev_start == hunk.prev_end
        assert hunk.next_end - hunk.next_start == 1

    def test_pure_deletion_has_an_empty_next_range(self):
        (hunk,) = diff_hunks("a\nb\nc", "a\nc")
        assert hunk.next_start == hunk.next_end
        assert hunk.prev_end - hunk.prev_start == 1

    def test_oversized_input_falls_back_to_a_single_hunk(self):
        previous = "\n".join(f"line {index}" for index in range(MAX_DIFF_LINES))
        following = "\n".join(f"changed {index}" for index in range(MAX_DIFF_LINES))
        assert len(diff_hunks(previous, following)) == 1

    def test_coarse_fallback_still_preserves_shared_prefix_and_suffix(self):
        shared = "\n".join(f"same {index}" for index in range(MAX_DIFF_LINES))
        previous = f"{shared}\nold\n{shared}"
        following = f"{shared}\nnew\n{shared}"
        (hunk,) = diff_hunks(previous, following)
        # Only the divergent middle is in the hunk, not the whole cell.
        assert hunk.prev_end - hunk.prev_start == 1
        assert hunk.next_end - hunk.next_start == 1


class TestFrontendParity:
    """Hunk boundaries must match what the editor would have drawn.

    The backend owns hunk boundaries so a Keep/Undo button can never act on a
    different region than the one rendered. These expectations were produced by
    running ``cellDiffRanges`` from frontend/src/notebook/cellDiff.ts over the
    same inputs; a 600-case randomised sweep agreed exactly. If the two diffs
    ever drift apart, this is the test that should fail.
    """

    @pytest.mark.parametrize(
        "previous,following,added_lines",
        [
            ("g\nd\nc\nd\nd\nc", "e", [0]),
            ("b\n\nd\ng\nd\nf", "d\nc\n\nc\nf\nb", [0, 1, 3, 5]),
            ("b\n\nf\ng\na\ne\na\ng", "f\nb\n", [0]),
            ("d\n\na\nf\nc\nd\nc\n", "c\nc\na\nb\na\nc\n\ng\nb", [0, 1, 2, 3, 7, 8]),
            ("b\nc\ne\na\ng\nf", "g\nd", [1]),
            ("c\ng", "b", [0]),
            ("c", "c\nc\ng\nc", [1, 2, 3]),
            ("f\n\nd", "b\nf\nc\nb", [0, 2, 3]),
        ],
    )
    def test_added_line_projection_matches_cell_diff_ts(
        self, previous, following, added_lines,
    ):
        projected: list[int] = []
        for hunk in diff_hunks(previous, following):
            projected.extend(range(hunk.next_start, hunk.next_end))
        assert projected == added_lines


class TestCompose:
    @pytest.mark.parametrize(
        "previous,following",
        [
            ("a\nb\nc", "a\nB\nc"),
            ("a\nb\nc\nd\ne", "a\nB\nc\nd\nE"),
            ("a\nc", "a\nb\nc"),
            ("a\nb\nc", "a\nc"),
            ("", "only line"),
            ("only line", ""),
            ("a\nb", "x\ny\nz"),
        ],
    )
    def test_round_trips_in_both_directions(self, previous, following):
        operations = build(previous, following)
        assert composed(previous, following, operations) == following
        assert composed(previous, following, reject_all(operations)) == previous

    def test_accepted_behaves_like_pending(self):
        previous, following = "a\nb\nc", "a\nB\nc"
        operations = build(previous, following)
        accepted = with_state(operations, operations[0].operation_id, ACCEPTED)
        assert composed(previous, following, accepted) == following

    def test_rejecting_one_hunk_leaves_the_others_applied(self):
        previous = "a\nb\nc\nd\ne"
        following = "a\nB\nc\nd\nE"
        operations = build(previous, following)
        partial = with_state(operations, operations[0].operation_id, REJECTED)
        assert composed(previous, following, partial) == "a\nb\nc\nd\nE"

    def test_rejecting_the_other_hunk_is_independent(self):
        previous = "a\nb\nc\nd\ne"
        following = "a\nB\nc\nd\nE"
        operations = build(previous, following)
        partial = with_state(operations, operations[1].operation_id, REJECTED)
        assert composed(previous, following, partial) == "a\nB\nc\nd\ne"

    def test_rejecting_hunks_in_any_order_reaches_the_pre_turn_source(self):
        previous = "a\nb\nc\nd\ne\nf\ng"
        following = "a\nB\nc\nD\ne\nF\ng"
        operations = build(previous, following)
        assert len(operations) == 3
        for order in ([2, 0, 1], [1, 2, 0]):
            state = operations
            for index in order:
                state = with_state(state, operations[index].operation_id, REJECTED)
            assert composed(previous, following, state) == previous

    def test_compose_is_order_independent_of_the_input_sequence(self):
        previous = "a\nb\nc\nd\ne"
        following = "a\nB\nc\nd\nE"
        operations = build(previous, following)
        assert composed(previous, following, tuple(reversed(operations))) == following

    def test_a_rejected_hunk_can_be_restored_by_flipping_state_back(self):
        previous, following = "a\nb\nc", "a\nB\nc"
        operations = build(previous, following)
        rejected = with_state(operations, operations[0].operation_id, REJECTED)
        restored = with_state(rejected, operations[0].operation_id, PENDING)
        assert composed(previous, following, restored) == following


class TestOperationIdentity:
    def test_ids_are_deterministic_across_rebuilds(self):
        first = build("a\nb", "a\nB")
        second = build("a\nb", "a\nB")
        assert [item.operation_id for item in first] == [
            item.operation_id for item in second
        ]

    def test_ids_are_scoped_to_turn_and_cell(self):
        assert operation_id("turn", "cell", 0) == "turn:cell:0"
        one = build("a\nb", "a\nB", turn_id="t1", cell_id="c1")
        two = build("a\nb", "a\nB", turn_id="t2", cell_id="c1")
        assert one[0].operation_id != two[0].operation_id

    def test_default_state_is_pending(self):
        assert all(item.state == PENDING for item in build("a\nb", "a\nB"))

    def test_with_state_leaves_other_operations_untouched(self):
        operations = build("a\nb\nc\nd\ne", "a\nB\nc\nd\nE")
        updated = with_state(operations, operations[0].operation_id, REJECTED)
        assert updated[0].state == REJECTED
        assert updated[1].state == PENDING

    def test_hunks_store_indices_not_line_text(self):
        # The ledger must stay cheap: sources live on the turn's changes, and
        # duplicating hunk text would double per-turn source memory against a
        # bounded history budget.
        (operation,) = build("a\nb\nc", "a\nB\nc")
        stored = vars(operation.hunk).values()
        assert all(isinstance(value, int) for value in stored)


class TestStaleness:
    def test_a_cell_matching_the_ledger_is_not_stale(self):
        previous, following = "a\nb\nc", "a\nB\nc"
        operations = build(previous, following)
        assert not is_stale(
            current_source=following, previous_source=previous,
            next_source=following, operations=operations,
        )

    def test_staleness_tracks_state_changes(self):
        previous, following = "a\nb\nc", "a\nB\nc"
        operations = build(previous, following)
        rejected = with_state(operations, operations[0].operation_id, REJECTED)
        # After rejecting, the cell should hold the pre-turn source.
        assert is_stale(
            current_source=following, previous_source=previous,
            next_source=following, operations=rejected,
        )
        assert not is_stale(
            current_source=previous, previous_source=previous,
            next_source=following, operations=rejected,
        )

    def test_a_manual_edit_makes_the_cell_stale_with_no_reject_attempted(self):
        # The case a stored flag would get wrong: nothing in the ledger changed,
        # only the document did.
        previous, following = "a\nb\nc", "a\nB\nc"
        operations = build(previous, following)
        assert is_stale(
            current_source="a\nB\nc\nmanually added", previous_source=previous,
            next_source=following, operations=operations,
        )


class TestEmptyLedger:
    def test_composing_with_no_operations_returns_the_pre_turn_source(self):
        assert composed("a\nb", "a\nb", ()) == "a\nb"

    def test_a_cell_with_no_operations_is_stale_only_if_it_moved(self):
        assert not is_stale(
            current_source="a\nb", previous_source="a\nb",
            next_source="a\nb", operations=(),
        )
        assert is_stale(
            current_source="a\nX", previous_source="a\nb",
            next_source="a\nb", operations=(),
        )


class TestTypeContract:
    def test_operations_are_immutable(self):
        (operation,) = build("a\nb", "a\nB")
        with pytest.raises(Exception):
            operation.state = REJECTED  # type: ignore[misc]

    def test_kind_defaults_to_source_hunk(self):
        (operation,) = build("a\nb", "a\nB")
        assert operation.kind == "source_hunk"
        assert isinstance(operation, TurnOperation)
