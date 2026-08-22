"""What each plotting library actually renders, through a real shadow kernel.

The AST-level coverage in test_plot_tuning_library_idioms.py answers "does
discovery understand this syntax". This file answers the other half: "does a
preview of it produce something the panel can show". They fail differently — a
library can be discovered perfectly and still emit a MIME type the output
renderer has no branch for.

pandas, seaborn and plotly are not declared dependencies; the app never imports
them and the user's kernel has whatever it has. They are installed in the branch
sandbox only, so every test here skips cleanly without them.
"""

from __future__ import annotations

from typing import Any

import pytest

from backend.app.plot_tuning.discovery import ChainCell
from backend.app.plot_tuning.shadow import ShadowSession


def chain(*sources: str) -> tuple[ChainCell, ...]:
    ids = [f"c{index}" for index in range(len(sources) - 1)] + ["plot"]
    return tuple(
        ChainCell(cell_id=cell_id, index=index, source=source)
        for index, (cell_id, source) in enumerate(zip(ids, sources))
    )


def mimes(outputs) -> list[str]:
    found = []
    for item in outputs:
        if item.get("output_type") == "stream":
            found.append("stream")
        elif item.get("output_type") == "error":
            found.append(f"error:{item.get('ename')}")
        else:
            found.extend((item.get("data") or {}).keys())
    return found


def png_of(outputs) -> list[Any]:
    return [
        (item.get("data") or {}).get("image/png")
        for item in outputs if "image/png" in (item.get("data") or {})
    ]


def rendered(cells, target: str, values: dict[str, Any]):
    """Warm, then preview at `values`. Returns (warm result, preview result)."""
    shadow = ShadowSession(target_cell_id=target, document_revision=1)
    try:
        warmed = shadow.warm(cells)
        assert not warmed.failed, f"replay failed at {warmed.failed_cell_id}"
        return warmed, shadow.preview(values)
    finally:
        shadow.close()


# --- libraries that bottom out in matplotlib ---------------------------------

def test_a_pandas_plot_renders_a_png_and_re_renders_on_a_knob():
    pytest.importorskip("pandas")
    warmed, preview = rendered(chain(
        "%matplotlib inline\nimport pandas as pd\nimport matplotlib.pyplot as plt\n",
        "ROWS = 40\nBINS = 6\n",
        "df = pd.DataFrame({'x': list(range(ROWS))})\n",
        "df.plot(kind='hist', bins=BINS)\nplt.show()\n",
    ), "plot", {"BINS": 3})

    assert "image/png" in mimes(warmed.outputs)
    assert not preview.failed
    assert "image/png" in mimes(preview.outputs)
    # A genuine re-render at the new bin count, not the committed picture.
    assert png_of(preview.outputs) != png_of(warmed.outputs)


def test_a_pandas_knob_reached_through_an_inplace_call_re_renders():
    # The idiom that was invisible to discovery until the in-place rule: this
    # asserts the whole path, not just that the knob is listed.
    pytest.importorskip("pandas")
    warmed, preview = rendered(chain(
        "%matplotlib inline\nimport pandas as pd\nimport matplotlib.pyplot as plt\n",
        "FILL = 0.0\nBINS = 5\n",
        "df = pd.DataFrame({'x': [1.0, None, 3.0, None, 5.0]})\n",
        "df.fillna(FILL, inplace=True)\n",
        "df.plot(kind='hist', bins=BINS)\nplt.show()\n",
    ), "plot", {"FILL": 99.0})

    assert not preview.failed
    assert png_of(preview.outputs) != png_of(warmed.outputs)


def test_a_seaborn_plot_renders_a_png_and_re_renders_on_a_knob():
    pytest.importorskip("seaborn")
    warmed, preview = rendered(chain(
        "%matplotlib inline\nimport seaborn as sns\nimport matplotlib.pyplot as plt\n",
        "BINS = 8\n",
        "values = [1, 2, 2, 3, 3, 3, 4, 5]\n",
        "sns.histplot(values, bins=BINS)\nplt.show()\n",
    ), "plot", {"BINS": 3})

    assert "image/png" in mimes(warmed.outputs)
    assert not preview.failed
    assert png_of(preview.outputs) != png_of(warmed.outputs)


# --- the library the panel cannot show ---------------------------------------

def test_plotly_emits_its_own_mime_and_so_is_never_offered_for_tuning():
    """Documents a real limit, and pins the reason.

    Plotly does not emit `image/png` or even `text/html` — it emits
    `application/vnd.plotly.v1+json`. Two consequences worth being explicit
    about, because both are silent:

    * `Outputs` has no branch for that MIME, so it falls through to the
      `text/plain` fallback and renders the figure as a wall of JSON.
    * The Tune button is gated on the cell having an image output, so a plotly
      cell never offers tuning at all.

    Discovery understands plotly's arguments perfectly well (see
    test_plot_tuning_library_idioms.py); it is the render path that stops. If
    this test ever fails because an `image/png` appeared, plotly gained a static
    renderer and the panel could support it — worth revisiting then.
    """
    pytest.importorskip("plotly")
    shadow = ShadowSession(target_cell_id="plot", document_revision=1)
    try:
        warmed = shadow.warm(chain(
            "import plotly.express as px\n",
            "POINTS = 3\n",
            "fig = px.scatter(x=list(range(POINTS)), y=list(range(POINTS)))\nfig\n",
        ))
        assert not warmed.failed
        assert "application/vnd.plotly.v1+json" in mimes(warmed.outputs)
        assert "image/png" not in mimes(warmed.outputs)
    finally:
        shadow.close()
