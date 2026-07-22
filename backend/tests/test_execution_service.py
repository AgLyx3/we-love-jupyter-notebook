import json
import threading
import time
import asyncio

import nbformat
import pytest

from backend.app.kernel_execution.kernel_session import KernelResult, KernelSession
from backend.app.kernel_execution.models import (
    ExecutionDecisionConflict, KernelCellTimeout, KernelExecutionCancelled,
    KernelOutputLimitExceeded, KernelRestartRequired,
)
from backend.app.kernel_execution.models import ExecutionNotFound
from backend.app.session_events.service import SessionEventService
from backend.app.kernel_execution.service import (
    KernelExecutionService, serialize_operation,
)
from backend.app.notebook_document.service import NotebookDocumentService
from backend.app.notebook_document.models import RevisionConflict
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
    def execution_correlation(self):
        return self.kernel_session_id, self.busy_attempt_id
    def restart(self): self.kernel_session_id = "kernel-2"
    def interrupt_correlated(self, kernel_session_id, attempt_id):
        matched = (
            kernel_session_id == self.kernel_session_id
            and attempt_id == self.busy_attempt_id
        )
        if matched:
            self.interrupt()
        return matched
    def restart_correlated(self, kernel_session_id, attempt_id, on_matched=None):
        if not self.interrupt_correlated(kernel_session_id, attempt_id): return False
        if on_matched is not None:
            on_matched()
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


def test_notebook_replacement_uses_fresh_kernel_even_when_old_shutdown_fails():
    class FailingShutdownKernel(FakeKernel):
        max_output_items = 1000
        max_output_bytes = 5 * 1024 * 1024

        def shutdown(self):
            raise RuntimeError("injected shutdown failure")

    documents = NotebookDocumentService()
    first = documents.import_notebook(notebook("x = 1"))
    service = KernelExecutionService(
        documents=documents, kernel=FailingShutdownKernel(),
    )
    service._kernel_notebook_session_id = first.session_id

    replaced = documents.import_notebook(
        notebook("x = 2"), expected_session_id=first.session_id,
        expected_revision=first.revision,
    )

    assert documents.get_snapshot() == replaced
    assert service._kernel_notebook_session_id == replaced.session_id
    assert service.kernel is not None
    assert not isinstance(service.kernel, FailingShutdownKernel)


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


def test_repeated_identical_approval_is_idempotent_after_attempt_advances():
    documents = NotebookDocumentService()
    snapshot = documents.import_notebook(notebook("open('x', 'w')"))
    service = KernelExecutionService(documents=documents, kernel=FakeKernel())
    lease = documents.coordinator.acquire(
        operation_type="agent_turn", operation_id="turn-1",
    )
    operation = service.create_downstream(
        parent_turn_id="turn-1", session_id=snapshot.session_id,
        expected_revision=snapshot.revision, cancel_event=threading.Event(),
    )
    worker = threading.Thread(target=lambda: service.run_downstream(
        operation.operation_id, changed_cell_ids={"cell-0"}, lease=lease,
    ))
    worker.start()
    for _ in range(100):
        pending = service.get(operation.operation_id)
        if pending.state == "awaiting_approval":
            break
        time.sleep(.01)
    attempt_id = pending.current_attempt_id
    decision = {
        "session_id": snapshot.session_id,
        "expected_revision": snapshot.revision,
        "turn_id": "turn-1",
        "cell_id": "cell-0",
    }
    service.approve(attempt_id, **decision)
    worker.join(2)
    documents.coordinator.release(lease)

    replayed = service.approve(attempt_id, **decision)
    assert replayed.state == "completed"
    with pytest.raises(ExecutionDecisionConflict):
        service.skip(attempt_id, **decision)


