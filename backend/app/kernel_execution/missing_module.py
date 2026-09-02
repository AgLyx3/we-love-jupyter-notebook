"""Reading a `ModuleNotFoundError` well enough to say what to do about it (#52).

The traceback names the module and nothing else. The fix depends on a fact it
does not carry — which interpreter looked — because the error means one of two
opposite things: the environment running the cells is the right one and is
missing a package, or it is the wrong environment entirely and installing the
package would paper over that. This module supplies the first half (what is
missing, and what to install for it); the interpreter comes from
`KernelSession.interpreter`, reported by `GET /kernel/status`.

Mirrored in TypeScript at `frontend/src/notebook/missingModule.ts` for the
editor tab. The package table below is checked against that one by
`backend/tests/test_missing_module.py`, so the two cannot drift.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
import shlex


# Import names that are not their own distribution name.
#
# Deliberately short and deliberately not exhaustive. `pip install cv2` fails
# outright, which is worse than no command at all, so the handful of names a
# notebook actually trips over are worth knowing exactly; the long tail is not
# worth pretending to. Anything absent falls back to the module name, which is
# right for the large majority of packages.
#
# Keep in sync with PACKAGE_NAMES in the TypeScript module named above.
PACKAGE_NAMES = {
    "bs4": "beautifulsoup4",
    "cv2": "opencv-python",
    "dateutil": "python-dateutil",
    "dotenv": "python-dotenv",
    "PIL": "pillow",
    "skimage": "scikit-image",
    "sklearn": "scikit-learn",
    "yaml": "PyYAML",
}

# `ModuleNotFoundError`'s message is built by CPython itself, so the quoting is
# stable enough to read. Anchored: a message that merely contains this phrase is
# somebody's own string, not the interpreter's.
_NAMED = re.compile(r"^No module named '([^']+)'")

# A module the message could plausibly be about. Guards the install command
# against anything odd arriving in `evalue` and being printed as a command.
_IMPORT_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


@dataclass(frozen=True)
class MissingModule:
    """The module an error named, and the distribution to install for it."""

    module: str
    package: str
    #: The table knew this distribution, rather than the module name being
    #: reused as a guess. What separates "it comes from scikit-learn" from "if
    #: it comes from a package at all" — plenty of failed imports are somebody's
    #: own `helpers.py` that is not on the path, and `pip install helpers`
    #: installs an unrelated stranger's package while hiding the real cause.
    known: bool = False

    def install_command(self, interpreter: str) -> str:
        """The command that installs into the interpreter the cells run in.

        `<interpreter> -m pip install`, never a guessed sibling `bin/pip`. The
        interpreter path is known exactly; a `pip` executable next to it is not
        guaranteed to exist, and a command that fails is worse than none — as
        would an unquoted path under `~/My Projects`, hence `shlex.quote`.
        """
        return f"{shlex.quote(interpreter)} -m pip install {self.package}"


def missing_module(ename: object, evalue: object) -> MissingModule | None:
    """What is missing, or None when this error is not one to give a command for.

    Scoped tightly on purpose:

    - Only `ModuleNotFoundError`. Its parent `ImportError` — a `from x import y`
      where `x` imports fine but has no `y` — is not fixed by installing
      anything, and offering `pip install x` for it would send a reader the
      wrong way.
    - Only a top-level name. CPython reports the *first* component it could not
      find, so a dotted `No module named 'a.b'` means `a` imported and `a.b` did
      not: the distribution is already installed and `pip install a` is a no-op.
    """
    if ename != "ModuleNotFoundError" or not isinstance(evalue, str):
        return None
    named = _NAMED.match(evalue)
    if not named:
        return None
    module = named.group(1)
    if not _IMPORT_NAME.match(module):
        return None
    package = PACKAGE_NAMES.get(module)
    return MissingModule(module, package or module, known=package is not None)


def missing_module_in(outputs: object) -> MissingModule | None:
    """The first missing module named by a cell's outputs, if any."""
    if not isinstance(outputs, list):
        return None
    for output in outputs:
        if not isinstance(output, dict) or output.get("output_type") != "error":
            continue
        found = missing_module(output.get("ename"), output.get("evalue"))
        if found is not None:
            return found
    return None
