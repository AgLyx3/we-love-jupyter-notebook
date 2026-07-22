import json
import threading
import time

import nbformat

from backend.app.kernel_execution.kernel_session import KernelResult, KernelSession
from backend.app.kernel_execution.models import KernelCellTimeout
from backend.app.kernel_execution.service import KernelExecutionService
from backend.app.notebook_document.service import NotebookDocumentService
from backend.app.agent_turns.service import AgentTurnService
from backend.app.agent_workspace.adapters import FakeAgentAdapter, FakeAttempt
from backend.app.turn_scope.service import TurnScopeService


class FakeKernel:
    kernel_session_id = "kernel-1"
    status = "idle"
    busy_attempt_id = None
    startup_timeout = 1
    cell_timeout = 1
    recovery_timeout = 1

    def __init__(self):
        self.sources = []

    def execute(self, source, attempt_id):
        self.sources.append(source)
        return KernelResult([{"output_type": "stream", "name": "stdout", "text": source}], len(self.sources), "raise Error" in source)

    def interrupt(self): pass
    def restart(self): self.kernel_session_id = "kernel-2"
    def interrupt_correlated(self, kernel_session_id, attempt_id):
        return kernel_session_id == self.kernel_session_id and attempt_id == self.busy_attempt_id
    def restart_correlated(self, kernel_session_id, attempt_id):
        if not self.interrupt_correlated(kernel_session_id, attempt_id): return False
        self.restart()
        return True
    def shutdown(self): pass


def notebook(*sources):
    return json.dumps({
        "cells": [
            {"cell_type": "code", "id": f"cell-{i}", "metadata": {}, "source": source, "execution_count": None, "outputs": []}
            for i, source in enumerate(sources)
        ],
        "metadata": {}, "nbformat": 4, "nbformat_minor": 5,
    }).encode()


def wait_terminal(service, operation_id, timeout=10):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        operation = service.get(operation_id)
        if operation.state in {"completed", "failed", "cancelled", "validation_incomplete", "timed_out"}:
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


def test_real_kernel_timeout_recovers_and_shutdown_stops_kernel():
    import pytest
    pytest.importorskip("jupyter_client")
    documents = NotebookDocumentService()
    snapshot = documents.import_notebook(notebook("import time; time.sleep(30)"))
    kernel = KernelSession(cell_timeout=.2, recovery_timeout=5)
    service = KernelExecutionService(documents=documents, kernel=kernel)
    manager = None
    try:
        operation = service.start_cell(cell_id="cell-0", session_id=snapshot.session_id, expected_revision=snapshot.revision)
        completed = wait_terminal(service, operation.operation_id, timeout=15)
        assert completed.state == "timed_out"
        assert completed.error == {
            "code": "cell_timed_out", "message": "Cell execution timed out",
            "details": {"kernelRecovered": True},
        }
        assert service.kernel_status()["state"] == "idle"
        manager = kernel._manager
        assert manager is not None and manager.is_alive()
    finally:
        service.shutdown()
    assert kernel.status == "not_started"
    assert manager is not None and not manager.has_kernel


def test_real_kernel_error_output_is_nbformat_valid():
    import pytest
    pytest.importorskip("jupyter_client")
    documents = NotebookDocumentService()
    snapshot = documents.import_notebook(notebook("raise ValueError('expected')"))
    service = KernelExecutionService(documents=documents)
    try:
        operation = service.start_cell(cell_id="cell-0", session_id=snapshot.session_id, expected_revision=snapshot.revision)
        completed = wait_terminal(service, operation.operation_id)
        assert completed.state == "failed"
        current = documents.get_snapshot()
        assert current.notebook["cells"][0]["outputs"][0]["output_type"] == "error"
        nbformat.validate(nbformat.from_dict(current.notebook))
    finally:
        service.shutdown()


