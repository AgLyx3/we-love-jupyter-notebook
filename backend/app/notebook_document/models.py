from __future__ import annotations

from dataclasses import dataclass
from typing import Any


class NotebookDomainError(Exception):
    code = "notebook_error"
    message = "Notebook operation failed"
    status_code = 400

    def __init__(self, message: str | None = None, **details: Any) -> None:
        super().__init__(message or self.message)
        self.message = message or self.message
        self.details = details


class NotebookImportError(NotebookDomainError):
    code = "invalid_notebook"
    message = "Notebook file is invalid"
    status_code = 422

    def __init__(self, message: str | None = None, *, code: str | None = None) -> None:
        super().__init__(message)
        if code is not None:
            self.code = code


class NotebookSizeError(NotebookImportError):
    code = "notebook_too_large"
    message = "Notebook exceeds the size limit"
    status_code = 413

    def __init__(self, max_bytes: int) -> None:
        NotebookDomainError.__init__(self, maxBytes=max_bytes)


class NotebookNotLoaded(NotebookDomainError):
    code = "notebook_not_loaded"
    message = "No notebook is loaded"
    status_code = 404


class CellNotFound(NotebookDomainError):
    code = "cell_not_found"
    message = "Notebook cell was not found"
    status_code = 404

    def __init__(self, cell_id: str) -> None:
        super().__init__(cellId=cell_id)


class RevisionConflict(NotebookDomainError):
    code = "revision_conflict"
    message = "Notebook revision does not match"
    status_code = 409

    def __init__(self, current_revision: int) -> None:
        self.current_revision = current_revision
        super().__init__(currentDocumentRevision=current_revision)


class SessionConflict(NotebookDomainError):
    code = "session_conflict"
    message = "Notebook session does not match"
    status_code = 409

    def __init__(self, current_session_id: str) -> None:
        super().__init__(currentSessionId=current_session_id)


class ReplacementPreconditionRequired(NotebookDomainError):
    code = "replacement_precondition_required"
    message = "Replacing the active notebook requires its session and revision"
    status_code = 409


class NotebookPathError(NotebookDomainError):
    code = "notebook_path_invalid"
    message = "Notebook path is not a readable .ipynb file within the workspace root"
    status_code = 400


class ExternalModificationConflict(NotebookDomainError):
    code = "external_modification_conflict"
    message = "Notebook file changed on disk since it was opened"
    status_code = 409


class MutationConflict(NotebookDomainError):
    code = "mutation_conflict"
    message = "Another notebook mutation is active"
    status_code = 409

    def __init__(
        self,
        *,
        active_operation_type: str,
        active_operation_id: str,
    ) -> None:
        self.active_operation_type = active_operation_type
        self.active_operation_id = active_operation_id
        super().__init__(
            activeOperationType=active_operation_type,
            activeOperationId=active_operation_id,
        )


@dataclass(frozen=True)
class MutationLease:
    token: str
    operation_type: str
    operation_id: str


@dataclass(frozen=True)
class OnDiskBaseline:
    path: str
    mtime_ns: int
    content_hash: str


@dataclass(frozen=True)
class NotebookSnapshot:
    session_id: str
    filename: str
    notebook: dict[str, Any]
    revision: int
    dirty: bool
    last_mutation_owner: str | None
    notebook_path: str | None = None


@dataclass(frozen=True)
class NotebookCloseResult:
    closed_session_id: str
    cleanup_errors: tuple[str, ...] = ()