@pytest.mark.parametrize("cancel_kind", ["attempt", "parent"])
def test_cancel_uses_immutable_correlation_across_attempt_turnover(cancel_kind):
    entered = threading.Event()
    release = threading.Event()

    class TurnoverKernel(FakeKernel):
        def __init__(self):
            super().__init__()
            self.busy_attempt_id = None
            self.correlations = []
            self.interrupted = []

        def execute(self, source, attempt_id):
            self.busy_attempt_id = attempt_id
            entered.set()
            assert release.wait(2)
            self.busy_attempt_id = None
            return KernelResult([], 1, False)

        def interrupt(self):
            self.interrupted.append(self.busy_attempt_id)

        def interrupt_correlated(self, kernel_session_id, attempt_id):
            self.correlations.append((kernel_session_id, attempt_id))
            self.busy_attempt_id = "subsequent-attempt"
            matched = (
                kernel_session_id == self.kernel_session_id
                and attempt_id == self.busy_attempt_id
            )
            if matched:
                self.interrupted.append(self.busy_attempt_id)
            release.set()
            return matched

    documents = NotebookDocumentService()
    snapshot = documents.import_notebook(notebook("value = 1"))
    kernel = TurnoverKernel()
    service = KernelExecutionService(documents=documents, kernel=kernel)
    lease = documents.coordinator.acquire(
        operation_type="agent_turn", operation_id="turn-1",
    )
    operation = service.create_downstream(
        parent_turn_id="turn-1", session_id=snapshot.session_id,
        expected_revision=snapshot.revision, cancel_event=threading.Event(),
    )
    worker = threading.Thread(target=lambda: service.run_downstream(
        operation.operation_id, changed_cell_ids={"cell-0"}, lease=lease,
    ))
    worker.start()
    assert entered.wait(2)
    attempt_id = service.get(operation.operation_id).current_attempt_id
    if cancel_kind == "attempt":
        service.cancel(
            attempt_id, session_id=snapshot.session_id,
            expected_revision=snapshot.revision, turn_id="turn-1",
            cell_id="cell-0",
        )
    else:
        service.cancel_parent("turn-1")
    worker.join(2)
    documents.coordinator.release(lease)

    assert kernel.correlations == [("kernel-1", attempt_id)]
    assert kernel.interrupted == []
    assert service.get(operation.operation_id).state == "cancelled"


def test_parent_cancel_while_awaiting_approval_never_executes_kernel():
    documents = NotebookDocumentService()
    snapshot = documents.import_notebook(notebook("open('x', 'w')"))
    kernel = FakeKernel()
    service = KernelExecutionService(documents=documents, kernel=kernel)
    lease = documents.coordinator.acquire(operation_type="agent_turn", operation_id="turn-1")
    operation = service.create_downstream(
        parent_turn_id="turn-1", session_id=snapshot.session_id,
        expected_revision=snapshot.revision, cancel_event=threading.Event(),
    )
    worker = threading.Thread(target=lambda: service.run_downstream(
        operation.operation_id, changed_cell_ids={"cell-0"}, lease=lease,
    ))
    worker.start()
    for _ in range(100):
        pending = service.get(operation.operation_id)
        if pending.state == "awaiting_approval":
            break
        time.sleep(.01)
    service.cancel_parent("turn-1")
    worker.join(2)
    documents.coordinator.release(lease)
    assert service.get(operation.operation_id).state == "cancelled"
    assert kernel.sources == []


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


def test_real_kernel_updates_display_output_in_place():
    import pytest
    pytest.importorskip("jupyter_client")
    source = (
        "from IPython.display import display\n"
        "handle = display('old', display_id=True)\n"
        "handle.update('new')"
    )
    documents = NotebookDocumentService()
    snapshot = documents.import_notebook(notebook(source))
    service = KernelExecutionService(documents=documents)
    try:
        created = service.start_cell(cell_id="cell-0", session_id=snapshot.session_id, expected_revision=snapshot.revision)
        assert wait_terminal(service, created.operation_id).state == "completed"
        outputs = documents.get_snapshot().notebook["cells"][0]["outputs"]
        assert len(outputs) == 1
        assert "new" in outputs[0]["data"]["text/plain"]
    finally:
        service.shutdown()


