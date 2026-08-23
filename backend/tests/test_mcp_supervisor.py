"""Starting and stopping the editor the MCP tools talk to."""

from __future__ import annotations

import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import pytest

from backend.app.mcp.supervisor import EditorProcess, EditorStartupError, free_loopback_port


class DyingEditor(EditorProcess):
    """A child that prints why it failed and exits, as uvicorn does.

    Writes to a file rather than a pipe, mirroring the real start: the kernel
    inherits these descriptors, and a pipe nobody drains blocks its writer.
    """

    def _start_locked(self) -> None:
        self._port = free_loopback_port()
        self._log = Path(tempfile.mkstemp(prefix="eval-editor-", suffix=".log")[1])
        with self._log.open("wb") as handle:
            self._process = subprocess.Popen(
                [
                    sys.executable, "-c",
                    "import sys; print('RuntimeError: No built frontend found. "
                    "Run `npm run build`'); sys.exit(1)",
                ],
                stdout=handle, stderr=subprocess.STDOUT, start_new_session=True,
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


def test_the_child_writes_to_a_file_not_a_pipe():
    """The kernel inherits the editor child's stdout and stderr.

    Nothing reads that stream until the child fails to start, so a pipe fills
    at the buffer size and blocks its writer forever — measured: a cell writing
    400 KB to fd 1 left the kernel stuck at `running` for 45s and never
    finished. With a file it completed in 1s. Anything that reintroduces a pipe
    here reintroduces that.
    """
    import subprocess as sp

    editor = EditorProcess(startup_timeout=5)
    captured: dict[str, object] = {}
    real_popen = sp.Popen

    def record(command, **kwargs):
        captured.update(kwargs)
        return real_popen(
            [sys.executable, "-c", "import sys; sys.exit(0)"],
            **{k: v for k, v in kwargs.items()},
        )

    sp.Popen = record
    try:
        with pytest.raises(EditorStartupError):
            editor.ensure_running()
    finally:
        sp.Popen = real_popen
        editor.stop()

    assert captured.get("stdout") is not sp.PIPE, "a pipe here can block the kernel"
    assert captured.get("stdout") is not None


def test_a_named_log_survives_the_editor_stopping(tmp_path, monkeypatch):
    """The difference between `tail -f` and guessing.

    By default the log is a temp file removed on stop, which is right for a
    failure — drained and reported — and useless for watching a server that is
    working. Naming a path keeps it.
    """
    from backend.app.mcp.supervisor import LOG_PATH_ENV_VAR

    wanted = tmp_path / "logs" / "editor.log"
    monkeypatch.setenv(LOG_PATH_ENV_VAR, str(wanted))
    editor = EditorProcess(startup_timeout=20.0)
    try:
        editor.ensure_running()
    finally:
        editor.stop()
    assert wanted.exists(), "the named log outlives the editor"
    assert wanted.parent.is_dir(), "and its directory is created if missing"


def test_an_unnamed_log_is_cleaned_up(monkeypatch):
    """The default must not litter temp files across a long session."""
    from backend.app.mcp.supervisor import LOG_PATH_ENV_VAR

    monkeypatch.delenv(LOG_PATH_ENV_VAR, raising=False)
    editor = EditorProcess(startup_timeout=20.0)
    editor.ensure_running()
    log = editor._log
    assert log is not None and log.exists()
    editor.stop()
    assert not log.exists()


def test_the_editor_announces_itself_on_stderr(monkeypatch, capfd):
    """stdout is the MCP transport and has to stay clean, so the one line that
    makes a randomly chosen port findable goes to stderr, where a client keeps
    its server logs."""
    monkeypatch.delenv("NOTEBOOK_EDITOR_LOG", raising=False)
    editor = EditorProcess(startup_timeout=20.0)
    try:
        editor.ensure_running()
        out, err = capfd.readouterr()
        assert "notebook editor ready at http://127.0.0.1:" in err
        assert "notebook editor ready" not in out, "never on the transport"
    finally:
        editor.stop()
