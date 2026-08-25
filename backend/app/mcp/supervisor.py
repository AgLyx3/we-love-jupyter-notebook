"""Owning the editor process the MCP tools talk to.

The MCP server does not serve the editor itself; it starts the bundled app as
a child process on a loopback port and calls it over HTTP, the same way the
browser tab does. That is what makes the tab and the tools two views of one
session rather than two implementations of one.

Two things matter here and both are about not leaving something running. The
child is started in its own process group and torn down as a group, because
uvicorn spawns a kernel that would otherwise outlive it — the launcher
already learned this, and its teardown is shared rather than rewritten. And
the start is lazy: a client that merely lists tools should not boot a Python
kernel.
"""

from __future__ import annotations

import os
import socket
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from mcp.server.mcpserver.exceptions import ToolError

from ..process_group import terminate_process_groups


# The directory that holds the `backend` package: the repo root in a source
# checkout, site-packages once installed. Put on the child's PYTHONPATH so
# `-m uvicorn backend.app.bundled:...` resolves in either layout, rather than
# depending on which directory the MCP client happened to launch us from.
IMPORT_ROOT = Path(__file__).resolve().parents[3]
STARTUP_TIMEOUT_SECONDS = 60.0
_POLL_SECONDS = 0.1

# Where to keep the editor's log. Unset, it goes to a temp file that is deleted
# when the editor stops — fine for a failure, which is drained and reported,
# and useless for watching a working server. Set it to a path and the log stays
# there, which is the difference between `tail -f` and guessing.
LOG_PATH_ENV_VAR = "NOTEBOOK_EDITOR_LOG"


class EditorStartupError(ToolError):
    """The editor process never became ready.

    Every tool call goes through `ensure_running()`, so this escapes tool
    bodies, and its message is the whole diagnosis — the drained child log that
    says the frontend was never built, or which port refused to bind. It
    subclasses the SDK's `ToolError` for the same reason `ToolFailure` does:
    that is the only exception whose text the SDK passes on instead of
    replacing with a bare `Error executing tool <name>`.
    """