def test_real_kernel_input_fails_promptly_without_stdin_wait():
    import pytest
    pytest.importorskip("jupyter_client")
    documents = NotebookDocumentService()
    snapshot = documents.import_notebook(notebook("input('prompt: ')"))
    service = KernelExecutionService(documents=documents)
    started = time.monotonic()
    try:
        created = service.start_cell(cell_id="cell-0", session_id=snapshot.session_id, expected_revision=snapshot.revision)
        assert wait_terminal(service, created.operation_id, timeout=10).state == "failed"
        assert time.monotonic() - started < 10
        output = documents.get_snapshot().notebook["cells"][0]["outputs"][0]
        assert output["output_type"] == "error"
        assert output["ename"] == "StdinNotImplementedError"
    finally:
        service.shutdown()


def test_real_kernel_output_limit_interrupts_without_commit():
    documents = NotebookDocumentService()
    snapshot = documents.import_notebook(notebook("print('x' * 100_000)"))
    kernel = KernelSession(max_output_items=10, max_output_bytes=1024)
    service = KernelExecutionService(documents=documents, kernel=kernel)
    try:
        created = service.start_cell(
            cell_id="cell-0", session_id=snapshot.session_id,
            expected_revision=snapshot.revision,
        )
        failed = wait_terminal(service, created.operation_id, timeout=10)
        assert failed.state == "failed"
        assert failed.error["code"] == "output_limit_exceeded"
        assert failed.error["details"]["truncated"] is True
        assert documents.get_snapshot().revision == snapshot.revision
    finally:
        service.shutdown()


def test_real_kernel_busy_restart_cancels_operation_promptly():
    import pytest
    pytest.importorskip("jupyter_client")
    documents = NotebookDocumentService()
    snapshot = documents.import_notebook(notebook("import time; time.sleep(30)"))
    service = KernelExecutionService(documents=documents)
    try:
        created = service.start_cell(cell_id="cell-0", session_id=snapshot.session_id, expected_revision=snapshot.revision)
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            status = service.kernel_status()
            if status["state"] == "busy":
                break
            time.sleep(.01)
        assert status["state"] == "busy"
        service.restart(
            status["kernelSessionId"],
            execution_attempt_id=status["executionAttemptId"],
        )
        cancelled = wait_terminal(service, created.operation_id, timeout=10)
        assert cancelled.state == "cancelled"
        assert documents.get_snapshot().revision == snapshot.revision
        deadline = time.monotonic() + 5
        while documents.coordinator.active_lease is not None and time.monotonic() < deadline:
            time.sleep(.01)
        assert documents.coordinator.active_lease is None
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
        def __init__(self):
            super().__init__()
            self.status = "idle"
            self.did_timeout = False
        def execute(self, source, attempt_id):
            if not self.did_timeout:
                self.did_timeout = True
                self.status = "restart_required"
                raise KernelCellTimeout(recovered=False)
            return super().execute(source, attempt_id)
        def restart_correlated(self, session_id, attempt_id, on_matched=None):
            if session_id != self.kernel_session_id or attempt_id is not None:
                return False
            if on_matched:
                on_matched()
            self.status = "idle"
            self.kernel_session_id = "kernel-restarted"
            return True

    documents = NotebookDocumentService()
    snapshot = documents.import_notebook(notebook("while True: pass"))
    service = KernelExecutionService(documents=documents, kernel=TimeoutKernel())
    operation = service.start_cell(cell_id="cell-0", session_id=snapshot.session_id, expected_revision=snapshot.revision)
    completed = wait_terminal(service, operation.operation_id)
    assert completed.state == "timed_out"
    assert completed.error["code"] == "cell_timed_out"
    assert completed.error["details"] == {"kernelRecovered": False}
    assert service.kernel_status()["state"] == "restart_required"
    with pytest.raises(KernelRestartRequired):
        service.start_cell(
            cell_id="cell-0", session_id=snapshot.session_id,
            expected_revision=snapshot.revision,
        )
    service.restart("kernel-1", execution_attempt_id=None)
    allowed = service.start_cell(
        cell_id="cell-0", session_id=snapshot.session_id,
        expected_revision=snapshot.revision,
    )
    assert wait_terminal(service, allowed.operation_id).state == "completed"


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


