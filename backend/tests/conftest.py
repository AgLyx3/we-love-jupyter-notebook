import json

import pytest
from fastapi.testclient import TestClient

from backend.app.main import create_app


@pytest.fixture
def notebook_payload():
    def build(*, cell_ids=("intro", "editable"), sources=None) -> bytes:
        # `sources` overrides a cell's body by id, for tests that need more than
        # one diff hunk in a cell.
        sources = sources or {}
        cells = [
            {
                "cell_type": "markdown",
                "id": cell_ids[0],
                "metadata": {},
                "source": ["# Example notebook\n"],
            },
            {
                "cell_type": "code",
                "id": cell_ids[1],
                "metadata": {},
                "source": ["value = 1\n"],
                "execution_count": None,
                "outputs": [],
            },
        ]
        for cell in cells:
            if cell["id"] in sources:
                cell["source"] = [sources[cell["id"]]]
        return json.dumps(
            {
                "cells": cells,
                "metadata": {},
                "nbformat": 4,
                "nbformat_minor": 5,
            }
        ).encode()

    return build


@pytest.fixture
def client():
    with TestClient(create_app()) as test_client:
        yield test_client


@pytest.fixture
def built_frontend(tmp_path_factory, monkeypatch):
    """A stand-in for `npm run build`, for tests that boot the real editor.

    The bundled app refuses to start without a built frontend, so anything that
    spawns the editor as a child process needs one on disk. Building it would
    make `pytest backend/tests` depend on Node; these tests are about the MCP
    tools, the kernel and the approval gate, and never fetch an asset. So they
    get the one file `_locate_dist` looks for, pointed at through the same env
    var a packager would use. The child inherits it: `EditorProcess` copies
    `os.environ`.
    """
    from backend.app.bundled import DIST_DIR_ENV_VAR

    root = tmp_path_factory.mktemp("stub-dist")
    (root / "index.html").write_text(
        '<!doctype html><html><body><div id="root"></div></body></html>',
        encoding="utf-8",
    )
    monkeypatch.setenv(DIST_DIR_ENV_VAR, str(root))
    return root
