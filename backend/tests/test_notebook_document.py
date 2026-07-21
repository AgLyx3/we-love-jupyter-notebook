import json

import pytest

from backend.app.notebook_document import service as service_module
from backend.app.notebook_document.models import (
    MutationConflict,
    NotebookImportError,
    RevisionConflict,
)
from backend.app.notebook_document.mutation_coordinator import MutationCoordinator
from backend.app.notebook_document.service import MAX_NOTEBOOK_BYTES, NotebookDocumentService


def test_import_normalizes_missing_invalid_and_duplicate_cell_ids():
    notebook = {
        "cells": [
            {"cell_type": "markdown", "metadata": {}, "source": ["missing"]},
            {"cell_type": "markdown", "id": "bad id!", "metadata": {}, "source": ["invalid"]},
            {"cell_type": "markdown", "id": "kept", "metadata": {}, "source": ["first"]},
            {"cell_type": "markdown", "id": "kept", "metadata": {}, "source": ["duplicate"]},
        ],
        "metadata": {},
        "nbformat": 4,
        "nbformat_minor": 5,
    }

    service = NotebookDocumentService()
    snapshot = service.import_notebook(json.dumps(notebook).encode(), filename="ids.ipynb")

    ids = [cell["id"] for cell in snapshot.notebook["cells"]]
    assert len(ids) == len(set(ids))
    assert ids[2] == "kept"
    assert all(1 <= len(cell_id) <= 64 for cell_id in ids)
    assert snapshot.revision == 1
    assert snapshot.dirty is True


def test_import_of_valid_notebook_starts_clean(notebook_payload):
    snapshot = NotebookDocumentService().import_notebook(
        notebook_payload(), filename="valid.ipynb"
    )

    assert snapshot.revision == 0
    assert snapshot.dirty is False
    assert snapshot.filename == "valid.ipynb"


@pytest.mark.parametrize(
    "payload",
    [
        b"not json",
        json.dumps([]).encode(),
        json.dumps({"cells": {}, "metadata": {}, "nbformat": 4, "nbformat_minor": 5}).encode(),
        json.dumps({"cells": [], "metadata": {}, "nbformat": 3, "nbformat_minor": 0}).encode(),
    ],
)
def test_import_rejects_invalid_notebooks(payload):
    with pytest.raises(NotebookImportError):
        NotebookDocumentService().import_notebook(payload, filename="bad.ipynb")


def test_import_rejects_oversized_notebook():
    with pytest.raises(NotebookImportError) as error:
        NotebookDocumentService().import_notebook(b" " * (MAX_NOTEBOOK_BYTES + 1))

    assert error.value.code == "notebook_too_large"


def test_source_edit_is_revision_guarded_and_snapshots_are_isolated(notebook_payload):
    service = NotebookDocumentService()
    imported = service.import_notebook(notebook_payload())
    before = service.get_snapshot()

    edited = service.update_cell_source(
        cell_id="editable",
        source="value = 2\n",
        expected_revision=imported.revision,
        owner="manual",
    )

    assert edited.revision == imported.revision + 1
    assert edited.dirty is True
    assert edited.notebook["cells"][1]["source"] == "value = 2\n"
    assert before.notebook["cells"][1]["source"] == ["value = 1\n"]
    with pytest.raises(RevisionConflict) as error:
        service.update_cell_source(
            cell_id="editable",
            source="value = 3\n",
            expected_revision=imported.revision,
            owner="manual",
        )
    assert error.value.current_revision == edited.revision


def test_failed_import_does_not_replace_active_session(notebook_payload):
    service = NotebookDocumentService()
    original = service.import_notebook(notebook_payload(), filename="original.ipynb")

    with pytest.raises(NotebookImportError):
        service.import_notebook(b"{}", filename="broken.ipynb")

    current = service.get_snapshot()
    assert current.session_id == original.session_id
    assert current.filename == "original.ipynb"


def test_mutation_coordinator_rejects_competing_owner():
    coordinator = MutationCoordinator()
    lease = coordinator.acquire(operation_type="agent_turn", operation_id="turn-1")

    with pytest.raises(MutationConflict) as error:
        coordinator.acquire(operation_type="manual_edit", operation_id="manual")

    assert error.value.active_operation_type == "agent_turn"
    assert error.value.active_operation_id == "turn-1"
    assert coordinator.release(lease) is True
    assert coordinator.active_lease is None


def test_source_edit_reports_active_owner_and_preserves_document(notebook_payload):
    coordinator = MutationCoordinator()
    service = NotebookDocumentService(coordinator)
    imported = service.import_notebook(notebook_payload())
    lease = coordinator.acquire(operation_type="agent_turn", operation_id="turn-1")

    with pytest.raises(MutationConflict) as error:
        service.update_cell_source(
            cell_id="editable",
            source="should_not_apply = True",
            expected_revision=imported.revision,
            owner="manual",
        )

    assert error.value.details == {
        "activeOperationType": "agent_turn",
        "activeOperationId": "turn-1",
        "currentDocumentRevision": imported.revision,
    }
    assert service.get_snapshot().notebook == imported.notebook
    coordinator.release(lease)


def test_active_owner_takes_precedence_over_stale_source_preconditions(notebook_payload):
    coordinator = MutationCoordinator()
    service = NotebookDocumentService(coordinator)
    imported = service.import_notebook(notebook_payload())
    lease = coordinator.acquire(operation_type="agent_turn", operation_id="turn-1")

    with pytest.raises(MutationConflict) as error:
        service.update_cell_source(
            cell_id="missing",
            source="should_not_apply = True",
            expected_revision=imported.revision + 100,
            expected_session_id="stale-session",
            owner="manual",
        )

    assert error.value.details["activeOperationId"] == "turn-1"
    assert error.value.details["currentDocumentRevision"] == imported.revision
    coordinator.release(lease)


def test_active_owner_takes_precedence_over_invalid_replacement(notebook_payload):
    coordinator = MutationCoordinator()
    service = NotebookDocumentService(coordinator)
    imported = service.import_notebook(notebook_payload())
    lease = coordinator.acquire(operation_type="agent_turn", operation_id="turn-1")

    with pytest.raises(MutationConflict) as error:
        service.import_notebook(
            b"not-json",
            expected_session_id="stale-session",
            expected_revision=imported.revision + 100,
        )

    assert error.value.details["activeOperationId"] == "turn-1"
    coordinator.release(lease)


def test_generated_id_does_not_displace_valid_later_id(monkeypatch):
    generated_ids = iter(("futureid", "generated"))
    monkeypatch.setattr(
        service_module,
        "_new_cell_id",
        lambda: next(generated_ids),
        raising=False,
    )
    notebook = {
        "cells": [
            {"cell_type": "markdown", "metadata": {}, "source": ["missing"]},
            {
                "cell_type": "markdown",
                "id": "futureid",
                "metadata": {},
                "source": ["preserve me"],
            },
        ],
        "metadata": {},
        "nbformat": 4,
        "nbformat_minor": 5,
    }

    snapshot = NotebookDocumentService().import_notebook(json.dumps(notebook).encode())

    assert [cell["id"] for cell in snapshot.notebook["cells"]] == [
        "generated",
        "futureid",
    ]
