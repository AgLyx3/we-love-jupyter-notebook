from __future__ import annotations

from dataclasses import dataclass
import json
import os
import sys
from queue import Empty
from threading import RLock
from typing import Any
from uuid import uuid4
from time import monotonic

from .models import (
    KernelCellTimeout, KernelExecutionCancelled, KernelOutputLimitExceeded,
    KernelRestartRequired,
)


# Which interpreter runs the cells. Unset, jupyter_client resolves the
# `python3` kernelspec, and the environment the editor is installed into wins
# that lookup — its own `share/jupyter/kernels` sorts ahead of the user
# directory. That is the right answer when the editor was installed beside the
# notebooks' own dependencies and the wrong one when it was installed apart
# from them, which is what `uvx` and `pipx` do.
KERNEL_PYTHON_ENV_VAR = "NOTEBOOK_KERNEL_PYTHON"


def _as_launched(argv0: str) -> str:
    """What a kernelspec's first argument becomes when the kernel is started.

    `jupyter_client.KernelManager.format_kernel_cmd` swaps a bare `python`,
    `python3` or `python3.12` for `sys.executable` before launching, on the
    reasoning that an interpreter running from an unactivated environment will
    not find its own `python` on PATH. Reporting the spec's literal `python`
    would therefore name something that never runs — and worse, it would hide
    the exact case this is for: the `python3` kernelspec inside the editor's
    own environment says `python`, and that substitution is how a notebook ends
    up executing against the editor's packages rather than the project's.

    Anything absolute is already the answer and is returned unchanged.
    """
    aliases = {
        "python",
        f"python{sys.version_info[0]}",
        f"python{sys.version_info[0]}.{sys.version_info[1]}",
    }
    return sys.executable if argv0 in aliases else argv0


@dataclass(frozen=True)
class KernelResult:
    outputs: list[dict[str, Any]]
    execution_count: int | None
    error: bool


