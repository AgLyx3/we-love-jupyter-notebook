"""Saying what to do about `ModuleNotFoundError` (#52).

The traceback names a module. What it cannot say is which interpreter looked,
and that is the whole question: the environment running the cells is the right
one and lacks a package, or it is the wrong environment and installing the
package would hide that. These tests pin the two halves — reading the error,
and the sentence the MCP `run_cell` result carries because of it.
"""

from __future__ import annotations

from pathlib import Path
import re

import pytest

from backend.app.kernel_execution.missing_module import (
    PACKAGE_NAMES, missing_module, missing_module_in,
)
from backend.app.mcp.polling import await_execution
from backend.app.mcp.server import EditorSession


# --- reading the error -------------------------------------------------------


def test_a_missing_module_is_recognised_and_named():
    found = missing_module("ModuleNotFoundError", "No module named 'pandas'")
    assert found is not None
    assert found.module == "pandas"
    assert found.package == "pandas"
    assert found.known is False, "the module name is a guess, however good a one"


def test_a_module_that_is_not_its_package_installs_the_package():
    """`pip install sklearn` fails outright. A command that does not work is
    worse than no command, so the names a notebook actually trips over are
    known exactly rather than guessed from the module."""
    sklearn = missing_module("ModuleNotFoundError", "No module named 'sklearn'")
    assert sklearn.package == "scikit-learn"
    assert sklearn.known is True
    assert missing_module("ModuleNotFoundError", "No module named 'cv2'").package == "opencv-python"


def test_an_import_error_is_not_offered_an_install():
    """`from x import y` where `x` imports fine raises ImportError, not its
    ModuleNotFoundError subclass. Nothing is missing from the environment, so
    `pip install x` would send the reader in the wrong direction."""
    assert missing_module("ImportError", "cannot import name 'y' from 'x'") is None
    assert missing_module("ImportError", "No module named 'pandas'") is None


def test_a_submodule_is_not_offered_its_parent_package():
    """CPython names the *first* component it could not find, so a dotted name
    means the parent imported: the distribution is already installed and
    `pip install pandas` for `pandas.nonesuch` is a no-op that reads as advice."""
    assert missing_module("ModuleNotFoundError", "No module named 'pandas.nonesuch'") is None


def test_a_message_that_is_not_the_interpreters_own_is_ignored():
    """`raise ModuleNotFoundError("...No module named 'x'...")` is somebody's
    own string; the phrase is only trustworthy anchored at the start."""
    assert missing_module("ModuleNotFoundError", "wrapped: No module named 'x'") is None
    assert missing_module("ModuleNotFoundError", "No module named 'not a module'") is None
    assert missing_module("ModuleNotFoundError", None) is None


def test_the_command_runs_pip_through_the_interpreter_itself():
    """Never a guessed sibling `bin/pip`: the interpreter path is known exactly
    and a pip executable next to it is not guaranteed to exist."""
    found = missing_module("ModuleNotFoundError", "No module named 'pandas'")
    assert found.install_command("/proj/.venv/bin/python") == (
        "/proj/.venv/bin/python -m pip install pandas"
    )
    assert "bin/pip " not in found.install_command("/proj/.venv/bin/python")


def test_a_path_a_shell_would_split_is_quoted():
    """A venv under `~/My Projects` would otherwise produce a command that runs
    `/Users/me/My` — the failing command this is meant to avoid."""
    found = missing_module("ModuleNotFoundError", "No module named 'pandas'")
    assert found.install_command("/Users/me/My Projects/.venv/bin/python") == (
        "'/Users/me/My Projects/.venv/bin/python' -m pip install pandas"
    )


def test_the_missing_module_is_found_among_a_cells_outputs():
    outputs = [
        {"output_type": "stream", "name": "stdout", "text": "starting\n"},
        {
            "output_type": "error", "ename": "ModuleNotFoundError",
            "evalue": "No module named 'pandas'", "traceback": ["..."],
        },
    ]
    assert missing_module_in(outputs).module == "pandas"
    assert missing_module_in([]) is None
    assert missing_module_in(None) is None


def test_the_package_table_matches_the_one_the_editor_tab_uses():
    """The tab and the MCP tools each read the error in their own language.
    Two tables that disagree would give two different install commands for the
    same failure, which is exactly the kind of wrong answer this is here to
    stop, so they are checked against each other rather than trusted."""
    source = (
        Path(__file__).resolve().parents[2]
        / "frontend" / "src" / "notebook" / "missingModule.ts"
    ).read_text()
    block = re.search(
        r"export const PACKAGE_NAMES: Record<string, string> = \{(.*?)\};",
        source, re.S,
    )
    assert block, "PACKAGE_NAMES not found in missingModule.ts"
    from_typescript = dict(re.findall(r'(\w+):\s*"([^"]+)"', block.group(1)))
    assert from_typescript == PACKAGE_NAMES


# --- what run_cell says about it ---------------------------------------------