def test_correlated_restart_failure_marks_kernel_unusable():
    class Manager:
        def restart_kernel(self, now):
            assert now is True
            raise RuntimeError("restart failed")

    kernel = KernelSession()
    kernel._manager = Manager()
    kernel._client = object()
    kernel._busy_attempt_id = "attempt-1"
    original_session_id = kernel.kernel_session_id

    with pytest.raises(RuntimeError, match="restart failed"):
        kernel.restart_correlated(original_session_id, "attempt-1")

    assert kernel.status == "restart_required"
    assert kernel.busy_attempt_id is None
    assert kernel.kernel_session_id == original_session_id


@pytest.mark.parametrize("failure_stage", ["blocking_client", "start_channels"])
def test_kernel_startup_failure_shuts_down_started_process(monkeypatch, failure_stage):
    calls = []

    class Client:
        def start_channels(self):
            calls.append("start_channels")
            if failure_stage == "start_channels":
                raise RuntimeError("channels failed")

        def stop_channels(self):
            calls.append("stop_channels")

        def wait_for_ready(self, timeout):
            calls.append("ready")

    class Manager:
        def start_kernel(self):
            calls.append("start_kernel")

        def blocking_client(self):
            calls.append("blocking_client")
            if failure_stage == "blocking_client":
                raise RuntimeError("client failed")
            return Client()

        def shutdown_kernel(self, now):
            calls.append(("shutdown_kernel", now))

    monkeypatch.setattr("jupyter_client.KernelManager", lambda **_: Manager())
    kernel = KernelSession()
    with pytest.raises(RuntimeError):
        kernel._ensure_started()

    assert ("shutdown_kernel", True) in calls
    assert kernel.status == "not_started"
    if failure_stage == "start_channels":
        assert "stop_channels" in calls


def test_busy_restart_invalidates_operation_and_prevents_commit():
    entered = threading.Event()
    release = threading.Event()

    class RestartableKernel(FakeKernel):
        def __init__(self):
            super().__init__()
            self.busy_attempt_id = None
        def execute(self, source, attempt_id):
            self.busy_attempt_id = attempt_id
            entered.set()
            assert release.wait(2)
            return KernelResult([{"output_type": "stream", "name": "stdout", "text": "late"}], 1, False)
        def restart_correlated(self, session_id, attempt_id, on_matched=None):
            if session_id != self.kernel_session_id or attempt_id != self.busy_attempt_id:
                return False
            if on_matched:
                on_matched()
            self.busy_attempt_id = None
            self.kernel_session_id = "kernel-2"
            release.set()
            return True

    documents = NotebookDocumentService()
    snapshot = documents.import_notebook(notebook("value = 1"))
    kernel = RestartableKernel()
    service = KernelExecutionService(documents=documents, kernel=kernel)
    created = service.start_cell(cell_id="cell-0", session_id=snapshot.session_id, expected_revision=snapshot.revision)
    assert entered.wait(2)
    attempt_id = service.get(created.operation_id).current_attempt_id
    status = service.restart("kernel-1", execution_attempt_id=attempt_id)
    assert status["kernelSessionId"] == "kernel-2"
    assert service.get(created.operation_id).state == "cancelled"
    for _ in range(100):
        if documents.coordinator.active_lease is None:
            break
        time.sleep(.01)
    assert documents.get_snapshot().revision == snapshot.revision


