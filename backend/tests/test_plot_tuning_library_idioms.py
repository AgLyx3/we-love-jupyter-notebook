"""Discovery against the code shapes real plotting libraries produce.

Discovery is pure AST, so these need none of the libraries installed — which is
the point: the risk in pandas or seaborn code is not the *output* (a pandas
`.plot()` returns a matplotlib Axes and lands on the same image/png path) but the
*syntax*. Chained calls, column writes, filter-rebinds and `inplace=True` all
reach the dependency graph differently, and getting any of them wrong either
hides a knob or offers one that controls nothing.
"""

from __future__ import annotations

from backend.app.plot_tuning.discovery import ChainCell, discover


def chain(*sources: str) -> tuple[ChainCell, ...]:
    ids = [f"c{index}" for index in range(len(sources) - 1)] + ["plot"]
    return tuple(
        ChainCell(cell_id=cell_id, index=index, source=source)
        for index, (cell_id, source) in enumerate(zip(ids, sources))
    )


def names(*sources: str) -> set[str]:
    return {knob.name for knob in discover(chain(*sources), "plot").knobs}


def reason_for(sources, name):
    result = discover(chain(*sources), "plot")
    return next(item.reason for item in result.rejections if item.name == name)


# --- pandas ------------------------------------------------------------------

def test_a_filter_rebind_keeps_the_knobs_behind_the_original_frame():
    # `df = df[...]` reads `df`, so it refines rather than replaces: the row
    # count behind the first assignment stays reachable.
    assert names(
        "THRESH = 0.5\nROWS = 1000\nBINS = 30\n",
        "df = pd.DataFrame({'x': np.random.rand(ROWS)})\n",
        "df['scaled'] = df['x'] * 2\ndf = df[df.scaled > THRESH]\n",
        "df.plot.hist(bins=BINS)\n",
    ) == {"THRESH", "ROWS", "BINS"}


def test_a_column_write_is_reported_as_mutation():
    assert reason_for((
        "BINS = 10\n", "df = load()\ndf['new'] = 1\n", "df.hist(bins=BINS)\n",
    ), "df") == "mutated"


def test_an_inplace_call_reaches_the_literal_behind_it():
    # `df.fillna(FILL, inplace=True)` mutates `df` while producing no AST store
    # at all, so without the in-place rule `FILL` is invisible — not offered and
    # not even explained. `inplace=True` is everyday pandas.
    assert names(
        "FILL = 0.0\nBINS = 20\n",
        "df.fillna(FILL, inplace=True)\n",
        "df.hist(bins=BINS)\n",
    ) == {"FILL", "BINS"}


def test_a_groupby_chain_keeps_its_arguments():
    assert names(
        "MIN_COUNT = 5\nCOL = 'category'\n",
        "grouped = raw.groupby(COL).filter(lambda g: len(g) >= MIN_COUNT)\n",
        "grouped.plot(kind='bar')\n",
    ) == {"MIN_COUNT", "COL"}


# --- the in-place rule must stay narrow --------------------------------------

def test_container_mutation_reaches_its_literal():
    assert names(
        "STEP = 3\nBINS = 10\n", "acc = []\n", "acc.append(STEP)\n",
        "plt.hist(acc, bins=BINS)\n",
    ) == {"STEP", "BINS"}


def test_the_notebook_display_idiom_does_not_connect():
    # A trailing `df.head(PEEK)` renders a table; it does not change `df`.
    # Treating every bare call as mutating would offer PEEK as a knob for a plot
    # it cannot move — the failure the rejection rules exist to prevent.
    assert names(
        "PEEK = 5\nBINS = 10\n", "df.head(PEEK)\n", "plt.hist(df, bins=BINS)\n",
    ) == {"BINS"}


def test_a_non_mutating_chained_call_does_not_connect():
    assert names(
        "K = 5\nBINS = 10\n", "df.nlargest(K, 'x')\n", "plt.hist(df, bins=BINS)\n",
    ) == {"BINS"}


# --- seaborn and plotly ------------------------------------------------------

def test_seaborn_keyword_arguments_are_ordinary_knobs():
    assert names(
        "BINS = 25\nPALETTE = 'deep'\n",
        "sns.histplot(data=df, bins=BINS, palette=PALETTE)\n",
    ) == {"BINS", "PALETTE"}


def test_plotly_arguments_are_discovered_even_though_its_output_is_inert():
    # Discovery works; the *rendering* is the documented limit. Plotly emits
    # text/html, which the design renders in a `sandbox=""` iframe, so the figure
    # appears but its interactivity does not run. That is decision D-level
    # (no JS in outputs), not a discovery gap.
    assert names(
        "POINTS = 500\nOPACITY = 0.6\n",
        "fig = px.scatter(df.head(POINTS), opacity=OPACITY)\nfig.show()\n",
    ) == {"POINTS", "OPACITY"}
