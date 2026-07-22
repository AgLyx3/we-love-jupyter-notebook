from __future__ import annotations

import signal
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from threading import Event

import pytest

from scripts.dev import child_exit_code, terminate_process_groups


ROOT = Path(__file__).resolve().parents[2]


class FakeProcess:
    def __init__(
        self,
        pid: int,
        *,
        exits_gracefully: bool,
        wait_error: OSError | None = None,
    ) -> None:
        self.pid = pid
        self.returncode = None
        self.exits_gracefully = exits_gracefully
        self.wait_error = wait_error
        self.wait_calls: list[float | None] = []

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        self.wait_calls.append(timeout)
        if self.wait_error is not None:
            raise self.wait_error
        if self.exits_gracefully or timeout is None:
            self.returncode = 0
            return 0
        raise subprocess.TimeoutExpired(str(self.pid), timeout)


def test_terminate_process_groups_escalates_only_stubborn_children(monkeypatch):
    graceful = FakeProcess(101, exits_gracefully=True)
    stubborn = FakeProcess(202, exits_gracefully=False)
    signals: list[tuple[int, int]] = []
    monkeypatch.setattr("scripts.dev.os.killpg", lambda pid, sig: signals.append((pid, sig)))

    errors = terminate_process_groups([graceful, stubborn], grace_period=0.1)

    assert signals == [
        (101, signal.SIGTERM),
        (202, signal.SIGTERM),
        (202, signal.SIGKILL),
    ]
    assert graceful.wait_calls and graceful.wait_calls[0] is not None
    assert stubborn.wait_calls[-1] is None
    assert errors == []


def test_terminate_process_groups_reports_errors_and_continues(monkeypatch):
    broken = FakeProcess(
        101,
        exits_gracefully=False,
        wait_error=OSError("wait failed"),
    )
    graceful = FakeProcess(202, exits_gracefully=True)
    signals: list[tuple[int, int]] = []
    monkeypatch.setattr("scripts.dev.os.killpg", lambda pid, sig: signals.append((pid, sig)))

    errors = terminate_process_groups([broken, graceful], grace_period=0.1)

    assert any("101" in error and "wait failed" in error for error in errors)
    assert graceful.wait_calls
    assert (202, signal.SIGTERM) in signals


def test_child_exit_code_treats_unexpected_clean_exit_as_failure():
    child = FakeProcess(101, exits_gracefully=True)
    child.returncode = 0

    assert child_exit_code([child], Event()) == 1


def test_child_exit_code_preserves_failure_and_intentional_shutdown():
    child = FakeProcess(101, exits_gracefully=True)
    child.returncode = 7
    shutdown_requested = Event()

    assert child_exit_code([child], shutdown_requested) == 7
    shutdown_requested.set()
    assert child_exit_code([child], shutdown_requested) == 0


def _free_ports(count: int) -> list[int]:
    listeners = [socket.socket() for _ in range(count)]
    try:
        for listener in listeners:
            listener.bind(("127.0.0.1", 0))
        return [listener.getsockname()[1] for listener in listeners]
    finally:
        for listener in listeners:
            listener.close()


def _port_accepts_connections(port: int) -> bool:
    with socket.socket() as client:
        client.settimeout(0.1)
        return client.connect_ex(("127.0.0.1", port)) == 0


@pytest.mark.parametrize("shutdown_signal", [signal.SIGINT, signal.SIGTERM])
def test_sigterm_launcher_exits_and_releases_server_ports(shutdown_signal):
    backend_port, frontend_port = _free_ports(2)
    with tempfile.TemporaryFile() as output:
        process = subprocess.Popen(
            [
                "/bin/sh",
                "-c",
                "exec \"$1\" scripts/dev.py --fake-agent --backend-port \"$2\" "
                "--frontend-port \"$3\"",
                "playwright-webserver",
                sys.executable,
                str(backend_port),
                str(frontend_port),
            ],
            cwd=ROOT,
            stdout=output,
            stderr=subprocess.STDOUT,
        )

        def diagnostics() -> str:
            output.flush()
            output.seek(0)
            return output.read().decode(errors="replace")

        try:
            deadline = time.monotonic() + 15
            while time.monotonic() < deadline:
                if _port_accepts_connections(backend_port) and _port_accepts_connections(frontend_port):
                    break
                assert process.poll() is None, (
                    "development launcher exited during startup:\n" + diagnostics()
                )
                time.sleep(0.05)
            else:
                raise AssertionError(
                    "development servers did not start within 15 seconds:\n" + diagnostics()
                )

            started = time.monotonic()
            process.send_signal(shutdown_signal)
            process.wait(timeout=8)
            assert time.monotonic() - started < 8
            assert process.returncode == 0, diagnostics()

            deadline = time.monotonic() + 2
            while time.monotonic() < deadline and (
                _port_accepts_connections(backend_port) or _port_accepts_connections(frontend_port)
            ):
                time.sleep(0.05)
            assert not _port_accepts_connections(backend_port), diagnostics()
            assert not _port_accepts_connections(frontend_port), diagnostics()
        finally:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=2)
