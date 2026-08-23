"""Turning a notebook snapshot into something worth putting in a context window.

`GET /notebooks/current` answers with the whole document, outputs included.
That is right for the browser, which renders images and scrolls long text, and
badly wrong as a tool result. Measured on `examples/plot-gallery.ipynb` — 18
cells, a small example — the snapshot is 92,550 bytes, about 23K tokens, and
roughly 93% of it is base64 PNG that a model cannot see. The notebook size
limit is 5 MB, so a real plotting notebook can exceed a context window in one
call.

So outputs are summarised rather than forwarded: an image becomes a line
saying it is an image and how big, long text is truncated with the count of
what was dropped, and the caller can ask for a subset of cells. The full bytes
are one click away in the browser tab, which is the right renderer for them.
"""

from __future__ import annotations

from typing import Any


# Text output longer than this is truncated. Generous enough for a stack trace
# or a dataframe repr, which are the outputs worth reading, and far short of a
# runaway loop printing for a thousand lines.
MAX_OUTPUT_CHARS = 2_000
CONCISE_SOURCE_CHARS = 400
# A whole response bigger than this is over the ~25K-token guidance for a tool
# result even before the client's own overhead.
SOFT_RESPONSE_CHAR_BUDGET = 60_000


def _text_of(value: Any) -> str:
    """Notebook text fields are a string or a list of lines."""
    if isinstance(value, list):
        return "".join(str(item) for item in value)
    return str(value) if value is not None else ""


def _decoded_size(encoded: str) -> int:
    """The image's real size, not the length of its base64 text.

    Base64 is about 4/3 of what it encodes, so reporting the string length
    overstates every image by a third — including in the example this module's
    own docstring used to give.
    """
    stripped = len(encoded.strip())
    if stripped == 0:
        return 0
    padding = encoded.strip().count("=", -2)
    return max(0, (stripped * 3) // 4 - padding)


def _truncate(text: str, limit: int) -> tuple[str, int]:
    """Return the kept prefix and how many characters were dropped."""
    if len(text) <= limit:
        return text, 0
    return text[:limit], len(text) - limit


def summarize_output(output: dict[str, Any]) -> dict[str, Any]:
    """One cell output, reduced to what a reader can act on.

    Images are described, never carried: their bytes are the bulk of a typical
    notebook and mean nothing as text. An error keeps its type, message and
    traceback, because that is usually the reason the notebook is being read at
    all.
    """
    kind = output.get("output_type")

    if kind == "stream":
        text, dropped = _truncate(_text_of(output.get("text")), MAX_OUTPUT_CHARS)
        summary: dict[str, Any] = {"type": "stream", "name": output.get("name", "stdout"), "text": text}
        if dropped:
            summary["truncatedChars"] = dropped
        return summary

    if kind == "error":
        traceback = [str(line) for line in output.get("traceback", [])]
        joined, dropped = _truncate("\n".join(traceback), MAX_OUTPUT_CHARS)
        summary = {
            "type": "error",
            "errorName": output.get("ename", ""),
            "errorValue": output.get("evalue", ""),
            "traceback": joined,
        }
        if dropped:
            summary["truncatedChars"] = dropped
        return summary

    if kind in {"display_data", "execute_result"}:
        data = output.get("data") or {}
        images = sorted(
            mime for mime in data if isinstance(mime, str) and mime.startswith("image/")
        )
        if "text/plain" in data:
            text, dropped = _truncate(_text_of(data["text/plain"]), MAX_OUTPUT_CHARS)
        else:
            text, dropped = "", 0
        summary = {"type": "result", "text": text}
        if dropped:
            summary["truncatedChars"] = dropped
        if images:
            # Named and measured, not carried. A model reading "image/png,
            # 17.7 KB" knows a plot rendered and can ask the person to look;
            # the base64 would tell it nothing and cost thousands of tokens.
            summary["images"] = [
                {"mimeType": mime, "bytes": _decoded_size(_text_of(data[mime]))}
                for mime in images
            ]
            if not text:
                summary["text"] = f"<{', '.join(images)} — view it in the editor tab>"
        return summary

    return {"type": str(kind or "unknown")}


def _select(cells: list[dict[str, Any]], wanted: list[str] | None) -> list[dict[str, Any]]:
    if wanted is None:
        return cells
    requested = list(dict.fromkeys(wanted))
    by_id = {cell.get("cellId"): cell for cell in cells}
    return [by_id[cell_id] for cell_id in requested if cell_id in by_id]


def shape_notebook(
    snapshot: dict[str, Any],
    *,
    cells: list[str] | None = None,
    include_outputs: bool = True,
    detail: str = "concise",
) -> dict[str, Any]:
    """A notebook snapshot, sized for a tool result.

    ``cells`` selects by id, in the order asked for; unknown ids are dropped
    rather than raising, so a stale id from an earlier read degrades to a
    smaller answer instead of an error. ``detail`` of ``"detailed"`` keeps full
    cell source; ``"concise"`` truncates it. Ids that were asked for and not
    found are reported, so a caller can tell an empty answer from a wrong one.
    """
    all_cells = list(snapshot.get("cells") or [])
    selected = _select(all_cells, cells)

    shaped_cells = []
    for cell in selected:
        source = _text_of(cell.get("source"))
        if detail == "concise":
            source, dropped_source = _truncate(source, CONCISE_SOURCE_CHARS)
        else:
            dropped_source = 0

        entry: dict[str, Any] = {
            "cellId": cell.get("cellId"),
            "index": cell.get("index"),
            "cellType": cell.get("cellType"),
            "source": source,
        }
        if dropped_source:
            entry["truncatedChars"] = dropped_source
        if cell.get("executionCount") is not None:
            entry["executionCount"] = cell["executionCount"]
        if include_outputs:
            outputs = [summarize_output(item) for item in (cell.get("outputs") or [])]
            if outputs:
                entry["outputs"] = outputs
        shaped_cells.append(entry)

    shaped: dict[str, Any] = {
        "sessionId": snapshot.get("sessionId"),
        "filename": snapshot.get("filename"),
        "notebookPath": snapshot.get("notebookPath"),
        "revision": snapshot.get("revision"),
        "dirty": snapshot.get("dirty"),
        "cellCount": len(all_cells),
        "cells": shaped_cells,
    }
    if cells is not None:
        missing = [
            cell_id for cell_id in dict.fromkeys(cells)
            if cell_id not in {item.get("cellId") for item in all_cells}
        ]
        if missing:
            shaped["missingCellIds"] = missing
        shaped["returnedCellCount"] = len(shaped_cells)
    return shaped


def estimated_chars(payload: dict[str, Any]) -> int:
    """Rough size of a shaped payload, for the over-budget hint below."""
    import json

    return len(json.dumps(payload, separators=(",", ":")))


def budget_hint(payload: dict[str, Any]) -> str | None:
    """Advice to attach when a shaped notebook is still large.

    Shaping removes the images, which is most of it, but a notebook with
    hundreds of cells is still big. Saying so — with the way to narrow it — is
    better than silently returning something that crowds out the conversation.
    """
    if estimated_chars(payload) <= SOFT_RESPONSE_CHAR_BUDGET:
        return None
    return (
        f"This notebook has {payload.get('cellCount')} cells and is large even "
        "summarised. Re-read with the `cells` argument to fetch only the ones "
        "you need, or with include_outputs=false."
    )
