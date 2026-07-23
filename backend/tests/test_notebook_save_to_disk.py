import hashlib
import os

import pytest

from backend.app.notebook_document.models import (
    ExternalModificationConflict,
    NotebookPathError,
    RevisionConflict,
    SessionConflict,
)
from backend.app.notebook_document.service import NotebookDocumentService


def _open(tmp_path, notebook_payload, name="analysis.ipynb"):
    file = tmp_path / name
    file.write_bytes(notebook_payload())
    service = NotebookDocumentService()
    snapshot = service.open_notebook_from_path(str(file))
    return service, file, snapshot


def test_save_writes_export_bytes_and_records_baseline(tmp_path, notebook_payload):
    service, file, opened = _open(tmp_path, notebook_payload)
    export_name, export_bytes = service.export_notebook()

    saved = service.save_notebook_to_disk(
        expected_session_id=opened.session_id,
        expected_revision=opened.revision,
    )

    assert file.read_bytes() == export_bytes
    assert saved.dirty is False
    assert saved.revision == opened.revision
    written_hash = hashlib.sha256(export_bytes).hexdigest()
    assert service._on_disk_baseline is not None
    assert service._on_disk_baseline.content_hash == written_hash
    assert service._on_disk_baseline.mtime_ns == file.stat().st_mtime_ns


def test_save_persists_in_memory_edit_and_round_trips(tmp_path, notebook_payload):
    service, file, opened = _open(tmp_path, notebook_payload)
    edited = service.update_cell_source(
        cell_id="editable",
        source="value = 99\n",
        expected_revision=opened.revision,
        owner="user",
    )
    assert edited.dirty is True
    assert edited.revision == opened.revision + 1

    saved = service.save_notebook_to_disk(
        expected_session_id=edited.session_id,
        expected_revision=edited.revision,
    )
    assert saved.dirty is False
    assert saved.revision == edited.revision
    _, export_bytes = service.export_notebook()
    assert file.read_bytes() == export_bytes
    assert service._on_disk_baseline.content_hash == hashlib.sha256(export_bytes).hexdigest()

    reopened = NotebookDocumentService().open_notebook_from_path(str(file))
    editable = next(c for c in reopened.notebook["cells"] if c["id"] == "editable")
    assert "value = 99" in "".join(editable["source"])


def test_save_refuses_and_preserves_external_modification(tmp_path, notebook_payload):
    service, file, opened = _open(tmp_path, notebook_payload)
    external = notebook_payload(cell_ids=("intro", "outside"))
    file.write_bytes(external)

    with pytest.raises(ExternalModificationConflict):
        service.save_notebook_to_disk(
            expected_session_id=opened.session_id,
            expected_revision=opened.revision,
        )

    assert file.read_bytes() == external
    snapshot = service.get_snapshot()
    assert snapshot.revision == opened.revision
    assert snapshot.dirty == opened.dirty


def test_save_without_path_raises_notebook_path_error(notebook_payload):
    service = NotebookDocumentService()
    opened = service.import_notebook(notebook_payload())
    with pytest.raises(NotebookPathError):
        service.save_notebook_to_disk(
            expected_session_id=opened.session_id,
            expected_revision=opened.revision,
        )


def test_save_with_stale_session_raises_session_conflict(tmp_path, notebook_payload):
    service, _, opened = _open(tmp_path, notebook_payload)
    with pytest.raises(SessionConflict):
        service.save_notebook_to_disk(
            expected_session_id="not-the-session",
            expected_revision=opened.revision,
        )


def test_save_with_stale_revision_raises_revision_conflict(tmp_path, notebook_payload):
    service, _, opened = _open(tmp_path, notebook_payload)
    with pytest.raises(RevisionConflict):
        service.save_notebook_to_disk(
            expected_session_id=opened.session_id,
            expected_revision=opened.revision + 5,
        )


def test_save_leaves_no_temp_files(tmp_path, notebook_payload):
    service, file, opened = _open(tmp_path, notebook_payload)
    service.save_notebook_to_disk(
        expected_session_id=opened.session_id,
        expected_revision=opened.revision,
    )
    assert [p.name for p in tmp_path.iterdir()] == [file.name]
