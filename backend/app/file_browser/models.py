from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from ..notebook_document.models import NotebookDomainError

EntryKind = Literal["directory", "notebook"]


class DirectoryListingError(NotebookDomainError):
    code = "directory_listing_invalid"
    message = "Path is not a readable directory"
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
