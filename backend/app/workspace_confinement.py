"""One boundary for every local path the server will accept.

The document service already knew how to keep a path inside a root, but the
root arrived *in the request* — so a caller that sent none got no boundary at
all, and one that sent ``/`` got the whole filesystem. That is the right shape
for a file picker a person is driving: it is their machine, their home
directory, and the dialog they clicked is the authorisation.

It stops being the right shape the moment something other than that person can
reach the same endpoints. A root fixed when the server starts cannot be widened
by anything a caller sends: a request may still name a narrower directory
inside it, which is how the file picker keeps working, but a path outside is
refused before it is opened, listed, or written.

Both the browser and any local client share this one object, so the boundary
cannot drift between them — one of them being laxer than the other is exactly
the failure this is meant to prevent.
"""

from __future__ import annotations

from pathlib import Path


class OutsideWorkspace(Exception):
    """A path fell outside the configured root.

    Deliberately not a ``NotebookDomainError``: this module is shared by the
    document service and the file browser, which answer with different domain
    errors. Each translates this into its own.
    """


class WorkspaceConfinement:
    """The root every path is checked against, or no root at all.

    Constructed with ``None`` it permits everything, which is what
    ``scripts/dev.py`` and the existing tests get, so behaviour is unchanged
    wherever no root is configured.
    """

    def __init__(self, root: str | Path | None = None) -> None:
        self.root = Path(root).expanduser().resolve() if root is not None else None

    @property
    def confined(self) -> bool:
        return self.root is not None

    def contains(self, path: str | Path) -> bool:
        """Whether ``path`` is the root or sits inside it. Unconfined: always."""
        if self.root is None:
            return True
        try:
            resolved = Path(path).expanduser().resolve()
        except (OSError, ValueError):
            return False
        return resolved == self.root or resolved.is_relative_to(self.root)

    def require(self, path: str | Path) -> Path:
        """Resolve ``path``, or raise ``OutsideWorkspace`` if it escapes."""
        if not self.contains(path):
            raise OutsideWorkspace(str(path))
        return Path(path).expanduser().resolve()

    def narrow(self, requested: str | None) -> str | None:
        """The effective root for one request.

        A caller may name a directory *inside* the configured root and get that
        narrower boundary — this is the workspace the file picker sends when a
        person opens a project folder. It may not name one outside, and with no
        root configured the caller's own value is passed through unchanged, so
        nothing about the unconfined case changes.
        """
        if self.root is None:
            return requested
        if requested is None:
            return str(self.root)
        return str(self.require(requested))

    def default_directory(self) -> Path:
        """Where a listing starts when the caller names no path.

        The root when there is one — offering the home directory as the opening
        view of a confined server would advertise exactly what it will not
        serve.
        """
        return self.root if self.root is not None else Path.home()

    def visible_parent(self, directory: Path) -> str | None:
        """The parent to offer for upward navigation, or None at the boundary.

        Without this the listing at the root still carries a link to its parent
        and the UI offers a step the server would then refuse.
        """
        parent = directory.parent
        if parent == directory:
            return None
        if not self.contains(parent):
            return None
        return str(parent)


UNCONFINED = WorkspaceConfinement(None)
