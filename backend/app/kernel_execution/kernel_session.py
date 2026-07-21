from __future__ import annotations

from dataclasses import dataclass
from queue import Empty
from threading import RLock
from typing import Any
from uuid import uuid4
from time import monotonic


@dataclass(frozen=True)
class KernelResult:
    outputs: list[dict[str, Any]]
    execution_count: int | None
    error: bool


class KernelSession:
    """One persistent local Jupyter kernel, serialized by the execution service."""

    def __init__(self, *, startup_timeout: float = 30, cell_timeout: float = 300) -> None:
        self.startup_timeout = startup_timeout
        self.cell_timeout = cell_timeout
        self.kernel_session_id = uuid4().hex
        self._manager = None
        self._client = None
        self._lock = RLock()
        self._busy_attempt_id: str | None = None

    @property
    def busy_attempt_id(self) -> str | None:
        with self._lock:
            return self._busy_attempt_id

    @property
    def status(self) -> str:
        with self._lock:
            if self._manager is None:
                return "not_started"
            return "busy" if self._busy_attempt_id else "idle"

    def execute(self, source: str, attempt_id: str) -> KernelResult:
        with self._lock:
            self._ensure_started()
            self._busy_attempt_id = attempt_id
            client = self._client
        try:
            message_id = client.execute(source, stop_on_error=True)
            deadline = monotonic() + self.cell_timeout
            outputs: list[dict[str, Any]] = []
            execution_count = None
            saw_error = False
            while True:
                try:
                    remaining = deadline - monotonic()
                    if remaining <= 0:
                        raise Empty()
                    message = client.get_iopub_msg(timeout=remaining)
                except Empty as error:
                    self.interrupt()
                    raise TimeoutError("Cell execution timed out") from error
                if message.get("parent_header", {}).get("msg_id") != message_id:
                    continue
                msg_type = message["header"]["msg_type"]
                content = message["content"]
                if msg_type == "execute_input":
                    execution_count = content.get("execution_count")
                elif msg_type == "stream":
                    outputs.append({"output_type": "stream", "name": content["name"], "text": content["text"]})
                elif msg_type in {"display_data", "execute_result"}:
                    output = {"output_type": msg_type, "data": content.get("data", {}), "metadata": content.get("metadata", {})}
                    if msg_type == "execute_result":
                        output["execution_count"] = content.get("execution_count")
                        execution_count = content.get("execution_count")
                    outputs.append(output)
                elif msg_type == "error":
                    saw_error = True
                    outputs.append({"output_type": "error", "ename": content.get("ename", "Error"), "evalue": content.get("evalue", ""), "traceback": content.get("traceback", [])})
                elif msg_type == "status" and content.get("execution_state") == "idle":
                    break
            return KernelResult(outputs, execution_count, saw_error)
        finally:
            with self._lock:
                if self._busy_attempt_id == attempt_id:
                    self._busy_attempt_id = None

    def interrupt(self) -> None:
        with self._lock:
            if self._manager is not None:
                self._manager.interrupt_kernel()

    def restart(self) -> str:
        with self._lock:
            if self._manager is None:
                self.kernel_session_id = uuid4().hex
                return self.kernel_session_id
            self._manager.restart_kernel(now=True)
            self._client.wait_for_ready(timeout=self.startup_timeout)
            self.kernel_session_id = uuid4().hex
            self._busy_attempt_id = None
            return self.kernel_session_id

    def shutdown(self) -> None:
        with self._lock:
            manager, client = self._manager, self._client
            self._manager = self._client = None
            self._busy_attempt_id = None
        if client is not None:
            client.stop_channels()
        if manager is not None:
            manager.shutdown_kernel(now=True)

    def _ensure_started(self) -> None:
        if self._manager is not None:
            return
        from jupyter_client import KernelManager

        manager = KernelManager(kernel_name="python3")
        manager.start_kernel()
        client = manager.blocking_client()
        client.start_channels()
        try:
            client.wait_for_ready(timeout=self.startup_timeout)
        except Exception:
            client.stop_channels()
            manager.shutdown_kernel(now=True)
            raise
        self._manager = manager
        self._client = client
