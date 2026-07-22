#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import signal
import subprocess
import sys
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the notebook editor backend and frontend")
    parser.add_argument("--fake-agent", action="store_true", help="use the deterministic test adapter")
    parser.add_argument("--backend-port", type=int, default=8000)
    parser.add_argument("--frontend-port", type=int, default=5173)
    return parser.parse_args()


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

    def stop(_signum: int | None = None, _frame: object | None = None) -> None:
        for child in children:
            if child.poll() is None:
                child.terminate()

    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGTERM, stop)
    try:
        for command in commands:
            children.append(subprocess.Popen(command, cwd=ROOT, env=environment))
        while all(child.poll() is None for child in children):
            time.sleep(0.2)
        return next((child.returncode or 0 for child in children if child.poll() is not None), 0)
    finally:
        stop()
        for child in children:
            try:
                child.wait(timeout=5)
            except subprocess.TimeoutExpired:
                child.kill()
                child.wait()


if __name__ == "__main__":
    raise SystemExit(main())
