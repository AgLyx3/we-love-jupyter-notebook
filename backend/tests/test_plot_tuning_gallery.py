"""Plot *shapes*, as opposed to plot mechanics.

The rest of the suite exercises discovery, rewriting and the shadow against
one-cell-one-plot fixtures. That leaves the shapes real notebooks actually
produce untested: two figures emitted from a single cell, one figure with
several subplots, a cell that interleaves prints with an image, a figure built
across two cells, and a cell with no image at all.

`examples/plot-gallery.ipynb` is that corpus, and running the scan over it is
what turned up the dead-binding leak covered in test_plot_tuning_discovery.py.
"""

from __future__ import annotations

import json
import pathlib

import pytest

from backend.app.plot_tuning.discovery import ChainCell, discover
from backend.app.plot_tuning.shadow import ShadowSession

GALLERY = pathlib.Path(__file__).resolve().parents[2] / "examples" / "plot-gallery.ipynb"


@pytest.fixture(scope="module")
def code_cells() -> tuple[ChainCell, ...]:
    notebook = json.loads(GALLERY.read_text())
    return tuple(
        ChainCell(cell_id=cell["id"], index=index, source="".join(cell["source"]))
        for index, cell in enumerate(notebook["cells"])
        if cell["cell_type"] == "code"
    )


def chain_to(cells: tuple[ChainCell, ...], target: str) -> tuple[ChainCell, ...]:
    stop = next(index for index, cell in enumerate(cells) if cell.cell_id == target)
    return cells[: stop + 1]


def knobs_for(cells: tuple[ChainCell, ...], target: str) -> set[str]:
    return {knob.name for knob in discover(chain_to(cells, target), target).knobs}


def image_count(outputs) -> int:
    return sum(1 for item in outputs if "image/png" in (item.get("data") or {}))


# --- what each cell shape is allowed to offer --------------------------------

def test_a_cell_emitting_two_figures_offers_the_knobs_both_share(code_cells):
    offered = knobs_for(code_cells, "two-figures")
    assert {"BINS", "ALPHA", "COLOR", "FIGSIZE", "N", "NOISE"} <= offered
    # Neither figure calls .grid(), so the grid flag must not be offered.
    assert "SHOW_GRID" not in offered


def test_a_subplot_grid_offers_the_flag_it_actually_uses(code_cells):
    assert "SHOW_GRID" in knobs_for(code_cells, "subplots")


def test_a_cell_mixing_prints_and_a_plot_offers_only_what_it_uses(code_cells):
    offered = knobs_for(code_cells, "mixed")
    assert {"ALPHA", "COLOR", "FIGSIZE"} <= offered
    assert "SHOW_GRID" not in offered
    # It plots sorted(samples) directly rather than binning, so BINS is not its
    # knob even though an earlier cell used it.
    assert "BINS" not in offered


def test_a_computed_plot_offers_only_its_upstream_data_knobs(code_cells):
    # Everything in this cell is derived (`edges = int(len(samples) ** 0.5)`),
    # so only the parameters behind `samples` genuinely move it. Before the
    # dead-binding fix this also offered FIGSIZE and SHOW_GRID, inherited from
    # an earlier cell's `fig, ax = ...` that this cell overwrites.
    assert knobs_for(code_cells, "no-knobs") == {"N", "NOISE"}


def test_a_figure_finished_in_a_later_cell_keeps_the_first_cells_knobs(code_cells):
    # The mirror case: this cell really does use the figure the previous one
    # built, so FIGSIZE stays live — and it calls .grid() itself.
    offered = knobs_for(code_cells, "fig-finish")
    assert {"FIGSIZE", "SHOW_GRID", "N", "NOISE"} <= offered


def test_a_cell_with_no_image_still_scans_cleanly(code_cells):
    # The panel is gated on the cell having an image output, which is a frontend
    # concern; discovery itself must not fall over on a text-only cell.
    result = discover(chain_to(code_cells, "table"), "table")
    assert result.blocked is None


# --- the shape question, end to end through a real kernel --------------------

def test_two_figures_in_one_cell_preview_as_two_images(code_cells):
    """A cell can emit more than one plot, and a preview must return all of them.

    Returning only the first would silently drop half the picture the user is
    comparing against — and it would look like the feature worked.
    """
    shadow = ShadowSession(target_cell_id="two-figures", document_revision=1)
    try:
        warmed = shadow.warm(chain_to(code_cells, "two-figures"))
        assert not warmed.failed
        assert image_count(warmed.outputs) == 2

        preview = shadow.preview({"BINS": 8})
        assert not preview.failed
        assert image_count(preview.outputs) == 2
        # A real re-render, not the committed picture handed back.
        assert [item.get("data", {}).get("image/png") for item in preview.outputs] \
            != [item.get("data", {}).get("image/png") for item in warmed.outputs]
    finally:
        shadow.close()


def test_interleaved_stream_and_image_outputs_keep_their_order(code_cells):
    # print / plot / print. If the panel reordered or dropped the streams the
    # preview would not match what running the cell actually shows.
    shadow = ShadowSession(target_cell_id="mixed", document_revision=1)
    try:
        shadow.warm(chain_to(code_cells, "mixed"))
        preview = shadow.preview({"ALPHA": 0.3})
        kinds = [
            "image" if "image/png" in (item.get("data") or {}) else item.get("output_type")
            for item in preview.outputs
        ]
        assert kinds == ["stream", "image", "stream"]
    finally:
        shadow.close()
