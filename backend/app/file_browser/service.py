from __future__ import annotations

import os
import stat
from pathlib import Path

from .models import (
    DirectoryEntry,
    DirectoryListing,
    DirectoryListingError,
    FileMatch,
    FileSearchError,
    FileSearchResult,
)

NOTEBOOK_SUFFIX = ".ipynb"

# Directory names pruned from the @-mention file walk. Mirrors the agent read
# deny list: version control, dependency/build trees, and caches are never
# useful as prompt context and are expensive to walk.
_PRUNED_DIRS = frozenset({
    ".git", "node_modules", "__pycache__", ".venv", "venv", "env",
    ".mypy_cache", ".pytest_cache", ".ruff_cache", ".vite",
    "dist", "build", ".idea", ".vscode", ".tox",
})
# File suffixes excluded from mention results (credential-bearing files).
_EXCLUDED_SUFFIXES = frozenset({".pem", ".key"})
_SEARCH_LIMIT = 50
# Bound on files visited so a huge tree cannot stall the request.
_SEARCH_SCAN_CAP = 20_000


# Sort order: folders first, then openable notebooks, then browse-only files.
_KIND_ORDER = {"directory": 0, "notebook": 1, "file": 2}


def list_directory(path: str | None = None) -> DirectoryListing:
    """Return the subfolders and files at a readable local directory.

    Folders and ``.ipynb`` notebooks are openable; every other regular file is
    tagged ``file`` and listed for visibility only (browse-for-context, never
    openable through the selector). Defaults to the user's home directory.
    Dotfiles are hidden and entries that fail to stat (unreadable) are skipped
    rather than failing the whole listing.
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
        elif stat.S_ISREG(mode):
            kind = "notebook" if child.suffix == NOTEBOOK_SUFFIX else "file"
        else:
            continue
        entries.append(DirectoryEntry(name=name, path=str(child), kind=kind))

    entries.sort(key=lambda entry: (_KIND_ORDER[entry.kind], entry.name.lower()))

    parent = None if resolved.parent == resolved else str(resolved.parent)
    return DirectoryListing(path=str(resolved), parent=parent, entries=tuple(entries))


def search_files(root: str, query: str = "", limit: int = _SEARCH_LIMIT) -> FileSearchResult:
    """Recursively find files under ``root`` for the chat ``@``-mention.

    Walks the workspace root, pruning version-control, dependency, build, and
    cache directories plus dotfiles, and returns files whose relative path
    matches ``query`` (case-insensitive substring). Results are ranked so a
    basename prefix match beats a mid-path match, then alphabetically, and are
    capped at ``limit``. Only file names/paths are returned — no contents — so
    the user can pick a path to reference; the agent still reads it itself.
    """
    resolved = Path(root).expanduser().resolve()
    if not resolved.is_dir():
        raise FileSearchError()

    needle = query.strip().lower()
    limit = max(1, min(limit, 200))
    matches: list[FileMatch] = []
    scanned = 0
    truncated = False

    for current, dirnames, filenames in os.walk(resolved):
        # Prune in place so os.walk never descends into excluded/dot dirs.
        dirnames[:] = [
            name for name in dirnames
            if not name.startswith(".") and name not in _PRUNED_DIRS
        ]
        for filename in sorted(filenames):
            if filename.startswith("."):
                continue
            if Path(filename).suffix in _EXCLUDED_SUFFIXES:
                continue
            scanned += 1
            if scanned > _SEARCH_SCAN_CAP:
                truncated = True
                break
            full = Path(current) / filename
            relative = os.path.relpath(full, resolved)
            if needle and needle not in relative.lower():
                continue
            kind = "notebook" if full.suffix == NOTEBOOK_SUFFIX else "file"
            matches.append(
                FileMatch(name=filename, path=str(full), relative_path=relative, kind=kind)
            )
        if truncated:
            break

    def rank(match: FileMatch) -> tuple[int, int, str]:
        name_lower = match.name.lower()
        prefix = 0 if needle and name_lower.startswith(needle) else 1
        return (prefix, match.relative_path.count(os.sep), match.relative_path.lower())

    matches.sort(key=rank)
    if len(matches) > limit:
        matches = matches[:limit]
        truncated = True

    return FileSearchResult(
        root=str(resolved), query=query, matches=tuple(matches), truncated=truncated,
    )