def test_restart_failure_marks_correlated_attempt_structured_and_inactive():
    entered = threading.Event()

    class FailingRestartKernel(FakeKernel):
        def __init__(self):
            super().__init__()
            self.busy_attempt_id = None
        def execute(self, source, attempt_id):
            self.busy_attempt_id = attempt_id
            entered.set()
            while self.busy_attempt_id == attempt_id:
                time.sleep(.01)
            raise KernelExecutionCancelled()
        def restart_correlated(self, session_id, attempt_id, on_matched=None):
            if on_matched:
                on_matched()
            self.busy_attempt_id = None
            raise RuntimeError("restart broke")

    documents = NotebookDocumentService()
    snapshot = documents.import_notebook(notebook("value = 1"))
    kernel = FailingRestartKernel()
    service = KernelExecutionService(documents=documents, kernel=kernel)
    created = service.start_cell(cell_id="cell-0", session_id=snapshot.session_id, expected_revision=snapshot.revision)
    assert entered.wait(2)
    attempt_id = service.get(created.operation_id).current_attempt_id
    with pytest.raises(RuntimeError, match="restart broke"):
        service.restart("kernel-1", execution_attempt_id=attempt_id)
    failed = service.get(created.operation_id)
    assert failed.state == "failed"
    assert failed.error["code"] == "kernel_restart_failed"
    assert failed.attempts[0].active is False


def test_kernel_message_output_state_machine_and_stdin_policy():
    class Client:
        def __init__(self, messages): self.messages = iter(messages)
        def execute(self, source, **kwargs):
            assert kwargs == {"stop_on_error": True, "allow_stdin": False}
            return "message-1"
        def get_iopub_msg(self, timeout): return next(self.messages)
    def message(kind, content):
        return {"parent_header": {"msg_id": "message-1"}, "header": {"msg_type": kind}, "content": content}
    messages = [
        message("display_data", {"data": {"text/plain": "old"}, "metadata": {}, "transient": {"display_id": "d"}}),
        message("display_data", {"data": {"text/plain": "also old"}, "metadata": {}, "transient": {"display_id": "d"}}),
        message("update_display_data", {"data": {"text/plain": "new"}, "metadata": {"updated": True}, "transient": {"display_id": "d"}}),
        message("status", {"execution_state": "idle"}),
    ]
    kernel = KernelSession()
    kernel._manager = object()
    kernel._client = Client(messages)
    result = kernel.execute("display", "attempt")
    assert result.outputs == [
        {"output_type": "display_data", "data": {"text/plain": "new"}, "metadata": {"updated": True}},
        {"output_type": "display_data", "data": {"text/plain": "new"}, "metadata": {"updated": True}},
    ]

    clear_messages = [
        message("stream", {"name": "stdout", "text": "old"}),
        message("clear_output", {"wait": True}),
        message("stream", {"name": "stdout", "text": "new"}),
        message("status", {"execution_state": "idle"}),
    ]
    kernel._client = Client(clear_messages)
    result = kernel.execute("clear", "attempt-2")
    assert result.outputs == [{"output_type": "stream", "name": "stdout", "text": "new"}]


def test_unexpected_kernel_error_marks_attempt_inactive_and_structured():
    class BrokenKernel(FakeKernel):
        def execute(self, source, attempt_id):
            raise RuntimeError("kernel broke")
    documents = NotebookDocumentService()
    snapshot = documents.import_notebook(notebook("value = 1"))
    service = KernelExecutionService(documents=documents, kernel=BrokenKernel())
    created = service.start_cell(cell_id="cell-0", session_id=snapshot.session_id, expected_revision=snapshot.revision)
    failed = wait_terminal(service, created.operation_id)
    assert failed.state == "failed"
    assert failed.error["code"] == "kernel_execution_failed"
    assert failed.attempts[0].state == "failed"
    assert failed.attempts[0].active is False


