"""The README as PyPI serves it, and the one connect failure it has to explain.

`readme` in `pyproject.toml` puts this file in the wheel, so the project page
is the README with no repository underneath it. Two things went wrong there for
a reader who had only that page (#56): every repo-relative link and image
resolved to nothing, and `claude mcp list` told them a working server was
broken.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from pathlib import Path

from mcp_types import UNSUPPORTED_PROTOCOL_VERSION

REPO = Path(__file__).resolve().parents[2]
README = REPO / "README.md"


def _readme() -> str:
    return README.read_text()


def test_no_link_in_the_readme_is_repo_relative():
    """On the project page there is no repository to be relative to.

    Four screenshots rendered as broken images and four links — the screenshot
    directory, CONTRIBUTING, the Code of Conduct, SECURITY — and the licence
    went nowhere. Written as a rule rather than a list of the eight, because the
    next relative link someone adds breaks the same way.
    """
    text = _readme()
    targets = re.findall(r"\]\(([^)]+)\)", text) + re.findall(r'src="([^"]+)"', text)
    relative = [
        target for target in targets
        if not target.startswith(("http://", "https://", "mailto:", "#", "data:"))
    ]
    assert not relative, (
        "these resolve on GitHub and nowhere on the PyPI page, which renders "
        f"the README packaged in the wheel: {relative}"
    )


def test_the_readme_explains_the_connect_failure_by_the_code_the_server_sends():
    """The first thing a newcomer runs after `claude mcp add` can report

        -32022: connection is serving the 2026-07-28 protocol; the initialize
        handshake is not accepted

    for a server that then drives fine. The number is not the client's
    invention — it is `UNSUPPORTED_PROTOCOL_VERSION`, raised inside this
    server's own process — so the page quotes it, and the quote is tied to the
    constant rather than typed once and left.
    """
    assert UNSUPPORTED_PROTOCOL_VERSION == -32022
    text = _readme()
    assert str(UNSUPPORTED_PROTOCOL_VERSION) in text, (
        "the README does not name the error code a reader will paste into a "
        "search box after their first `claude mcp list`"
    )
    assert "2026-07-28" in text


PROBE = {
    "jsonrpc": "2.0", "id": 0, "method": "server/discover",
    "params": {"_meta": {"io.modelcontextprotocol/protocolVersion": "2026-07-28"}},
}
HANDSHAKE = {
    "jsonrpc": "2.0", "id": 1, "method": "initialize",
    "params": {
        "protocolVersion": "2025-11-25", "capabilities": {},
        "clientInfo": {"name": "probe-then-handshake", "version": "0"},
    },
}


def _replies_to(*messages: dict) -> list[dict]:
    """Send frames down a real stdio server and read one reply per frame."""
    server = subprocess.Popen(
        [sys.executable, "-m", "backend.app.mcp.server", "--no-browser"],
        cwd=REPO, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL, bufsize=0,
    )
    try:
        # stdin stays open until every reply is read: closing it after the
        # writes races the dispatcher, and the server can exit on EOF before it
        # answers the last frame.
        for message in messages:
            server.stdin.write((json.dumps(message) + "\n").encode())
            server.stdin.flush()
        return [json.loads(server.stdout.readline()) for _ in messages]
    finally:
        server.stdin.close()
        server.terminate()
        server.wait(timeout=30)


def test_a_handshake_behind_an_unread_probe_is_refused():
    """Why that note has to be on the page: the refusal is real, not cosmetic.

    A client whose protocol-version probe times out falls back to `initialize`
    on the same pipe. The probe is still queued there, the SDK reads it first
    and locks the connection to the modern protocol, and the handshake behind
    it is refused. Pinned here so that the day an `mcp` release stops doing
    this, this test fails and the README note comes out with it.
    """
    _, refusal = _replies_to(PROBE, HANDSHAKE)
    assert refusal["id"] == 1
    assert refusal["error"]["code"] == UNSUPPORTED_PROTOCOL_VERSION, (
        f"expected the handshake to be refused, got {refusal}"
    )


def test_the_same_handshake_arriving_first_is_accepted():
    """The other half of the contradiction the page has to explain.

    Nothing is wrong with the handshake or with the server: it is the frame
    ahead of it that decides. Without this, the test above would still pass on a
    server that had simply stopped speaking the handshake protocol at all.
    """
    reply, = _replies_to(HANDSHAKE)
    assert "error" not in reply, reply
    assert reply["result"]["serverInfo"]["name"] == "agent-notebook"
