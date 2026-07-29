"""Per-operation review: accept, reject, staleness, lineage and pruning.

Covers the service-level behaviour built on backend/app/agent_turns/operations.py.
The pure diff/compose layer is tested in test_turn_operations.py.
"""

from __future__ import annotations

import json

import pytest

from backend.app.agent_turns.operations import ACCEPTED, PENDING, REJECTED
from backend.app.agent_turns.service import (
    AgentTurnService, MAX_PENDING_REVIEW_TURNS, OperationConflict,
    OperationNotFound, RevertConflict, UndoConflict,
)
from backend.app.agent_workspace.adapters import FakeAgentAdapter, FakeAttempt
from backend.app.notebook_document.models import SessionConflict
from backend.app.notebook_document.service import NotebookDocumentService
from backend.app.turn_scope.service import TurnScopeService


ORIGINAL = "value = 1\n"
# Two separated edits so the turn yields two independent operations.
THREE_LINE = "a = 1\nb = 2\nc = 3\n"
THREE_LINE_BOTH_EDITED = "a = 99\nb = 2\nc = 99\n"


def services(notebook_payload, attempts, *, source=None):
    documents = NotebookDocumentService()
    snapshot = documents.import_notebook(notebook_payload())
    if source is not None:
        snapshot = documents.update_cell_source(
            cell_id="editable", source=source,
            expected_revision=snapshot.revision,
            expected_session_id=snapshot.session_id, owner="manual",
        )
    scopes = TurnScopeService(documents)
    scopes.add("editable", editable=True)
    turns = AgentTurnService(
        documents=documents, scopes=scopes,
        adapter=FakeAgentAdapter(attempts), timeout=1,
    )
    return documents, scopes, turns, snapshot


def source_of(snapshot, cell_id="editable"):
    cell = next(item for item in snapshot.notebook["cells"] if item["id"] == cell_id)
    value = cell["source"]
    return "".join(value) if isinstance(value, list) else value


def two_operation_turn(notebook_payload):
    documents, scopes, turns, snapshot = services(
        notebook_payload,
        [FakeAttempt(edits={"editable/cell_editable.py": THREE_LINE_BOTH_EDITED})],
        source=THREE_LINE,
    )
    turn = turns.start(
        prompt="edit", session_id=snapshot.session_id,
        expected_revision=snapshot.revision, background=False,
    )
    assert len(turn.operations) == 2, "fixture must produce two hunks"
    return documents, turns, snapshot, turn


class TestLedgerConstruction:
    def test_applied_turn_records_operations_per_hunk(self, notebook_payload):
        _documents, _turns, _snapshot, turn = two_operation_turn(notebook_payload)
        assert [item.state for item in turn.operations] == [PENDING, PENDING]
        assert {item.cell_id for item in turn.operations} == {"editable"}

    def test_a_turn_with_no_changes_has_no_operations(self, notebook_payload):
        _documents, _scopes, turns, snapshot = services(
            notebook_payload, [FakeAttempt(edits={})]
        )
        turn = turns.start(
            prompt="noop", session_id=snapshot.session_id,
            expected_revision=snapshot.revision, background=False,
        )
        assert turn.operations == ()


