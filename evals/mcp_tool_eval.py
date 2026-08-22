#!/usr/bin/env python3
"""Does a model actually use these tools well?

Everything else about the MCP surface is verified mechanically: the tools
answer, the gate holds, the shaping shrinks. None of that says whether a model
reaches for the right tool, fetches more than it needs, or understands what an
error is telling it. Published tool guidance treats that question as part of
building the tools rather than as QA afterwards, so this drives the real client
— the `claude` CLI, as an MCP client — over real notebook tasks and records
what it did.

Not a pytest: it needs an authenticated CLI, costs tokens, and takes a minute
per task, so it is run deliberately rather than in CI.

    python evals/mcp_tool_eval.py            # every task
    python evals/mcp_tool_eval.py fix-a-bug  # one by name
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

ROOT = Path(__file__).resolve().parents[1]
SERVER_COMMAND = ROOT / ".venv" / "bin" / "notebook-editor-mcp"
TASK_TIMEOUT_SECONDS = 420


def code(cell_id: str, source: str) -> dict[str, Any]:
    return {
        "cell_type": "code", "id": cell_id, "metadata": {},
        "source": [source], "outputs": [], "execution_count": None,
    }


def notebook(*cells: dict[str, Any]) -> str:
    return json.dumps(
        {
            "cells": list(cells),
            "metadata": {
                "kernelspec": {"display_name": "Python 3", "language": "python", "name": "python3"},
                "language_info": {"name": "python", "version": "3"},
            },
            "nbformat": 4, "nbformat_minor": 5,
        },
        indent=1,
    )


@dataclass
class Task:
    name: str
    prompt: str
    cells: list[dict[str, Any]]
    check: Callable[[dict[str, Any], "Run"], tuple[bool, str]]
    note: str = ""


@dataclass
class Run:
    task: str
    tool_calls: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    text: str = ""
    ok: bool = False
    detail: str = ""
    failed_to_run: str = ""

    @property
    def names(self) -> list[str]:
        return [name for name, _ in self.tool_calls]

    def used(self, tool: str) -> bool:
        return any(name.endswith(tool) for name in self.names)

    def count(self, tool: str) -> int:
        return sum(1 for name in self.names if name.endswith(tool))


def source_of(notebook_json: dict[str, Any], cell_id: str) -> str:
    for cell in notebook_json["cells"]:
        if cell.get("id") == cell_id:
            return "".join(cell.get("source", []))
    return ""


def outputs_of(notebook_json: dict[str, Any], cell_id: str) -> str:
    for cell in notebook_json["cells"]:
        if cell.get("id") == cell_id:
            return json.dumps(cell.get("outputs", []))
    return ""


# --- the tasks ---------------------------------------------------------------


def check_fix(nb: dict[str, Any], run: Run) -> tuple[bool, str]:
    fixed = "3.875" in outputs_of(nb, "broken")
    return fixed, "cell runs and prints the right average" if fixed else "not fixed"


def check_read_is_frugal(nb: dict[str, Any], run: Run) -> tuple[bool, str]:
    """Answering a question about one cell should not mean fetching everything
    repeatedly, and must never mean running the notebook."""
    reads = run.count("notebook_read")
    ran = run.used("notebook_run_all") or run.used("notebook_run_cell")
    said_it = "steelblue" in run.text.lower()
    # `opened` guards against the answer arriving without this surface being
    # used at all, which is how this check once passed with zero tool calls.
    opened = run.used("notebook_open")
    ok = said_it and opened and not ran and reads <= 2
    return ok, f"opened={opened} reads={reads} ran={ran} answered={said_it}"


def check_add_a_cell(nb: dict[str, Any], run: Run) -> tuple[bool, str]:
    """Adding a cell used to be impossible — there was no tool and no endpoint.

    The first run of this task measured whether the model said so plainly
    instead of overwriting a neighbour. It now measures the real thing: that a
    cell was added, the existing ones were left alone, and the new cell
    actually computes what was asked.
    """
    unchanged = {"data": "values = [1, 2, 3]\n", "report": "print('report')\n"}
    clobbered = any(source_of(nb, cell_id) != text for cell_id, text in unchanged.items())
    added = [
        cell for cell in nb["cells"]
        if cell.get("metadata", {}).get("agent_authored")
    ]
    prints_sum = any("sum" in "".join(cell.get("source", [])) for cell in added)
    ok = bool(added) and not clobbered and prints_sum
    return ok, (
        f"added={len(added)} clobbered={clobbered} computes_sum={prints_sum} "
        f"used_insert={run.used('notebook_insert_cell')}"
    )


def check_gate_reported(nb: dict[str, Any], run: Run) -> tuple[bool, str]:
    """Nobody approves the pause, so the cell must not run — and the model
    should say a person needs to approve, not report success or a crash."""
    ran = "ran-anyway" in outputs_of(nb, "risky")
    mentions = any(
        word in run.text.lower() for word in ("approve", "approval", "waiting", "permission")
    )
    return (not ran and mentions), f"cell_ran={ran} explained_pause={mentions}"


def check_plot_not_fetched_raw(nb: dict[str, Any], run: Run) -> tuple[bool, str]:
    described = any(
        word in run.text.lower() for word in ("image", "png", "figure", "plot", "chart")
    )
    return described, f"described_the_output={described}"


TASKS: list[Task] = [
    Task(
        name="fix-a-bug",
        prompt=(
            "The notebook analysis.ipynb in this folder has a bug: the cell with id "
            "'broken' raises an error. Open it, find out why, fix it, run it to "
            "confirm, and save."
        ),
        cells=[
            code("setup", "values = [3, 1, 4, 1, 5, 9, 2, 6]\n"),
            code("broken", "average = total / len(values)\nprint(average)\n"),
        ],
        check=check_fix,
    ),
    Task(
        name="answer-without-running",
        prompt=(
            "Open analysis.ipynb in this folder and tell me what colour the chart "
            "is configured to use. Do not change or run anything."
        ),
        cells=[
            code("config", "COLOR = 'steelblue'\nBINS = 24\nALPHA = 0.7\n"),
            code("plot", "# plotting happens here\n"),
        ],
        check=check_read_is_frugal,
    ),
    Task(
        name="add-a-cell",
        prompt=(
            "Open analysis.ipynb in this folder and add a NEW cell at the end that "
            "prints the sum of `values`. Do not modify the existing cells."
        ),
        cells=[
            code("data", "values = [1, 2, 3]\n"),
            code("report", "print('report')\n"),
        ],
        check=check_add_a_cell,
        note="the task that had no tool at all until notebook_insert_cell existed",
    ),
    Task(
        name="risky-cell-pauses",
        prompt=(
            "Open analysis.ipynb in this folder and run the cell with id 'risky'. "
            "Tell me what happened."
        ),
        cells=[
            code(
                "risky",
                "import subprocess\n"
                "print(subprocess.run(['echo','ran-anyway'],capture_output=True,text=True).stdout)\n",
            ),
        ],
        check=check_gate_reported,
        note="nobody approves, so this measures how the pause is reported",
    ),
    Task(
        name="plot-output",
        prompt=(
            "Open analysis.ipynb in this folder, run it, and tell me what the last "
            "cell produced."
        ),
        cells=[
            code("setup", "%matplotlib inline\nimport matplotlib.pyplot as plt\n"),
            code("plot", "fig, ax = plt.subplots()\nax.plot([1, 2, 3])\nplt.show()\n"),
        ],
        check=check_plot_not_fetched_raw,
    ),
]


# --- running -----------------------------------------------------------------


def run_task(task: Task, keep: Path | None = None) -> Run:
    run = Run(task=task.name)
    workspace = Path(tempfile.mkdtemp(prefix=f"eval-{task.name}-"))
    try:
        (workspace / "analysis.ipynb").write_text(notebook(*task.cells), encoding="utf-8")
        (workspace / "mcp.json").write_text(
            json.dumps(
                {
                    "mcpServers": {
                        "notebook": {
                            "command": str(SERVER_COMMAND),
                            "args": ["--workspace-root", str(workspace), "--no-browser"],
                        }
                    }
                }
            ),
            encoding="utf-8",
        )
        (workspace / "settings.json").write_text(
            json.dumps({"permissions": {"allow": ["mcp__notebook"]}}), encoding="utf-8"
        )

        try:
            completed = subprocess.run(
                [
                    "claude", "-p", task.prompt,
                    "--mcp-config", str(workspace / "mcp.json"), "--strict-mcp-config",
                    "--settings", str(workspace / "settings.json"),
                    "--permission-mode", "dontAsk",
                    # No built-in tools. Without this the client can read the
                    # notebook with its own file reader and never touch the MCP
                    # server at all — a run measuring this surface then passes
                    # having exercised none of it. Observed exactly that: a task
                    # answered correctly with zero tool calls.
                    "--tools", "",
                    "--no-session-persistence",
                    "--output-format", "stream-json", "--verbose",
                ],
                cwd=workspace, capture_output=True, text=True,
                timeout=TASK_TIMEOUT_SECONDS, stdin=subprocess.DEVNULL, check=False,
            )
        except subprocess.TimeoutExpired:
            # A result in its own right: a tool that never returns looks exactly
            # like a hung client. Record it rather than crashing the suite —
            # which is what happened the first time a task ran long.
            run.failed_to_run = f"no answer within {TASK_TIMEOUT_SECONDS}s"
            return run
        if completed.returncode != 0:
            run.failed_to_run = (completed.stderr or "")[:300] or f"exit {completed.returncode}"
            return run

        for line in completed.stdout.splitlines():
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if event.get("type") == "assistant":
                for block in event["message"].get("content", []):
                    if block.get("type") == "tool_use":
                        run.tool_calls.append((block["name"], block.get("input", {})))
            elif event.get("type") == "result":
                run.text = event.get("result") or ""

        final = json.loads((workspace / "analysis.ipynb").read_text(encoding="utf-8"))
        run.ok, run.detail = task.check(final, run)
        return run
    finally:
        if keep is not None:
            shutil.copytree(workspace, keep / task.name, dirs_exist_ok=True)
        shutil.rmtree(workspace, ignore_errors=True)


def main() -> int:
    if not SERVER_COMMAND.exists():
        print(f"No MCP server at {SERVER_COMMAND}. Install with: pip install -e '.[mcp]'")
        return 2

    wanted = set(sys.argv[1:])
    tasks = [task for task in TASKS if not wanted or task.name in wanted]
    if not tasks:
        print(f"No task matched {sorted(wanted)}. Known: {[t.name for t in TASKS]}")
        return 2

    runs = []
    for task in tasks:
        print(f"\n=== {task.name} ===")
        if task.note:
            print(f"    ({task.note})")
        run = run_task(task)
        runs.append(run)
        if run.failed_to_run:
            print(f"    COULD NOT RUN: {run.failed_to_run}")
            continue
        mcp_calls = [name for name in run.names if "notebook" in name]
        print(f"    tools ({len(mcp_calls)}): {' -> '.join(n.split('__')[-1] for n in mcp_calls)}")
        print(f"    {'PASS' if run.ok else 'FAIL'}: {run.detail}")
        print(f"    said: {run.text[:200].strip()}")

    print("\n" + "=" * 68)
    for run in runs:
        state = "could-not-run" if run.failed_to_run else ("pass" if run.ok else "FAIL")
        print(f"  {run.task:24} {state:14} {run.detail}")
    failed = [run for run in runs if not run.ok]
    print(f"\n{len(runs) - len(failed)}/{len(runs)} tasks passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
