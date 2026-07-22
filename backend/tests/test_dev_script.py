from __future__ import annotations

import signal
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from threading import Event

import pytest

from scripts.dev import child_exit_code, run_commands, terminate_process_groups


ROOT = Path(__file__).resolve().parents[2]


class FakeProcess:
    def __init__(
        self,
        pid: int,
        *,
        exits_gracefully: bool,
        poll_error: OSError | None = None,
        wait_errors: list[BaseException | None] | None = None,
    ) -> None:
        self.pid = pid
        self.returncode = None
        self.exits_gracefully = exits_gracefully
        self.poll_error = poll_error
        self.wait_errors = list(wait_errors or [])
        self.wait_calls: list[float | None] = []

    def poll(self):
        if self.poll_error is not None:
            raise self.poll_error
        return self.returncode

    def wait(self, timeout=None):
        self.wait_calls.append(timeout)
        if self.wait_errors:
            error = self.wait_errors.pop(0)
            if error is not None:
                raise error
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
        wait_errors=[OSError("wait failed"), None],
    )
    graceful = FakeProcess(202, exits_gracefully=True)
    signals: list[tuple[int, int]] = []
    monkeypatch.setattr("scripts.dev.os.killpg", lambda pid, sig: signals.append((pid, sig)))

    errors = terminate_process_groups([broken, graceful], grace_period=0.1)

    assert any("101" in error and "wait failed" in error for error in errors)
    assert (101, signal.SIGKILL) in signals
    assert len(broken.wait_calls) == 2
    assert graceful.wait_calls
    assert (202, signal.SIGTERM) in signals


def test_terminate_process_groups_kills_and_reaps_after_poll_error(monkeypatch):
    broken = FakeProcess(
        101,
        exits_gracefully=True,
        poll_error=OSError("poll failed"),
    )
    graceful = FakeProcess(202, exits_gracefully=True)
    signals: list[tuple[int, int]] = []
    monkeypatch.setattr("scripts.dev.os.killpg", lambda pid, sig: signals.append((pid, sig)))

    errors = terminate_process_groups([broken, graceful], grace_period=0.1)

    assert signals[:2] == [(101, signal.SIGTERM), (202, signal.SIGTERM)]
    assert (101, signal.SIGKILL) in signals
    assert broken.wait_calls
    assert graceful.wait_calls
    assert any("101" in error and "poll failed" in error for error in errors)


def test_terminate_process_groups_kills_and_reaps_after_term_error(monkeypatch):
    broken = FakeProcess(101, exits_gracefully=True)
    graceful = FakeProcess(202, exits_gracefully=True)
    signals: list[tuple[int, int]] = []

    def signal_group(pid, sig):
        signals.append((pid, sig))
        if pid == 101 and sig == signal.SIGTERM:
            raise PermissionError("term denied")

    monkeypatch.setattr("scripts.dev.os.killpg", signal_group)

    errors = terminate_process_groups([broken, graceful], grace_period=0.1)

    assert signals[:2] == [(101, signal.SIGTERM), (202, signal.SIGTERM)]
    assert (101, signal.SIGKILL) in signals
    assert len(broken.wait_calls) == 2
    assert graceful.wait_calls
    assert any("101" in error and "term denied" in error for error in errors)


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


@pytest.mark.parametrize("returncodes", [(0, 7), (7, 0)])
def test_child_exit_code_prefers_nonzero_exit_in_any_child_order(returncodes):
    children = [FakeProcess(101, exits_gracefully=True), FakeProcess(202, exits_gracefully=True)]
    for child, returncode in zip(children, returncodes, strict=True):
        child.returncode = returncode

    assert child_exit_code(children, Event()) == 7


@pytest.mark.parametrize("shutdown_requested, expected", [(False, 1), (True, 0)])
def test_run_commands_classifies_popen_error_after_shutdown(
    monkeypatch, shutdown_requested, expected,
):
    shutdown = Event()

    def fail_to_start(*_args, **_kwargs):
        if shutdown_requested:
            shutdown.set()
        raise OSError("injected startup failure")

    monkeypatch.setattr("scripts.dev.subprocess.Popen", fail_to_start)

    assert run_commands([["missing"]], {}, shutdown, []) == expected


def _reserve_ports(count: int) -> tuple[list[int], list[socket.socket]]:
    listeners = [socket.socket() for _ in range(count)]
    try:
        for listener in listeners:
            listener.bind(("127.0.0.1", 0))
        return [listener.getsockname()[1] for listener in listeners], listeners
    except BaseException:
        for listener in listeners:
            listener.close()
        raise


def _port_accepts_connections(port: int) -> bool:
    with socket.socket() as client:
        client.settimeout(0.1)
        return client.connect_ex(("127.0.0.1", port)) == 0


def _read_url(url: str) -> bytes | None:
    try:
        with urllib.request.urlopen(url, timeout=0.2) as response:
            if response.status != 200:
                return None
            return response.read()
    except (OSError, TimeoutError, urllib.error.URLError):
        return None


def _servers_have_expected_identity(backend_port: int, frontend_port: int) -> bool:
    backend_ready = _read_url(f"http://127.0.0.1:{backend_port}/health/ready")
    frontend_page = _read_url(f"http://127.0.0.1:{frontend_port}/")
    proxied_ready = _read_url(f"http://127.0.0.1:{frontend_port}/api/health/ready")
    return (
        backend_ready == b'{"status":"ready"}'
        and frontend_page is not None
        and b"<title>Notebook Agent Editor</title>" in frontend_page
        and b'/frontend/src/main.tsx' in frontend_page
        and proxied_ready == b'{"status":"ready"}'
    )


@pytest.mark.parametrize("shutdown_signal", [signal.SIGINT, signal.SIGTERM])
def test_sigterm_launcher_exits_and_releases_server_ports(shutdown_signal):
    (backend_port, frontend_port), reservations = _reserve_ports(2)
    with tempfile.TemporaryFile() as output:
        for reservation in reservations:
            reservation.close()
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
            ready_since = None
            while time.monotonic() < deadline:
                assert process.poll() is None, (
                    "development launcher exited during startup:\n" + diagnostics()
                )
                if _servers_have_expected_identity(backend_port, frontend_port):
                    ready_since = ready_since or time.monotonic()
                    if time.monotonic() - ready_since >= 0.5:
                        break
                else:
                    ready_since = None
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
