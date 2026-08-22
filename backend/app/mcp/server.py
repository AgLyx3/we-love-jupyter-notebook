"""The editor as a set of MCP tools, with a browser tab showing the same session.

The tool surface is deliberately much smaller than the HTTP API. Three groups
of endpoints are left out on purpose:

* **Agent turns.** They shell out to the `claude` CLI, so exposing them would
  put a second agent underneath the one already calling these tools — separately
  billed, with its own workspace and scope rules, doing work the caller can do
  directly with `notebook_set_cell_source`. A person can still send a turn from
  the browser tab; only the tool surface omits it.
* **File listing and search.** The tab has a file picker, and a person browsing
  their own machine is a different thing from a model enumerating it.
* **Plot tuning.** Its value is a live preview loop; called through tools it is
  a slower way to edit a literal.

Execution is the interesting one. `notebook_run_cell` and `notebook_run_all`
declare themselves as model-initiated, which routes them through the risky-cell
gate: a cell the classifier flags stops and waits for a person to approve it in
the tab. That pause is the point of running a browser at all.
"""

from __future__ import annotations

import json
import threading
import webbrowser
from pathlib import Path
from typing import Any, Literal

import httpx
from mcp.server import MCPServer
from mcp.types import ToolAnnotations

from .shaping import budget_hint, shape_notebook
from .supervisor import EditorProcess

REQUEST_TIMEOUT_SECONDS = 30.0
# How long a run tool waits before handing control back. Long enough for an
# ordinary cell to finish inside the call, and deliberately far short of how
# long a person might take to approve a paused one: holding a tool call open
# for minutes gives the caller nothing to act on and invites the client's own
# timeout to fire instead. The eval showed the model reaching for
# notebook_status of its own accord after a pause, so returning promptly with
# what it is waiting for fits how it already behaves. The run continues in the
# background either way.
EXECUTION_POLL_TIMEOUT_SECONDS = 45.0


class ToolFailure(RuntimeError):
    """A failure worth showing the caller, phrased as what to do about it."""


class EditorSession:
    """The revision each write is checked against.

    Deliberately the revision from the caller's *last read*, not one fetched
    just before writing. Re-reading first would make every write succeed by
    construction and quietly overwrite whatever a person changed in the tab
    meanwhile; the whole value of the precondition is that a write based on a
    stale view is refused. So a conflict is surfaced and the caller re-reads —
    never retried underneath them.
    """

    def __init__(self) -> None:
        self.session_id: str | None = None
        self.revision: int | None = None
        self._lock = threading.Lock()

    def observe(self, payload: dict[str, Any]) -> None:
        session_id = payload.get("sessionId")
        revision = payload.get("revision")
        if revision is None:
            revision = payload.get("documentRevision")
        if session_id is None or revision is None:
            return
        with self._lock:
            self.session_id = session_id
            self.revision = revision

    def mutation(self) -> dict[str, Any]:
        with self._lock:
            if self.session_id is None or self.revision is None:
                raise ToolFailure(
                    "No notebook has been read yet. Call notebook_open or "
                    "notebook_read first, so this edit is checked against the "
                    "version you actually looked at."
                )
            return {
                "sessionId": self.session_id,
                "expectedDocumentRevision": self.revision,
            }

    def forget(self) -> None:
        with self._lock:
            self.session_id = None
            self.revision = None


def resolve_requested_path(path: str, workspace_root: str | None) -> str:
    """Make a caller's path unambiguous before it reaches the editor.

    A model asked to work on "analysis.ipynb" passes exactly that. Left alone
    it would be resolved by whichever process happened to receive it, against
    whichever directory that process was started in — which in the observed
    case was the client's, and only matched the workspace by luck. Anchoring a
    relative path to the workspace root makes it mean one thing.
    """
    candidate = Path(path).expanduser()
    if candidate.is_absolute():
        return str(candidate)
    if workspace_root:
        return str((Path(workspace_root) / candidate).resolve())
    return str(candidate.resolve())


