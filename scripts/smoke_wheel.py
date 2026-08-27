#!/usr/bin/env python3
"""Prove a built wheel is installable and drivable, with no checkout in sight.

Two failures this catches, both of which a fully green test suite misses:

* **A wheel with no frontend.** `npm run build` writes the SPA into
  `backend/app/web/`, which is gitignored, and `python -m build` is perfectly
  happy to package a tree where that directory is absent: the wheel builds,
  uploads and installs, and the first tool call is the first anyone hears of
  it. Building in the wrong order fails silently, so something has to look.
* **A wheel that only works beside its source.** Run from a checkout,
  `import backend.app` finds the repo before site-packages, so a layout that is
  broken for every real user resolves fine for the person testing it.

Run it against a wheel, from anywhere:

    python scripts/smoke_wheel.py dist/notebook_editor_mcp-*-py3-none-any.whl

It builds a throwaway venv, installs the wheel with its `mcp` extra, then
re-executes itself with that venv's interpreter to play the client: list the
tools over stdio and open a notebook, the way a real client would.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import tempfile
import venv
import zipfile
from pathlib import Path

# The tool surface the README documents. A rename that misses the docs is a
# rename this notices.
EXPECTED_TOOLS = frozenset({
    "open", "read", "status", "set_cell_source", "insert_cell", "delete_cell",
    "run_cell", "run_all", "cancel_run", "save", "show",
})

# Enough notebook to open: one code cell, nbformat 4.5 (hence the cell `id`).
SAMPLE_NOTEBOOK = {
    "cells": [{
        "cell_type": "code", "id": "smoke", "metadata": {},
        "source": ["value = 21 * 2\n"], "outputs": [], "execution_count": None,
    }],
    "metadata": {
        "kernelspec": {"display_name": "Python 3", "language": "python", "name": "python3"},
        "language_info": {"name": "python"},
    },
    "nbformat": 4, "nbformat_minor": 5,
}

TIMEOUT_SECONDS = 120


def fail(message: str) -> None:
    print(f"smoke: FAIL — {message}", file=sys.stderr)
    raise SystemExit(1)


def check_wheel_contents(wheel: Path) -> None:
    """The frontend has to be in the archive before anything else is worth doing."""
    with zipfile.ZipFile(wheel) as archive:
        names = archive.namelist()
    web = [n for n in names if n.startswith("backend/app/web/")]
    if "backend/app/web/index.html" not in names:
        fail(
            f"{wheel.name} carries no built frontend ({len(web)} files under "
            "backend/app/web/). Run `npm run build` before `python -m build` — "
            "the wheel builds without it and fails at the client's first call."
        )
    if not any(n.startswith("backend/app/mcp/") for n in names):
        fail(f"{wheel.name} carries no backend/app/mcp/ package")
    print(f"smoke: wheel carries the frontend ({len(web)} files) and the mcp package")


def install(wheel: Path, into: Path) -> Path:
    """A venv with the wheel and nothing else.

    Deliberately no extra: this is the install the README tells a stranger to
    do, and naming one here would test a command nobody runs. Anything the
    server needs to start has to be a plain dependency to survive this.
    """
    venv.EnvBuilder(with_pip=True, clear=True).create(into)
    python = into / "bin" / "python"
    subprocess.run(
        [str(python), "-m", "pip", "install", "--quiet", str(wheel)],
        check=True,
    )
    return python


def check_installed_layout() -> None:
    """Runs inside the venv: the package, and its frontend, resolve from site-packages."""
    import backend.app
    from backend.app.bundled import _locate_dist

    origin = Path(backend.app.__file__).resolve()
    if "site-packages" not in origin.parts:
        fail(f"backend.app resolved to {origin}, not the installed wheel — a checkout is shadowing it")
    root, index = _locate_dist(None)
    if not index.is_file() or "site-packages" not in root.parts:
        fail(f"built frontend resolved to {root}, which is not inside the install")
    print(f"smoke: frontend resolves to {root}")


async def drive_the_server(workspace: Path, log_path: Path) -> None:
    """List the tools and open a notebook, over stdio, as a client would."""
    from mcp import ClientSession, StdioServerParameters
    from mcp.client.stdio import stdio_client

    command = Path(sys.executable).parent / "notebook-editor-mcp"
    if not command.exists():
        fail(f"console script missing at {command} — check [project.scripts]")

    params = StdioServerParameters(
        command=str(command),
        args=["--workspace-root", str(workspace), "--no-browser"],
        env={**os.environ, "NOTEBOOK_EDITOR_LOG": str(log_path)},
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            listed = await session.list_tools()
            found = {tool.name for tool in listed.tools}
            if found != EXPECTED_TOOLS:
                fail(
                    "tool surface changed — "
                    f"missing {sorted(EXPECTED_TOOLS - found)}, "
                    f"unexpected {sorted(found - EXPECTED_TOOLS)}"
                )
            print(f"smoke: {len(found)} tools listed")

            notebook = workspace / "smoke.ipynb"
            result = await session.call_tool("open", {"path": str(notebook)})
            if result.is_error:
                body = result.content[0].text if result.content else "(no content)"
                fail(f"`open` returned an error: {body}")
            payload = json.loads(result.content[0].text)
            if not payload.get("cellCount"):
                fail(f"`open` returned no cells: {payload}")
            if not payload.get("editorUrl"):
                fail(f"`open` returned no editorUrl: {payload}")
            print(f"smoke: opened {payload['cellCount']}-cell notebook at {payload['editorUrl']}")


def client_phase() -> None:
    with tempfile.TemporaryDirectory(prefix="smoke-workspace-") as directory:
        workspace = Path(directory)
        (workspace / "smoke.ipynb").write_text(json.dumps(SAMPLE_NOTEBOOK), encoding="utf-8")
        log_path = workspace / "editor.log"
        check_installed_layout()
        try:
            asyncio.run(asyncio.wait_for(drive_the_server(workspace, log_path), TIMEOUT_SECONDS))
        except BaseException:
            # The server keeps stdout for the protocol, so its own log is the
            # only account of why a start or a call went wrong.
            if log_path.exists():
                print("smoke: editor log tail:", file=sys.stderr)
                print(log_path.read_text(encoding="utf-8")[-2000:], file=sys.stderr)
            raise


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("wheel", nargs="?", type=Path, help="the .whl to test")
    parser.add_argument(
        "--client-phase", action="store_true",
        help="internal: run the client checks inside the throwaway venv",
    )
    args = parser.parse_args()

    if args.client_phase:
        client_phase()
        print("smoke: OK")
        return

    if args.wheel is None:
        parser.error("a wheel path is required")
    wheel = args.wheel.resolve()
    if not wheel.is_file():
        fail(f"no such wheel: {wheel}")

    check_wheel_contents(wheel)
    with tempfile.TemporaryDirectory(prefix="smoke-venv-") as directory:
        python = install(wheel, Path(directory) / "venv")
        # Re-exec under the venv so the client library and the server come from
        # the installed wheel, not from whatever ran this script.
        subprocess.run([str(python), str(Path(__file__).resolve()), "--client-phase"], check=True)


if __name__ == "__main__":
    main()
