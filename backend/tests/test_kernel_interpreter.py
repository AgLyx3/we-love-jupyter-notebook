"""Which Python runs the cells, reported before anyone has to guess.

`ModuleNotFoundError: No module named 'pandas'` has two opposite fixes — install
the package, or point the kernel at a different environment — and nothing on the
page or on the wire distinguished them (#52). These tests hold the answer to
being both present and *true*, which is harder than it sounds: the kernelspec
does not necessarily name the interpreter that ends up running.
"""

from __future__ import annotations

import sys

import jupyter_client.kernelspec as kernelspec_module
import pytest

from backend.app.kernel_execution.kernel_session import KernelSession


class StubSpec:
    def __init__(self, argv):
        self.argv = argv


def spec_returning(argv, *, on_lookup=None):
    """A KernelSpecManager stand-in whose `python3` spec has `argv`."""

    class StubManager:
        def get_kernel_spec(self, name):
            assert name == "python3"
            if on_lookup is not None:
                on_lookup()
            return StubSpec(argv)

    return StubManager


def test_the_status_payload_names_the_interpreter():
    """The field has to exist on the wire, not only in the object.

    An MCP client gets the same `ModuleNotFoundError` from `run_cell` and has
    the same two readings of it, with rather more incentive to guess wrong.
    """
    from backend.app.kernel_execution.service import KernelExecutionService
    from backend.app.notebook_document.service import NotebookDocumentService

    service = KernelExecutionService(
        documents=NotebookDocumentService(),
        kernel=KernelSession(kernel_python="/somewhere/.venv/bin/python"),
    )
    status = service.kernel_status()
    assert status["interpreter"] == "/somewhere/.venv/bin/python"
    assert status["interpreterSource"] == "kernel-python"


def test_kernel_python_is_reported_exactly_as_it_was_given(tmp_path):
    """Not resolved — the string the caller passed is the one they recognise.

    On macOS `/tmp/x/bin/python` and `/private/tmp/x/bin/python` are the same
    file, and a running kernel reports the second. Reporting the realpath would
    show a reader a path they never typed, and `--kernel-python` deliberately
    uses `abspath` rather than `resolve()` for a related reason: resolving a
    virtualenv's `bin/python` follows the symlink to the base interpreter,
    landing on the environment the flag exists to escape.
    """
    session = KernelSession(kernel_python="/tmp/some-env/bin/python")
    assert session.interpreter == "/tmp/some-env/bin/python"
    assert session.interpreter_source == "kernel-python"


@pytest.mark.parametrize(
    "alias",
    ["python", f"python{sys.version_info[0]}", f"python{sys.version_info[0]}.{sys.version_info[1]}"],
)
def test_a_bare_python_kernelspec_reports_the_interpreter_that_will_launch(monkeypatch, alias):
    """The substitution jupyter_client performs, mirrored — or this would lie.

    `KernelManager.format_kernel_cmd` swaps a bare `python`/`python3` for
    `sys.executable` before launching. This is not a corner case: the `python3`
    kernelspec inside the editor's own environment says exactly `python`, and
    that swap is the mechanism by which a notebook ends up running against the
    editor's packages instead of the project's. Reporting the literal `python`
    would name something that never runs, and would hide the case #52 is for.
    """
    monkeypatch.setattr(kernelspec_module, "KernelSpecManager", spec_returning([alias, "-m", "ipykernel_launcher"]))
    session = KernelSession()
    assert session.interpreter == sys.executable
    assert session.interpreter_source == "kernelspec"


def test_an_absolute_kernelspec_is_reported_unchanged(monkeypatch):
    """A spec that already names an interpreter is already the answer."""
    monkeypatch.setattr(
        kernelspec_module, "KernelSpecManager",
        spec_returning(["/opt/envs/analysis/bin/python", "-m", "ipykernel_launcher"]),
    )
    assert KernelSession().interpreter == "/opt/envs/analysis/bin/python"


def test_a_kernelspec_that_cannot_be_read_leaves_the_status_answerable(monkeypatch):
    """Never raise. A status call that fails because of this is a worse bug.

    No kernelspec at all is a real state — an environment without ipykernel
    registered — and the editor still has a status to report.
    """
    class ExplodingManager:
        def get_kernel_spec(self, name):
            raise kernelspec_module.NoSuchKernel(name)

    monkeypatch.setattr(kernelspec_module, "KernelSpecManager", ExplodingManager)
    session = KernelSession()
    assert session.interpreter is None
    assert session.interpreter_source == "kernelspec"


def test_the_kernelspec_is_looked_up_once(monkeypatch):
    """Status is polled; a filesystem search per poll would be careless."""
    lookups = []
    monkeypatch.setattr(
        kernelspec_module, "KernelSpecManager",
        spec_returning(["/opt/envs/x/bin/python"], on_lookup=lambda: lookups.append(1)),
    )
    session = KernelSession()
    for _ in range(5):
        session.interpreter
    assert len(lookups) == 1


def test_a_started_kernel_outranks_the_kernelspec_guess(monkeypatch):
    """Once something is running, what it was launched with is the truth.

    Before start this can only predict; after start it should stop predicting.
    """
    monkeypatch.setattr(kernelspec_module, "KernelSpecManager", spec_returning(["/guessed/bin/python"]))
    session = KernelSession()
    assert session.interpreter == "/guessed/bin/python"

    class StartedManager:
        kernel_spec = StubSpec(["/actually/running/bin/python", "-m", "ipykernel_launcher"])

    session._manager = StartedManager()
    assert session.interpreter == "/actually/running/bin/python"