class TestAccept:
    def test_accept_settles_review_without_touching_the_document(
        self, notebook_payload,
    ):
        documents, turns, snapshot, turn = two_operation_turn(notebook_payload)
        before = documents.get_snapshot()

        updated = turns.accept_operations(
            turn.turn_id, [turn.operations[0].operation_id],
            session_id=snapshot.session_id,
        )

        after = documents.get_snapshot()
        assert after.revision == before.revision
        assert source_of(after) == THREE_LINE_BOTH_EDITED
        assert updated.operations[0].state == ACCEPTED
        assert updated.operations[1].state == PENDING

    def test_accept_does_not_break_undo_lineage(self, notebook_payload):
        _documents, turns, snapshot, turn = two_operation_turn(notebook_payload)
        turns.accept_operations(
            turn.turn_id, None, session_id=snapshot.session_id
        )
        assert turns.is_undo_eligible(turns.get(turn.turn_id))

    def test_accept_all_settles_every_pending_operation(self, notebook_payload):
        _documents, turns, snapshot, turn = two_operation_turn(notebook_payload)
        updated = turns.accept_operations(
            turn.turn_id, None, session_id=snapshot.session_id
        )
        assert all(item.state == ACCEPTED for item in updated.operations)

    def test_accept_is_idempotent(self, notebook_payload):
        _documents, turns, snapshot, turn = two_operation_turn(notebook_payload)
        target = [turn.operations[0].operation_id]
        turns.accept_operations(turn.turn_id, target, session_id=snapshot.session_id)
        updated = turns.accept_operations(
            turn.turn_id, target, session_id=snapshot.session_id
        )
        assert updated.operations[0].state == ACCEPTED

    def test_accepting_a_rejected_operation_conflicts(self, notebook_payload):
        _documents, turns, snapshot, turn = two_operation_turn(notebook_payload)
        target = turn.operations[0].operation_id
        current = turns.reject_operations(
            turn.turn_id, [target], session_id=snapshot.session_id,
            expected_revision=turn.applied_revision,
        )
        # Re-applying undone content behind an "accept" would be silent.
        with pytest.raises(OperationConflict):
            turns.accept_operations(
                turn.turn_id, [target], session_id=snapshot.session_id
            )
        assert source_of(current) == "a = 1\nb = 2\nc = 99\n"

    def test_accept_rejects_a_foreign_session(self, notebook_payload):
        _documents, turns, _snapshot, turn = two_operation_turn(notebook_payload)
        with pytest.raises(SessionConflict):
            turns.accept_operations(
                turn.turn_id, None, session_id="not-the-session"
            )

    def test_unknown_operation_id_is_not_found(self, notebook_payload):
        _documents, turns, snapshot, turn = two_operation_turn(notebook_payload)
        with pytest.raises(OperationNotFound):
            turns.accept_operations(
                turn.turn_id, ["nope"], session_id=snapshot.session_id
            )


class TestReject:
    def test_rejecting_one_hunk_leaves_the_other_applied(self, notebook_payload):
        _documents, turns, snapshot, turn = two_operation_turn(notebook_payload)
        updated = turns.reject_operations(
            turn.turn_id, [turn.operations[0].operation_id],
            session_id=snapshot.session_id, expected_revision=turn.applied_revision,
        )
        assert source_of(updated) == "a = 1\nb = 2\nc = 99\n"

    def test_rejecting_both_hunks_restores_the_pre_turn_source(
        self, notebook_payload,
    ):
        _documents, turns, snapshot, turn = two_operation_turn(notebook_payload)
        first = turns.reject_operations(
            turn.turn_id, [turn.operations[0].operation_id],
            session_id=snapshot.session_id, expected_revision=turn.applied_revision,
        )
        second = turns.reject_operations(
            turn.turn_id, [turn.operations[1].operation_id],
            session_id=snapshot.session_id, expected_revision=first.revision,
        )
        assert source_of(second) == THREE_LINE

    def test_reject_all_restores_the_pre_turn_source_in_one_mutation(
        self, notebook_payload,
    ):
        _documents, turns, snapshot, turn = two_operation_turn(notebook_payload)
        updated = turns.reject_operations(
            turn.turn_id, None, session_id=snapshot.session_id,
            expected_revision=turn.applied_revision,
        )
        assert source_of(updated) == THREE_LINE
        assert updated.revision == turn.applied_revision + 1

    def test_reject_all_preserves_already_accepted_operations(
        self, notebook_payload,
    ):
        """The difference between reject-all and whole-turn undo."""
        _documents, turns, snapshot, turn = two_operation_turn(notebook_payload)
        turns.accept_operations(
            turn.turn_id, [turn.operations[0].operation_id],
            session_id=snapshot.session_id,
        )
        updated = turns.reject_operations(
            turn.turn_id, None, session_id=snapshot.session_id,
            expected_revision=turn.applied_revision,
        )
        assert source_of(updated) == "a = 99\nb = 2\nc = 3\n"

    def test_a_manual_edit_blocks_further_rejection_of_that_cell(
        self, notebook_payload,
    ):
        documents, turns, snapshot, turn = two_operation_turn(notebook_payload)
        edited = documents.update_cell_source(
            cell_id="editable", source="totally different\n",
            expected_revision=turn.applied_revision,
            expected_session_id=snapshot.session_id, owner="manual",
        )
        with pytest.raises(OperationConflict):
            turns.reject_operations(
                turn.turn_id, [turn.operations[0].operation_id],
                session_id=snapshot.session_id, expected_revision=edited.revision,
            )

    def test_rejecting_an_already_rejected_operation_is_idempotent(
        self, notebook_payload,
    ):
        _documents, turns, snapshot, turn = two_operation_turn(notebook_payload)
        target = [turn.operations[0].operation_id]
        first = turns.reject_operations(
            turn.turn_id, target, session_id=snapshot.session_id,
            expected_revision=turn.applied_revision,
        )
        second = turns.reject_operations(
            turn.turn_id, target, session_id=snapshot.session_id,
            expected_revision=first.revision,
        )
        assert second.revision == first.revision
        assert source_of(second) == "a = 1\nb = 2\nc = 99\n"

    def test_reject_releases_the_mutation_lease(self, notebook_payload):
        documents, turns, snapshot, turn = two_operation_turn(notebook_payload)
        turns.reject_operations(
            turn.turn_id, None, session_id=snapshot.session_id,
            expected_revision=turn.applied_revision,
        )
        assert documents.coordinator.active_lease is None


