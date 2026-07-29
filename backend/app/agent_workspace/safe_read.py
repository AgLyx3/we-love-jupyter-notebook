from __future__ import annotations

import os
import stat
from pathlib import Path


def safe_read_bytes(path: Path, *, max_bytes: int) -> bytes:
    """Read a regular workspace file without following symlinks or hardlinks.

    Shared by the Blocking and Trusted collect paths so the dangerous
    open/fstat/read sequence has exactly one implementation and cannot drift.
    Rejects (raises ``ValueError``/``OSError``):

    - symlinks — ``O_NOFOLLOW`` on open and an ``lstat`` regular-file precheck,
    - non-regular files — ``S_ISREG`` on both ``lstat`` and ``fstat``,
    - hardlinked files — ``st_nlink != 1`` (symlink checks alone miss these),
    - files larger than ``max_bytes``.
    """
    if not stat.S_ISREG(os.lstat(path).st_mode):
        raise ValueError("not a regular file")
    fd = os.open(
        path,
        os.O_RDONLY
        | getattr(os, "O_NOFOLLOW", 0)
        | getattr(os, "O_NONBLOCK", 0),
    )
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("not a regular file")
        if info.st_nlink != 1:
            raise ValueError("hardlinked file is not allowed")
        if info.st_size > max_bytes:
            raise ValueError("file exceeds source size limit")
        chunks = []
        remaining = info.st_size + 1
        while remaining:
            chunk = os.read(fd, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        return b"".join(chunks)
    finally:
        os.close(fd)


def resolve_directly_under(root: Path, relative: str, *, parent: str) -> Path:
    """Return ``root/relative`` only if it resolves directly under ``root/parent``.

    Uses ``Path.resolve()`` (not string prefixing) so ``cells/../x``, absolute
    paths, and symlinked path components are rejected. The returned path's
    resolved parent must equal the resolved ``root/parent`` — i.e. no nested
    subdirectories and no escapes.
    """
    allowed = (root / parent).resolve()
    resolved = (root / relative).resolve()
    if resolved.parent != allowed:
        raise ValueError(f"source path escapes {parent}/: {relative}")
    return root / relative
