#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import time
from pathlib import Path
from threading import Event


ROOT = Path(__file__).resolve().parents[1]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the notebook editor backend and frontend")
    parser.add_argument("--test-agent", action="store_true", help="use the deterministic in-process adapter (automated tests only; the app otherwise always uses the real Claude CLI)")
    parser.add_argument("--backend-port", type=int, default=8000)
    parser.add_argument("--frontend-port", type=int, default=5173)
    return parser.parse_args()


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


def child_exit_code(
    children: list[subprocess.Popen[bytes]], shutdown_requested: Event,
) -> int:
    """Return the first nonzero exit in command order, or one for a clean exit."""
    if shutdown_requested.is_set():
        return 0
    returncodes = [
        returncode
        for child in children
        if (returncode := child.poll()) is not None
    ]
    for returncode in returncodes:
        if returncode != 0:
            return returncode
    return 1


def _final_exit_code(
    children: list[subprocess.Popen[bytes]],
    pre_cleanup_returncodes: list[int | None],
    shutdown_requested: Event,
    cleanup_errors: list[str],
    startup_exit_code: int | None,
) -> int:
    for returncode in pre_cleanup_returncodes:
        if returncode not in (None, 0):
            return returncode
    if cleanup_errors:
        return 1
    if startup_exit_code is not None:
        return startup_exit_code
    if shutdown_requested.is_set():
        return 0

    cleanup_signals = {-signal.SIGTERM, -signal.SIGKILL}
    for child, before_cleanup in zip(children, pre_cleanup_returncodes, strict=True):
        final_returncode = child.poll()
        if (
            before_cleanup is None
            and final_returncode not in (None, 0)
            and final_returncode not in cleanup_signals
        ):
            return final_returncode
    return 1


def run_commands(
    commands: list[list[str]],
    environment: dict[str, str],
    shutdown_requested: Event,
    shutdown_errors: list[str],
) -> int:
    children: list[subprocess.Popen[bytes]] = []
    startup_exit_code: int | None = None
    pre_cleanup_returncodes: list[int | None] = []
    try:
        for command in commands:
            if shutdown_requested.is_set():
                break
            try:
                child = subprocess.Popen(
                    command, cwd=ROOT, env=environment, start_new_session=True,
                )
            except OSError as error:
                print(f"could not start {command[0]}: {error}", file=sys.stderr)
                startup_exit_code = 0 if shutdown_requested.is_set() else 1
                break
            children.append(child)
            if shutdown_requested.is_set():
                shutdown_errors.extend(signal_process_groups([child], signal.SIGTERM))

        if startup_exit_code is None:
            while (
                not shutdown_requested.is_set()
                and children
                and all(child.poll() is None for child in children)
            ):
                time.sleep(0.2)
        pre_cleanup_returncodes = [child.poll() for child in children]
    finally:
        shutdown_errors.extend(terminate_process_groups(children))
        for error in shutdown_errors:
            print(f"development launcher cleanup: {error}", file=sys.stderr)

    return _final_exit_code(
        children,
        pre_cleanup_returncodes,
        shutdown_requested,
        shutdown_errors,
        startup_exit_code,
    )


def main() -> int:
    args = parse_args()
    environment = os.environ.copy()
    environment["NOTEBOOK_DEFAULT_AGENT"] = "fake" if args.test_agent else environment.get("NOTEBOOK_DEFAULT_AGENT", "claude")
    environment["BACKEND_PORT"] = str(args.backend_port)
    python = ROOT / ".venv" / "bin" / "python"
    if not python.exists():
        python = Path(sys.executable)
    commands = [
        [str(python), "-m", "uvicorn", "backend.app.main:app", "--host", "127.0.0.1", "--port", str(args.backend_port)],
        ["npm", "run", "dev", "--", "--host", "127.0.0.1", "--port", str(args.frontend_port), "--strictPort"],
    ]
    shutdown_requested = Event()
    shutdown_errors: list[str] = []

    def stop(_signum: int | None = None, _frame: object | None = None) -> None:
        shutdown_requested.set()

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    return run_commands(commands, environment, shutdown_requested, shutdown_errors)


if __name__ == "__main__":
    raise SystemExit(main())
