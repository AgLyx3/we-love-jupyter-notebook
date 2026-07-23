import hashlib
import json
import os

import pytest

from backend.app.notebook_document.models import (
    ExternalModificationConflict,
    NotebookPathError,
    NotebookSizeError,
    ReplacementPreconditionRequired,
)
from backend.app.notebook_document.service import (
    MAX_NOTEBOOK_BYTES,
    NotebookDocumentService,
)


def _write(path, payload: bytes) -> None:
    path.write_bytes(payload)


def _notebook_needing_normalization() -> bytes:
    return json.dumps(
        {
            "cells": [
                {"cell_type": "markdown", "metadata": {}, "source": ["# missing id\n"]},
                {
                    "cell_type": "code",
                    "id": "bad id!",
                    "metadata": {},
                    "source": ["value = 1\n"],
                    "execution_count": None,
                    "outputs": [],
                },
            ],
            "metadata": {},
            "nbformat": 4,
            "nbformat_minor": 5,
        }
    ).encode()


def test_open_valid_notebook_sets_canonical_path_and_stays_clean(tmp_path, notebook_payload):
    file = tmp_path / "analysis.ipynb"
    _write(file, notebook_payload())

    snapshot = NotebookDocumentService().open_notebook_from_path(str(file))

    assert snapshot.notebook_path == os.path.realpath(str(file))
    assert snapshot.filename == "analysis.ipynb"
    assert snapshot.revision == 0
    assert snapshot.dirty is False


def test_open_normalizes_ids_and_baseline_hashes_original_bytes(tmp_path):
    original = _notebook_needing_normalization()
    file = tmp_path / "needs_ids.ipynb"
    _write(file, original)

    service = NotebookDocumentService()
    snapshot = service.open_notebook_from_path(str(file))

    assert snapshot.dirty is True
    assert snapshot.revision == 1
    baseline = service._on_disk_baseline
    assert baseline is not None
    assert baseline.path == os.path.realpath(str(file))
    assert baseline.content_hash == hashlib.sha256(original).hexdigest()
    normalized_hash = hashlib.sha256(
        service._serialize_notebook(snapshot.notebook)
    ).hexdigest()
    assert baseline.content_hash != normalized_hash


def test_open_rejects_path_outside_workspace_root(tmp_path, notebook_payload):
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside.ipynb"
    _write(outside, notebook_payload())

    with pytest.raises(NotebookPathError):
        NotebookDocumentService().open_notebook_from_path(
            str(outside), workspace_root=str(root)
        )


def test_open_rejects_symlink_resolving_outside_root(tmp_path, notebook_payload):
    root = tmp_path / "root"
    root.mkdir()
    target = tmp_path / "secret.ipynb"
    _write(target, notebook_payload())
    link = root / "link.ipynb"
    link.symlink_to(target)

    with pytest.raises(NotebookPathError):
        NotebookDocumentService().open_notebook_from_path(
            str(link), workspace_root=str(root)
        )


def test_open_rejects_parent_traversal_escape(tmp_path, notebook_payload):
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "escape.ipynb"
    _write(outside, notebook_payload())
    traversal = str(root / ".." / "escape.ipynb")

    with pytest.raises(NotebookPathError):
        NotebookDocumentService().open_notebook_from_path(
            traversal, workspace_root=str(root)
        )


def test_open_rejects_non_ipynb(tmp_path, notebook_payload):
    file = tmp_path / "notebook.txt"
    _write(file, notebook_payload())

    with pytest.raises(NotebookPathError):
        NotebookDocumentService().open_notebook_from_path(str(file))


def test_open_rejects_missing_file(tmp_path):
    with pytest.raises(NotebookPathError):
        NotebookDocumentService().open_notebook_from_path(
            str(tmp_path / "nope.ipynb")
        )


def test_open_rejects_oversize_file(tmp_path):
    file = tmp_path / "huge.ipynb"
    _write(file, b" " * (MAX_NOTEBOOK_BYTES + 1))

    with pytest.raises(NotebookSizeError):
        NotebookDocumentService().open_notebook_from_path(str(file))


def test_open_oversize_rejected_by_stat_before_reading(tmp_path, monkeypatch):
    file = tmp_path / "huge.ipynb"
    _write(file, b" " * (MAX_NOTEBOOK_BYTES + 1))

    from pathlib import Path

    def _forbidden(self):
        raise AssertionError("read_bytes must not be called for oversize file")

    monkeypatch.setattr(Path, "read_bytes", _forbidden)

    with pytest.raises(NotebookSizeError):
        NotebookDocumentService().open_notebook_from_path(str(file))


def test_open_replacement_without_preconditions_is_refused(tmp_path, notebook_payload):
    first = tmp_path / "first.ipynb"
    second = tmp_path / "second.ipynb"
    _write(first, notebook_payload())
    _write(second, notebook_payload())

    service = NotebookDocumentService()
    service.open_notebook_from_path(str(first))

    with pytest.raises(ReplacementPreconditionRequired):
        service.open_notebook_from_path(str(second))


def test_open_replacement_with_preconditions_succeeds(tmp_path, notebook_payload):
    first = tmp_path / "first.ipynb"
    second = tmp_path / "second.ipynb"
    _write(first, notebook_payload())
    _write(second, notebook_payload())

    service = NotebookDocumentService()
    opened = service.open_notebook_from_path(str(first))
    replaced = service.open_notebook_from_path(
        str(second),
        expected_session_id=opened.session_id,
        expected_revision=opened.revision,
    )

    assert replaced.notebook_path == os.path.realpath(str(second))
    assert replaced.session_id != opened.session_id


def test_close_clears_notebook_path_state(tmp_path, notebook_payload):
    file = tmp_path / "note.ipynb"
    _write(file, notebook_payload())

    service = NotebookDocumentService()
    opened = service.open_notebook_from_path(str(file))
    service.close_notebook(
        expected_session_id=opened.session_id,
        expected_revision=opened.revision,
    )

    assert service._notebook_path is None
    assert service._on_disk_baseline is None


def test_external_modification_conflict_is_a_conflict():
    error = ExternalModificationConflict()
    assert error.status_code == 409