def free_loopback_port() -> int:
    """A port the OS says is free, released immediately for the child to bind.

    Racy in principle — something else could take it in between — but the
    alternative is a fixed port, which collides with a second session and with
    whatever else is on 8000. The child failing to bind is loud and recoverable;
    a silent collision with another editor's kernel is neither.
    """
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class EditorProcess:
    """The bundled editor, started on demand and stopped with this object."""

    def __init__(
        self,
        *,
        workspace_root: str | Path | None = None,
        python: str | None = None,
        kernel_python: str | Path | None = None,
        startup_timeout: float = STARTUP_TIMEOUT_SECONDS,
    ) -> None:
        self.workspace_root = str(workspace_root) if workspace_root is not None else None
        self.python = python or sys.executable
        # `python` is the interpreter that serves the editor; `kernel_python`
        # is the one that runs the notebook's cells. They are the same by
        # default and deliberately separable: an install apart from the
        # notebook's dependencies can serve the tab perfectly well while being
        # the wrong environment to execute in.
        self.kernel_python = str(kernel_python) if kernel_python is not None else None
        self.startup_timeout = startup_timeout
        self._process: subprocess.Popen[bytes] | None = None
        self._port: int | None = None
        self._log: Path | None = None
        self._keep_log = False
        # Reentrant on purpose. A failed start tears itself down while
        # `ensure_running` still holds this, and with a plain Lock that second
        # acquire never returns — the first tool call hangs, and so does every
        # call after it, because the lock is never released. The most likely
        # first-run mistake (forgetting `npm run build`) took exactly that path.
        self._lock = threading.RLock()

    @property
    def running(self) -> bool:
        return self._process is not None and self._process.poll() is None

    @property
    def base_url(self) -> str:
        if self._port is None:
            raise EditorStartupError("The editor is not running")
        return f"http://127.0.0.1:{self._port}"

    @property
    def api_url(self) -> str:
        return f"{self.base_url}/api"

    def ensure_running(self) -> str:
        """Start the editor if it is not up, and return its base URL.

        Held under a lock so two tool calls arriving together start one process
        rather than two racing for the same port.
        """
        with self._lock:
            if self.running:
                return self.base_url
            self._start_locked()
            return self.base_url

    def _start_locked(self) -> None:
        port = free_loopback_port()
        environment = os.environ.copy()
        existing = environment.get("PYTHONPATH")
        environment["PYTHONPATH"] = (
            f"{IMPORT_ROOT}{os.pathsep}{existing}" if existing else str(IMPORT_ROOT)
        )
        if self.workspace_root is not None:
            environment["NOTEBOOK_WORKSPACE_ROOT"] = self.workspace_root
        if self.kernel_python is not None:
            environment["NOTEBOOK_KERNEL_PYTHON"] = self.kernel_python
        command = [
            self.python, "-m", "uvicorn",
            "backend.app.bundled:create_bundled_app", "--factory",
            "--host", "127.0.0.1", "--port", str(port),
            "--log-level", "warning",
        ]
        # A file, not a pipe. The kernel jupyter_client starts inherits these
        # descriptors — it is launched with stdout=None — so anything a cell
        # writes to fd 1 or 2 lands here: a C extension's chatter, os.write, a
        # subprocess that did not capture its output. Nothing reads the child's
        # output until it fails to start, so a pipe fills at the buffer size
        # and blocks the writer forever, wedging the kernel mid-cell.
        # Measured: a cell writing 400 KB to fd 1 never returned. A file has no
        # such limit and still keeps the startup diagnostics readable.
        chosen = os.environ.get(LOG_PATH_ENV_VAR)
        self._keep_log = bool(chosen)
        if chosen:
            log = Path(chosen).expanduser()
            log.parent.mkdir(parents=True, exist_ok=True)
        else:
            log = Path(tempfile.mkstemp(prefix="notebook-editor-", suffix=".log")[1])
        try:
            handle = log.open("wb")
        except OSError as error:
            raise EditorStartupError(f"Could not open an editor log: {error}") from error
        try:
            with handle:
                process = subprocess.Popen(
                    command,
                    env=environment,
                    stdout=handle,
                    stderr=subprocess.STDOUT,
                    # Its own group, so the kernel uvicorn spawns is torn down
                    # with it rather than reparented and left running.
                    start_new_session=True,
                )
        except OSError as error:
            self._discard_log(log)
            raise EditorStartupError(f"Could not start the editor: {error}") from error

        self._process = process
        self._port = port
        self._log = log
        try:
            self._await_ready(process, port)
        except EditorStartupError:
            self.stop()
            raise
        # stdout is the MCP transport and must stay clean; stderr is where a
        # client puts server output in its own logs. One line there is what
        # makes a randomly chosen port findable at all.
        print(
            f"notebook editor ready at http://127.0.0.1:{port} (log: {log})",
            file=sys.stderr, flush=True,
        )

    def _await_ready(self, process: subprocess.Popen[bytes], port: int) -> None:
        deadline = time.monotonic() + self.startup_timeout
        health = f"http://127.0.0.1:{port}/api/health/ready"
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise EditorStartupError(
                    "The editor exited during startup:\n" + self._drain(process)
                )
            try:
                with urllib.request.urlopen(health, timeout=1) as response:
                    if response.status == 200:
                        return
            except (urllib.error.URLError, OSError, TimeoutError):
                pass
            time.sleep(_POLL_SECONDS)
        raise EditorStartupError(
            f"The editor did not become ready within {self.startup_timeout:.0f}s"
        )

    def _drain(self, _process: subprocess.Popen[bytes]) -> str:
        """The child's output, so a startup failure says why.

        Without it the caller gets "the editor exited" and has to go looking;
        the usual causes — no built frontend, a port taken, a bad workspace
        root — all announce themselves clearly on stderr.
        """
        if self._log is None:
            return "(no output captured)"
        try:
            text = self._log.read_text(errors="replace").strip()
        except OSError:
            return "(output unavailable)"
        # The tail is where the traceback ends and the reason is stated; a
        # long-running editor's log can be large.
        return text[-4000:] if text else "(no output)"

    def stop(self) -> None:
        """Terminate the editor and its whole process group."""
        with self._lock:
            process = self._process
            log = self._log
            self._process = None
            self._port = None
            self._log = None
        if process is None:
            self._discard_log(log)
            return
        if process.poll() is None or _group_alive(process.pid):
            terminate_process_groups([process])
        self._discard_log(log)

    def _discard_log(self, log: Path | None) -> None:
        """Remove the log unless it is one the operator asked to keep."""
        if log is not None and not self._keep_log:
            log.unlink(missing_ok=True)

    def __enter__(self) -> EditorProcess:
        self.ensure_running()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.stop()


def _group_alive(process_group_id: int) -> bool:
    try:
        os.killpg(process_group_id, 0)
    except (ProcessLookupError, PermissionError, OSError):
        return False
    return True
