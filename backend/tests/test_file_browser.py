from __future__ import annotations

import os
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.app.file_browser.models import DirectoryListingError, FileSearchError
from backend.app.file_browser.service import list_directory, search_files
from backend.app.main import create_app


def _populate(root: Path) -> None:
    (root / "beta").mkdir()
    (root / "Alpha").mkdir()
    (root / "second.ipynb").write_text("{}")
    (root / "first.ipynb").write_text("{}")
    (root / "notes.txt").write_text("ignore me")
    (root / ".hidden").mkdir()
    (root / ".secret.ipynb").write_text("{}")


def test_list_directory_returns_folders_notebooks_and_other_files_sorted(tmp_path):
    _populate(tmp_path)

    listing = list_directory(str(tmp_path))

    names = [entry.name for entry in listing.entries]
    kinds = {entry.name: entry.kind for entry in listing.entries}
    # Folders first, then openable notebooks, then browse-only files; dotfiles
    # (.hidden, .secret.ipynb) stay hidden.
    assert names == ["Alpha", "beta", "first.ipynb", "second.ipynb", "notes.txt"]
    assert kinds == {
        "Alpha": "directory",
        "beta": "directory",
        "first.ipynb": "notebook",
        "second.ipynb": "notebook",
        "notes.txt": "file",
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
    (tmp_path / "data.csv").write_text("a,b\n1,2\n")

    response = client.get("/files", params={"path": str(tmp_path)})

    assert response.status_code == 200
    body = response.json()
    assert body["path"] == os.path.realpath(str(tmp_path))
    assert body["parent"] == os.path.realpath(str(tmp_path.parent))
    assert body["entries"] == [
        {"name": "sub", "path": os.path.join(body["path"], "sub"), "kind": "directory"},
        {"name": "nb.ipynb", "path": os.path.join(body["path"], "nb.ipynb"), "kind": "notebook"},
        {"name": "data.csv", "path": os.path.join(body["path"], "data.csv"), "kind": "file"},
    ]


def test_files_route_defaults_to_home(client):
    response = client.get("/files")
    assert response.status_code == 200
    assert "entries" in response.json()


def test_files_route_rejects_invalid_path(client, tmp_path):
    response = client.get("/files", params={"path": str(tmp_path / "missing")})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "directory_listing_invalid"


def _populate_tree(root: Path) -> None:
    (root / "src").mkdir()
    (root / "src" / "train.py").write_text("x")
    (root / "data").mkdir()
    (root / "data" / "train.csv").write_text("x")
    (root / "data" / "test.csv").write_text("x")
    (root / "analysis.ipynb").write_text("{}")
    (root / "README.md").write_text("x")
    (root / ".git").mkdir()
    (root / ".git" / "config").write_text("secret")
    (root / "node_modules").mkdir()
    (root / "node_modules" / "left-pad.js").write_text("x")
    (root / ".env").write_text("SECRET=1")
    (root / "id_rsa.key").write_text("secret")


def test_search_files_walks_recursively_and_matches_query(tmp_path):
    _populate_tree(tmp_path)

    result = search_files(str(tmp_path), "train")

    rels = [m.relative_path for m in result.matches]
    # Both nested files match; basename-prefix match ("train.*") ranks first.
    assert os.path.join("data", "train.csv") in rels
    assert os.path.join("src", "train.py") in rels
    assert "README.md" not in rels
    assert result.root == os.path.realpath(str(tmp_path))


def test_search_files_prunes_vcs_deps_and_secrets(tmp_path):
    _populate_tree(tmp_path)

    names = {m.name for m in search_files(str(tmp_path), "").matches}

    # Pruned dirs and credential/dotfiles never surface.
    assert "config" not in names        # under .git
    assert "left-pad.js" not in names   # under node_modules
    assert ".env" not in names
    assert "id_rsa.key" not in names
    # Ordinary project files do.
    assert {"train.py", "train.csv", "test.csv", "analysis.ipynb", "README.md"} <= names


def test_search_files_tags_notebook_kind(tmp_path):
    _populate_tree(tmp_path)

    kinds = {m.name: m.kind for m in search_files(str(tmp_path), "").matches}

    assert kinds["analysis.ipynb"] == "notebook"
    assert kinds["train.csv"] == "file"


def test_search_files_rejects_missing_root(tmp_path):
    with pytest.raises(FileSearchError) as excinfo:
        search_files(str(tmp_path / "nope"))
    assert excinfo.value.status_code == 400


def test_search_route_returns_matches(client, tmp_path):
    _populate_tree(tmp_path)

    response = client.get("/files/search", params={"root": str(tmp_path), "query": "test"})

    assert response.status_code == 200
    body = response.json()
    rels = [m["relativePath"] for m in body["matches"]]
    assert os.path.join("data", "test.csv") in rels
    assert all("kind" in m and "path" in m for m in body["matches"])


def test_search_route_rejects_invalid_root(client, tmp_path):
    response = client.get("/files/search", params={"root": str(tmp_path / "missing")})
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "file_search_invalid"
