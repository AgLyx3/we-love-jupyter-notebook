from __future__ import annotations

import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.app.file_browser.models import DirectoryListingError
from backend.app.file_browser.service import list_directory
from backend.app.main import create_app


def _populate(root: Path) -> None:
    (root / "beta").mkdir()
    (root / "Alpha").mkdir()
    (root / "second.ipynb").write_text("{}")
    (root / "first.ipynb").write_text("{}")
    (root / "notes.txt").write_text("ignore me")
    (root / ".hidden").mkdir()
    (root / ".secret.ipynb").write_text("{}")


def test_list_directory_returns_only_folders_and_notebooks_sorted(tmp_path):
    _populate(tmp_path)

    listing = list_directory(str(tmp_path))

    names = [entry.name for entry in listing.entries]
    kinds = {entry.name: entry.kind for entry in listing.entries}
    assert names == ["Alpha", "beta", "first.ipynb", "second.ipynb"]
    assert kinds == {
        "Alpha": "directory",
        "beta": "directory",
        "first.ipynb": "notebook",
        "second.ipynb": "notebook",
    }
    assert listing.path == os.path.realpath(str(tmp_path))
    assert listing.parent == os.path.realpath(str(tmp_path.parent))
    for entry in listing.entries:
        assert entry.path == os.path.join(listing.path, entry.name)


def test_list_directory_defaults_to_home(monkeypatch, tmp_path):
    home = tmp_path / "home"
    home.mkdir()
    (home / "project").mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))

    listing = list_directory()

    assert listing.path == os.path.realpath(str(home))
    assert [entry.name for entry in listing.entries] == ["project"]


def test_list_directory_parent_is_null_at_filesystem_root():
    listing = list_directory(os.path.abspath(os.sep))
    assert listing.parent is None


def test_list_directory_rejects_missing_path(tmp_path):
    with pytest.raises(DirectoryListingError) as excinfo:
        list_directory(str(tmp_path / "does-not-exist"))
    assert excinfo.value.status_code == 400


def test_list_directory_rejects_file(tmp_path):
    file = tmp_path / "note.ipynb"
    file.write_text("{}")
    with pytest.raises(DirectoryListingError):
        list_directory(str(file))


def test_list_directory_skips_unreadable_child(tmp_path, monkeypatch):
    (tmp_path / "readable").mkdir()
    unreadable = tmp_path / "broken"
    unreadable.mkdir()

    real_stat = Path.stat

    def _flaky_stat(self, *args, **kwargs):
        if self.name == "broken":
            raise OSError("permission denied")
        return real_stat(self, *args, **kwargs)

    monkeypatch.setattr(Path, "stat", _flaky_stat)

    listing = list_directory(str(tmp_path))

    assert [entry.name for entry in listing.entries] == ["readable"]


@pytest.fixture
def client():
    with TestClient(create_app()) as test_client:
        yield test_client


def test_files_route_returns_listing(client, tmp_path):
    (tmp_path / "sub").mkdir()
    (tmp_path / "nb.ipynb").write_text("{}")

    response = client.get("/files", params={"path": str(tmp_path)})

    assert response.status_code == 200
    body = response.json()
    assert body["path"] == os.path.realpath(str(tmp_path))
    assert body["parent"] == os.path.realpath(str(tmp_path.parent))
    assert body["entries"] == [
        {"name": "sub", "path": os.path.join(body["path"], "sub"), "kind": "directory"},
        {"name": "nb.ipynb", "path": os.path.join(body["path"], "nb.ipynb"), "kind": "notebook"},
    ]


def test_files_route_defaults_to_home(client):
    response = client.get("/files")
    assert response.status_code == 200
    assert "entries" in response.json()


def test_files_route_rejects_invalid_path(client, tmp_path):
    response = client.get("/files", params={"path": str(tmp_path / "missing")})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "directory_listing_invalid"