def test_accepted_cancel_disables_attempt_before_result_commit():
    entered = threading.Event()
    release = threading.Event()

    class BarrierKernel(FakeKernel):
        busy_attempt_id = None
        def execute(self, source, attempt_id):
            self.busy_attempt_id = attempt_id
            entered.set()
            assert release.wait(2)
            self.busy_attempt_id = None
            return KernelResult([{"output_type": "stream", "name": "stdout", "text": "late"}], 1, False)

    documents = NotebookDocumentService()
    snapshot = documents.import_notebook(notebook("value = 2"))
    service = KernelExecutionService(documents=documents, kernel=BarrierKernel())
    lease = documents.coordinator.acquire(operation_type="agent_turn", operation_id="turn-1")
    operation = service.create_downstream(parent_turn_id="turn-1", session_id=snapshot.session_id, expected_revision=snapshot.revision, cancel_event=threading.Event())
    worker = threading.Thread(target=lambda: service.run_downstream(operation.operation_id, changed_cell_ids={"cell-0"}, lease=lease))
    worker.start()
    assert entered.wait(2)
    attempt_id = service.get(operation.operation_id).current_attempt_id
    service.cancel(attempt_id, session_id=snapshot.session_id, expected_revision=snapshot.revision, turn_id="turn-1", cell_id="cell-0")
    release.set()
    worker.join(2)
    documents.coordinator.release(lease)
    assert documents.get_snapshot().revision == snapshot.revision
    assert documents.get_snapshot().notebook["cells"][0]["outputs"] == []
    assert service.get(operation.operation_id).state == "cancelled"


def test_timeout_is_explicit_and_unrecovered_kernel_requires_restart():
    class TimeoutKernel(FakeKernel):
        status = "restart_required"
        def execute(self, source, attempt_id):
            raise KernelCellTimeout(recovered=False)

    documents = NotebookDocumentService()
    snapshot = documents.import_notebook(notebook("while True: pass"))
    service = KernelExecutionService(documents=documents, kernel=TimeoutKernel())
    operation = service.start_cell(cell_id="cell-0", session_id=snapshot.session_id, expected_revision=snapshot.revision)
    completed = wait_terminal(service, operation.operation_id)
    assert completed.state == "timed_out"
    assert completed.error["code"] == "cell_timed_out"
    assert completed.error["details"] == {"kernelRecovered": False}
    assert service.kernel_status()["state"] == "restart_required"


def test_kernel_control_compare_and_action_are_atomic_against_turnover():
    entered = threading.Event()
    release = threading.Event()

    class Manager:
        def interrupt_kernel(self):
            entered.set()
            assert release.wait(2)

    kernel = KernelSession()
    kernel._manager = Manager()
    kernel._busy_attempt_id = "attempt-1"
    result = {}
    control = threading.Thread(target=lambda: result.setdefault("ok", kernel.interrupt_correlated(kernel.kernel_session_id, "attempt-1")))
    control.start()
    assert entered.wait(2)
    turnover_done = threading.Event()
    def turnover():
        with kernel._lock:
            kernel._busy_attempt_id = "attempt-2"
        turnover_done.set()
    turnover_thread = threading.Thread(target=turnover)
    turnover_thread.start()
    assert not turnover_done.wait(.05)
    release.set()
    control.join(2)
    turnover_thread.join(2)
    assert result["ok"] is True
    assert kernel.busy_attempt_id == "attempt-2"


def test_kernel_restart_compare_and_action_are_atomic_against_turnover():
    entered = threading.Event()
    release = threading.Event()

    class Manager:
        def restart_kernel(self, now):
            assert now is True
            entered.set()
            assert release.wait(2)

    class Client:
        def wait_for_ready(self, timeout):
            assert timeout == 30

    kernel = KernelSession()
    kernel._manager = Manager()
    kernel._client = Client()
    kernel._busy_attempt_id = "attempt-1"
    old_session_id = kernel.kernel_session_id
    result = {}
    control = threading.Thread(target=lambda: result.setdefault(
        "ok", kernel.restart_correlated(old_session_id, "attempt-1"),
    ))
    control.start()
    assert entered.wait(2)
    turnover_done = threading.Event()
    def turnover():
        with kernel._lock:
            kernel._busy_attempt_id = "attempt-2"
        turnover_done.set()
    turnover_thread = threading.Thread(target=turnover)
    turnover_thread.start()
    assert not turnover_done.wait(.05)
    release.set()
    control.join(2)
    turnover_thread.join(2)
    assert result["ok"] is True
    assert kernel.kernel_session_id != old_session_id
    assert kernel.busy_attempt_id == "attempt-2"


