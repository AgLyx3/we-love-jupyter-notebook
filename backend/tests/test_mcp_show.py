"""Pointing a person at the browser tab.

`notebook_show` had no behavioural test at all — it appeared once in the list
of expected tool names and nowhere else. It is the one tool whose entire job is
a side effect on the person's machine, which is exactly the kind of thing that
breaks quietly: a headless host, a second call that does nothing, a URL for an
editor that was never started.

These stub the editor process rather than booting one. What is being tested is
the browser-opening policy, not uvicorn.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from backend.app.mcp.server import build_server


class StubEditor:
    """An editor that reports a URL without costing a kernel."""

    def __init__(self, *, port: int = 51234) -> None:
        self.workspace_root = None
        self.starts = 0
        self._port = port

    def ensure_running(self) -> str:
        self.starts += 1
        return self.base_url

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self._port}"

    def stop(self) -> None:
        pass


@pytest.fixture
def shown(monkeypatch):
    """The server, its tools, a stub editor, and a record of every tab opened."""
    opened: list[str] = []
    monkeypatch.setattr(
        "backend.app.mcp.server.webbrowser.open", lambda url: opened.append(url)
    )
    server, tools = build_server(open_browser=False)
    tools.editor = StubEditor()
    try:
        yield server, tools, opened
    finally:
        tools.close()


def call(server, name, **arguments):
    result = asyncio.run(server.call_tool(name, arguments))
    if getattr(result, "structuredContent", None):
        return result.structuredContent
    return json.loads(result.content[0].text)


def test_show_hands_back_the_url_of_the_running_editor(shown):
    server, tools, opened = shown
    assert call(server, "notebook_show")["url"] == tools.editor.base_url
    assert opened == [tools.editor.base_url]


def test_show_starts_the_editor_when_nothing_else_has(shown):
    """A caller may reach for the tab before opening a notebook — to check the
    editor is alive, or because it was told to show its work first."""
    server, tools, _ = shown
    call(server, "notebook_show")
    assert tools.editor.starts == 1


def test_show_opens_a_tab_every_time_it_is_asked(shown):
    """The point of the tool. A person who closed the tab, or who is being
    pointed at a second thing, asked for a tab and must get one."""
    server, _, opened = shown
    for _ in range(3):
        call(server, "notebook_show")
    assert len(opened) == 3


def test_the_automatic_open_happens_only_once(shown):
    """The contrast that gives the forced open its meaning: opening a notebook
    raises the tab the first time and then leaves the person alone, rather than
    stealing focus on every tool call."""
    _, tools, opened = shown
    tools.show_in_browser()
    tools.show_in_browser()
    assert len(opened) == 1


def test_a_host_with_no_browser_still_answers(monkeypatch):
    """The first-run path on a server, a container, or over SSH.

    `webbrowser.open` raises when there is nothing to open. Failing the tool
    there would break every notebook_open too, since that opens the tab as a
    side effect — the notebook would be open and the call would still report an
    error.
    """
    def refuse(_url):
        raise RuntimeError("no display")

    monkeypatch.setattr("backend.app.mcp.server.webbrowser.open", refuse)
    server, tools = build_server(open_browser=False)
    tools.editor = StubEditor()
    try:
        assert call(server, "notebook_show")["url"] == tools.editor.base_url
    finally:
        tools.close()


def test_a_headless_host_is_retried_rather_than_written_off(monkeypatch):
    """A failed open still marks the tab as raised, so the automatic open does
    not nag. An explicit notebook_show is a fresh request and must try again —
    the display may have arrived since, and either way the caller is entitled
    to know it was attempted."""
    attempts: list[str] = []

    def refuse(url):
        attempts.append(url)
        raise RuntimeError("no display")

    monkeypatch.setattr("backend.app.mcp.server.webbrowser.open", refuse)
    server, tools = build_server(open_browser=False)
    tools.editor = StubEditor()
    try:
        call(server, "notebook_show")
        call(server, "notebook_show")
    finally:
        tools.close()
    assert len(attempts) == 2
