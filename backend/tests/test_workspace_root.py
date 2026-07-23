from __future__ import annotations

import os

from fastapi.testclient import TestClient

from backend.app.main import create_app
from backend.app.notebook_document.service import NotebookDocumentService


def _write(path, payload: bytes) -> None:
    path.write_bytes(payload)


def test_open_defaults_workspace_root_to_notebook_folder(tmp_path, notebook_payload):
    folder = tmp_path / "project"
    folder.mkdir()
    file = folder / "analysis.ipynb"
    _write(file, notebook_payload())

    snapshot = NotebookDocumentService().open_notebook_from_path(str(file))

    assert snapshot.workspace_root == os.path.realpath(str(folder))


def test_open_uses_explicit_workspace_root(tmp_path, notebook_payload):
    root = tmp_path / "root"
    nested = root / "nested"
    nested.mkdir(parents=True)
    file = nested / "analysis.ipynb"
    _write(file, notebook_payload())

    snapshot = NotebookDocumentService().open_notebook_from_path(
        str(file), workspace_root=str(root)
    )

    assert snapshot.workspace_root == os.path.realpath(str(root))


def test_workspace_root_resets_on_close(tmp_path, notebook_payload):
    file = tmp_path / "note.ipynb"
    _write(file, notebook_payload())

    service = NotebookDocumentService()
    opened = service.open_notebook_from_path(str(file))
    assert service._workspace_root is not None
    service.close_notebook(
        expected_session_id=opened.session_id,
        expected_revision=opened.revision,
    )

    assert service._workspace_root is None


def test_workspace_root_null_for_uploaded_blob(notebook_payload):
    service = NotebookDocumentService()
    snapshot = service.import_notebook(notebook_payload())
    assert snapshot.workspace_root is None


class _Payload:
    @staticmethod
    def build() -> bytes:
        import json

        return json.dumps(
            {
                "cells": [
                    {
                        "cell_type": "code",
                        "id": "editable",
                        "metadata": {},
                        "source": ["value = 1\n"],
                        "execution_count": None,
                        "outputs": [],
                    }
                ],
                "metadata": {},
                "nbformat": 4,
                "nbformat_minor": 5,
            }
        ).encode()


def test_open_dto_exposes_workspace_root(tmp_path):
    file = tmp_path / "analysis.ipynb"
    _write(file, _Payload.build())

    with TestClient(create_app()) as client:
        response = client.post("/notebooks/open", json={"path": str(file)})
        assert response.status_code == 200
        assert response.json()["workspaceRoot"] == os.path.realpath(str(tmp_path))


def test_open_dto_honors_explicit_workspace_root(tmp_path):
    root = tmp_path / "root"
    nested = root / "nested"
    nested.mkdir(parents=True)
    file = nested / "analysis.ipynb"
    _write(file, _Payload.build())

    with TestClient(create_app()) as client:
        response = client.post(
            "/notebooks/open",
            json={"path": str(file), "workspaceRoot": str(root)},
        )
        assert response.status_code == 200
        assert response.json()["workspaceRoot"] == os.path.realpath(str(root))


def test_upload_dto_has_null_workspace_root(tmp_path):
    with TestClient(create_app()) as client:
        response = client.post(
            "/notebooks/upload",
            files={"file": ("sample.ipynb", _Payload.build(), "application/json")},
        )
        assert response.status_code == 201
        assert response.json()["workspaceRoot"] is None
