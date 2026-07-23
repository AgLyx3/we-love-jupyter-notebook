import hashlib

import pytest

from backend.app.notebook_document.models import (
    NotebookPathError,
    RevisionConflict,
    SessionConflict,
)
from backend.app.notebook_document.service import NotebookDocumentService


def _open(tmp_path, notebook_payload, name="source.ipynb"):
    file = tmp_path / name
    file.write_bytes(notebook_payload())
    service = NotebookDocumentService()
    snapshot = service.open_notebook_from_path(str(file))
    return service, file, snapshot


def test_save_as_writes_current_bytes_and_rebinds(tmp_path, notebook_payload):
    service, _source, opened = _open(tmp_path, notebook_payload)
    _, export_bytes = service.export_notebook()
    target = tmp_path / "target.ipynb"

    saved = service.save_notebook_as(
        str(target),
        expected_session_id=opened.session_id,
        expected_revision=opened.revision,
    )

    assert target.read_bytes() == export_bytes
    assert saved.notebook_path == str(target.resolve())
    assert saved.filename == "target.ipynb"
    assert saved.workspace_root == str(target.resolve().parent)
    assert saved.dirty is False
    assert saved.revision == opened.revision
    assert service._on_disk_baseline is not None
    assert service._on_disk_baseline.content_hash == hashlib.sha256(export_bytes).hexdigest()
    assert service._on_disk_baseline.mtime_ns == target.stat().st_mtime_ns

    # A subsequent in-place save writes to the NEW path, guarded by the fresh baseline.
    resaved = service.save_notebook_to_disk(
        expected_session_id=saved.session_id,
        expected_revision=saved.revision,
    )
    assert resaved.notebook_path == str(target.resolve())
    _, export_bytes_after = service.export_notebook()
    assert target.read_bytes() == export_bytes_after


def test_save_as_overwrites_existing_target(tmp_path, notebook_payload):
    service, _source, opened = _open(tmp_path, notebook_payload)
    target = tmp_path / "target.ipynb"
    target.write_bytes(b"stale non-notebook contents")
    _, export_bytes = service.export_notebook()

    saved = service.save_notebook_as(
        str(target),
        expected_session_id=opened.session_id,
        expected_revision=opened.revision,
    )

    assert target.read_bytes() == export_bytes
    assert saved.notebook_path == str(target.resolve())


def test_save_as_binds_given_workspace_root(tmp_path, notebook_payload):
    service, _source, opened = _open(tmp_path, notebook_payload)
    root = tmp_path / "root"
    root.mkdir()
    target = root / "target.ipynb"

    saved = service.save_notebook_as(
        str(target),
        expected_session_id=opened.session_id,
        expected_revision=opened.revision,
        workspace_root=str(root),
    )

    assert target.exists()
    assert saved.workspace_root == str(root.resolve())
    assert saved.notebook_path == str(target.resolve())


def test_save_as_rejects_non_ipynb(tmp_path, notebook_payload):
    service, _source, opened = _open(tmp_path, notebook_payload)
    target = tmp_path / "target.txt"

    with pytest.raises(NotebookPathError):
        service.save_notebook_as(
            str(target),
            expected_session_id=opened.session_id,
            expected_revision=opened.revision,
        )
    assert not target.exists()


def test_save_as_rejects_missing_parent_directory(tmp_path, notebook_payload):
    service, _source, opened = _open(tmp_path, notebook_payload)

    with pytest.raises(NotebookPathError):
        service.save_notebook_as(
            str(tmp_path / "nope" / "target.ipynb"),
            expected_session_id=opened.session_id,
            expected_revision=opened.revision,
        )


def test_save_as_rejects_target_outside_workspace_root(tmp_path, notebook_payload):
    service, _source, opened = _open(tmp_path, notebook_payload)
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside.ipynb"

    with pytest.raises(NotebookPathError):
        service.save_notebook_as(
            str(outside),
            expected_session_id=opened.session_id,
            expected_revision=opened.revision,
            workspace_root=str(root),
        )
    assert not outside.exists()


def test_save_as_with_stale_session_raises_conflict(tmp_path, notebook_payload):
    service, _source, opened = _open(tmp_path, notebook_payload)
    target = tmp_path / "target.ipynb"

    with pytest.raises(SessionConflict):
        service.save_notebook_as(
            str(target),
            expected_session_id="not-the-session",
            expected_revision=opened.revision,
        )
    assert not target.exists()


def test_save_as_with_stale_revision_raises_conflict(tmp_path, notebook_payload):
    service, _source, opened = _open(tmp_path, notebook_payload)
    target = tmp_path / "target.ipynb"

    with pytest.raises(RevisionConflict):
        service.save_notebook_as(
            str(target),
            expected_session_id=opened.session_id,
            expected_revision=opened.revision + 5,
        )
    assert not target.exists()
