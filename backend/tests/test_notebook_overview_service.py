"""The cache, the fallback, and the promises the panel makes about spending.

Four behaviours here are non-negotiable and each has a test that would fail
loudly if it regressed:

* Opening a notebook never calls the model.
* Running a cell never invalidates the map.
* Editing a cell marks it stale and nothing regenerates on its own.
* A response that is not a valid partition is discarded, not rendered.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from backend.app.agent_workspace.models import AdapterResult, AgentAdapterError
from backend.app.main import create_app
from backend.app.notebook_overview.service import NotebookOverviewService, source_fingerprint
from backend.app.notebook_document.service import NotebookDocumentService
from backend.app.notebook_overview import analysis

FIXTURES = Path(__file__).parent / "fixtures"


class CountingAdapter:
    """Counts model calls, so "free" can be asserted rather than assumed."""

    def __init__(self, responses=None, error: Exception | None = None) -> None:
        self.responses = list(responses or [])
        self.error = error
        self.calls = 0
        self.prompts: list[str] = []

    def run_prompt(self, prompt, *, timeout, cancel_event, model=None):
        self.calls += 1
        self.prompts.append(prompt)
        if self.error:
            raise self.error
        if self.responses:
            return AdapterResult(self.responses.pop(0))
        return AdapterResult("[]")


def partition(ranges) -> str:
    return json.dumps([
        {"start": start, "end": end, "name": f"block {start}"} for start, end in ranges
    ])


@pytest.fixture
def loaded():
    """A document service holding the simulation fixture, plus its snapshot."""
    documents = NotebookDocumentService()
    payload = (FIXTURES / "simulation-sweep.ipynb").read_bytes()
    documents.import_notebook(payload=payload, filename="simulation-sweep.ipynb")
    return documents


def make(documents, adapter) -> NotebookOverviewService:
    return NotebookOverviewService(documents=documents, adapter=adapter)


def current(documents):
    snapshot = documents.get_snapshot()
    return snapshot.session_id, snapshot.revision


# ------------------------------------------------------------ never spends


def test_get_never_calls_the_model(loaded):
    """Opening a notebook must not spend anything (spec §3.2)."""
    adapter = CountingAdapter()
    service = make(loaded, adapter)
    session, revision = current(loaded)
    overview = service.get(session_id=session, expected_revision=revision)
    assert adapter.calls == 0
    assert overview.generated is False
    assert overview.blocks  # the fallback, so the panel is never empty


def test_get_after_generation_still_never_calls_the_model(loaded):
    adapter = CountingAdapter([partition([(0, 20)])])
    service = make(loaded, adapter)
    session, revision = current(loaded)
    service.generate(session_id=session, expected_revision=revision)
    assert adapter.calls == 1
    for _ in range(3):
        service.get(session_id=session, expected_revision=revision)
    assert adapter.calls == 1


# ------------------------------------------------------------------- cache


def test_generation_is_cached_and_served_back(loaded):
    adapter = CountingAdapter([partition([(0, 9), (10, 20)])])
    service = make(loaded, adapter)
    session, revision = current(loaded)
    service.generate(session_id=session, expected_revision=revision)
    overview = service.get(session_id=session, expected_revision=revision)
    assert overview.generated is True
    assert overview.stale is False
    assert [(b.start, b.end) for b in overview.blocks] == [(0, 9), (10, 20)]


def test_running_a_cell_does_not_invalidate_the_map(loaded):
    """The frequent case, and the reason the key is sources not revision.

    `_revision` bumps on `set_execution_count`, so keying the cache on it would
    stale the map on every cell run.
    """
    adapter = CountingAdapter([partition([(0, 20)])])
    service = make(loaded, adapter)
    session, revision = current(loaded)
    service.generate(session_id=session, expected_revision=revision)

    cell = loaded.get_snapshot().notebook["cells"][1]
    source = "".join(cell["source"]) if isinstance(cell["source"], list) else cell["source"]
    lease = loaded.coordinator.acquire(operation_type="execution", operation_id="run-1")
    snapshot = loaded.apply_execution_result_under_lease(
        cell_id=cell["id"], outputs=[], execution_count=99,
        expected_revision=revision,
        expected_source_hash=hashlib.sha256(source.encode()).hexdigest(),
        owner="execution", lease=lease,
    )
    loaded.coordinator.release(lease)
    assert snapshot.revision != revision  # the revision really did move

    overview = service.get(session_id=session, expected_revision=snapshot.revision)
    assert overview.generated is True
    assert overview.stale is False
    assert adapter.calls == 1


def test_editing_a_cell_marks_the_map_stale_but_keeps_showing_it(loaded):
    adapter = CountingAdapter([partition([(0, 20)])])
    service = make(loaded, adapter)
    session, revision = current(loaded)
    service.generate(session_id=session, expected_revision=revision)

    cell_id = loaded.get_snapshot().notebook["cells"][1]["id"]
    snapshot = loaded.update_cell_source(
        cell_id=cell_id, source="N = 12345\n", owner="user",
        expected_session_id=session, expected_revision=revision,
    )
    overview = service.get(session_id=session, expected_revision=snapshot.revision)
    assert overview.stale is True
    assert overview.generated is True   # still shown, visibly stale
    assert overview.blocks
    assert adapter.calls == 1           # and nothing regenerated on its own


def test_a_stale_map_still_reports_fresh_computed_fields(loaded):
    """Computed fields recompute every request; only boundaries are cached."""
    # Two blocks, so the first has later cells to be depended on by — `produces`
    # is "bound here and read later", so a whole-notebook block reports nothing.
    adapter = CountingAdapter([partition([(0, 3), (4, 20)])])
    service = make(loaded, adapter)
    session, revision = current(loaded)
    service.generate(session_id=session, expected_revision=revision)

    before = service.get(session_id=session, expected_revision=revision)
    assert "gamma" in before.blocks[0].produces

    # Cell 2 is the parameters cell; dropping `gamma` from it should drop
    # `gamma` from what the block advertises, because later cells still read it.
    cell_id = loaded.get_snapshot().notebook["cells"][2]["id"]
    snapshot = loaded.update_cell_source(
        cell_id=cell_id, source="N = 1_000_000\nI0 = 12\n", owner="user",
        expected_session_id=session, expected_revision=revision,
    )
    overview = service.get(session_id=session, expected_revision=snapshot.revision)
    assert overview.stale is True
    # Boundaries are still the cached ones, but the computed fields moved with
    # the edit: they are recomputed per request, never stored with the map.
    assert [(b.start, b.end) for b in overview.blocks] == [(0, 3), (4, 20)]
    assert "gamma" not in overview.blocks[0].produces
    assert "N" in overview.blocks[0].produces


def test_a_changed_cell_count_falls_back_rather_than_mispartitioning(loaded):
    """Cached ranges that cannot cover the notebook are inapplicable, not stale."""
    adapter = CountingAdapter([partition([(0, 20)])])
    service = make(loaded, adapter)
    session, revision = current(loaded)
    service.generate(session_id=session, expected_revision=revision)

    notebook = loaded.get_snapshot().notebook
    trimmed = {**notebook, "cells": notebook["cells"][:10]}
    lease = loaded.coordinator.acquire(operation_type="agent_turn", operation_id="t1")
    snapshot = loaded.restore_under_lease(
        notebook=trimmed, expected_revision=revision, owner="agent_turn", lease=lease,
    )
    loaded.coordinator.release(lease)

    overview = service.get(session_id=session, expected_revision=snapshot.revision)
    assert overview.generated is False
    covered = [i for b in overview.blocks for i in range(b.start, b.end + 1)]
    assert covered == list(range(10))


def test_fingerprint_ignores_outputs_and_execution_counts():
    base = {"cells": [
        {"cell_type": "code", "source": ["a = 1\n"], "execution_count": None, "outputs": []},
    ]}
    ran = {"cells": [
        {"cell_type": "code", "source": ["a = 1\n"], "execution_count": 7,
         "outputs": [{"output_type": "stream", "text": ["1\n"]}]},
    ]}
    assert source_fingerprint(analysis.read_cells(base)) == source_fingerprint(analysis.read_cells(ran))


def test_fingerprint_changes_when_a_cell_type_changes():
    """Code becoming markdown changes the map even when the text does not."""
    code = {"cells": [{"cell_type": "code", "source": ["a = 1\n"], "execution_count": None}]}
    prose = {"cells": [{"cell_type": "markdown", "source": ["a = 1\n"]}]}
    assert source_fingerprint(analysis.read_cells(code)) != source_fingerprint(analysis.read_cells(prose))


# ---------------------------------------------------------------- rejection


def test_an_invalid_partition_is_discarded_not_rendered(loaded):
    adapter = CountingAdapter([partition([(0, 5)])])  # covers 6 of 21
    service = make(loaded, adapter)
    session, revision = current(loaded)
    overview = service.generate(session_id=session, expected_revision=revision)
    assert overview.generated is False       # the bad map never reaches the panel
    assert overview.error                    # and the reason is surfaced
    assert "uncovered" in overview.error
    assert overview.blocks                   # the fallback still fills the panel


def test_a_rejected_regeneration_keeps_the_previous_good_map(loaded):
    service = make(loaded, CountingAdapter([partition([(0, 20)]), partition([(0, 2)])]))
    session, revision = current(loaded)
    service.generate(session_id=session, expected_revision=revision)
    second = service.generate(session_id=session, expected_revision=revision)
    assert second.generated is True          # the good map survives
    assert second.error                      # and the failure is still reported


def test_error_clears_after_a_successful_regeneration(loaded):
    service = make(loaded, CountingAdapter([partition([(0, 2)]), partition([(0, 20)])]))
    session, revision = current(loaded)
    assert service.generate(session_id=session, expected_revision=revision).error
    good = service.generate(session_id=session, expected_revision=revision)
    assert good.error is None
    assert good.generated is True


# ----------------------------------------------------------------- fallback


def test_no_cli_falls_back_without_crashing_and_names_are_absent(loaded):
    """Spec §6: the panel stays functional with no Claude CLI."""
    service = make(loaded, CountingAdapter(error=AgentAdapterError("Claude CLI is unavailable")))
    session, revision = current(loaded)
    overview = service.generate(session_id=session, expected_revision=revision)
    assert overview.generated is False
    assert overview.blocks
    assert all(block.name == "" for block in overview.blocks)
    assert "unavailable" in (overview.error or "")
    # The computed fields all work normally in this mode.
    assert any(block.produces for block in overview.blocks)
    assert any(block.defines for block in overview.blocks)
    assert any(block.marks for block in overview.blocks)


def test_revision_conflict_is_raised_before_anything_is_spent(loaded):
    adapter = CountingAdapter([partition([(0, 20)])])
    service = make(loaded, adapter)
    session, revision = current(loaded)
    from backend.app.notebook_document.models import RevisionConflict

    with pytest.raises(RevisionConflict):
        service.generate(session_id=session, expected_revision=revision + 5)
    assert adapter.calls == 0


def test_the_map_survives_closing_and_reopening_the_same_file(tmp_path):
    """Cache is keyed by path, so a reopen gets its map back for free."""
    path = tmp_path / "sweep.ipynb"
    path.write_bytes((FIXTURES / "simulation-sweep.ipynb").read_bytes())
    documents = NotebookDocumentService()
    documents.open_notebook_from_path(path=str(path), workspace_root=str(tmp_path))
    adapter = CountingAdapter([partition([(0, 20)])])
    service = make(documents, adapter)
    session, revision = current(documents)
    service.generate(session_id=session, expected_revision=revision)

    documents.close_notebook(expected_session_id=session, expected_revision=revision)
    documents.open_notebook_from_path(path=str(path), workspace_root=str(tmp_path))
    session2, revision2 = current(documents)
    overview = service.get(session_id=session2, expected_revision=revision2)
    assert overview.generated is True
    assert adapter.calls == 1


# --------------------------------------------------------------------- API


def test_routes_expose_the_free_and_costly_split():
    adapter = CountingAdapter([partition([(0, 20)])])
    with TestClient(create_app(agent_adapter=adapter)) as client:
        payload = (FIXTURES / "simulation-sweep.ipynb").read_bytes()
        snapshot = client.post(
            "/notebooks/upload",
            files={"file": ("simulation-sweep.ipynb", payload, "application/json")},
        ).json()
        query = f"?sessionId={snapshot['sessionId']}&expectedDocumentRevision={snapshot['revision']}"

        free = client.get(f"/overview{query}")
        assert free.status_code == 200
        assert free.json()["generated"] is False
        assert adapter.calls == 0

        costly = client.post("/overview/generate", json={
            "sessionId": snapshot["sessionId"],
            "expectedDocumentRevision": snapshot["revision"],
        })
        assert costly.status_code == 200
        body = costly.json()
        assert body["generated"] is True
        assert adapter.calls == 1
        block = body["blocks"][0]
        assert set(block) == {"start", "end", "name", "state", "produces", "defines", "marks"}
        assert block["defines"][0]["name"] == "sir"
        assert block["defines"][0]["callSites"] == [4, 14]


def test_route_revision_conflict_is_a_409():
    with TestClient(create_app(agent_adapter=CountingAdapter())) as client:
        payload = (FIXTURES / "simulation-sweep.ipynb").read_bytes()
        snapshot = client.post(
            "/notebooks/upload",
            files={"file": ("simulation-sweep.ipynb", payload, "application/json")},
        ).json()
        response = client.get(
            f"/overview?sessionId={snapshot['sessionId']}"
            f"&expectedDocumentRevision={snapshot['revision'] + 3}"
        )
        assert response.status_code == 409
        assert response.json()["error"]["code"] == "revision_conflict"


def test_generated_blocks_always_partition_the_notebook():
    """The shape assertion spec §8 asks for — never the names."""
    for name in ("messy-exploration.ipynb", "simulation-sweep.ipynb"):
        documents = NotebookDocumentService()
        documents.import_notebook(payload=(FIXTURES / name).read_bytes(), filename=name)
        count = len(documents.get_snapshot().notebook["cells"])
        ranges = [(i, min(i + 3, count - 1)) for i in range(0, count, 4)]
        service = make(documents, CountingAdapter([partition(ranges)]))
        session, revision = current(documents)
        overview = service.generate(session_id=session, expected_revision=revision)

        assert overview.generated is True
        covered = [i for b in overview.blocks for i in range(b.start, b.end + 1)]
        assert covered == list(range(count)), name
        assert all(block.name.strip() for block in overview.blocks), name
        assert 1 <= len(overview.blocks) <= count, name
