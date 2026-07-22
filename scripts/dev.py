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


def signal_process_groups(children: list[subprocess.Popen[bytes]], sig: int) -> None:
    for child in children:
        if child.poll() is None:
            try:
                os.killpg(child.pid, sig)
            except ProcessLookupError:
                pass


def terminate_process_groups(
    children: list[subprocess.Popen[bytes]], *, grace_period: float = 5,
) -> None:
    signal_process_groups(children, signal.SIGTERM)
    deadline = time.monotonic() + grace_period
    for child in children:
        if child.poll() is not None:
            continue
        try:
            child.wait(timeout=max(0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            child.wait()


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

    def stop(_signum: int | None = None, _frame: object | None = None) -> None:
        shutdown_requested.set()
        signal_process_groups(children, signal.SIGTERM)

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    try:
        for command in commands:
            children.append(subprocess.Popen(
                command, cwd=ROOT, env=environment, start_new_session=True,
            ))
        while not shutdown_requested.is_set() and all(child.poll() is None for child in children):
            time.sleep(0.2)
        if shutdown_requested.is_set():
            return 0
        return next((child.returncode or 0 for child in children if child.poll() is not None), 0)
    finally:
        terminate_process_groups(children)


if __name__ == "__main__":
    raise SystemExit(main())