class TestUndoSurvivesItsOwnRejections:
    """The gap that made partial review self-defeating.

    Undoing one hunk used to advance the document past the turn's recorded
    revision, which made the checkpoint ineligible *and dropped it*, so
    rejecting one change destroyed the ability to undo the rest.
    """

    def test_whole_turn_undo_survives_a_partial_reject(self, notebook_payload):
        _documents, turns, snapshot, turn = two_operation_turn(notebook_payload)
        updated = turns.reject_operations(
            turn.turn_id, [turn.operations[0].operation_id],
            session_id=snapshot.session_id, expected_revision=turn.applied_revision,
        )

        assert turns.is_undo_eligible(turns.get(turn.turn_id))
        restored = turns.undo(
            turn.turn_id, session_id=snapshot.session_id,
            expected_revision=updated.revision,
        )
        assert source_of(restored) == THREE_LINE

    def test_undo_restores_accepted_changes_too(self, notebook_payload):
        _documents, turns, snapshot, turn = two_operation_turn(notebook_payload)
        turns.accept_operations(
            turn.turn_id, [turn.operations[0].operation_id],
            session_id=snapshot.session_id,
        )
        restored = turns.undo(
            turn.turn_id, session_id=snapshot.session_id,
            expected_revision=turn.applied_revision,
        )
        assert source_of(restored) == THREE_LINE

    def test_an_unrelated_manual_edit_still_ends_undo(self, notebook_payload):
        documents, turns, snapshot, turn = two_operation_turn(notebook_payload)
        changed = documents.update_cell_source(
            cell_id="intro", source="# manual\n",
            expected_revision=turn.applied_revision,
            expected_session_id=snapshot.session_id, owner="manual",
        )
        assert not turns.is_undo_eligible(turns.get(turn.turn_id))
        assert turns.get(turn.turn_id).checkpoint is None
        with pytest.raises(UndoConflict):
            turns.undo(
                turn.turn_id, session_id=snapshot.session_id,
                expected_revision=changed.revision,
            )

    @pytest.mark.parametrize("owner_template", ["{turn_id}", "reject:{turn_id}"])
    def test_a_poll_inside_the_commit_window_does_not_destroy_the_checkpoint(
        self, notebook_payload, owner_template,
    ):
        """The race, reproduced deterministically rather than by threading.

        ``is_undo_eligible`` has a destructive side effect and is called from
        the polled session-status endpoint. Between committing a document
        mutation and updating the turn's bookkeeping, the document sits ahead of
        ``applied_revision``; a poll landing there used to drop the checkpoint
        permanently. Rather than race two threads and hope to land in the
        window, put the service in exactly that state and poll.

        Both owners matter: ``reject:`` is per-operation review, and the bare
        turn id is downstream execution, which has always had this window.
        """
        documents, turns, snapshot, turn = two_operation_turn(notebook_payload)
        owner = owner_template.format(turn_id=turn.turn_id)

        lease = documents.coordinator.acquire(
            operation_type="agent_reject", operation_id=turn.turn_id,
        )
        try:
            documents.apply_source_changes_under_lease(
                changes={"editable": "a = 1\nb = 2\nc = 99\n"},
                expected_revision=turn.applied_revision,
                owner=owner, lease=lease,
            )
        finally:
            documents.coordinator.release(lease)

        stored = turns.get(turn.turn_id)
        assert stored.applied_revision < documents.get_snapshot().revision

        assert turns.is_undo_eligible(stored)
        assert turns.get(turn.turn_id).checkpoint is not None

    def test_a_poll_after_a_foreign_mutation_still_ends_undo(
        self, notebook_payload,
    ):
        """The owner check must not blanket-forgive every revision gap."""
        documents, turns, snapshot, turn = two_operation_turn(notebook_payload)
        lease = documents.coordinator.acquire(
            operation_type="manual", operation_id="someone-else",
        )
        try:
            documents.apply_source_changes_under_lease(
                changes={"editable": "someone else wrote this\n"},
                expected_revision=turn.applied_revision,
                owner="manual", lease=lease,
            )
        finally:
            documents.coordinator.release(lease)

        assert not turns.is_undo_eligible(turns.get(turn.turn_id))
        assert turns.get(turn.turn_id).checkpoint is None

    def test_repeated_rejects_keep_the_checkpoint_across_polls(
        self, notebook_payload,
    ):
        _documents, turns, snapshot, turn = two_operation_turn(notebook_payload)
        revision = turn.applied_revision
        for operation in turn.operations:
            updated = turns.reject_operations(
                turn.turn_id, [operation.operation_id],
                session_id=snapshot.session_id, expected_revision=revision,
            )
            revision = updated.revision
            assert turns.is_undo_eligible(turns.get(turn.turn_id))

        restored = turns.undo(
            turn.turn_id, session_id=snapshot.session_id,
            expected_revision=revision,
        )
        assert source_of(restored) == THREE_LINE


