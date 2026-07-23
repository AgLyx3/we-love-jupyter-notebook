from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request

from ..file_browser.models import DirectoryListing, FileSearchResult
from ..file_browser.service import list_directory, search_files

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


def serialize_search(result: FileSearchResult) -> dict[str, Any]:
    return {
        "root": result.root,
        "query": result.query,
        "truncated": result.truncated,
        "matches": [
            {
                "name": match.name,
                "path": match.path,
                "relativePath": match.relative_path,
                "kind": match.kind,
            }
            for match in result.matches
        ],
    }


@router.get("/files")
def list_files(request: Request, path: str | None = None) -> dict[str, Any]:
    return serialize_listing(list_directory(path))


@router.get("/files/search")
def search_workspace_files(request: Request, root: str, query: str = "") -> dict[str, Any]:
    return serialize_search(search_files(root, query))
