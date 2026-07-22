from __future__ import annotations

import os
import selectors
import signal
import subprocess
import time
from pathlib import Path
from threading import Event, RLock

from .models import AgentAdapterError, AgentCancelled, AgentTimedOut

MAX_CAPTURE_BYTES = 1024 * 1024


class ProcessRunner:
    def __init__(self, *, max_capture_bytes: int = MAX_CAPTURE_BYTES) -> None:
        self.max_capture_bytes = max_capture_bytes
        self._lock = RLock()
        self._cleanup_lock = RLock()
        self._active: dict[int, subprocess.Popen] = {}
        self._shutting_down = False

    def run(
        self, args: list[str], *, cwd: Path, timeout: float,
        cancel_event: Event, grace_period: float = 1.0,
    ) -> tuple[str, str]:
        selector = selectors.DefaultSelector()
        try:
            with self._lock:
                if self._shutting_down:
                    raise AgentAdapterError("Agent process runner is shutting down")
                process = subprocess.Popen(
                    args, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                    start_new_session=True,
                )
                self._active[process.pid] = process
        except AgentAdapterError:
            selector.close()
            raise
        except (OSError, ValueError) as error:
            selector.close()
            raise AgentAdapterError("Agent process could not be started") from error

        output = {"stdout": bytearray(), "stderr": bytearray()}
        streams = (("stdout", process.stdout), ("stderr", process.stderr))
        failure: Exception | None = None
        deadline = time.monotonic() + timeout
        return_code: int | None = None
        try:
            for name, stream in streams:
                if stream is not None:
                    os.set_blocking(stream.fileno(), False)
                    selector.register(stream, selectors.EVENT_READ, name)
            while True:
                if cancel_event.is_set():
                    failure = AgentCancelled()
                    break
                if time.monotonic() >= deadline:
                    failure = AgentTimedOut()
                    break
                self._read_ready(selector, output)
                return_code = process.poll()
                if return_code is not None:
                    break
        except Exception as error:
            failure = AgentAdapterError("Agent process communication failed")
            failure.__cause__ = error
        finally:
            try:
                self._terminate_group(process, grace_period)
            except Exception as error:
                if failure is None:
                    failure = AgentAdapterError("Agent process cleanup failed")
                    failure.__cause__ = error
            try:
                self._drain(selector, output)
            except Exception as error:
                if failure is None:
                    failure = AgentAdapterError("Agent process communication failed")
                    failure.__cause__ = error
            finally:
                with self._lock:
                    self._active.pop(process.pid, None)
                selector.close()
                for _name, stream in streams:
                    if stream is not None:
                        stream.close()

        if failure is None and cancel_event.is_set():
            failure = AgentCancelled()
        if failure is not None:
            raise failure
        try:
            stdout = bytes(output["stdout"]).decode("utf-8")
            stderr = bytes(output["stderr"]).decode("utf-8")
        except UnicodeDecodeError as error:
            raise AgentAdapterError("Agent process output was not valid UTF-8") from error
        if return_code:
            raise AgentAdapterError(
                "Agent process failed", exitCode=return_code,
                stderr=stderr, stdout=stdout,
            )
        return stdout, stderr

    def shutdown(self, *, grace_period: float = 0.1) -> None:
        """Reject new processes and idempotently terminate every active group."""
        with self._lock:
            self._shutting_down = True
            active = tuple(self._active.values())
        for process in active:
            self._terminate_group(process, grace_period)

    def _read_ready(self, selector, output, timeout: float = 0.05) -> None:
        for key, _mask in selector.select(timeout):
            data = os.read(key.fileobj.fileno(), 65536)
            if not data:
                selector.unregister(key.fileobj)
                continue
            target = output[key.data]
            remaining = self.max_capture_bytes - len(target)
            if remaining > 0:
                target.extend(data[:remaining])

    def _drain(self, selector, output) -> None:
        deadline = time.monotonic() + 0.25
        while selector.get_map() and time.monotonic() < deadline:
            self._read_ready(selector, output, timeout=0.01)

    def _terminate_group(
        self, process: subprocess.Popen, grace_period: float,
    ) -> None:
        with self._cleanup_lock:
            self._terminate_group_unlocked(process, grace_period)

    @staticmethod
    def _terminate_group_unlocked(
        process: subprocess.Popen, grace_period: float,
    ) -> None:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except OSError:
            pass
        try:
            process.wait(timeout=grace_period)
        except subprocess.TimeoutExpired:
            pass
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except OSError:
            pass
        if process.poll() is None:
            try:
                process.wait(timeout=max(grace_period, 0.1))
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