class KernelSession:
    """One persistent local Jupyter kernel, serialized by the execution service."""

    def __init__(
        self, *, startup_timeout: float = 30, cell_timeout: float = 300,
        recovery_timeout: float = 5, max_output_items: int = 1000,
        max_output_bytes: int = 5 * 1024 * 1024,
        kernel_python: str | None = None,
    ) -> None:
        # Read once, here, rather than at start: a restart builds a replacement
        # session from this object's attributes, and a value re-read later
        # would let the interpreter change under a notebook mid-session.
        self.kernel_python = kernel_python or os.environ.get(KERNEL_PYTHON_ENV_VAR) or None
        self.startup_timeout = startup_timeout
        self.cell_timeout = cell_timeout
        self.recovery_timeout = recovery_timeout
        self.max_output_items = max_output_items
        self.max_output_bytes = max_output_bytes
        self.kernel_session_id = uuid4().hex
        self._manager = None
        # Resolved lazily and remembered, because the answer is wanted before a
        # kernel has ever started — a person reading "Kernel not started" is
        # entitled to know which interpreter it would be — and looking up a
        # kernelspec on every status poll would be silly.
        self._kernelspec_interpreter: str | None = None
        self._looked_for_kernelspec = False
        self._client = None
        self._lock = RLock()
        self._busy_attempt_id: str | None = None
        self._restart_required = False

    @property
    def interpreter(self) -> str | None:
        """The Python that runs the cells, or None if it cannot be determined.

        This exists because `ModuleNotFoundError` has two opposite fixes and
        nothing on screen said which one applied (#52): the environment is
        right and is missing a package, or the environment is wrong entirely.
        Naming the interpreter is what tells those apart.

        Three sources, most authoritative first. A started kernel's spec is the
        truth about what is running. Before one starts, `kernel_python` is what
        it *will* be. With neither, jupyter_client would resolve the `python3`
        kernelspec, so this resolves the same one to give the same answer.

        Never raises. A status endpoint that fails because a kernelspec lookup
        threw would be a worse bug than the one this is here to diagnose.
        """
        if self._manager is not None:
            argv = getattr(getattr(self._manager, "kernel_spec", None), "argv", None)
            if argv:
                return _as_launched(argv[0])
        if self.kernel_python is not None:
            return self.kernel_python
        if not self._looked_for_kernelspec:
            self._looked_for_kernelspec = True
            try:
                from jupyter_client.kernelspec import KernelSpecManager

                argv = KernelSpecManager().get_kernel_spec("python3").argv
                self._kernelspec_interpreter = _as_launched(argv[0]) if argv else None
            except Exception:  # noqa: BLE001 - no kernelspec is a real state
                self._kernelspec_interpreter = None
        return self._kernelspec_interpreter

    @property
    def interpreter_source(self) -> str:
        """Where `interpreter` came from, so a caller can say *why* it is that.

        The distinction a reader needs is whether someone chose this
        interpreter or whether it is simply what the editor's own environment
        resolves to — which is the case that goes wrong, and the case a
        `--kernel-python` that did not take effect looks exactly like.
        """
        return "kernel-python" if self.kernel_python is not None else "kernelspec"

    @property
    def busy_attempt_id(self) -> str | None:
        with self._lock:
            return self._busy_attempt_id

    def execution_correlation(self) -> tuple[str, str | None]:
        with self._lock:
            return self.kernel_session_id, self._busy_attempt_id

    @property
    def status(self) -> str:
        with self._lock:
            if self._manager is None:
                return "not_started"
            if self._restart_required:
                return "restart_required"
            return "busy" if self._busy_attempt_id else "idle"

    def execute(self, source: str, attempt_id: str) -> KernelResult:
        with self._lock:
            if self._restart_required:
                raise KernelRestartRequired()
            self._ensure_started()
            self._busy_attempt_id = attempt_id
            client = self._client
        try:
            message_id = client.execute(
                source, stop_on_error=True, allow_stdin=False,
            )
            deadline = monotonic() + self.cell_timeout
            outputs: list[dict[str, Any]] = []
            display_indexes: dict[str, list[int]] = {}
            output_bytes = 0
            clear_pending = False
            execution_count = None
            saw_error = False

            def retain(output: dict[str, Any], display_id: str | None = None) -> None:
                nonlocal output_bytes
                size = self._output_size(output)
                if (
                    len(outputs) >= self.max_output_items
                    or output_bytes + size > self.max_output_bytes
                ):
                    self._raise_output_limit(client)
                outputs.append(output)
                output_bytes += size
                if display_id:
                    display_indexes.setdefault(display_id, []).append(len(outputs) - 1)
            while True:
                try:
                    remaining = deadline - monotonic()
                    if remaining <= 0:
                        raise Empty()
                    message = client.get_iopub_msg(timeout=min(remaining, 0.1))
                except Empty as error:
                    with self._lock:
                        if self._busy_attempt_id != attempt_id:
                            raise KernelExecutionCancelled() from error
                    if monotonic() < deadline:
                        continue
                    recovered = self._interrupt_and_wait_idle(client)
                    with self._lock:
                        self._restart_required = not recovered
                    raise KernelCellTimeout(recovered=recovered) from error
                if message.get("parent_header", {}).get("msg_id") != message_id:
                    continue
                msg_type = message["header"]["msg_type"]
                content = message["content"]
                if msg_type == "clear_output":
                    if content.get("wait", False):
                        clear_pending = True
                    else:
                        outputs.clear()
                        display_indexes.clear()
                        output_bytes = 0
                    continue
                if msg_type in {
                    "stream", "display_data", "update_display_data",
                    "execute_result", "error",
                } and clear_pending:
                    outputs.clear()
                    display_indexes.clear()
                    output_bytes = 0
                    clear_pending = False
                if msg_type == "execute_input":
                    execution_count = content.get("execution_count")
                elif msg_type == "stream":
                    retain({"output_type": "stream", "name": content["name"], "text": content["text"]})
                elif msg_type in {"display_data", "execute_result"}:
                    output = {"output_type": msg_type, "data": content.get("data", {}), "metadata": content.get("metadata", {})}
                    if msg_type == "execute_result":
                        output["execution_count"] = content.get("execution_count")
                        execution_count = content.get("execution_count")
                    display_id = content.get("transient", {}).get("display_id")
                    retain(output, display_id)
                elif msg_type == "update_display_data":
                    display_id = content.get("transient", {}).get("display_id")
                    if display_id in display_indexes:
                        replacements = []
                        new_total = output_bytes
                        for index in display_indexes[display_id]:
                            target = dict(outputs[index])
                            new_total -= self._output_size(outputs[index])
                            target["data"] = content.get("data", {})
                            target["metadata"] = content.get("metadata", {})
                            new_total += self._output_size(target)
                            replacements.append((index, target))
                        if new_total > self.max_output_bytes:
                            self._raise_output_limit(client)
                        for index, target in replacements:
                            outputs[index] = target
                        output_bytes = new_total
                elif msg_type == "error":
                    saw_error = True
                    retain({"output_type": "error", "ename": content.get("ename", "Error"), "evalue": content.get("evalue", ""), "traceback": content.get("traceback", [])})
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

    def interrupt_correlated(
        self, kernel_session_id: str, execution_attempt_id: str | None,
    ) -> bool:
        with self._lock:
            if (
                kernel_session_id != self.kernel_session_id
                or execution_attempt_id != self._busy_attempt_id
            ):
                return False
            if self._manager is not None:
                self._manager.interrupt_kernel()
            return True

    def restart(self) -> str:
        with self._lock:
            if self._manager is None:
                self.kernel_session_id = uuid4().hex
                self._restart_required = False
                return self.kernel_session_id
            try:
                self._manager.restart_kernel(now=True)
                self._client.wait_for_ready(timeout=self.startup_timeout)
            except Exception:
                self._busy_attempt_id = None
                self._restart_required = True
                raise
            self.kernel_session_id = uuid4().hex
            self._busy_attempt_id = None
            self._restart_required = False
            return self.kernel_session_id

    def restart_correlated(
        self, kernel_session_id: str, execution_attempt_id: str | None,
        on_matched=None,
    ) -> bool:
        with self._lock:
            if (
                kernel_session_id != self.kernel_session_id
                or execution_attempt_id != self._busy_attempt_id
            ):
                return False
            if on_matched is not None:
                on_matched()
            self._busy_attempt_id = None
            if self._manager is not None:
                try:
                    self._manager.restart_kernel(now=True)
                    self._client.wait_for_ready(timeout=self.startup_timeout)
                except Exception:
                    self._restart_required = True
                    raise
            self.kernel_session_id = uuid4().hex
            self._restart_required = False
            return True

    def shutdown(self) -> None:
        with self._lock:
            manager, client = self._manager, self._client
            self._manager = self._client = None
            self._busy_attempt_id = None
            self._restart_required = False
        errors: list[Exception] = []
        if client is not None:
            try:
                client.stop_channels()
            except Exception as error:
                errors.append(error)
        if manager is not None:
            try:
                manager.shutdown_kernel(now=True)
            except Exception as error:
                errors.append(error)
        if errors:
            raise ExceptionGroup("Kernel shutdown cleanup failed", errors)

    def _ensure_started(self) -> None:
        if self._manager is not None:
            return
        from jupyter_client import KernelManager

        manager = KernelManager(kernel_name="python3")
        if self.kernel_python is not None:
            # Same launcher, different interpreter. Replacing argv rather than
            # naming another kernelspec means the caller points at a virtualenv
            # they already have instead of first registering one with Jupyter.
            manager.kernel_spec.argv = [
                self.kernel_python, "-m", "ipykernel_launcher",
                "-f", "{connection_file}",
            ]
        manager.start_kernel()
        client = None
        try:
            client = manager.blocking_client()
            client.start_channels()
            client.wait_for_ready(timeout=self.startup_timeout)
        except Exception:
            if client is not None:
                try:
                    client.stop_channels()
                except Exception:
                    pass
            try:
                manager.shutdown_kernel(now=True)
            except Exception:
                pass
            raise
        self._manager = manager
        self._client = client

    def _interrupt_and_wait_idle(self, client) -> bool:
        with self._lock:
            manager = self._manager
            if manager is None:
                return False
            manager.interrupt_kernel()
        deadline = monotonic() + self.recovery_timeout
        while monotonic() < deadline:
            try:
                message = client.get_iopub_msg(timeout=max(0.01, deadline - monotonic()))
            except Empty:
                return False
            if (
                message.get("header", {}).get("msg_type") == "status"
                and message.get("content", {}).get("execution_state") == "idle"
            ):
                return True
        return False

    @staticmethod
    def _output_size(output: dict[str, Any]) -> int:
        return len(json.dumps(output, default=str, separators=(",", ":")).encode())

    def _raise_output_limit(self, client) -> None:
        recovered = self._interrupt_and_wait_idle(client)
        with self._lock:
            self._restart_required = not recovered
        raise KernelOutputLimitExceeded(recovered=recovered)
