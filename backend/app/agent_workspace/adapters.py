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

    def run(
        self, workspace: AgentWorkspace, *, timeout: float, cancel_event: Event,
        model: str | None = None, permission_mode: str = "acceptEdits",
    ) -> AdapterResult:
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


class DevelopmentFakeAgentAdapter:
    """Prompt-driven adapter used only by the explicit local fake mode."""

    auxiliary_paths = frozenset()

    def run(
        self, workspace: AgentWorkspace, *, timeout: float, cancel_event: Event,
        model: str | None = None, permission_mode: str = "acceptEdits",
    ) -> AdapterResult:
        if cancel_event.is_set():
            raise AgentCancelled()
        instructions = (workspace.root / "INSTRUCTIONS.md").read_text(encoding="utf-8")
        prompt = instructions.splitlines()[0].lower() if instructions else ""
        if workspace.is_trusted:
            return AdapterResult("The development fake adapter does not perform Trusted structural edits.")
        editable = workspace.manifest.editable_cells
        if not editable:
            return AdapterResult("No editable cell was selected.")
        target = workspace.root / editable[0].relative_path
        if "[risk]" in prompt:
            source = "import os\nvalues = [5, 10, 15]\n_ = os.getenv('HOME')\n"
            output = "Updated values and added an environment lookup for approval testing."
        else:
            source = "values = [3, 6, 9]\n"
            output = "Updated the selected values deterministically."
        target.write_text(source, encoding="utf-8")
        return AdapterResult(output)


class ClaudeAgentAdapter:
    auxiliary_paths = frozenset()
    _VERSION = re.compile(r"(?P<major>\d+)\.(?P<minor>\d+)\.(?P<patch>\d+)")
    # Model aliases the UI may request. Anything else falls back to the CLI default.
    _MODEL_ALIASES = frozenset({"opus", "sonnet", "haiku"})
    # Permission modes the UI may request. Anything else falls back to acceptEdits.
    _PERMISSION_MODES = frozenset({"acceptEdits", "plan"})
    _PLAN_PREAMBLE = (
        "You are operating in plan mode. Do not edit, create, or modify any file.\n"
        "Investigate as needed, then respond in your final message with a concrete,\n"
        "step-by-step plan for how you would carry out the request below. Stop after\n"
        "presenting the plan.\n\n"
    )

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

    def run(
        self, workspace: AgentWorkspace, *, timeout: float, cancel_event: Event,
        model: str | None = None, permission_mode: str = "acceptEdits",
    ) -> AdapterResult:
        self.verify_supported()
        prompt = (workspace.root / "INSTRUCTIONS.md").read_text(encoding="utf-8")
        if permission_mode not in self._PERMISSION_MODES:
            permission_mode = "acceptEdits"
        # In plan mode the CLI blocks writes; we also reframe the prompt so the
        # agent answers with a plan instead of attempting an edit.
        if permission_mode == "plan":
            prompt = self._PLAN_PREAMBLE + prompt
        # A Trusted turn makes the whole notebook editable, so it always gets
        # edit/write tools. A Blocking read-only turn (no editable cells) gets no
        # edit/write tools, so the boundary is enforced at the tool level as well
        # as by the workspace audit.
        if workspace.is_trusted:
            tools = "Read,Edit,Write"
        else:
            tools = "Read,Edit,Write" if workspace.manifest.editable_cells else "Read"
        args = [
            self.executable, "-p", prompt, "--no-session-persistence",
            "--safe-mode", "--disable-slash-commands", "--no-chrome",
            "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
            "--tools", tools, "--permission-mode", permission_mode,
        ]
        if model in self._MODEL_ALIASES:
            args += ["--model", model]
        stdout, _stderr = self.runner.run(
            args, cwd=workspace.root, timeout=timeout, cancel_event=cancel_event
        )
        return AdapterResult(stdout.strip())

    def shutdown(self) -> None:
        self.runner.shutdown()
