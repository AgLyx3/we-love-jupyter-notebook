"""Confining every local path the server accepts to one configured root.

The containment logic predates this: the document service already refused a
notebook outside a workspace root. What it lacked was a root the caller could
not choose. These tests are mostly about that distinction — a boundary sent in
a request is a hint, and a boundary fixed at startup is a boundary.
"""

from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from backend.app.main import (
    WORKSPACE_ROOT_ENV_VAR,
    configured_workspace_root,
    create_app,
)
from backend.app.workspace_confinement import (
    UNCONFINED,
    OutsideWorkspace,
    WorkspaceConfinement,
)


def notebook_bytes() -> bytes:
    return json.dumps(
        {
            "cells": [
                {
                    "cell_type": "code",
                    "id": "only",
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


@pytest.fixture
def workspace(tmp_path):
    """A root with a notebook inside, and a sibling the server must not reach."""
    root = tmp_path / "project"
    (root / "nested").mkdir(parents=True)
    (root / "inside.ipynb").write_bytes(notebook_bytes())
    (root / "nested" / "deeper.ipynb").write_bytes(notebook_bytes())

    outside = tmp_path / "elsewhere"
    outside.mkdir()
    (outside / "secret.ipynb").write_bytes(notebook_bytes())
    (outside / "secret.txt").write_text("private", encoding="utf-8")
    return root, outside


@pytest.fixture
def confined(workspace):
    root, _ = workspace
    with TestClient(create_app(workspace_root=root)) as client:
        yield client


@pytest.fixture
def unconfined(workspace):
    with TestClient(create_app()) as client:
        yield client


# --- the unit the services share --------------------------------------------


def test_unconfined_permits_everything():
    assert UNCONFINED.confined is False
    assert UNCONFINED.contains("/anywhere/at/all") is True
    assert UNCONFINED.narrow(None) is None
    assert UNCONFINED.narrow("/some/root") == "/some/root"


def test_a_root_contains_itself_and_its_children(workspace):
    root, outside = workspace
    confinement = WorkspaceConfinement(root)
    assert confinement.contains(root)
    assert confinement.contains(root / "nested" / "deeper.ipynb")
    assert not confinement.contains(outside)
    assert not confinement.contains(root.parent)


def test_traversal_out_of_the_root_is_not_containment(workspace):
    """`..` is resolved before the check, so it cannot walk out."""
    root, _ = workspace
    confinement = WorkspaceConfinement(root)
    assert not confinement.contains(root / ".." / "elsewhere")
    assert not confinement.contains(f"{root}/nested/../../elsewhere/secret.ipynb")


def test_a_sibling_sharing_a_name_prefix_is_outside(tmp_path):
    """`/x/project-2` must not count as inside `/x/project`.

    String-prefix containment would accept it; path containment does not.
    """
    (tmp_path / "project").mkdir()
    (tmp_path / "project-2").mkdir()
    confinement = WorkspaceConfinement(tmp_path / "project")
    assert not confinement.contains(tmp_path / "project-2")


def test_narrow_lets_a_caller_go_deeper_but_not_wider(workspace):
    root, outside = workspace
    confinement = WorkspaceConfinement(root)
    assert confinement.narrow(None) == str(root)
    assert confinement.narrow(str(root / "nested")) == str(root / "nested")
    with pytest.raises(OutsideWorkspace):
        confinement.narrow(str(outside))
    with pytest.raises(OutsideWorkspace):
        confinement.narrow(str(root.parent))


def test_the_boundary_offers_no_parent_to_climb_to(workspace):
    root, _ = workspace
    confinement = WorkspaceConfinement(root)
    assert confinement.visible_parent(root) is None
    assert confinement.visible_parent(root / "nested") == str(root)


# --- opening notebooks -------------------------------------------------------


def test_a_notebook_inside_the_root_opens(confined, workspace):
    root, _ = workspace
    response = confined.post("/notebooks/open", json={"path": str(root / "inside.ipynb")})
    assert response.status_code == 200
    assert response.json()["filename"] == "inside.ipynb"


def test_a_notebook_outside_the_root_is_refused(confined, workspace):
    _, outside = workspace
    response = confined.post(
        "/notebooks/open", json={"path": str(outside / "secret.ipynb")}
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "notebook_path_invalid"


def test_omitting_the_workspace_root_does_not_remove_the_boundary(confined, workspace):
    """The gap this change closes.

    The per-request `workspaceRoot` was the only boundary there was, so a
    caller that simply left it out was unconfined.
    """
    _, outside = workspace
    body = {"path": str(outside / "secret.ipynb")}
    assert "workspaceRoot" not in body
    assert confined.post("/notebooks/open", json=body).status_code == 400


def test_a_caller_cannot_widen_the_boundary_by_asking(confined, workspace):
    """Nor by naming a root of its own that contains the target."""
    _, outside = workspace
    response = confined.post(
        "/notebooks/open",
        json={"path": str(outside / "secret.ipynb"), "workspaceRoot": str(outside)},
    )
    assert response.status_code == 400


def test_a_caller_may_still_narrow_to_a_subfolder(confined, workspace):
    """What the file picker does when a person opens a project folder."""
    root, _ = workspace
    response = confined.post(
        "/notebooks/open",
        json={
            "path": str(root / "nested" / "deeper.ipynb"),
            "workspaceRoot": str(root / "nested"),
        },
    )
    assert response.status_code == 200


def test_traversal_in_the_open_path_is_refused(confined, workspace):
    root, outside = workspace
    response = confined.post(
        "/notebooks/open",
        json={"path": f"{root}/nested/../../elsewhere/secret.ipynb"},
    )
    assert response.status_code == 400


# --- saving ------------------------------------------------------------------


def test_save_as_outside_the_root_is_refused(confined, workspace):
    root, outside = workspace
    opened = confined.post(
        "/notebooks/open", json={"path": str(root / "inside.ipynb")}
    ).json()
    response = confined.post(
        "/notebooks/save-as",
        json={
            "path": str(outside / "exfiltrated.ipynb"),
            "sessionId": opened["sessionId"],
            "expectedDocumentRevision": opened["revision"],
        },
    )
    assert response.status_code == 400
    assert not (outside / "exfiltrated.ipynb").exists()


def test_save_as_inside_the_root_is_allowed(confined, workspace):
    root, _ = workspace
    opened = confined.post(
        "/notebooks/open", json={"path": str(root / "inside.ipynb")}
    ).json()
    response = confined.post(
        "/notebooks/save-as",
        json={
            "path": str(root / "nested" / "copy.ipynb"),
            "sessionId": opened["sessionId"],
            "expectedDocumentRevision": opened["revision"],
        },
    )
    assert response.status_code == 200
    assert (root / "nested" / "copy.ipynb").is_file()


# --- browsing and searching --------------------------------------------------


def test_listing_defaults_to_the_root_not_the_home_directory(confined, workspace):
    root, _ = workspace
    listing = confined.get("/files").json()
    assert listing["path"] == str(root)
    assert listing["parent"] is None, "the boundary must not offer a step above it"
    assert {entry["name"] for entry in listing["entries"]} == {"inside.ipynb", "nested"}


def test_listing_outside_the_root_is_refused(confined, workspace):
    _, outside = workspace
    assert confined.get(f"/files?path={outside}").status_code == 400


def test_listing_inside_the_root_offers_a_parent_up_to_the_boundary(confined, workspace):
    root, _ = workspace
    listing = confined.get(f"/files?path={root / 'nested'}").json()
    assert listing["parent"] == str(root)


def test_searching_outside_the_root_is_refused(confined, workspace):
    _, outside = workspace
    assert confined.get(f"/files/search?root={outside}&query=secret").status_code == 400


def test_searching_inside_the_root_works(confined, workspace):
    root, _ = workspace
    result = confined.get(f"/files/search?root={root}&query=deeper").json()
    assert [match["name"] for match in result["matches"]] == ["deeper.ipynb"]


# --- the unconfined server is unchanged --------------------------------------


def test_without_a_root_nothing_is_confined(unconfined, workspace):
    """`scripts/dev.py` and every existing caller must behave exactly as before."""
    _, outside = workspace
    assert unconfined.post(
        "/notebooks/open", json={"path": str(outside / "secret.ipynb")}
    ).status_code == 200
    assert unconfined.get(f"/files?path={outside}").status_code == 200
    assert unconfined.get(
        f"/files/search?root={outside}&query=secret"
    ).status_code == 200


# --- reaching the setting from the existing run path -------------------------


def test_the_module_level_app_reads_a_root_from_the_environment(tmp_path, monkeypatch):
    """`scripts/dev.py` runs uvicorn against `backend.app.main:app`, so the
    setting has to be reachable without calling `create_app` directly."""
    monkeypatch.setenv(WORKSPACE_ROOT_ENV_VAR, str(tmp_path))
    assert configured_workspace_root() == str(tmp_path)


def test_an_unset_root_leaves_the_app_unconfined(monkeypatch):
    monkeypatch.delenv(WORKSPACE_ROOT_ENV_VAR, raising=False)
    assert configured_workspace_root() is None


def test_a_blank_root_is_treated_as_unset(monkeypatch):
    """An empty environment variable is a common accident; `""` must not
    resolve to the current directory and silently confine the server there."""
    monkeypatch.setenv(WORKSPACE_ROOT_ENV_VAR, "   ")
    assert configured_workspace_root() is None


def test_a_root_that_is_not_a_directory_fails_loudly(tmp_path, monkeypatch):
    """Better than starting unconfined, which is what a silent fallback means."""
    missing = tmp_path / "not-there"
    monkeypatch.setenv(WORKSPACE_ROOT_ENV_VAR, str(missing))
    with pytest.raises(RuntimeError, match=WORKSPACE_ROOT_ENV_VAR):
        configured_workspace_root()
