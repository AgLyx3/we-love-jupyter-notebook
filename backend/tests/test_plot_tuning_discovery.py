"""Discovery is the correctness boundary of plot tuning.

A knob that silently does not control the value it claims to is the worst
failure this feature has, so every rejection rule in the design gets its own
test, and each asserts the *reason* as well as the absence — the panel shows
those reasons in its empty state.
"""

from __future__ import annotations

import pytest

from backend.app.plot_tuning.discovery import ChainCell, discover


def chain(*sources: str) -> tuple[ChainCell, ...]:
    """Build a chain whose last cell is the plot cell, id ``plot``."""
    ids = [f"c{index}" for index in range(len(sources) - 1)] + ["plot"]
    return tuple(
        ChainCell(cell_id=cell_id, index=index, source=source)
        for index, (cell_id, source) in enumerate(zip(ids, sources))
    )


def names(result) -> set[str]:
    return {knob.name for knob in result.knobs}


def reason_for(result, name: str) -> str:
    return next(item.reason for item in result.rejections if item.name == name)


# --- the case the whole feature exists for -----------------------------------

def test_finds_a_knob_two_hops_upstream():
    # The plot cell never mentions `n_samples`; it reads `df`, which is built
    # from `n_samples`. A direct-read scan would miss it entirely.
    result = discover(chain(
        "n_samples = 1000\n",
        "df = generate(n_samples)\n",
        "plt.hist(df)\n",
    ), "plot")
    assert names(result) == {"n_samples"}
    knob = result.knobs[0]
    assert (knob.kind, knob.value, knob.cell_id) == ("int", 1000, "c0")


def test_finds_a_knob_in_the_plot_cell_itself():
    result = discover(chain("bins = 30\nplt.hist(data, bins=bins)\n"), "plot")
    assert names(result) == {"bins"}


def test_ignores_names_with_no_binding_in_the_chain():
    # `plt` is an import; listing it as a rejection would bury the real reasons.
    result = discover(chain("bins = 30\n", "plt.hist(data, bins=bins)\n"), "plot")
    assert names(result) == {"bins"}
    assert [item.name for item in result.rejections] == []


def test_only_reports_knobs_the_plot_actually_depends_on():
    result = discover(chain("used = 1\nunused = 2\n", "plt.plot(used)\n"), "plot")
    assert names(result) == {"used"}


def test_follows_in_place_mutation_to_the_literal_behind_it():
    # The ordinary imperative training loop never rebinds `weights`, so a
    # closure that followed assignment right-hand-sides alone would never reach
    # the two literals that actually decide the result. Found on a real
    # notebook, where this missed 3 of the 4 hyperparameters.
    result = discover(chain(
        "LEARNING_RATE = 0.05\nEPOCHS = 200\n",
        "weights = [0.0]\n",
        "for _ in range(EPOCHS):\n    weights[0] -= LEARNING_RATE\n",
        "plt.plot(weights)\n",
    ), "plot")
    assert names(result) == {"LEARNING_RATE", "EPOCHS"}


def test_a_rejected_name_still_carries_its_dependencies():
    # `a, b = split(frac)` is unpacking, so `a` can never be a knob — but
    # dropping it from the graph would hide `frac`, which is a perfectly good
    # one. Rejecting a name must not sever the edge.
    result = discover(chain(
        "frac = 0.25\n",
        "a, b = split(frac)\n",
        "plt.plot(a)\n",
    ), "plot")
    assert names(result) == {"frac"}
    assert reason_for(result, "a") == "unpacked"


def test_functions_and_lambdas_are_not_reported_as_rejections():
    # "`sigmoid` is computed rather than set to a fixed value" is true and
    # useless; it buries the rejections describing something actually tunable.
    result = discover(chain(
        "def helper():\n    return 1\nshort = lambda x: x\n",
        "plt.plot(helper(), short(1))\n",
    ), "plot")
    assert [item.name for item in result.rejections] == []


def test_function_locals_are_never_reported():
    result = discover(chain(
        "def make():\n    tmp = 5\n    return tmp\n",
        "plt.plot(make())\n",
    ), "plot")
    assert names(result) == set()
    assert [item.name for item in result.rejections] == []


def test_locals_of_a_function_nested_in_a_block_are_never_reported():
    # A `def` inside an `if` is not a top-level statement, so the statement-level
    # guard does not catch it; the walk itself has to stop at function bodies or
    # `tmp` is promoted to a module binding and rejected as though the user
    # might have wanted to tune it.
    result = discover(chain(
        "if flag:\n    def helper():\n        tmp = 5\n        return tmp\n",
        "plt.plot(tmp)\n",
    ), "plot")
    assert names(result) == set()
    assert [item.name for item in result.rejections] == []


# --- one test per rejection rule ---------------------------------------------

