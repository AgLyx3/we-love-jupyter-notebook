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
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

from ..process_group import terminate_process_groups


# The directory that holds the `backend` package: the repo root in a source
# checkout, site-packages once installed. Put on the child's PYTHONPATH so
# `-m uvicorn backend.app.bundled:...` resolves in either layout, rather than
# depending on which directory the MCP client happened to launch us from.
IMPORT_ROOT = Path(__file__).resolve().parents[3]
STARTUP_TIMEOUT_SECONDS = 60.0
_POLL_SECONDS = 0.1


class EditorStartupError(RuntimeError):
    """The editor process never became ready."""


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
        startup_timeout: float = STARTUP_TIMEOUT_SECONDS,
    ) -> None:
        self.workspace_root = str(workspace_root) if workspace_root is not None else None
        self.python = python or sys.executable
        self.startup_timeout = startup_timeout
        self._process: subprocess.Popen[bytes] | None = None
        self._port: int | None = None
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
        command = [
            self.python, "-m", "uvicorn",
            "backend.app.bundled:create_bundled_app", "--factory",
            "--host", "127.0.0.1", "--port", str(port),
            "--log-level", "warning",
        ]
        try:
            process = subprocess.Popen(
                command,
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                # Its own group, so the kernel uvicorn spawns is torn down with
                # it rather than reparented and left running.
                start_new_session=True,
            )
        except OSError as error:
            raise EditorStartupError(f"Could not start the editor: {error}") from error

        self._process = process
        self._port = port
        try:
            self._await_ready(process, port)
        except EditorStartupError:
            self.stop()
            raise

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

    @staticmethod
    def _drain(process: subprocess.Popen[bytes]) -> str:
        """The child's output, so a startup failure says why.

        Without it the caller gets "the editor exited" and has to go looking;
        the usual causes — no built frontend, a port taken, a bad workspace
        root — all announce themselves clearly on stderr.
        """
        if process.stdout is None:
            return "(no output captured)"
        try:
            return process.stdout.read().decode(errors="replace").strip() or "(no output)"
        except (OSError, ValueError):
            return "(output unavailable)"

    def stop(self) -> None:
        """Terminate the editor and its whole process group."""
        with self._lock:
            process = self._process
            self._process = None
            self._port = None
        if process is None:
            return
        if process.poll() is None or _group_alive(process.pid):
            terminate_process_groups([process])
        if process.stdout is not None:
            try:
                process.stdout.close()
            except OSError:
                pass

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
