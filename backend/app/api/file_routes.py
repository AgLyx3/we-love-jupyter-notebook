from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request

from ..file_browser.models import DirectoryListing
from ..file_browser.service import list_directory

router = APIRouter()


def serialize_listing(listing: DirectoryListing) -> dict[str, Any]:
    return {
        "path": listing.path,
        "parent": listing.parent,
        "entries": [
            {"name": entry.name, "path": entry.path, "kind": entry.kind}
            for entry in listing.entries
        ],
    }


@router.get("/files")
def list_files(request: Request, path: str | None = None) -> dict[str, Any]:
    return serialize_listing(list_directory(path))
