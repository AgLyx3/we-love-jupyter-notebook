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
import select
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from mcp.types import UNSUPPORTED_PROTOCOL_VERSION

REPO = Path(__file__).resolve().parents[2]
README = REPO / "README.md"

REPLY_TIMEOUT_SECONDS = 60.0


def _readme() -> str:
    return README.read_text()


def test_no_link_in_the_readme_is_repo_relative():
    """On the project page there is no repository to be relative to.

    Nine targets went nowhere: four screenshots that rendered as broken images,
    and links to the screenshot directory, CONTRIBUTING, the Code of Conduct,
    SECURITY and the licence. Written as a rule rather than a list of the nine,
    because the next relative link someone adds breaks the same way — so it
    covers the raw HTML this page also contains, and reference-style
    definitions, not only inline Markdown.
    """
    # Fenced blocks are examples, not links: the install commands contain
    # paths that would read as relative targets and are not.
    text = re.sub(r"^```.*?^```", "", _readme(), flags=re.S | re.M)
    targets = (
        re.findall(r"\]\(\s*([^)\s]+)", text)                 # [text](target)
        + re.findall(r"""(?:src|href)\s*=\s*["']([^"']+)""", text)  # raw HTML
        + re.findall(r"^\s*\[[^\]]+\]:\s*(\S+)", text, flags=re.M)  # [ref]: target
    )
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
    """Send frames down a real stdio server and read one reply per frame.

    Bounded, and with the child's stderr kept: the failure this guards against
    is an SDK release that stops answering one of these frames, which as a bare
    `readline()` would hang the suite rather than fail it. A server that dies on
    startup instead has its own reason to give, and it is on stderr.
    """
    log = tempfile.TemporaryFile()
    server = subprocess.Popen(
        [sys.executable, "-m", "backend.app.mcp.server", "--no-browser"],
        cwd=REPO, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
        stderr=log, bufsize=0,
    )

    def stderr() -> str:
        log.seek(0)
        return log.read().decode("utf-8", "replace").strip() or "(nothing on stderr)"

    try:
        # stdin stays open until every reply is read: closing it after the
        # writes races the dispatcher, and the server can exit on EOF before it
        # answers the last frame.
        for message in messages:
            server.stdin.write((json.dumps(message) + "\n").encode())
            server.stdin.flush()
        buffer = bytearray()
        deadline = time.monotonic() + REPLY_TIMEOUT_SECONDS
        while buffer.count(b"\n") < len(messages):
            remaining = deadline - time.monotonic()
            got = buffer.count(b"\n")
            if remaining <= 0 or not select.select([server.stdout], [], [], remaining)[0]:
                raise AssertionError(
                    f"the server answered {got} of {len(messages)} frames in "
                    f"{REPLY_TIMEOUT_SECONDS}s. stderr:\n{stderr()}"
                )
            chunk = server.stdout.read(4096)
            if not chunk:
                raise AssertionError(
                    f"the server exited after {got} of {len(messages)} frames "
                    f"(rc={server.poll()}). stderr:\n{stderr()}"
                )
            buffer += chunk
        return [json.loads(line) for line in buffer.splitlines()[: len(messages)]]
    finally:
        server.stdin.close()
        server.terminate()
        try:
            server.wait(timeout=30)
        except subprocess.TimeoutExpired:  # pragma: no cover - a wedged child
            server.kill()
            server.wait(timeout=30)
        log.close()


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
