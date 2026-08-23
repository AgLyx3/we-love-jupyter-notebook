"""Sizing a notebook snapshot for a tool result rather than a browser."""

from __future__ import annotations

import json

from backend.app.mcp.shaping import (
    CONCISE_SOURCE_CHARS,
    MAX_OUTPUT_CHARS,
    budget_hint,
    shape_notebook,
    summarize_output,
)


def cell(cell_id, *, source="value = 1\n", cell_type="code", outputs=None, index=0):
    return {
        "cellId": cell_id,
        "index": index,
        "cellType": cell_type,
        "source": source,
        "outputs": outputs or [],
        "executionCount": None,
        "metadata": {},
    }


def snapshot(*cells):
    return {
        "sessionId": "session-1",
        "filename": "notebook.ipynb",
        "notebookPath": "/w/notebook.ipynb",
        "revision": 3,
        "dirty": False,
        "cells": list(cells),
    }


# --- outputs -----------------------------------------------------------------


def test_an_image_is_described_never_carried():
    """The measured reason this module exists.

    A base64 PNG is the bulk of a plotting notebook and conveys nothing as
    text. Its size is worth reporting; its bytes are not.
    """
    png = "iVBORw0KGgo" + "A" * 20_000
    summary = summarize_output(
        {
            "output_type": "display_data",
            "data": {"image/png": png, "text/plain": "<Figure size 600x300>"},
            "metadata": {},
        }
    )
    assert summary["images"][0]["mimeType"] == "image/png"
    # The decoded size, not the length of the base64 text — see
    # test_an_image_is_measured_decoded_not_as_base64_text.
    assert 0 < summary["images"][0]["bytes"] < len(png)
    assert summary["text"] == "<Figure size 600x300>"
    assert png[:40] not in json.dumps(summary)


def test_an_image_with_no_text_repr_still_says_what_it_is():
    summary = summarize_output(
        {"output_type": "display_data", "data": {"image/png": "AAAA"}, "metadata": {}}
    )
    assert "image/png" in summary["text"]
    assert "editor tab" in summary["text"]


def test_stream_text_is_truncated_and_the_loss_reported():
    """Silently dropping output would make a truncated log look complete."""
    summary = summarize_output(
        {"output_type": "stream", "name": "stdout", "text": "x" * (MAX_OUTPUT_CHARS + 500)}
    )
    assert len(summary["text"]) == MAX_OUTPUT_CHARS
    assert summary["truncatedChars"] == 500


def test_short_output_is_not_marked_truncated():
    summary = summarize_output({"output_type": "stream", "name": "stdout", "text": "ok\n"})
    assert summary["text"] == "ok\n"
    assert "truncatedChars" not in summary


def test_source_and_text_may_arrive_as_a_list_of_lines():
    """nbformat allows either; the browser normalises it and so must this."""
    summary = summarize_output(
        {"output_type": "stream", "name": "stdout", "text": ["one\n", "two\n"]}
    )
    assert summary["text"] == "one\ntwo\n"


def test_an_error_keeps_what_makes_it_diagnosable():
    """Usually the reason the notebook is being read at all."""
    summary = summarize_output(
        {
            "output_type": "error",
            "ename": "NameError",
            "evalue": "name 'total' is not defined",
            "traceback": ["Traceback:", "  line 1", "NameError: ..."],
        }
    )
    assert summary["errorName"] == "NameError"
    assert summary["errorValue"] == "name 'total' is not defined"
    assert "line 1" in summary["traceback"]


def test_an_unknown_output_type_does_not_raise():
    assert summarize_output({"output_type": "something_new"}) == {"type": "something_new"}


# --- notebooks ---------------------------------------------------------------


def test_the_shape_keeps_what_identifies_the_document():
    shaped = shape_notebook(snapshot(cell("a"), cell("b", index=1)))
    assert shaped["sessionId"] == "session-1"
    assert shaped["revision"] == 3
    assert shaped["cellCount"] == 2
    assert [item["cellId"] for item in shaped["cells"]] == ["a", "b"]


