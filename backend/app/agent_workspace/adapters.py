from __future__ import annotations

import re
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from threading import Event
from typing import Callable

from .models import (
    AdapterResult, AgentAdapterError, AgentCancelled, AgentTimedOut, AgentWorkspace,
)
from .runner import ProcessRunner


@dataclass
class FakeAttempt:
    edits: dict[str, str] = field(default_factory=dict)
    creates: dict[str, bytes | str] = field(default_factory=dict)
    final_output: str = ""
    delay: float = 0.0
    error: Exception | None = None


class FakeAgentAdapter:
    auxiliary_paths = frozenset()

    def __init__(self, attempts: list[FakeAttempt] | None = None) -> None:
        self.attempts = list(attempts or [FakeAttempt()])
        self.call_count = 0

    def run(self, workspace: AgentWorkspace, *, timeout: float, cancel_event: Event) -> AdapterResult:
        index = min(self.call_count, len(self.attempts) - 1)
        attempt = self.attempts[index]
        self.call_count += 1
        started = time.monotonic()
        deadline = started + min(timeout, attempt.delay)
        while time.monotonic() < deadline:
            if cancel_event.is_set():
                raise AgentCancelled()
            time.sleep(0.01)
        if cancel_event.is_set():
            raise AgentCancelled()
        if attempt.delay > timeout:
            raise AgentTimedOut()
        if attempt.error:
            raise attempt.error
        for relative, content in {**attempt.edits, **attempt.creates}.items():
            path = workspace.root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            if isinstance(content, bytes):
                path.write_bytes(content)
            else:
                path.write_text(content, encoding="utf-8")
        return AdapterResult(attempt.final_output)


class ClaudeAgentAdapter:
    auxiliary_paths = frozenset({".claude"})
    _VERSION = re.compile(r"(?P<major>\d+)\.(?P<minor>\d+)\.(?P<patch>\d+)")

    def __init__(self, executable: str = "claude", runner: ProcessRunner | None = None) -> None:
        self.executable = executable
        self.runner = runner or ProcessRunner()

    def verify_supported(self) -> str:
        try:
            result = subprocess.run(
                [self.executable, "--version"], capture_output=True, text=True,
                timeout=5, check=False,
            )
        except (OSError, subprocess.TimeoutExpired) as error:
            raise AgentAdapterError("Claude CLI is unavailable") from error
        match = self._VERSION.search(result.stdout + result.stderr)
        version = tuple(int(match.group(name)) for name in ("major", "minor", "patch")) if match else None
        if result.returncode or version is None or not ((2, 1, 203) <= version < (2, 2, 0)):
            raise AgentAdapterError("Unsupported Claude CLI version")
        return match.group(0)

    def run(self, workspace: AgentWorkspace, *, timeout: float, cancel_event: Event) -> AdapterResult:
        self.verify_supported()
        prompt = (workspace.root / "INSTRUCTIONS.md").read_text(encoding="utf-8")
        args = [
            self.executable, "-p", prompt, "--no-session-persistence",
            "--permission-mode", "acceptEdits", "--allowedTools", "Read", "Edit", "Write",
        ]
        stdout, _stderr = self.runner.run(
            args, cwd=workspace.root, timeout=timeout, cancel_event=cancel_event
        )
        return AdapterResult(stdout.strip())