def test_terminal_operation_retention_prunes_operations_and_attempts():
    documents = NotebookDocumentService()
    snapshot = documents.import_notebook(notebook("value = 1"))
    service = KernelExecutionService(
        documents=documents, kernel=FakeKernel(),
        max_terminal_operations=2, max_history_bytes=1024 * 1024,
    )
    completed = []
    revision = snapshot.revision
    for _ in range(3):
        created = service.start_cell(cell_id="cell-0", session_id=snapshot.session_id, expected_revision=revision)
        operation = wait_terminal(service, created.operation_id)
        completed.append(operation)
        revision = operation.current_revision
    with pytest.raises(ExecutionNotFound):
        service.get(completed[0].operation_id)
    with pytest.raises(ExecutionNotFound):
        service.get(completed[0].attempts[0].attempt_id)
    assert service.get(completed[-2].operation_id).state == "completed"
    assert service.get(completed[-1].operation_id).state == "completed"

    byte_documents = NotebookDocumentService()
    byte_snapshot = byte_documents.import_notebook(notebook("value = 1"))
    byte_service = KernelExecutionService(
        documents=byte_documents, kernel=FakeKernel(),
        max_terminal_operations=100, max_history_bytes=1,
    )
    first = wait_terminal(byte_service, byte_service.start_cell(
        cell_id="cell-0", session_id=byte_snapshot.session_id,
        expected_revision=byte_snapshot.revision,
    ).operation_id)
    second = wait_terminal(byte_service, byte_service.start_cell(
        cell_id="cell-0", session_id=byte_snapshot.session_id,
        expected_revision=first.current_revision,
    ).operation_id)
    with pytest.raises(ExecutionNotFound):
        byte_service.get(first.operation_id)
    assert byte_service.get(second.operation_id).state == "completed"


def test_single_large_operation_history_is_bounded_with_durable_truncation():
    documents = NotebookDocumentService()
    snapshot = documents.import_notebook(notebook(*(["x" * 2_000] * 10)))
    byte_budget = 10_000
    service = KernelExecutionService(
        documents=documents, kernel=FakeKernel(),
        max_terminal_operations=100, max_history_bytes=byte_budget,
    )
    completed = wait_terminal(service, service.start_all(
        session_id=snapshot.session_id,
        expected_revision=snapshot.revision,
    ).operation_id)

    first_read = service.get(completed.operation_id)
    second_read = service.get(completed.operation_id)
    assert any(attempt.outputs_truncated for attempt in first_read.attempts)
    assert [item.outputs_truncated for item in first_read.attempts] == [
        item.outputs_truncated for item in second_read.attempts
    ]
    payload_size = len(json.dumps(
        serialize_operation(first_read), default=str, separators=(",", ":"),
    ).encode())
    assert payload_size <= byte_budget


def test_event_journal_enforces_count_bytes_and_active_session():
    events = SessionEventService(max_events=2, max_bytes=10_000, retention_seconds=60)
    events.activate_session("one")
    for index in range(3):
        events.publish("test", {"sessionId": "one", "index": index, "payload": "x" * 100})
    retained = events.list(session_id="one")
    assert [item.data["index"] for item in retained] == [1, 2]
    byte_events = SessionEventService(max_events=10, max_bytes=120)
    byte_events.activate_session("one")
    for index in range(2):
        byte_events.publish("test", {"sessionId": "one", "index": index, "payload": "x" * 100})
    assert [item.data["index"] for item in byte_events.list()] == [1]
    events.activate_session("two")
    assert events.list(session_id="one") == []
    events.publish("test", {"sessionId": "two", "index": 4})
    assert [item.data["index"] for item in events.list(session_id="two")] == [4]

    async def disconnected_stream_stops():
        stream = events.stream(
            session_id="two", is_disconnected=lambda: asyncio.sleep(0, result=True),
        )
        with pytest.raises(StopAsyncIteration):
            await anext(stream)
    asyncio.run(disconnected_stream_stops())

    async def replacement_closes_open_stream():
        events.activate_session("stream-one")
        events.publish("test", {"sessionId": "stream-one", "index": 1})
        stream = events.stream(
            session_id="stream-one",
            is_disconnected=lambda: asyncio.sleep(0, result=False),
        )
        assert (await anext(stream)).data["index"] == 1
        events.activate_session("stream-two")
        with pytest.raises(StopAsyncIteration):
            await anext(stream)
    asyncio.run(replacement_closes_open_stream())