def _explain(status: int, body: dict[str, Any] | None, *, what: str) -> str:
    """Turn the API's error envelope into a next step.

    Published tool guidance is blunt about this: an opaque code tells a caller
    something went wrong and nothing about what to do instead.
    """
    error = (body or {}).get("error") or {}
    code = error.get("code", "")
    message = error.get("message") or f"HTTP {status}"

    if code == "notebook_not_loaded":
        return "No notebook is open. Call notebook_open with a path first."
    if code == "cell_not_found":
        return (
            f"There is no cell {message!r} in the open notebook. Call "
            "notebook_read for the current cell ids — they change when cells "
            "are added or removed."
        )
    if code == "notebook_path_invalid":
        return (
            f"{message}. The path must be a readable .ipynb file inside the "
            "workspace this editor was started for."
        )
    if code == "notebook_too_large":
        return f"{message}. This editor cannot open it."
    if code == "invalid_notebook":
        return f"{message}. It is not a notebook this editor can parse."
    if code in {"revision_conflict", "session_conflict"}:
        return (
            f"The notebook changed since you last read it ({message}). Someone "
            "may have edited it in the editor tab. Call notebook_read to see "
            "the current state, then decide whether this edit still applies — "
            "it has not been made."
        )
    if code == "external_modification_conflict":
        return (
            f"{message}. Re-open it with notebook_open to pick up what is on "
            "disk; the edit has not been made."
        )
    if code == "replacement_precondition_required":
        return (
            "A notebook is already open. Call notebook_read first so the "
            "replacement is checked against the version you looked at."
        )
    if code == "mutation_conflict":
        return (
            f"Another operation is holding the notebook ({message}). Call "
            "notebook_status to see what is running, then try again once it "
            "finishes."
        )
    if code == "kernel_restart_required":
        return "The kernel needs restarting before anything else will run."
    if status == 409:
        return f"{what} conflicts with the notebook's current state: {message}"
    if status == 422:
        return f"{what} was rejected as malformed: {message}"
    return f"{what} failed ({status}): {message}"


class NotebookTools:
    """The HTTP calls behind the tools, with the editor started on demand."""

    def __init__(self, editor: EditorProcess) -> None:
        self.editor = editor
        self.state = EditorSession()
        self._client = httpx.Client(timeout=REQUEST_TIMEOUT_SECONDS)
        self._opened_browser = False

    def close(self) -> None:
        self._client.close()
        self.editor.stop()

    def request(
        self, method: str, path: str, *, json_body: dict[str, Any] | None = None,
        what: str = "The request",
    ) -> Any:
        base = self.editor.ensure_running()
        try:
            response = self._client.request(
                method, f"{base}/api{path}", json=json_body,
            )
        except httpx.HTTPError as error:
            raise ToolFailure(f"Could not reach the editor: {error}") from error
        if response.status_code >= 400:
            try:
                body = response.json()
            except ValueError:
                body = None
            raise ToolFailure(_explain(response.status_code, body, what=what))
        if response.status_code == 204 or not response.content:
            return None
        return response.json()

    def show_in_browser(self, *, force: bool = False) -> str:
        """Open the editor tab, once per session unless asked again."""
        url = self.editor.ensure_running()
        if force or not self._opened_browser:
            try:
                webbrowser.open(url)
            except Exception:  # noqa: BLE001 - a headless host is not a failure
                pass
            self._opened_browser = True
        return url


