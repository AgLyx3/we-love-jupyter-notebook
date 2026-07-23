from __future__ import annotations

import stat
from pathlib import Path

from .models import DirectoryEntry, DirectoryListing, DirectoryListingError

NOTEBOOK_SUFFIX = ".ipynb"


def list_directory(path: str | None = None) -> DirectoryListing:
    """Return the subfolders and ``.ipynb`` files at a readable local directory.

    Defaults to the user's home directory. Dotfiles are hidden and entries that
    fail to stat (unreadable) are skipped rather than failing the whole listing.
    """
    base = Path(path).expanduser() if path else Path.home()
    resolved = base.resolve()

    if not resolved.is_dir():
        raise DirectoryListingError()
    try:
        children = list(resolved.iterdir())
    except OSError as error:
        raise DirectoryListingError() from error

    entries: list[DirectoryEntry] = []
    for child in children:
        name = child.name
        if name.startswith("."):
            continue
        try:
            mode = child.stat().st_mode
        except OSError:
            continue
        if stat.S_ISDIR(mode):
            kind = "directory"
        elif stat.S_ISREG(mode) and child.suffix == NOTEBOOK_SUFFIX:
            kind = "notebook"
        else:
            continue
        entries.append(DirectoryEntry(name=name, path=str(child), kind=kind))

    entries.sort(key=lambda entry: (entry.kind != "directory", entry.name.lower()))

    parent = None if resolved.parent == resolved else str(resolved.parent)
    return DirectoryListing(path=str(resolved), parent=parent, entries=tuple(entries))