def test_rejects_a_name_bound_twice():
    result = discover(chain("n = 1\n", "n = 2\n", "plt.plot(n)\n"), "plot")
    assert names(result) == set()
    assert reason_for(result, "n") == "rebound"


def test_rejects_augmented_assignment():
    result = discover(chain("n = 1\nn += 4\n", "plt.plot(n)\n"), "plot")
    assert names(result) == set()
    assert reason_for(result, "n") == "augmented"


def test_rejects_a_loop_variable():
    result = discover(chain("for n in range(3):\n    pass\n", "plt.plot(n)\n"), "plot")
    assert reason_for(result, "n") == "loop_target"


def test_rejects_a_comprehension_variable():
    result = discover(chain("rows = [n for n in range(3)]\n", "plt.plot(n)\n"), "plot")
    assert reason_for(result, "n") == "comprehension_target"


def test_rejects_a_walrus_binding():
    result = discover(chain("if (n := 5):\n    pass\n", "plt.plot(n)\n"), "plot")
    assert reason_for(result, "n") == "walrus"


def test_rejects_a_context_manager_binding():
    result = discover(chain("with open('f') as n:\n    pass\n", "plt.plot(n)\n"), "plot")
    assert reason_for(result, "n") == "context_manager"


def test_rejects_a_name_reassigned_globally_inside_a_function():
    result = discover(chain(
        "n = 5\ndef bump():\n    global n\n    n = 9\n",
        "plt.plot(n)\n",
    ), "plot")
    assert names(result) == set()
    assert reason_for(result, "n") == "global_in_function"


def test_rejects_a_binding_nested_in_a_conditional():
    result = discover(chain("if flag:\n    n = 5\n", "plt.plot(n)\n"), "plot")
    assert reason_for(result, "n") == "nested"


def test_rejects_tuple_unpacking():
    result = discover(chain("width, height = 4, 3\n", "plt.figure(figsize=(width, height))\n"), "plot")
    assert names(result) == set()
    assert reason_for(result, "width") == "unpacked"


def test_rejects_a_subscript_write_as_mutation_not_unpacking():
    # `df["col"] = ...` is one of the most common lines in a real notebook.
    # Calling it unpacking is flatly untrue and reads as a bug in the panel.
    result = discover(chain("df = load()\ndf['new'] = 1\n", "plt.plot(df)\n"), "plot")
    assert names(result) == set()
    assert reason_for(result, "df") == "mutated"


def test_rejects_an_attribute_write_as_mutation():
    result = discover(chain("cfg = load()\ncfg.bins = 1\n", "plt.plot(cfg)\n"), "plot")
    assert reason_for(result, "cfg") == "mutated"


def test_rejects_a_deleted_name():
    # Dependency-only edges are not counted as rebinds, so without an explicit
    # guard this would look like a single clean literal assignment and be
    # offered as a knob for a name that no longer exists.
    result = discover(chain("n = 5\ndel n\n", "plt.plot(n)\n"), "plot")
    assert names(result) == set()
    assert reason_for(result, "n") == "deleted"


def test_rejects_a_computed_value():
    result = discover(chain("n = 10 * 2\n", "plt.plot(n)\n"), "plot")
    assert reason_for(result, "n") == "not_literal"


def test_rejects_a_literal_type_with_no_control():
    result = discover(chain("n = None\n", "plt.plot(n)\n"), "plot")
    assert reason_for(result, "n") == "unsupported_type"


# --- the refinement: function locals must NOT disqualify a global ------------

def test_a_function_local_of_the_same_name_does_not_reject_the_global():
    # `n` inside the helper is local and cannot affect the module-level `n`.
    # Rejecting here would kill a good knob because a helper reused the name.
    result = discover(chain(
        "n = 7\ndef helper():\n    n = 99\n    return n\n",
        "plt.plot(n)\n",
    ), "plot")
    assert names(result) == {"n"}


def test_a_class_body_attribute_does_not_reject_the_global():
    result = discover(chain("n = 7\nclass Config:\n    n = 99\n", "plt.plot(n)\n"), "plot")
    assert names(result) == {"n"}


# --- types -------------------------------------------------------------------

@pytest.mark.parametrize(("source", "kind", "value"), [
    ("n = 5\n", "int", 5),
    ("n = 0.25\n", "float", 0.25),
    ("n = True\n", "bool", True),
    ("n = 'red'\n", "str", "red"),
    ("n = (0, 10)\n", "tuple", (0, 10)),
    ("n = [0, 10]\n", "list", (0, 10)),
    ("n: int = 5\n", "int", 5),
])
def test_classifies_literal_types(source, kind, value):
    result = discover(chain(source, "plt.plot(n)\n"), "plot")
    assert (result.knobs[0].kind, result.knobs[0].value) == (kind, value)


def test_bool_is_not_reported_as_int():
    # bool subclasses int, so an unguarded isinstance check would give a slider
    # where the design calls for a toggle.
    result = discover(chain("flag = False\n", "plt.plot(flag)\n"), "plot")
    assert result.knobs[0].kind == "bool"