def build_server(
    *, workspace_root: str | None = None, open_browser: bool = True,
) -> tuple[MCPServer, NotebookTools]:
    """Wire the tools onto a server. Returns both so a caller can shut down."""
    tools = NotebookTools(EditorProcess(workspace_root=workspace_root))
    server = MCPServer(
        name="notebook-editor",
        version="0.1.0",
        instructions=(
            "A local Jupyter notebook editor with a live browser tab. Tool calls "
            "and the tab are two views of one session, so a person can watch and "
            "intervene while you work.\n\n"
            "Read before you write: edits are checked against the version you "
            "last read, and a conflict means someone changed the notebook in the "
            "tab. Running a cell may pause for that person to approve it."
        ),
    )

    read_only = ToolAnnotations(read_only_hint=True)

    @server.tool(
        name="notebook_open",
        description=(
            "Open a local .ipynb notebook in the editor and show it in a browser "
            "tab. Starts the editor if it is not already running. `path` may be "
            "absolute, or relative to the workspace root this editor was started "
            "for. Returns the session and a summary of the notebook. Use this "
            "once per notebook; to re-read one already open, use notebook_read."
        ),
    )
    def notebook_open(path: str) -> dict[str, Any]:
        body: dict[str, Any] = {
            "path": resolve_requested_path(path, tools.editor.workspace_root)
        }
        if tools.state.session_id is not None and tools.state.revision is not None:
            # Replacing an open notebook needs the precondition, same as any
            # other mutation of the active session.
            body.update(tools.state.mutation())
        snapshot = tools.request(
            "POST", "/notebooks/open", json_body=body, what="Opening the notebook",
        )
        tools.state.observe(snapshot)
        if open_browser:
            tools.show_in_browser()
        shaped = shape_notebook(snapshot)
        hint = budget_hint(shaped)
        if hint:
            shaped["note"] = hint
        return shaped

    @server.tool(
        name="notebook_read",
        description=(
            "Read the open notebook: cells, their source, and a summary of their "
            "outputs. Images are described rather than returned — their bytes are "
            "useless as text and would crowd out the conversation; look at the "
            "editor tab to see one. Pass `cells` to fetch only the ids you need "
            "from a large notebook, and detail='detailed' for untruncated source."
        ),
        annotations=read_only,
    )
    def notebook_read(
        cells: list[str] | None = None,
        include_outputs: bool = True,
        detail: Literal["concise", "detailed"] = "concise",
    ) -> dict[str, Any]:
        snapshot = tools.request(
            "GET", "/notebooks/current", what="Reading the notebook",
        )
        tools.state.observe(snapshot)
        shaped = shape_notebook(
            snapshot, cells=cells, include_outputs=include_outputs, detail=detail,
        )
        hint = budget_hint(shaped)
        if hint:
            shaped["note"] = hint
        return shaped

    @server.tool(
        name="notebook_status",
        description=(
            "What the editor is doing now: the current revision, any run in "
            "progress, and whether one is paused waiting for a person to approve "
            "a risky cell. Call this when a run does not finish."
        ),
        annotations=read_only,
    )
    def notebook_status() -> dict[str, Any]:
        status = tools.request("GET", "/session/status", what="Reading session status")
        tools.state.observe(status)
        execution = status.get("activeExecution")
        summary: dict[str, Any] = {
            "sessionId": status.get("sessionId"),
            "revision": status.get("documentRevision"),
            "activeExecution": _summarize_execution(execution) if execution else None,
        }
        return summary

    @server.tool(
        name="notebook_set_cell_source",
        description=(
            "Replace the source of one cell. Checked against the version you last "
            "read: if the notebook changed in the meantime the edit is refused, "
            "not applied — re-read and decide again. This does not run anything."
        ),
    )
    def notebook_set_cell_source(cell_id: str, source: str) -> dict[str, Any]:
        body = {**tools.state.mutation(), "source": source}
        result = tools.request(
            "POST", f"/cells/{cell_id}/source", json_body=body,
            what=f"Editing cell {cell_id!r}",
        )
        tools.state.observe(result)
        return {
            "cellId": result.get("cellId"),
            "revision": result.get("revision"),
            "dirty": result.get("dirty"),
        }

    @server.tool(
        name="notebook_insert_cell",
        description=(
            "Add a new cell to the open notebook. Goes at the end unless you "
            "give `index` (0 is the top). The cell is marked as agent-authored, "
            "which the editor shows as a badge, and it is NOT run — call "
            "notebook_run_cell with the id this returns when you want it to. "
            "To change a cell that already exists, use "
            "notebook_set_cell_source instead."
        ),
    )
    def notebook_insert_cell(
        source: str,
        cell_type: Literal["code", "markdown"] = "code",
        index: int | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            **tools.state.mutation(), "source": source, "cellType": cell_type,
        }
        if index is not None:
            body["index"] = index
        snapshot = tools.request(
            "POST", "/cells", json_body=body, what="Adding a cell",
        )
        tools.state.observe(snapshot)
        added = _added_cell(snapshot, index)
        return {
            "cellId": added.get("cellId") if added else None,
            "index": added.get("index") if added else None,
            "cellCount": len(snapshot.get("cells") or []),
            "revision": snapshot.get("revision"),
            "note": "Added but not run — it has no output until you run it.",
        }

    @server.tool(
        name="notebook_delete_cell",
        description=(
            "Remove a cell from the open notebook by id. A notebook must keep at "
            "least one cell, so deleting the last one is refused. This cannot be "
            "undone through these tools."
        ),
    )
    def notebook_delete_cell(cell_id: str) -> dict[str, Any]:
        snapshot = tools.request(
            "DELETE", f"/cells/{cell_id}", json_body=tools.state.mutation(),
            what=f"Deleting cell {cell_id!r}",
        )
        tools.state.observe(snapshot)
        return {
            "deletedCellId": cell_id,
            "cellCount": len(snapshot.get("cells") or []),
            "revision": snapshot.get("revision"),
        }

    @server.tool(
        name="notebook_run_cell",
        description=(
            "Run one cell against the live kernel and wait for it to finish. A "
            "cell the risk classifier flags — running a shell command, starting a "
            "process — pauses for a person to approve it in the editor tab, so "
            "this can block for as long as they take. Returns the cell's output."
        ),
    )
    def notebook_run_cell(cell_id: str) -> dict[str, Any]:
        return _run(tools, f"/execution/cells/{cell_id}/run", what=f"Running cell {cell_id!r}")

    @server.tool(
        name="notebook_run_all",
        description=(
            "Run every code cell in order and wait. Like notebook_run_cell, any "
            "cell the classifier flags pauses for a person to approve. Prefer "
            "notebook_run_cell when you only need one."
        ),
    )
    def notebook_run_all() -> dict[str, Any]:
        return _run(tools, "/execution/run-all", what="Running the notebook")

    @server.tool(
        name="notebook_save",
        description="Write the open notebook back to its file on disk.",
    )
    def notebook_save() -> dict[str, Any]:
        result = tools.request(
            "POST", "/notebooks/save", json_body=tools.state.mutation(),
            what="Saving the notebook",
        )
        tools.state.observe(result)
        return {
            "notebookPath": result.get("notebookPath"),
            "revision": result.get("revision"),
            "dirty": result.get("dirty"),
        }

    @server.tool(
        name="notebook_show",
        description=(
            "Open or re-open the editor tab in a browser. Use it to point a person "
            "at something — a plot you cannot see, or a cell waiting on approval."
        ),
    )
    def notebook_show() -> dict[str, Any]:
        return {"url": tools.show_in_browser(force=True)}

    return server, tools