def test_agent_turn_publishes_execution_id_before_risky_wait():
    documents = NotebookDocumentService()
    snapshot = documents.import_notebook(notebook("value = 1"))
    scopes = TurnScopeService(documents)
    scopes.add("cell-0", editable=True)
    executions = KernelExecutionService(documents=documents, kernel=FakeKernel())
    turns = AgentTurnService(
        documents=documents, scopes=scopes,
        adapter=FakeAgentAdapter([FakeAttempt(edits={
            "editable/cell_cell-0.py": "open('out.txt', 'w')",
        })]), executions=executions,
    )
    turn = turns.start(
        prompt="write", session_id=snapshot.session_id,
        expected_revision=snapshot.revision,
    )
    for _ in range(200):
        current = turns.get(turn.turn_id)
        if current.execution_operation_id:
            operation = executions.get(current.execution_operation_id)
            if operation.state == "awaiting_approval":
                break
        time.sleep(.01)
    assert current.execution_operation_id == operation.operation_id
    assert current.state == "executing"
    attempt = operation.attempts[0]
    executions.skip(
        attempt.attempt_id, session_id=snapshot.session_id,
        expected_revision=operation.current_revision,
        turn_id=turn.turn_id, cell_id="cell-0",
    )
    for _ in range(200):
        current = turns.get(turn.turn_id)
        if current.state == "validation_incomplete":
            break
        time.sleep(.01)
    assert current.state == "validation_incomplete"
    assert documents.coordinator.active_lease is None


def test_agent_turn_commits_downstream_output_before_terminal_release():
    documents = NotebookDocumentService()
    snapshot = documents.import_notebook(notebook("value = 1"))
    scopes = TurnScopeService(documents)
    scopes.add("cell-0", editable=True)
    executions = KernelExecutionService(documents=documents, kernel=FakeKernel())
    turns = AgentTurnService(
        documents=documents, scopes=scopes,
        adapter=FakeAgentAdapter([FakeAttempt(edits={
            "editable/cell_cell-0.py": "value = 2",
        })]), executions=executions,
    )
    turn = turns.start(
        prompt="change", session_id=snapshot.session_id,
        expected_revision=snapshot.revision, background=False,
    )
    current = documents.get_snapshot()
    assert turn.state == "completed"
    assert turn.execution_operation_id is not None
    assert turn.applied_revision == snapshot.revision + 2
    assert current.notebook["cells"][0]["outputs"][0]["text"] == "value = 2"
    assert documents.coordinator.active_lease is None


def test_real_kernel_agent_turn_commits_output_and_releases_lease():
    import pytest
    pytest.importorskip("jupyter_client")
    documents = NotebookDocumentService()
    snapshot = documents.import_notebook(notebook("value = 1"))
    scopes = TurnScopeService(documents)
    scopes.add("cell-0", editable=True)
    executions = KernelExecutionService(documents=documents)
    turns = AgentTurnService(
        documents=documents, scopes=scopes,
        adapter=FakeAgentAdapter([FakeAttempt(edits={
            "editable/cell_cell-0.py": "print(42)",
        })]), executions=executions,
    )
    try:
        turn = turns.start(
            prompt="print", session_id=snapshot.session_id,
            expected_revision=snapshot.revision, background=False,
        )
        current = documents.get_snapshot()
        assert turn.state == "completed"
        assert "42" in current.notebook["cells"][0]["outputs"][0]["text"]
        assert turn.applied_revision == current.revision
        assert documents.coordinator.active_lease is None
    finally:
        executions.shutdown()
