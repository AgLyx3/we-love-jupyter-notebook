import json
from threading import Event, Thread

import pytest

from backend.app.notebook_document import service as service_module
from backend.app.notebook_document.models import (
    MutationConflict,
    NotebookImportError,
    NotebookSizeError,
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


def test_competing_mutation_fails_without_waiting_for_document_lock(
    monkeypatch, notebook_payload
):
    service = NotebookDocumentService()
    imported = service.import_notebook(notebook_payload())
    validation_started = Event()
    release_validation = Event()
    original_parse = service._parse_and_validate
    replacement_errors = []

    def slow_parse(payload):
        validation_started.set()
        assert release_validation.wait(timeout=2)
        return original_parse(payload)

    monkeypatch.setattr(service, "_parse_and_validate", slow_parse)

    def replace_notebook():
        try:
            service.import_notebook(
                notebook_payload(cell_ids=("new-intro", "new-code")),
                expected_session_id=imported.session_id,
                expected_revision=imported.revision,
            )
        except Exception as error:  # pragma: no cover - asserted after joining
            replacement_errors.append(error)

    competing_errors = []

    def compete():
        try:
            service.update_cell_source(
                cell_id="editable",
                source="blocked = True",
                expected_revision=imported.revision,
                owner="manual",
            )
        except Exception as error:
            competing_errors.append(error)

    replacement = Thread(target=replace_notebook)
    replacement.start()
    assert validation_started.wait(timeout=1)
    competing = Thread(target=compete)
    competing.start()
    competing.join(timeout=0.2)
    returned_before_release = not competing.is_alive()
    release_validation.set()
    replacement.join(timeout=2)
    competing.join(timeout=2)

    assert returned_before_release is True
    assert len(competing_errors) == 1
    assert isinstance(competing_errors[0], MutationConflict)
    assert replacement_errors == []


def test_manual_mutations_have_unique_operation_ids_and_manual_owner(notebook_payload):
    coordinator = MutationCoordinator()
    service = NotebookDocumentService(coordinator)
    imported = service.import_notebook(notebook_payload())
    acquired = []
    original_acquire = coordinator.acquire

    def recording_acquire(**kwargs):
        lease = original_acquire(**kwargs)
        acquired.append(lease)
        return lease

    coordinator.acquire = recording_acquire
    first = service.update_cell_source(
        cell_id="editable",
        source="value = 2",
        expected_revision=imported.revision,
        owner="manual",
    )
    second = service.update_cell_source(
        cell_id="editable",
        source="value = 3",
        expected_revision=first.revision,
        owner="manual",
    )

    assert [lease.operation_type for lease in acquired] == ["manual", "manual"]
    assert acquired[0].operation_id != acquired[1].operation_id
    assert all(lease.operation_id != "manual" for lease in acquired)
    assert second.last_mutation_owner == "manual"


def test_oversized_source_is_rejected_without_mutating_notebook(notebook_payload):
    service = NotebookDocumentService()
    imported = service.import_notebook(notebook_payload())

    with pytest.raises(NotebookSizeError):
        service.update_cell_source(
            cell_id="editable",
            source="x" * MAX_NOTEBOOK_BYTES,
            expected_revision=imported.revision,
            owner="manual",
        )

    assert service.get_snapshot() == imported
    assert service.coordinator.active_lease is None