class TestUndoSettlesTheLedger:
    def test_whole_turn_undo_settles_every_operation(self, notebook_payload):
        """Otherwise the cell keeps advertising a change that is already gone.

        Leaving operations pending after a checkpoint restore strands them: they
        no longer match the document, so they serialize as stale and the cell
        shows a permanent "can no longer be undone" banner for work that was
        already reverted.
        """
        _documents, turns, snapshot, turn = two_operation_turn(notebook_payload)
        turns.undo(
            turn.turn_id, session_id=snapshot.session_id,
            expected_revision=turn.applied_revision,
        )
        settled = turns.get(turn.turn_id)
        assert all(item.state == REJECTED for item in settled.operations)
        assert turns.stale_cell_ids(settled) == frozenset()


class TestConcurrentReview:
    def test_reject_does_not_clobber_an_accept_of_another_operation(
        self, notebook_payload,
    ):
        """Accept takes no lease, so it can land while a reject is committing.

        Rejecting used to write back the whole ledger tuple it captured before
        the document commit, silently rolling any such accept back to pending.
        """
        documents, turns, snapshot, turn = two_operation_turn(notebook_payload)
        first, second = turn.operations

        original = documents.apply_source_changes_under_lease

        def accept_during_commit(**kwargs):
            turns.accept_operations(
                turn.turn_id, [second.operation_id],
                session_id=snapshot.session_id,
            )
            return original(**kwargs)

        documents.apply_source_changes_under_lease = accept_during_commit
        try:
            turns.reject_operations(
                turn.turn_id, [first.operation_id],
                session_id=snapshot.session_id,
                expected_revision=turn.applied_revision,
            )
        finally:
            documents.apply_source_changes_under_lease = original

        states = {item.operation_id: item.state for item in turns.get(turn.turn_id).operations}
        assert states[first.operation_id] == REJECTED
        assert states[second.operation_id] == ACCEPTED


class TestRejectAllWithStaleCells:
    def test_a_stale_cell_does_not_block_undoing_the_rest(self, notebook_payload):
        documents = NotebookDocumentService()
        snapshot = documents.import_notebook(notebook_payload())
        snapshot = documents.update_cell_source(
            cell_id="editable", source=THREE_LINE,
            expected_revision=snapshot.revision,
            expected_session_id=snapshot.session_id, owner="manual",
        )
        scopes = TurnScopeService(documents)
        scopes.add("editable", editable=True)
        scopes.add("intro", editable=True)
        turns = AgentTurnService(
            documents=documents, scopes=scopes,
            adapter=FakeAgentAdapter([FakeAttempt(edits={
                "editable/cell_editable.py": THREE_LINE_BOTH_EDITED,
                "editable/cell_intro.md": "# agent wrote this\n",
            })]), timeout=1,
        )
        turn = turns.start(
            prompt="edit", session_id=snapshot.session_id,
            expected_revision=snapshot.revision, background=False,
        )
        edited = documents.update_cell_source(
            cell_id="editable", source="hand written\n",
            expected_revision=turn.applied_revision,
            expected_session_id=snapshot.session_id, owner="manual",
        )

        updated = turns.reject_operations(
            turn.turn_id, None, session_id=snapshot.session_id,
            expected_revision=edited.revision,
        )

        # The untouched cell is undone; the hand-edited one keeps both its
        # content and its operations so it can still explain itself.
        assert source_of(updated, "intro") == "# Example notebook\n"
        assert source_of(updated, "editable") == "hand written\n"
        assert turns.stale_cell_ids(turns.get(turn.turn_id)) == frozenset({"editable"})


