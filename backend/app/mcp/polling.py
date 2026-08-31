"""Waiting for a run, including one paused for a person to approve.

`POST /execution/...` answers 202 and the work happens on a background thread,
so a tool that returns immediately would tell the caller nothing about whether
the cell ran. Polling to a terminal state is what makes `run_cell`
behave like a function call.

The pause is the part worth care. A flagged cell sits in `awaiting_approval`
until a person decides in the browser tab, which may be a while, and there is
no useful action for the caller in the meantime — so the wait continues, and
if it does time out the answer says what it is waiting for rather than just
that time ran out.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, Any

from .shaping import summarize_output

if TYPE_CHECKING:  # pragma: no cover
    from .server import NotebookTools

POLL_INTERVAL_SECONDS = 0.25
TERMINAL_STATES = {
    "completed", "failed", "cancelled", "validation_incomplete", "timed_out",
}


def await_execution(
    tools: NotebookTools,
    started: dict[str, Any],
    *,
    timeout: float,
    interval: float = POLL_INTERVAL_SECONDS,
    now: Any = time.monotonic,
    sleep: Any = time.sleep,
    whole_notebook: bool = False,
) -> dict[str, Any]:
    """Poll one execution to a terminal state and report what happened.

    ``whole_notebook`` says a run-all was in flight, which the operation record
    itself cannot tell us — its ``kind`` distinguishes who started the run, not
    how much of the notebook it covered. It matters only when the run stops
    early: for one cell there are no later cells to report on.
    """
    operation_id = started.get("operationId")
    if not operation_id:
        return {"state": started.get("state", "unknown"), "cells": []}

    deadline = now() + timeout
    operation = started
    saw_approval_pause = False

    while operation.get("state") not in TERMINAL_STATES:
        if operation.get("state") == "awaiting_approval":
            saw_approval_pause = True
        if now() >= deadline:
            return _timed_out(operation, timeout)
        sleep(interval)
        operation = tools.request(
            "GET", f"/execution/{operation_id}", what="Checking the run",
        )

    return _finished(
        tools, operation,
        saw_approval_pause=saw_approval_pause,
        whole_notebook=whole_notebook,
    )


def _pending_approval(operation: dict[str, Any]) -> dict[str, Any] | None:
    for attempt in operation.get("attempts") or []:
        if attempt.get("state") == "awaiting_approval" and not attempt.get("decision"):
            return attempt
    return None


def _timed_out(operation: dict[str, Any], timeout: float) -> dict[str, Any]:
    waiting = _pending_approval(operation)
    if waiting is not None:
        risk = waiting.get("risk") or {}
        return {
            "state": "awaiting_approval",
            "operationId": operation.get("operationId"),
            "cellId": waiting.get("cellId"),
            "reasons": risk.get("reasons", []),
            "note": (
                f"Still waiting after {timeout:.0f}s for a person to approve this "
                "cell in the editor tab. Nothing has run. Ask them to look, or "
                "call status later."
            ),
        }
    return {
        "state": operation.get("state"),
        "operationId": operation.get("operationId"),
        "note": f"The run had not finished after {timeout:.0f}s. Call status.",
    }


def _stopped_early(operation: dict[str, Any], *, whole_notebook: bool) -> str:
    """The sentence that says the rest of the notebook did not run.

    A run that stops partway returns only the cells it attempted, so the cells
    after the failure are absent rather than marked — indistinguishable, to a
    reader, from a notebook that is simply shorter. The skipped-approval path
    has always said so; failure and cancellation said nothing.
    """
    if not whole_notebook:
        return ""
    return (
        " The run stopped there, so any later cells did not run — only the "
        "cells listed here were attempted."
    )


def _failing_cell(operation: dict[str, Any]) -> str | None:
    for attempt in reversed(operation.get("attempts") or []):
        if attempt.get("state") == "failed":
            return attempt.get("cellId")
    return None


def _missing_module_note(tools: NotebookTools, operation: dict[str, Any]) -> str:
    """What to do about a `ModuleNotFoundError`, for a caller who cannot look.

    A person who hits this has the editor tab: the kernel chip names the
    interpreter and the failed cell carries the same remediation this builds.
    A tool caller has the traceback and nothing else, so it is strictly worse
    off than the reader whose case was already judged worth fixing — and its
    wrong guesses are worse too. Left to itself it writes `!pip install pandas`
    into the notebook, which installs against whatever `pip` is first on PATH
    rather than against the kernel's interpreter, and reports success.

    The interpreter costs one extra loopback request, made only when a run has
    already failed on a missing module — never on the hot path.
    """
    from ..kernel_execution.missing_module import missing_module_in

    missing = None
    for attempt in reversed(operation.get("attempts") or []):
        if attempt.get("state") == "failed":
            missing = missing_module_in(attempt.get("outputs"))
            break
    if missing is None:
        return ""
    try:
        status = tools.request(
            "GET", "/kernel/status", what="Reading kernel status",
        )
    except Exception:  # noqa: BLE001 - a diagnostic must not fail the run report
        return ""
    interpreter = status.get("interpreter")
    if not interpreter:
        return ""
    chosen = status.get("interpreterSource") == "kernel-python"
    return (
        f" Cells run in {interpreter}, which does not have {missing.module}"
        f" — installing it anywhere else will not help:"
        f" `{missing.install_command(interpreter)}`."
        " Run that in a terminal rather than as `!pip install` in a cell, which"
        " installs against whatever pip is first on PATH."
        + (
            " That interpreter was chosen with --kernel-python."
            if chosen
            else " Nobody chose that interpreter — it is what the editor's own"
            " environment resolves to, so it may simply be the wrong"
            " environment, in which case the editor needs restarting with"
            " --kernel-python."
        )
    )


# The operation-level message when a cell raised. It repeats what naming the
# cell already says, so it is dropped rather than printed twice; anything more
# specific — a dead kernel, an output limit — is kept.
_GENERIC_FAILURE = "cell execution failed"


def _failure_note(operation: dict[str, Any]) -> str:
    """One sentence saying what failed, pointing at where the detail is."""
    reason = (operation.get("error") or {}).get("message") or ""
    cell_id = _failing_cell(operation)
    if cell_id is None:
        return reason.rstrip(".") + "." if reason else "The run failed; see the cell outputs."
    if reason and reason.strip().rstrip(".").lower() != _GENERIC_FAILURE:
        return f"Cell {cell_id!r} failed: {reason.rstrip('.')}."
    return f"Cell {cell_id!r} failed — its outputs carry the traceback."


def _finished(
    tools: NotebookTools,
    operation: dict[str, Any],
    *,
    saw_approval_pause: bool,
    whole_notebook: bool = False,
) -> dict[str, Any]:
    cells = []
    for attempt in operation.get("attempts") or []:
        entry: dict[str, Any] = {
            "cellId": attempt.get("cellId"),
            "state": attempt.get("state"),
        }
        outputs = [summarize_output(item) for item in (attempt.get("outputs") or [])]
        if outputs:
            entry["outputs"] = outputs
        if attempt.get("decision"):
            entry["decision"] = attempt["decision"]
        risk = attempt.get("risk") or {}
        if risk.get("level") == "confirm":
            entry["risk"] = risk.get("reasons", [])
        cells.append(entry)

    result: dict[str, Any] = {
        "state": operation.get("state"),
        "operationId": operation.get("operationId"),
        "revision": operation.get("currentDocumentRevision"),
        "cells": cells,
    }
    if operation.get("currentDocumentRevision") is not None:
        tools.state.observe(
            {
                "sessionId": operation.get("sessionId"),
                "revision": operation.get("currentDocumentRevision"),
            }
        )
    if saw_approval_pause:
        result["note"] = "A cell was paused for approval and a person decided it."
    if operation.get("state") == "validation_incomplete":
        result["note"] = (
            "A person skipped a cell that needed approval, so the run stopped "
            "there. Later cells did not run."
        )
    if operation.get("state") == "cancelled":
        result["note"] = (
            "The run was cancelled." + _stopped_early(operation, whole_notebook=whole_notebook)
        )
    if operation.get("state") == "failed":
        result["note"] = (
            _failure_note(operation)
            + _missing_module_note(tools, operation)
            + _stopped_early(operation, whole_notebook=whole_notebook)
        )
    return result
