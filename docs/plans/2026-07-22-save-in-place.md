# Plan: Save In Place (backend)

Implements the spec's `Save In Place` (under `## Workspace Root`) in the
Notebook Document domain. Backend only; frontend wiring is a later slice.

Branch: `feature/save-in-place` (worktree). Deps live in the main repo `.venv`.

Test command (run from the worktree; PYTHONPATH makes the worktree source win
over the editable install that points at the main repo):

    # Run from the worktree root. PYTHONPATH="$PWD" makes the worktree source win
    # over the editable install (which points at the main repo). Use the main
    # repo's virtualenv Python.
    PYTHONPATH="$PWD" <main-repo>/.venv/bin/python -m pytest backend/tests -q

## Task 1 — OpenNotebookFromPath + models + containment
Add `OnDiskBaseline` (path, mtime_ns, content_hash) and `notebook_path` to
`NotebookSnapshot`; exceptions `NotebookPathError`, `ExternalModificationConflict`.
Add `NotebookDocumentService.open_notebook_from_path(path, *, workspace_root=None,
expected_session_id=None, expected_revision=None)`: canonicalize + containment
(reject `..`/symlink escape when a root is given), require an existing regular
`.ipynb` file, size cap, reuse the import parse/validate/normalize path, capture
the baseline from the ORIGINAL bytes (before normalization), set notebook_path.
Reset notebook_path/baseline on close and replacement.

## Task 2 — SaveNotebookToDisk
`save_notebook_to_disk(*, expected_session_id, expected_revision)`: require a
notebook_path (else `NotebookPathError`), session/revision precondition,
serialize a consistent committed snapshot, guard with the on-disk baseline
(hash authoritative → `ExternalModificationConflict` on mismatch, never clobber),
atomic temp-file + `os.replace`, refresh baseline, clear dirty, revision unchanged.

## Task 3 — API routes + DTO
`POST /notebooks/open` and `POST /notebooks/save` in `notebook_routes.py`,
expose `notebookPath` in the notebook response DTO, map new exceptions to
`400/409/404`, tests in `test_notebook_api.py`.

Each task: TDD, run the full backend suite, commit in the worktree. Two-stage
review (spec compliance, then code quality) after each.