def test_concise_truncates_source_and_detailed_does_not():
    long_source = "y = 2\n" * 500
    concise = shape_notebook(snapshot(cell("a", source=long_source)))
    assert len(concise["cells"][0]["source"]) == CONCISE_SOURCE_CHARS
    assert concise["cells"][0]["truncatedChars"] > 0

    detailed = shape_notebook(snapshot(cell("a", source=long_source)), detail="detailed")
    assert detailed["cells"][0]["source"] == long_source
    assert "truncatedChars" not in detailed["cells"][0]


def test_cells_can_be_selected_by_id_in_the_order_asked_for():
    shaped = shape_notebook(
        snapshot(cell("a"), cell("b", index=1), cell("c", index=2)), cells=["c", "a"]
    )
    assert [item["cellId"] for item in shaped["cells"]] == ["c", "a"]
    assert shaped["cellCount"] == 3, "the whole notebook is still described"
    assert shaped["returnedCellCount"] == 2


def test_an_unknown_cell_id_is_reported_not_raised():
    """A stale id from an earlier read should shrink the answer, not fail it —
    but silently returning fewer cells would read as 'that cell is gone'."""
    shaped = shape_notebook(snapshot(cell("a")), cells=["a", "vanished"])
    assert [item["cellId"] for item in shaped["cells"]] == ["a"]
    assert shaped["missingCellIds"] == ["vanished"]


def test_a_repeated_cell_id_is_asked_for_once():
    shaped = shape_notebook(snapshot(cell("a")), cells=["a", "a"])
    assert [item["cellId"] for item in shaped["cells"]] == ["a"]


def test_outputs_can_be_left_out_entirely():
    with_outputs = snapshot(
        cell("a", outputs=[{"output_type": "stream", "name": "stdout", "text": "hi"}])
    )
    assert "outputs" not in shape_notebook(with_outputs, include_outputs=False)["cells"][0]
    assert "outputs" in shape_notebook(with_outputs)["cells"][0]


def test_a_large_notebook_is_told_how_to_ask_for_less():
    big = snapshot(
        *(
            cell(f"c{index}", index=index, source="z = 1\n", outputs=[
                {"output_type": "stream", "name": "stdout", "text": "y" * 500}
            ])
            for index in range(400)
        )
    )
    shaped = shape_notebook(big)
    hint = budget_hint(shaped)
    assert hint is not None
    assert "cells" in hint

    assert budget_hint(shape_notebook(snapshot(cell("a")))) is None


def test_shaping_collapses_a_realistic_plot_notebook():
    """The case that was measured: 18 cells, ~93% of it base64 PNG.

    The real snapshot was 92,550 bytes; shaped it was 5,283. This reproduces
    the shape of that notebook rather than its exact bytes, and pins the
    order of magnitude.
    """
    png = "iVBORw0KGgo" + "B" * 12_000
    plots = snapshot(
        *(
            cell(
                f"plot{index}",
                index=index,
                source="fig, ax = plt.subplots()\nax.plot(values)\n",
                outputs=[
                    {
                        "output_type": "display_data",
                        "data": {"image/png": png, "text/plain": "<Figure size 600x300>"},
                        "metadata": {},
                    }
                ],
            )
            for index in range(6)
        )
    )
    raw = len(json.dumps(plots))
    shaped = len(json.dumps(shape_notebook(plots)))
    assert raw > 70_000
    assert shaped < raw // 20, f"expected a large reduction, got {shaped} from {raw}"


def test_an_image_is_measured_decoded_not_as_base64_text():
    """Base64 is ~4/3 of what it encodes, so the string length overstates every
    image by a third — the number a caller uses to judge whether to look."""
    import base64

    raw = b"\x89PNG" + b"\x00" * 9_000
    encoded = base64.b64encode(raw).decode()
    summary = summarize_output(
        {"output_type": "display_data", "data": {"image/png": encoded}, "metadata": {}}
    )
    reported = summary["images"][0]["bytes"]
    assert abs(reported - len(raw)) <= 2, f"{reported} vs real {len(raw)}"
    assert reported < len(encoded)


def test_an_empty_image_payload_measures_zero():
    summary = summarize_output(
        {"output_type": "display_data", "data": {"image/png": ""}, "metadata": {}}
    )
    assert summary["images"][0]["bytes"] == 0
