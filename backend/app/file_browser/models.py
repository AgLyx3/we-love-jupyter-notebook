from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from ..notebook_document.models import NotebookDomainError

EntryKind = Literal["directory", "notebook", "file"]


class DirectoryListingError(NotebookDomainError):
    code = "directory_listing_invalid"
    message = "Path is not a readable directory"
    status_code = 400


class FileSearchError(NotebookDomainError):
    code = "file_search_invalid"
    message = "Search root is not a readable directory"
    status_code = 400


@dataclass(frozen=True)
class DirectoryEntry:
    name: str
    path: str
    kind: EntryKind


@dataclass(frozen=True)
class DirectoryListing:
    path: str
    parent: str | None
    entries: tuple[DirectoryEntry, ...]


@dataclass(frozen=True)
class FileMatch:
    name: str
    path: str
    relative_path: str
    kind: EntryKind


@dataclass(frozen=True)
class FileSearchResult:
    root: str
    query: str
    matches: tuple[FileMatch, ...]
    truncated: bool
