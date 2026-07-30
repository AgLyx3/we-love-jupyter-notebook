"""Manual smoke test: drive one real Codex CLI turn through the app backend.

Usage:
    python3 scripts/codex_smoke.py             # editable turn
    python3 scripts/codex_smoke.py --read-only # read-only turn
    python3 scripts/codex_smoke.py --agent claude

Runs the real CLI (spends tokens); requires a logged-in codex CLI.
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi.testclient import TestClient  # noqa: E402

from backend.app.main import configured_agent_adapters, create_app  # noqa: E402

TERMINAL = {"completed", "failed", "cancelled", "validation_incomplete"}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--agent", default="codex")
    parser.add_argument("--read-only", action="store_true")
    parser.add_argument("--timeout", type=float, default=600.0)
    args = parser.parse_args()

    adapters, default = configured_agent_adapters()
    app = create_app(agent_adapters=adapters, default_agent=default)
    sample = Path(__file__).resolve().parent.parent / "examples/sample.ipynb"
    with TestClient(app) as client:
        upload = client.post(
            "/notebooks/upload", files={"file": ("sample.ipynb", sample.read_bytes())},
        )
        upload.raise_for_status()
        snapshot = upload.json()
        session, revision = snapshot["sessionId"], snapshot["revision"]
        if args.read_only:
            prompt = "Explain what this notebook does in two sentences."
        else:
            cell = next(c for c in snapshot["cells"] if c["cellType"] == "code")
            client.post("/turn-scope/editable-cells", json={
                "sessionId": session, "expectedDocumentRevision": revision,
                "cellId": cell["cellId"],
            }).raise_for_status()
            prompt = "Add a short clarifying comment at the top of the selected cell."
        started = client.post("/agent-turns", json={
            "sessionId": session, "expectedDocumentRevision": revision,
            "prompt": prompt, "agent": args.agent,
        })
        started.raise_for_status()
        turn_id = started.json()["turnId"]
        deadline = time.monotonic() + args.timeout
        while time.monotonic() < deadline:
            turn = client.get(f"/agent-turns/{turn_id}").json()
            if turn["state"] in TERMINAL:
                break
            time.sleep(2)
        print(f"agent: {turn['agent']}  state: {turn['state']}  attempts: {turn['attempts']}")
        print(f"changes: {[c['cellId'] for c in turn['changes']]}")
        print(f"error: {turn['error']}")
        print(f"final output:\n{turn['finalOutput']}")
        return 0 if turn["state"] == "completed" and not turn["error"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
