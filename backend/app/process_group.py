"""Stopping a child process and everything it spawned.

Lives in the package rather than in ``scripts/`` because two callers need it
and only one of them is a development script: the MCP supervisor starts the
same kind of child and has to tear it down the same way, and ``scripts/`` is
not part of the distribution — importing it from installed code fails with
``No module named 'scripts'``.

The group is the unit throughout. uvicorn spawns a Jupyter kernel, so
signalling the process alone leaves the kernel reparented and running; every
child here is started with ``start_new_session=True`` and signalled by group.
"""

from __future__ import annotations

import os
import signal
import subprocess
import time


def _process_error(action: str, child: subprocess.Popen[bytes], error: BaseException) -> str:
    return f"could not {action} process group {child.pid}: {error}"


def signal_process_groups(
    children: list[subprocess.Popen[bytes]], sig: int,
) -> list[str]:
    errors: list[str] = []
    for child in children:
        try:
            os.killpg(child.pid, sig)
        except ProcessLookupError:
            pass
        except OSError as error:
            errors.append(_process_error(f"send signal {sig} to", child, error))
    return errors


def _process_group_exists(process_group_id: int) -> bool:
    try:
        os.killpg(process_group_id, 0)
    except ProcessLookupError:
        return False
    return True


def terminate_process_groups(
    children: list[subprocess.Popen[bytes]], *, grace_period: float = 5,
) -> list[str]:
    errors: list[str] = []
    uncertain_children: set[int] = set()
    for child in children:
        try:
            os.killpg(child.pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
        except OSError as error:
            errors.append(_process_error("terminate", child, error))
            uncertain_children.add(id(child))

    deadline = time.monotonic() + grace_period
    for child in children:
        uncertain = id(child) in uncertain_children
        try:
            exited = child.poll() is not None
        except OSError as error:
            errors.append(_process_error("poll", child, error))
            uncertain = True
            exited = False

        if not exited:
            try:
                child.wait(timeout=max(0, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                uncertain = True
            except (OSError, subprocess.SubprocessError) as error:
                errors.append(_process_error("wait for", child, error))
                uncertain = True

        group_exists = False
        try:
            while not uncertain and _process_group_exists(child.pid):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    break
                time.sleep(min(0.02, remaining))
            group_exists = _process_group_exists(child.pid)
        except OSError as error:
            errors.append(_process_error("inspect", child, error))
            uncertain = True

        if not uncertain and not group_exists:
            continue

        kill_succeeded = False
        try:
            os.killpg(child.pid, signal.SIGKILL)
            kill_succeeded = True
        except ProcessLookupError:
            pass
        except OSError as error:
            errors.append(_process_error("kill", child, error))
        try:
            child.wait(timeout=1)
        except (OSError, subprocess.SubprocessError) as error:
            errors.append(_process_error("finally reap", child, error))
        if kill_succeeded:
            group_deadline = time.monotonic() + 1
            try:
                while _process_group_exists(child.pid) and time.monotonic() < group_deadline:
                    time.sleep(0.02)
                if _process_group_exists(child.pid):
                    errors.append(
                        f"process group {child.pid} still exists after SIGKILL"
                    )
            except OSError as error:
                errors.append(_process_error("inspect after SIGKILL", child, error))
    return errors
