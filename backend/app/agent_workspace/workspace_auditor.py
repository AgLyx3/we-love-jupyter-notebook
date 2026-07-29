from __future__ import annotations

import hashlib
import json
from pathlib import Path

from ..notebook_document.service import MAX_NOTEBOOK_BYTES
from .models import AgentWorkspace, ReturnedStructureEntry, WorkspaceBoundaryError
from .safe_read import resolve_directly_under, safe_read_bytes


# Generous ceiling on how many source files a Trusted turn may leave under
# cells/, so an agent cannot fill the workspace with unreferenced files.
MAX_TRUSTED_CELL_FILES = 512


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
                data = safe_read_bytes(path, max_bytes=self.per_file_limit)
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

    @staticmethod
    def _protected_path_violations(workspace: AgentWorkspace) -> list[str]:
        """Verify every baseline-hashed (protected) file is an unmodified regular
        file. Shared by the Blocking and Trusted audits so the check cannot drift."""
        violations: list[str] = []
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
        return violations

    def audit(self, workspace: AgentWorkspace, *, auxiliary_paths: frozenset[str]) -> list[str]:
        violations = self._protected_path_violations(workspace)
        allowed = {item.relative_path for item in workspace.manifest.editable_cells} | set(workspace.baseline_hashes) | set(auxiliary_paths)
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

    def audit_trusted(self, workspace: AgentWorkspace) -> list[str]:
        """Audit a Trusted workspace: protected files intact; only structure.json
        and regular files directly under cells/ are writable; bounded file count."""
        violations = self._protected_path_violations(workspace)
        root = workspace.root
        cells_dir = (root / "cells").resolve()
        allowed_top = set(workspace.baseline_hashes) | {"structure.json"}
        cell_file_count = 0
        for path in root.rglob("*"):
            relative = path.relative_to(root).as_posix()
            if path.is_dir() and not path.is_symlink():
                if relative != "cells":
                    violations.append(f"undeclared path: {relative}")
                continue
            if relative in allowed_top:
                continue
            # Any NON-symlink regular file directly under cells/ is a legitimate
            # agent-created source file. Symlinks/hardlinks are caught here or on read.
            if (
                not path.is_symlink()
                and path.is_file()
                and path.parent.resolve() == cells_dir
            ):
                cell_file_count += 1
                continue
            violations.append(f"undeclared path: {relative}")
        if cell_file_count > MAX_TRUSTED_CELL_FILES:
            violations.append(f"too many files under cells/: {cell_file_count}")
        return violations

    def collect_trusted(
        self, workspace: AgentWorkspace,
    ) -> list[ReturnedStructureEntry]:
        """Parse the agent-written structure.json and read every referenced
        source through the shared hardened reader. Raises WorkspaceBoundaryError
        on any file-safety, containment, size, or shape violation."""
        # Fail before reading any source when the workspace shape is already
        # wrong: an undeclared/symlinked path (e.g. a symlinked cells/ dir) must
        # not let source reads proceed and resolve outside the workspace.
        audit_violations = self.audit_trusted(workspace)
        if audit_violations:
            raise WorkspaceBoundaryError(sorted(set(audit_violations)))
        violations: list[str] = []
        root = workspace.root
        try:
            raw = safe_read_bytes(root / "structure.json", max_bytes=self.per_file_limit)
            document = json.loads(raw.decode("utf-8"))
        except (OSError, UnicodeDecodeError, ValueError, json.JSONDecodeError) as error:
            violations.append(f"structure.json: {error}")
            raise WorkspaceBoundaryError(sorted(set(violations)))
        if not isinstance(document, dict) or not isinstance(document.get("cells"), list):
            violations.append("structure.json: top-level must be an object with a cells array")
            raise WorkspaceBoundaryError(sorted(set(violations)))

        entries: list[ReturnedStructureEntry] = []
        total = 0
        seen_sources: set[str] = set()
        for position, entry in enumerate(document["cells"]):
            if not isinstance(entry, dict):
                violations.append(f"structure.json[{position}]: entry must be an object")
                continue
            source = entry.get("source")
            if not isinstance(source, str) or not source:
                violations.append(f"structure.json[{position}]: missing source")
                continue
            if source in seen_sources:
                violations.append(f"structure.json[{position}]: duplicate source {source}")
                continue
            seen_sources.add(source)
            try:
                path = resolve_directly_under(root, source, parent="cells")
                data = safe_read_bytes(path, max_bytes=self.per_file_limit)
            except (OSError, ValueError) as error:
                violations.append(f"{source}: {error}")
                continue
            total += len(data)
            if total > self.aggregate_limit:
                violations.append("candidate set exceeds aggregate size limit")
                break
            try:
                content = data.decode("utf-8")
            except UnicodeDecodeError as error:
                violations.append(f"{source}: {error}")
                continue
            is_add = entry.get("op") == "add"
            raw_id = entry.get("cellId")
            cell_id = None if is_add else (raw_id if isinstance(raw_id, str) else None)
            cell_type = entry.get("cellType")
            entries.append(
                ReturnedStructureEntry(
                    is_add=is_add,
                    cell_id=cell_id,
                    cell_type=cell_type if isinstance(cell_type, str) else "",
                    relative_path=source,
                    content=content,
                )
            )
        if violations:
            raise WorkspaceBoundaryError(sorted(set(violations)))
        return entries
