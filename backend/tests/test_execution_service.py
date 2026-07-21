import json
import threading
import time

from backend.app.kernel_execution.kernel_session import KernelResult
from backend.app.kernel_execution.service import KernelExecutionService
from backend.app.notebook_document.service import NotebookDocumentService


class FakeKernel:
    kernel_session_id = "kernel-1"
    status = "idle"
    busy_attempt_id = None
    startup_timeout = 1
    cell_timeout = 1

    def __init__(self):
        self.sources = []

    def execute(self, source, attempt_id):
        self.sources.append(source)
        return KernelResult([{"output_type": "stream", "name": "stdout", "text": source}], len(self.sources), "raise Error" in source)

    def interrupt(self): pass
    def restart(self): self.kernel_session_id = "kernel-2"
    def shutdown(self): pass


def notebook(*sources):
    return json.dumps({
        "cells": [
            {"cell_type": "code", "id": f"cell-{i}", "metadata": {}, "source": source, "execution_count": None, "outputs": []}
            for i, source in enumerate(sources)
        ],
        "metadata": {}, "nbformat": 4, "nbformat_minor": 5,
    }).encode()


def wait_terminal(service, operation_id):
    for _ in range(200):
        operation = service.get(operation_id)
        if operation.state in {"completed", "failed", "cancelled", "validation_incomplete"}:
            return operation
        time.sleep(.01)
    raise AssertionError("operation did not finish")


def test_manual_run_all_persists_outputs_and_kernel_state():
    documents = NotebookDocumentService()
    snapshot = documents.import_notebook(notebook("x = 1", "print(x)"))
    kernel = FakeKernel()
    service = KernelExecutionService(documents=documents, kernel=kernel)
    operation = service.start_all(session_id=snapshot.session_id, expected_revision=snapshot.revision)
    completed = wait_terminal(service, operation.operation_id)
    assert completed.state == "completed"
    assert kernel.sources == ["x = 1", "print(x)"]
    current = documents.get_snapshot()
    assert current.revision == snapshot.revision + 2
    assert current.notebook["cells"][1]["execution_count"] == 2


def test_agent_execution_pauses_and_correlates_approval():
    documents = NotebookDocumentService()
    snapshot = documents.import_notebook(notebook("open('x', 'w')"))
    kernel = FakeKernel()
    service = KernelExecutionService(documents=documents, kernel=kernel)
    lease = documents.coordinator.acquire(operation_type="agent_turn", operation_id="turn-1")
    result = {}
    thread = threading.Thread(target=lambda: result.setdefault("operation", service.execute_downstream(
        parent_turn_id="turn-1", session_id=snapshot.session_id,
        expected_revision=snapshot.revision, changed_cell_ids={"cell-0"},
        lease=lease, cancel_event=threading.Event(),
    )))
    thread.start()
    for _ in range(100):
        operations = list(service._operations.values())
        if operations and operations[0].state == "awaiting_approval":
            break
        time.sleep(.01)
    pending = service.get(operations[0].operation_id)
    attempt = pending.attempts[0]
    service.approve(attempt.attempt_id, session_id=snapshot.session_id,
                    expected_revision=snapshot.revision, turn_id="turn-1", cell_id="cell-0")
    thread.join(2)
    documents.coordinator.release(lease)
    assert result["operation"].state == "completed"
    assert kernel.sources == ["open('x', 'w')"]


def test_skip_is_idempotent_and_conflicting_decision_is_rejected():
    from backend.app.kernel_execution.models import ExecutionDecisionConflict

    documents = NotebookDocumentService()
    snapshot = documents.import_notebook(notebook("!echo no"))
    service = KernelExecutionService(documents=documents, kernel=FakeKernel())
    lease = documents.coordinator.acquire(operation_type="agent_turn", operation_id="turn-1")
    thread = threading.Thread(target=lambda: service.execute_downstream(
        parent_turn_id="turn-1", session_id=snapshot.session_id,
        expected_revision=snapshot.revision, changed_cell_ids={"cell-0"},
        lease=lease, cancel_event=threading.Event()))
    thread.start()
    while not service._attempts:
        time.sleep(.01)
    attempt_id = next(iter(service._attempts))
    kwargs = dict(session_id=snapshot.session_id, expected_revision=snapshot.revision, turn_id="turn-1", cell_id="cell-0")
    service.skip(attempt_id, **kwargs)
    service.skip(attempt_id, **kwargs)
    import pytest
    with pytest.raises(ExecutionDecisionConflict):
        service.approve(attempt_id, **kwargs)
    thread.join(2)
    documents.coordinator.release(lease)


def test_real_kernel_persists_variables_between_cells():
    import pytest
    pytest.importorskip("jupyter_client")
    documents = NotebookDocumentService()
    snapshot = documents.import_notebook(notebook("persistent_value = 41", "print(persistent_value + 1)"))
    service = KernelExecutionService(documents=documents)
    try:
        operation = service.start_all(session_id=snapshot.session_id, expected_revision=snapshot.revision)
        completed = wait_terminal(service, operation.operation_id)
        assert completed.state == "completed"
        assert "42" in documents.get_snapshot().notebook["cells"][1]["outputs"][0]["text"]
    finally:
        service.shutdown()
