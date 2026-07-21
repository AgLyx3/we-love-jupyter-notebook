from __future__ import annotations

import hashlib
import os
import stat
from pathlib import Path

from ..notebook_document.service import MAX_NOTEBOOK_BYTES
from .models import AgentWorkspace, WorkspaceBoundaryError


class WorkspaceAuditor:
    def __init__(self, *, per_file_limit: int = MAX_NOTEBOOK_BYTES, aggregate_limit: int = MAX_NOTEBOOK_BYTES) -> None:
        self.per_file_limit = per_file_limit
        self.aggregate_limit = aggregate_limit

    def collect(self, workspace: AgentWorkspace, *, auxiliary_paths: frozenset[str] = frozenset()) -> dict[str, str]:
        violations = self.audit(workspace, auxiliary_paths=auxiliary_paths)
        changes: dict[str, str] = {}
        total = 0
        for item in workspace.manifest.editable_cells:
            path = workspace.root / item.relative_path
            try:
                if path.parent.resolve() != (workspace.root / "editable").resolve():
                    raise ValueError("path is not directly under editable")
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
                    if info.st_size > self.per_file_limit:
                        raise ValueError("file exceeds source size limit")
                    chunks = []
                    remaining = info.st_size + 1
                    while remaining:
                        chunk = os.read(fd, remaining)
                        if not chunk:
                            break
                        chunks.append(chunk)
                        remaining -= len(chunk)
                    data = b"".join(chunks)
                finally:
                    os.close(fd)
                total += len(data)
                if total > self.aggregate_limit:
                    raise ValueError("candidate set exceeds aggregate size limit")
                text = data.decode("utf-8")
                if text != item.original_source:
                    changes[item.cell_id] = text
            except (OSError, UnicodeDecodeError, ValueError) as error:
                violations.append(f"{item.relative_path}: {error}")
        if violations:
            raise WorkspaceBoundaryError(sorted(set(violations)))
        return changes

    def audit(self, workspace: AgentWorkspace, *, auxiliary_paths: frozenset[str]) -> list[str]:
        violations: list[str] = []
        allowed = {item.relative_path for item in workspace.manifest.editable_cells} | set(workspace.baseline_hashes) | set(auxiliary_paths)
        for relative, expected in workspace.baseline_hashes.items():
            path = workspace.root / relative
            try:
                if path.is_symlink() or not path.is_file():
                    raise OSError("protected path is not a regular file")
                actual = hashlib.sha256(path.read_bytes()).hexdigest()
            except OSError:
                actual = "missing"
            if actual != expected:
                violations.append(f"protected path modified: {relative}")
        def declared_auxiliary(relative: str) -> bool:
            return any(relative == item or relative.startswith(f"{item}/") for item in auxiliary_paths)

        for path in workspace.root.rglob("*"):
            relative = path.relative_to(workspace.root).as_posix()
            if path.is_dir() and not path.is_symlink():
                if relative == "editable":
                    continue
                # Auxiliary directories are allowed only when declared exactly.
                if relative not in allowed and not declared_auxiliary(relative):
                    violations.append(f"undeclared path: {relative}")
            elif relative not in allowed and not declared_auxiliary(relative):
                violations.append(f"undeclared path: {relative}")
        return violations