def test_kernel_output_collection_is_bounded_before_commit():
    class Manager:
        def __init__(self): self.interrupted = 0
        def interrupt_kernel(self): self.interrupted += 1
    class Client:
        def __init__(self):
            self.messages = iter([
                {"parent_header": {"msg_id": "m"}, "header": {"msg_type": "stream"}, "content": {"name": "stdout", "text": "a" * 20}},
                {"parent_header": {"msg_id": "m"}, "header": {"msg_type": "stream"}, "content": {"name": "stdout", "text": "b" * 20}},
                {"parent_header": {"msg_id": "m"}, "header": {"msg_type": "stream"}, "content": {"name": "stdout", "text": "c" * 20}},
                {"parent_header": {"msg_id": "m"}, "header": {"msg_type": "status"}, "content": {"execution_state": "idle"}},
            ])
        def execute(self, source, **kwargs): return "m"
        def get_iopub_msg(self, timeout): return next(self.messages)
    kernel = KernelSession(max_output_items=2, max_output_bytes=10_000)
    manager = Manager()
    kernel._manager = manager
    kernel._client = Client()
    with pytest.raises(KernelOutputLimitExceeded) as raised:
        kernel.execute("spam", "attempt")
    assert raised.value.recovered is True
    assert manager.interrupted == 1

    class LimitedKernel(FakeKernel):
        def execute(self, source, attempt_id):
            raise KernelOutputLimitExceeded(recovered=True)
    documents = NotebookDocumentService()
    snapshot = documents.import_notebook(notebook("print('spam')"))
    service = KernelExecutionService(documents=documents, kernel=LimitedKernel())
    created = service.start_cell(
        cell_id="cell-0", session_id=snapshot.session_id,
        expected_revision=snapshot.revision,
    )
    failed = wait_terminal(service, created.operation_id)
    assert failed.state == "failed"
    assert failed.error == {
        "code": "output_limit_exceeded",
        "message": "Kernel output exceeded the configured limit",
        "details": {"truncated": True, "kernelRecovered": True},
    }
    assert documents.get_snapshot().revision == snapshot.revision


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


def test_agent_cancel_retry_uses_accepted_child_output_lineage():
    second_entered = threading.Event()
    release_second = threading.Event()

    class TwoCellKernel(FakeKernel):
        def execute(self, source, attempt_id):
            self.busy_attempt_id = attempt_id
            if len(self.sources) == 1:
                second_entered.set()
                assert release_second.wait(2)
            try:
                return super().execute(source, attempt_id)
            finally:
                self.busy_attempt_id = None
        def interrupt(self):
            release_second.set()

    documents = NotebookDocumentService()
    snapshot = documents.import_notebook(notebook("value = 1", "value = 2"))
    scopes = TurnScopeService(documents)
    scopes.add("cell-0", editable=True)
    executions = KernelExecutionService(documents=documents, kernel=TwoCellKernel())
    turns = AgentTurnService(
        documents=documents, scopes=scopes,
        adapter=FakeAgentAdapter([FakeAttempt(edits={
            "editable/cell_cell-0.py": "value = 10",
        })]), executions=executions,
    )
    turn = turns.start(
        prompt="change", session_id=snapshot.session_id,
        expected_revision=snapshot.revision,
    )
    assert second_entered.wait(2)
    accepted_revision = documents.get_snapshot().revision
    first = turns.cancel(
        turn.turn_id, session_id=snapshot.session_id,
        expected_revision=accepted_revision,
    )
    second = turns.cancel(
        turn.turn_id, session_id=snapshot.session_id,
        expected_revision=accepted_revision,
    )
    assert first.accepted_cancel_revision == second.accepted_cancel_revision
    for _ in range(200):
        cancelled = turns.get(turn.turn_id)
        if cancelled.state == "cancelled":
            break
        time.sleep(.01)
    assert cancelled.state == "cancelled"
    current = documents.get_snapshot()
    updated = documents.update_cell_source(
        cell_id="cell-1", source="unrelated = True",
        expected_revision=current.revision, owner="manual",
        expected_session_id=current.session_id,
    )
    with pytest.raises(RevisionConflict):
        turns.cancel(
            turn.turn_id, session_id=updated.session_id,
            expected_revision=accepted_revision,
        )


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
