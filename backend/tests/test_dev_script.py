from __future__ import annotations

import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

from scripts.dev import terminate_process_groups


ROOT = Path(__file__).resolve().parents[2]


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


def _free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return listener.getsockname()[1]


def _port_accepts_connections(port: int) -> bool:
    with socket.socket() as client:
        client.settimeout(0.1)
        return client.connect_ex(("127.0.0.1", port)) == 0


def test_sigterm_launcher_exits_and_releases_server_ports():
    backend_port = _free_port()
    frontend_port = _free_port()
    process = subprocess.Popen(
        [
            sys.executable,
            "scripts/dev.py",
            "--fake-agent",
            "--backend-port",
            str(backend_port),
            "--frontend-port",
            str(frontend_port),
        ],
        cwd=ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 15
        while time.monotonic() < deadline:
            if _port_accepts_connections(backend_port) and _port_accepts_connections(frontend_port):
                break
            assert process.poll() is None, "development launcher exited during startup"
            time.sleep(0.05)
        else:
            raise AssertionError("development servers did not start within 15 seconds")

        started = time.monotonic()
        process.send_signal(signal.SIGTERM)
        process.wait(timeout=8)
        assert time.monotonic() - started < 8
        assert process.returncode == 0

        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and (
            _port_accepts_connections(backend_port) or _port_accepts_connections(frontend_port)
        ):
            time.sleep(0.05)
        assert not _port_accepts_connections(backend_port)
        assert not _port_accepts_connections(frontend_port)
    finally:
        if process.poll() is None:
            process.kill()
            process.wait(timeout=2)