class TestTrustedTurnsHaveNoLedger:
    """Per-operation review covers source hunks against stable cell ids.

    A Trusted turn rewrites the whole notebook — it may add, delete, reorder and
    retype cells — so a per-hunk recompose against a cell id is not well defined.
    Those turns stay whole-turn undo only, and the ledger must stay empty rather
    than half-describing a structural change.
    """

    def trusted_turn(self, notebook_payload):
        documents = NotebookDocumentService()
        snapshot = documents.import_notebook(notebook_payload())
        scopes = TurnScopeService(documents)
        structure = json.dumps({"cells": [
            {"cellId": "intro", "cellType": "markdown", "source": "cells/cell_intro.md"},
            {"cellId": "editable", "cellType": "code", "source": "cells/cell_editable.py"},
            {"op": "add", "cellType": "markdown", "source": "cells/new_1.md"},
        ]})
        turns = AgentTurnService(
            documents=documents, scopes=scopes,
            adapter=FakeAgentAdapter([FakeAttempt(edits={
                "structure.json": structure,
                "cells/cell_editable.py": "value = 99\n",
                "cells/new_1.md": "## summary\n",
            })]), timeout=1,
        )
        turn = turns.start(
            prompt="restructure", session_id=snapshot.session_id,
            expected_revision=snapshot.revision, write_scope="trusted",
            background=False,
        )
        assert turn.state == "completed", turn.error
        return documents, turns, snapshot, turn

    def test_a_trusted_turn_records_changes_but_no_operations(
        self, notebook_payload,
    ):
        _documents, _turns, _snapshot, turn = self.trusted_turn(notebook_payload)
        # changes are populated for the inline diff, deliberately display-only.
        assert turn.changes
        assert turn.operations == ()
        assert turn.structural_ops

    def test_per_cell_revert_is_refused_on_a_trusted_turn(self, notebook_payload):
        _documents, turns, snapshot, turn = self.trusted_turn(notebook_payload)
        with pytest.raises(RevertConflict):
            turns.revert_cell(
                turn.turn_id, "editable", session_id=snapshot.session_id,
                expected_revision=turn.applied_revision,
            )

    def test_reject_all_is_a_no_op_on_a_trusted_turn(self, notebook_payload):
        documents, turns, snapshot, turn = self.trusted_turn(notebook_payload)
        before = documents.get_snapshot()
        updated = turns.reject_operations(
            turn.turn_id, None, session_id=snapshot.session_id,
            expected_revision=turn.applied_revision,
        )
        assert updated.revision == before.revision

    def test_a_trusted_turn_reports_no_stale_cells(self, notebook_payload):
        _documents, turns, _snapshot, turn = self.trusted_turn(notebook_payload)
        # An empty ledger must not be read as "everything drifted", which would
        # put a "can no longer be undone" banner on every changed cell.
        assert turns.stale_cell_ids(turns.get(turn.turn_id)) == frozenset()

    def test_whole_turn_undo_still_works(self, notebook_payload):
        documents, turns, snapshot, turn = self.trusted_turn(notebook_payload)
        assert turns.is_undo_eligible(turns.get(turn.turn_id))
        restored = turns.undo(
            turn.turn_id, session_id=snapshot.session_id,
            expected_revision=turn.applied_revision,
        )
        assert len(restored.notebook["cells"]) == 2
        assert source_of(restored) == "value = 1\n"


