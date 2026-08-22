"""Starting and stopping the editor the MCP tools talk to."""

from __future__ import annotations

import subprocess
import sys
import threading
import time

import pytest

from backend.app.mcp.supervisor import EditorProcess, EditorStartupError, free_loopback_port


class DyingEditor(EditorProcess):
    """A child that prints why it failed and exits, as uvicorn does."""

    def _start_locked(self) -> None:
        self._port = free_loopback_port()
        self._process = subprocess.Popen(
            [
                sys.executable, "-c",
                "import sys; print('RuntimeError: No built frontend found. "
                "Run `npm run build`'); sys.exit(1)",
            ],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, start_new_session=True,
        )
        self._await_ready(self._process, self._port)


def test_a_failed_start_raises_instead_of_deadlocking():
    """The first-run bug this test exists for.

    A failed start tears itself down while `ensure_running` still holds the
    lock. With a non-reentrant lock that second acquire never returned: the
    first tool call hung forever and every later one hung behind it, with
    nothing said. Forgetting `npm run build` took exactly that path.
    """
    editor = DyingEditor(startup_timeout=10)
    finished = threading.Event()
    outcome: list[BaseException | None] = []

    def attempt() -> None:
        try:
            editor.ensure_running()
            outcome.append(None)
        except BaseException as error:  # noqa: BLE001 - recording, not handling
            outcome.append(error)
        finally:
            finished.set()

    threading.Thread(target=attempt, daemon=True).start()
    assert finished.wait(20), "ensure_running never returned — the lock deadlocked"
    assert isinstance(outcome[0], EditorStartupError)


def test_the_failure_says_what_the_child_said():
    """A first-timer needs the child's own words, not "it exited"."""
    editor = DyingEditor(startup_timeout=10)
    with pytest.raises(EditorStartupError) as raised:
        editor.ensure_running()
    assert "npm run build" in str(raised.value)


def test_the_editor_is_usable_again_after_a_failed_start():
    """The lock must be free, and no half-started process left behind."""
    editor = DyingEditor(startup_timeout=10)
    for _ in range(3):
        with pytest.raises(EditorStartupError):
            editor.ensure_running()
    assert editor.running is False
    editor.stop()


def test_a_free_port_is_a_real_one():
    port = free_loopback_port()
    assert 1024 < port < 65536


def test_stopping_an_editor_that_never_started_is_harmless():
    EditorProcess().stop()