class FakeTools:
    """Serves one terminal operation, plus the kernel status when asked."""

    def __init__(self, kernel_status):
        self.state = EditorSession()
        self.kernel_status = kernel_status
        self.paths: list[str] = []

    def request(self, method, path, *, json_body=None, what=""):
        self.paths.append(path)
        if path == "/kernel/status":
            if isinstance(self.kernel_status, Exception):
                raise self.kernel_status
            return self.kernel_status
        raise AssertionError(f"unexpected request to {path}")


def failed_on(evalue, *, ename="ModuleNotFoundError"):
    return {
        "operationId": "op1", "sessionId": "s1", "state": "failed",
        "currentDocumentRevision": 4,
        "error": {"message": "Cell execution failed"},
        "attempts": [
            {
                "cellId": "code-1", "state": "failed",
                "outputs": [{
                    "output_type": "error", "ename": ename,
                    "evalue": evalue, "traceback": [f"{ename}: {evalue}"],
                }],
            },
        ],
    }


def note_for(operation, kernel_status):
    tools = FakeTools(kernel_status)
    result = await_execution(
        tools, operation, timeout=10, interval=0, sleep=lambda _s: None,
    )
    return result.get("note", ""), tools


RESOLVED = {"interpreter": "/proj/.venv/bin/python", "interpreterSource": "kernelspec"}


def test_a_failed_run_says_where_the_module_is_missing_from_and_what_to_run():
    """The gap this fixes. A tool caller sees the traceback and nothing else —
    strictly less than the person with the editor tab open — and its wrong
    guess is worse, because it writes `!pip install` into someone's notebook."""
    note, _ = note_for(failed_on("No module named 'pandas'"), RESOLVED)
    assert "/proj/.venv/bin/python" in note
    assert "/proj/.venv/bin/python -m pip install pandas" in note
    assert "!pip install" in note, "the cell-magic trap is the likely wrong turn"


def test_the_note_does_not_route_an_agent_around_the_approval_gate():
    """`pip install` in a cell is a `package_change` the classifier stops for a
    person to approve. A note that answered it with "run this in a terminal
    instead" would walk an agent past that control, over a package name that
    came out of a notebook the person may not have written."""
    note, _ = note_for(failed_on("No module named 'pandas'"), RESOLVED)
    assert "in a terminal" not in note
    assert "the person's call" in note


def test_the_note_says_nobody_chose_the_interpreter_when_nobody_did():
    """The wrong-environment half of the diagnosis. A resolved interpreter is
    the case that goes wrong; one passed with --kernel-python is not."""
    resolved, _ = note_for(failed_on("No module named 'pandas'"), RESOLVED)
    assert "--kernel-python" in resolved and "Nobody chose" in resolved
    chosen, _ = note_for(
        failed_on("No module named 'pandas'"),
        {"interpreter": "/proj/.venv/bin/python", "interpreterSource": "kernel-python"},
    )
    assert "was chosen with --kernel-python" in chosen
    assert "Nobody chose" not in chosen


def test_the_note_does_not_claim_a_package_for_a_module_it_does_not_know():
    """`import helpers` usually fails because somebody's own file is not on the
    path. `helpers` is on PyPI, so a note asserting the install is the fix would
    have an agent pull in a stranger's package and bury the real cause."""
    unknown, _ = note_for(failed_on("No module named 'helpers'"), RESOLVED)
    assert "If it comes from a package rather than a file of your own" in unknown
    assert "It comes from the" not in unknown

    known, _ = note_for(failed_on("No module named 'sklearn'"), RESOLVED)
    assert "It comes from the scikit-learn package" in known
    assert "-m pip install scikit-learn" in known


def test_a_kernel_status_that_answers_with_nothing_is_survivable():
    """`NotebookTools.request` hands back None for a 204 or an empty body, and
    a diagnostic that dereferences that takes the whole run result down."""
    note, _ = note_for(failed_on("No module named 'pandas'"), None)
    assert "'code-1'" in note
    assert "pip install" not in note


def test_an_unrelated_failure_costs_no_extra_request():
    """The kernel status is fetched only when there is something to say about
    it, so an ordinary NameError does not pay for a diagnosis it will not get."""
    note, tools = note_for(failed_on("name 'x' is not defined", ename="NameError"), RESOLVED)
    assert "pip install" not in note
    assert tools.paths == []


def test_an_unknown_interpreter_says_nothing_rather_than_something_empty():
    """Without a path there is no environment to name and no command to give —
    only the traceback restated."""
    note, _ = note_for(
        failed_on("No module named 'pandas'"),
        {"interpreter": None, "interpreterSource": "kernelspec"},
    )
    assert "pip install" not in note
    assert "'code-1'" in note, "the ordinary failure note still stands"


def test_a_kernel_status_that_fails_does_not_fail_the_run_report():
    """A diagnostic that can take the result down with it is a worse bug than
    the one it diagnoses."""
    note, _ = note_for(failed_on("No module named 'pandas'"), RuntimeError("boom"))
    assert "'code-1'" in note
    assert "pip install" not in note


@pytest.mark.parametrize("state", ["completed", "cancelled"])
def test_only_a_failed_run_is_diagnosed(state):
    operation = failed_on("No module named 'pandas'")
    operation["state"] = state
    note, tools = note_for(operation, RESOLVED)
    assert "pip install" not in note
    assert tools.paths == []