class TestStalenessIsDerived:
    def test_a_manual_edit_marks_the_cell_stale_with_no_reject_attempted(
        self, notebook_payload,
    ):
        documents, turns, snapshot, turn = two_operation_turn(notebook_payload)
        assert turns.stale_cell_ids(turns.get(turn.turn_id)) == frozenset()

        documents.update_cell_source(
            cell_id="editable", source="hand written\n",
            expected_revision=turn.applied_revision,
            expected_session_id=snapshot.session_id, owner="manual",
        )
        # Nothing in the ledger changed — only the document did.
        assert turns.stale_cell_ids(turns.get(turn.turn_id)) == frozenset({"editable"})

    def test_rejecting_keeps_the_cell_fresh(self, notebook_payload):
        _documents, turns, snapshot, turn = two_operation_turn(notebook_payload)
        turns.reject_operations(
            turn.turn_id, [turn.operations[0].operation_id],
            session_id=snapshot.session_id, expected_revision=turn.applied_revision,
        )
        assert turns.stale_cell_ids(turns.get(turn.turn_id)) == frozenset()


class TestRevertCellOnTheLedger:
    def test_revert_cell_undoes_every_operation_for_that_cell(
        self, notebook_payload,
    ):
        _documents, turns, snapshot, turn = two_operation_turn(notebook_payload)
        updated = turns.revert_cell(
            turn.turn_id, "editable", session_id=snapshot.session_id,
            expected_revision=turn.applied_revision,
        )
        assert source_of(updated) == THREE_LINE
        assert all(item.state == REJECTED for item in turns.get(turn.turn_id).operations)

    def test_revert_cell_still_raises_revert_conflict_on_drift(
        self, notebook_payload,
    ):
        documents, turns, snapshot, turn = two_operation_turn(notebook_payload)
        edited = documents.update_cell_source(
            cell_id="editable", source="manual\n",
            expected_revision=turn.applied_revision,
            expected_session_id=snapshot.session_id, owner="manual",
        )
        with pytest.raises(RevertConflict):
            turns.revert_cell(
                turn.turn_id, "editable", session_id=snapshot.session_id,
                expected_revision=edited.revision,
            )


class TestPruningProtectsUnfinishedReview:
    def test_turns_with_pending_operations_survive_the_count_limit(
        self, notebook_payload,
    ):
        documents = NotebookDocumentService()
        snapshot = documents.import_notebook(notebook_payload())
        scopes = TurnScopeService(documents)
        attempts = [
            FakeAttempt(edits={"editable/cell_editable.py": f"value = {index}\n"})
            for index in range(2, 14)
        ]
        turns = AgentTurnService(
            documents=documents, scopes=scopes,
            adapter=FakeAgentAdapter(attempts), timeout=1,
        )
        created = []
        current = snapshot
        for index in range(12):
            scopes.add("editable", editable=True)
            created.append(turns.start(
                prompt=f"turn {index}", session_id=current.session_id,
                expected_revision=current.revision, background=False,
            ))
            current = documents.get_snapshot()

        retained = {
            item.turn_id for item in turns.history_for_session(
                snapshot.session_id, limit=100
            )
        }
        newest = [item.turn_id for item in created[-MAX_PENDING_REVIEW_TURNS:]]
        assert set(newest) <= retained, "unreviewed turns must keep a live ledger"

    def test_turns_past_the_ceiling_are_auto_kept_not_silently_dropped(
        self, notebook_payload,
    ):
        documents = NotebookDocumentService()
        snapshot = documents.import_notebook(notebook_payload())
        scopes = TurnScopeService(documents)
        attempts = [
            FakeAttempt(edits={"editable/cell_editable.py": f"value = {index}\n"})
            for index in range(2, 16)
        ]
        turns = AgentTurnService(
            documents=documents, scopes=scopes,
            adapter=FakeAgentAdapter(attempts), timeout=1,
        )
        created = []
        current = snapshot
        for index in range(14):
            scopes.add("editable", editable=True)
            created.append(turns.start(
                prompt=f"turn {index}", session_id=current.session_id,
                expected_revision=current.revision, background=False,
            ))
            current = documents.get_snapshot()

        history = {
            item.turn_id: item
            for item in turns.history_for_session(snapshot.session_id, limit=100)
        }
        settled = [
            item for turn_id, item in history.items()
            if turn_id in {entry.turn_id for entry in created[:2]}
        ]
        # Whatever survived from the oldest turns must be settled, never left
        # pending-but-unreachable. Content is untouched either way.
        assert all(
            operation.state != PENDING
            for item in settled for operation in item.operations
        )
        assert source_of(documents.get_snapshot()) == "value = 15\n"
