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
    parser.add_argument("--fake-agent", action="store_true", help="use the deterministic test adapter")
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


def terminate_process_groups(
    children: list[subprocess.Popen[bytes]], *, grace_period: float = 5,
) -> list[str]:
    errors = signal_process_groups(children, signal.SIGTERM)
    deadline = time.monotonic() + grace_period
    for child in children:
        try:
            if child.poll() is not None:
                continue
        except OSError as error:
            errors.append(_process_error("poll", child, error))
            continue
        try:
            child.wait(timeout=max(0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            except OSError as error:
                errors.append(_process_error("kill", child, error))
            try:
                child.wait()
            except (OSError, subprocess.SubprocessError) as error:
                errors.append(_process_error("wait for", child, error))
        except (OSError, subprocess.SubprocessError) as error:
            errors.append(_process_error("wait for", child, error))
    return errors


def child_exit_code(
    children: list[subprocess.Popen[bytes]], shutdown_requested: Event,
) -> int:
    if shutdown_requested.is_set():
        return 0
    for child in children:
        returncode = child.poll()
        if returncode is not None:
            return returncode if returncode != 0 else 1
    return 1


def main() -> int:
    args = parse_args()
    environment = os.environ.copy()
    environment["NOTEBOOK_AGENT_ADAPTER"] = "fake" if args.fake_agent else environment.get("NOTEBOOK_AGENT_ADAPTER", "claude")
    environment["BACKEND_PORT"] = str(args.backend_port)
    python = ROOT / ".venv" / "bin" / "python"
    if not python.exists():
        python = Path(sys.executable)
    commands = [
        [str(python), "-m", "uvicorn", "backend.app.main:app", "--host", "127.0.0.1", "--port", str(args.backend_port)],
        ["npm", "run", "dev", "--", "--host", "127.0.0.1", "--port", str(args.frontend_port), "--strictPort"],
    ]
    children: list[subprocess.Popen[bytes]] = []
    shutdown_requested = Event()
    shutdown_errors: list[str] = []

    def stop(_signum: int | None = None, _frame: object | None = None) -> None:
        shutdown_requested.set()
        shutdown_errors.extend(signal_process_groups(children, signal.SIGTERM))

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    exit_code: int | None = None
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
                exit_code = 1
                break
            children.append(child)
            if shutdown_requested.is_set():
                shutdown_errors.extend(signal_process_groups([child], signal.SIGTERM))

        if exit_code is None:
            while (
                not shutdown_requested.is_set()
                and children
                and all(child.poll() is None for child in children)
            ):
                time.sleep(0.2)
            exit_code = child_exit_code(children, shutdown_requested)
    finally:
        shutdown_errors.extend(terminate_process_groups(children))
        for error in shutdown_errors:
            print(f"development launcher cleanup: {error}", file=sys.stderr)

    assert exit_code is not None
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