def _added_cell(snapshot: dict[str, Any], index: int | None) -> dict[str, Any] | None:
    """Which cell the insert produced, so the caller gets an id to run.

    Identified by position rather than by diffing ids: the snapshot is the
    post-insert state and the position is exactly what was asked for.
    """
    cells = snapshot.get("cells") or []
    if not cells:
        return None
    position = len(cells) - 1 if index is None else index
    if 0 <= position < len(cells):
        return cells[position]
    return None


def _summarize_execution(execution: dict[str, Any]) -> dict[str, Any]:
    """An execution operation, without the output bytes."""
    attempts = execution.get("attempts") or []
    summary: dict[str, Any] = {
        "operationId": execution.get("operationId"),
        "state": execution.get("state"),
        "kind": execution.get("kind"),
    }
    awaiting = next(
        (
            attempt for attempt in attempts
            if attempt.get("state") == "awaiting_approval" and not attempt.get("decision")
        ),
        None,
    )
    if awaiting is not None:
        risk = awaiting.get("risk") or {}
        summary["awaitingApproval"] = {
            "cellId": awaiting.get("cellId"),
            "reasons": risk.get("reasons", []),
            "note": (
                "Waiting for a person to approve this cell in the editor tab. "
                "It will not run until they do."
            ),
        }
    return summary


def _run(tools: NotebookTools, path: str, *, what: str) -> dict[str, Any]:
    """Start a run, then wait for it, reporting an approval pause as it happens."""
    from .polling import await_execution

    body = {**tools.state.mutation(), "initiator": "mcp"}
    started = tools.request("POST", path, json_body=body, what=what)
    return await_execution(tools, started, timeout=EXECUTION_POLL_TIMEOUT_SECONDS)


def main() -> None:  # pragma: no cover - process entry point
    import argparse

    parser = argparse.ArgumentParser(description="Run the notebook editor MCP server")
    parser.add_argument(
        "--workspace-root",
        help="Confine every path the editor will open, list or write to this directory",
    )
    parser.add_argument(
        "--no-browser", action="store_true",
        help="Do not open a browser tab automatically (notebook_show still does)",
    )
    args = parser.parse_args()

    server, tools = build_server(
        workspace_root=args.workspace_root, open_browser=not args.no_browser,
    )
    try:
        server.run(transport="stdio")
    finally:
        tools.close()


if __name__ == "__main__":  # pragma: no cover
    main()
