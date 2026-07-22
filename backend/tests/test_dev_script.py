from __future__ import annotations

import signal
import subprocess

from scripts.dev import terminate_process_groups


class FakeProcess:
    def __init__(self, pid: int, *, exits_gracefully: bool) -> None:
        self.pid = pid
        self.returncode = None
        self.exits_gracefully = exits_gracefully
        self.wait_calls: list[float | None] = []

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        self.wait_calls.append(timeout)
        if self.exits_gracefully or timeout is None:
            self.returncode = 0
            return 0
        raise subprocess.TimeoutExpired(str(self.pid), timeout)


def test_terminate_process_groups_escalates_only_stubborn_children(monkeypatch):
    graceful = FakeProcess(101, exits_gracefully=True)
    stubborn = FakeProcess(202, exits_gracefully=False)
    signals: list[tuple[int, int]] = []
    monkeypatch.setattr("scripts.dev.os.killpg", lambda pid, sig: signals.append((pid, sig)))

    terminate_process_groups([graceful, stubborn], grace_period=0.1)

    assert signals == [
        (101, signal.SIGTERM),
        (202, signal.SIGTERM),
        (202, signal.SIGKILL),
    ]
    assert graceful.wait_calls and graceful.wait_calls[0] is not None
    assert stubborn.wait_calls[-1] is None