def test_rejects_a_tuple_containing_a_non_number():
    result = discover(chain("n = (0, 'x')\n", "plt.plot(n)\n"), "plot")
    assert reason_for(result, "n") == "not_literal"


# --- magics and unparseable cells --------------------------------------------

def test_line_magics_do_not_prevent_discovery():
    result = discover(chain("%matplotlib inline\nbins = 30\n", "plt.hist(d, bins=bins)\n"), "plot")
    assert names(result) == {"bins"}


def test_shell_escapes_do_not_prevent_discovery():
    result = discover(chain("!echo hi\nbins = 30\n", "plt.hist(d, bins=bins)\n"), "plot")
    assert names(result) == {"bins"}


def test_line_magic_keeps_source_ranges_intact():
    # The magic line is replaced by `pass`, not removed, so `bins` must still
    # report line 2 — every rewrite offset depends on this.
    result = discover(chain("%matplotlib inline\nbins = 30\n", "plt.hist(d, bins=bins)\n"), "plot")
    assert result.knobs[0].lineno == 2


def test_a_string_literal_containing_a_magic_line_keeps_its_real_value():
    # Stripping magics before parsing would rewrite this line to `pass` and the
    # panel would show the user a string their file does not contain.
    source = 'BANNER = """\n%complete\n"""\n'
    result = discover(chain(source, "plt.title(BANNER)\n"), "plot")
    assert result.knobs[0].value == "\n%complete\n"


def test_a_string_literal_containing_a_shell_line_keeps_its_real_value():
    source = 'CMD = """\n!rm -rf tmp\n"""\n'
    result = discover(chain(source, "plt.title(CMD)\n"), "plot")
    assert result.knobs[0].value == "\n!rm -rf tmp\n"


def test_python_bodied_cell_magic_is_analysed():
    result = discover(chain("%%time\nbins = 30\n", "plt.hist(d, bins=bins)\n"), "plot")
    assert names(result) == {"bins"}


def test_non_python_cell_magic_blocks_the_chain():
    result = discover(chain("%%bash\necho hi\n", "bins = 30\n", "plt.hist(d, bins=bins)\n"), "plot")
    assert result.knobs == ()
    assert result.blocked is not None and "cell magic" in result.blocked


def test_unparseable_cell_blocks_the_chain():
    result = discover(chain("def (:\n", "plt.plot(n)\n"), "plot")
    assert result.knobs == ()
    assert result.blocked is not None


def test_missing_target_cell_is_reported():
    result = discover(chain("n = 1\n"), "nope")
    assert result.blocked is not None


# --- ordering ----------------------------------------------------------------

def test_knobs_are_ordered_by_position_in_the_notebook():
    result = discover(chain(
        "second = 2\n",
        "first = 1\nalso = 3\n",
        "plt.plot(second, first, also)\n",
    ), "plot")
    assert [knob.name for knob in result.knobs] == ["second", "first", "also"]


# Regression: dead bindings must not leak their dependencies into the closure.
#
# Found by running the scan over a gallery notebook where several cells each
# open their own figure. Following *every* binding of a name meant a plot cell
# that rebinds `fig, ax` itself inherited whatever an earlier cell's
# `fig, ax = plt.subplots(figsize=FIGSIZE)` had read — so FIGSIZE was offered
# for a cell that never uses it. Turning it re-runs the chain and changes
# nothing, which is exactly the failure the rejection rules exist to prevent.
def test_a_rebound_name_does_not_leak_the_dead_bindings_dependencies():
    result = discover(chain(
        "FIGSIZE = (6, 3)\nBINS = 20\n",
        "fig, ax = plt.subplots(figsize=FIGSIZE)\nax.hist(d, bins=BINS)\n",
        "fig, ax = plt.subplots()\nax.hist(d)\nplt.show()\n",
    ), "plot")
    assert names(result) == set()


def test_the_live_binding_still_reaches_its_own_dependencies():
    # The mirror image: the target uses the earlier figure rather than making
    # its own, so FIGSIZE genuinely does control it and must survive.
    result = discover(chain(
        "FIGSIZE = (6, 3)\n",
        "fig, ax = plt.subplots(figsize=FIGSIZE)\n",
        "ax.set_title('t')\nfig\n",
    ), "plot")
    assert names(result) == {"FIGSIZE"}


def test_an_accumulating_rebinding_keeps_the_earlier_dependency():
    # `data = clean(data, THRESH)` reads `data`, so it refines rather than
    # replaces: N must stay reachable through the first binding.
    result = discover(chain(
        "N = 100\nTHRESH = 0.5\n",
        "data = load(N)\n",
        "data = clean(data, THRESH)\n",
        "plt.plot(data)\n",
    ), "plot")
    assert names(result) == {"N", "THRESH"}
