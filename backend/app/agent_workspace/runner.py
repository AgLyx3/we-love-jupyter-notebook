from __future__ import annotations

import os
import signal
import subprocess
import time
from pathlib import Path
from threading import Event

from .models import AgentAdapterError, AgentCancelled, AgentTimedOut


class ProcessRunner:
    def run(
        self, args: list[str], *, cwd: Path, timeout: float,
        cancel_event: Event, grace_period: float = 1.0,
    ) -> tuple[str, str]:
        try:
            process = subprocess.Popen(
                args, cwd=cwd, text=True, stdout=subprocess.PIPE,
                stderr=subprocess.PIPE, start_new_session=True,
            )
        except OSError as error:
            raise AgentAdapterError("Agent process could not be started") from error
        deadline = time.monotonic() + timeout
        try:
            while True:
                if cancel_event.is_set():
                    raise AgentCancelled()
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise AgentTimedOut()
                try:
                    stdout, stderr = process.communicate(timeout=min(0.05, remaining))
                    break
                except subprocess.TimeoutExpired:
                    continue
            if process.returncode:
                raise AgentAdapterError(
                    "Agent process failed", exitCode=process.returncode,
                    stderr=stderr[-2000:], stdout=stdout[-2000:],
                )
            return stdout, stderr
        except (AgentCancelled, AgentTimedOut):
            self._terminate_group(process, grace_period)
            raise

    @staticmethod
    def _terminate_group(process: subprocess.Popen, grace_period: float) -> None:
        if process.poll() is not None:
            return
        try:
            os.killpg(process.pid, signal.SIGTERM)
            process.wait(timeout=grace_period)
        except subprocess.TimeoutExpired:
            os.killpg(process.pid, signal.SIGKILL)
            process.wait()
        except ProcessLookupError:
            pass
