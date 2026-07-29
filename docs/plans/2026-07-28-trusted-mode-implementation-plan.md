# Trusted Mode — Implementation Plan

**Companion to:** `2026-07-28-trusted-mode-structural-editing.md` (validated design)
**Branch:** `feature/trusted-mode-structural-editing`
**Status:** implementation plan (grounded in current code), pre-build

## Grounding — what already exists (verified against code)

- **Whole-turn undo is already whole-notebook.** `AgentTurnService.start()` stores
  `turn.checkpoint = deepcopy(snapshot.notebook)`; `undo()` calls
  `documents.restore_under_lease(notebook=turn.checkpoint, ...)`. Structural undo needs
  **no new inverse logic** — the checkpoint restore already reverts adds/deletes/reorders.
- **Optimistic-abort already exists.** Apply guards on
  `expected_revision=scope.notebook_revision` → `RevisionConflict`. An active turn holds the
  coordinator lease (`documents.coordinator.acquire`), so manual `update_cell_source` edits
  raise `MutationConflict` during a turn; `save_notebook_to_disk` holds only the RLock and
  does not bump revision. Concurrency guard = existing revision check; no new lock needed.
- **Apply is currently source-only.** `apply_source_changes_under_lease` mutates
  `cell["source"]` for existing cells only. Structural ops need a **new applier**.
- **Boundary validation is scope-membership.** `BoundaryValidator.validate` checks the
  authoritative manifest equals the frozen editable set and candidates ⊆ scope. Trusted
  needs a **structural derivation** instead.
- **Workspace writes only editable cells** and marks `notebook.ipynb` +
  `AGENT_CELL_MANIFEST.json` + `INSTRUCTIONS.md` as protected (chmod 444, baseline-hashed).
  `WorkspaceAuditor.audit` flags any **undeclared path** — so agent-created files need an
  explicitly widened allow-rule for Trusted.

## Design deltas that map to code

| Design element | Code touch point |
|---|---|
| `writeScope: blocking\|trusted` per turn | `api/agent_turn_routes.py` `StartTurnRequest`; `AgentTurn`; `AgentTurnService.start` |
| Whole-notebook frozen scope | `turn_scope/models.py` `ScopeSelection`/`FrozenTurnScope` (+`write_scope`); `turn_scope/service.freeze` |
| All-cells + agent-writable `manifest.json` | `agent_workspace/workspace_builder.py`; `agent_workspace/models.py` |
| Allow agent-created files under `cells/` | `agent_workspace/workspace_auditor.py` |
| Manifest-diff → structural ops | new `boundary_validation/structural_validator.py` |
| Structural atomic apply | new `notebook_document/service.apply_structural_changes_under_lease` |
| Trusted turn branch (no retry) | `agent_turns/service.py` `_run` |
| Structural ops in history/diff | `AgentTurn` (+`structural_ops`); `serialize_turn` |
| Composer toggle + diff badges | `frontend/src/agentChat/AgentChatPanel.tsx`, `turnScope/`, `notebook/cellDiff.ts`, `NotebookCell.tsx`, `NotebookView.tsx`, `api/client.ts` |

## Review-driven changes folded in (see design doc "Multi-Agent Review")

- Adds carry **no `cellId`** (`{"op":"add", cellType, source}`); the string `"new"` is never a
  sentinel (R1). The agent-writable file is renamed **`structure.json`** to avoid confusion
  with the read-only `AGENT_CELL_MANIFEST.json` (R14).
- **Bounded ×2 structural-format retry** feeds parse/validation errors back as a correction
  (R3); scope-retry remains N/A.
- **Trusted applies structure only; no auto-execution** — the user runs cells after review
  (R4). Phase 3 drops the downstream-execution wiring for Trusted turns.
- New applier checks **session_id + revision** (R5). Cancel losing the race to a committed
  apply ⇒ **completed + undoable**, never a misleading "cancelled" (R16).
- Write-scope toggle is **sticky** — it persists the last-selected scope across turns
  (initial default Blocking); no auto-revert (R6). Agent-created cells get
  `metadata.agent_authored = true` (R7).
- Security: one shared hardened `read_workspace_file` (`O_NOFOLLOW`, `S_ISREG`, `st_nlink==1`,
  size) used by both paths (R10/R11/R14); `source` containment via `resolve()`+`is_relative_to`
  (R12); **total `cells/` bytes + file-count cap** (R13); distinct `source` per entry (R15).
- Deterministic LIS for move badges (R17); `structural_ops` counted in `_history_size` (R18).

## Phasing (each phase independently testable)

### Phase 0 — Request plumbing (no behavior change)
- Add `write_scope: Literal["blocking","trusted"] = "blocking"` to `StartTurnRequest`
  (alias `writeScope`), `AgentTurn.write_scope`, `AgentTurnService.start(...)`, and
  `serialize_turn`. Trusted still runs the blocking path until Phase 3.
- **Tests:** request parses default + trusted; serializer round-trips `writeScope`.

### Phase 1 — Structural workspace protocol
- `agent_workspace/models.py`: add `ManifestCell(cell_id|"new" sentinel via cell_id, cell_type,
  relative_path, original_source, index)` and a Trusted manifest shape; keep the blocking
  `WorkspaceManifest` intact.
- `turn_scope`: carry `write_scope` on `FrozenTurnScope` so the builder can branch.
- `workspace_builder.build`: on Trusted, write **every** cell to `cells/cell_<id>.<ext>`,
  write an **agent-editable** `manifest.json` (ordered `{cellId, cellType, source}` list, NOT
  chmod 444, NOT in `baseline_hashes`), keep `notebook.readonly.ipynb` protected, and write a
  structural `INSTRUCTIONS.md` (edit `manifest.json`; `"new"` for adds; no shell; edit optional).
- **Tests:** all cells materialized; `manifest.json` writable; protected set correct.

### Phase 2 — Structural collect + validate
- `workspace_auditor`: Trusted-aware `collect` — parse agent `manifest.json`, read each
  referenced source file, allow **any regular file directly under `cells/`** and a writable
  `manifest.json`; keep protected-path + size + symlink/`O_NOFOLLOW` guards. Return the raw
  ordered entries.
- new `structural_validator.py`: diff returned manifest vs frozen snapshot by `cellId` →
  ordered ops (`edit-source`, `add`, `delete`, `retype`, `reorder` via **LIS** on surviving
  order). Validate: valid JSON; `cellType ∈ {code,markdown,raw}`; non-`"new"` ids exist in
  snapshot; no duplicate ids; source paths contained under `cells/`; **zero-cell floor**.
  Hard-fail with a `WorkspaceBoundaryError`-style error (no retry). Output = full next
  ordered cell list + op list for the UI.
- **Tests:** each op type + combos; LIS (top insert → only inserted badged); each failure
  mode; empty manifest → hard error.

### Phase 3 — Structural apply + turn wiring
- `notebook_document/service.apply_structural_changes_under_lease(*, next_cells, expected_revision,
  owner, lease)`: build candidate from the ordered next-cell list; assign fresh ids to `"new"`
  (reuse `_new_cell_id`, dedupe vs used); **preserve** `outputs`/`execution_count` for cells whose
  id survives; **empty** them for added cells; **drop** them on retype away from `code`; validate
  nbformat; size guard; bump revision. Source-edit-keeps-outputs parity with existing apply.
- `agent_turns/service._run`: branch on `turn.write_scope`. Trusted = **single attempt** (no
  retry loop), build Trusted workspace, run adapter with `acceptEdits`, structural collect +
  validate, then `apply_structural_changes_under_lease`. Reuse `turn.checkpoint` for undo.
  Downstream execution runs changed **code** cells (edited + added), keyed by post-apply ids.
- `AgentTurn.structural_ops` + `serialize_turn`.
- **Tests:** trusted add/delete/reorder/retype/edit end-to-end; undo restores original;
  mid-turn revision change → `RevisionConflict`, nothing applied; added code cell still gated by
  risky-cell approval before running.

### Phase 4 — Frontend
- `api/client.ts`: send `writeScope`; types for `structuralOps`.
- `AgentChatPanel.tsx`: Blocking/Trusted toggle, disabled when `mode=plan`, caption.
- `turnScope/`: in Trusted, editable/context render as attention highlights + a "whole
  notebook editable this turn" banner (drag/drop still works).
- `notebook/`: `cellDiff.ts` + `NotebookCell.tsx`/`NotebookView.tsx` render added / deleted
  (ghost tombstone) / moved / retyped badges; per-cell revert only on source edits.
- **Tests:** toggle state + plan disabling; each diff badge; ghost-cell render.

### Phase 5 — Spec + docs (same change)
- Update `docs/notebook-agent-editor-spec.md`: Permission Model, Turn Scope, Agent Workspace
  Protocol, Boundary Validation, CLI Execution Policy + a Decision Log entry.
- Update README Security Limits: Trusted mode permits agent-introduced executable cells;
  review-before-run is load-bearing; risky-cell approval unchanged.

## Verification
- Per phase: `.venv/bin/python -m pytest backend/tests -q` (focused files first);
  `npm test -- --run`; `npm run build`.
- End: `npm run test:e2e` (Trusted add/delete/reorder/edit + undo). Manual smoke uses the
  **real Claude CLI**, never `--test-agent`.

## Risk notes carried from architect-review
- Auditor must allow agent-created files under `cells/` without opening a path-escape hole —
  keep `O_NOFOLLOW`, regular-file, parent-dir-resolve, and size checks; only the *allow-list
  breadth* widens (any regular file directly under `cells/`), not the safety checks.
- Move-detection must be LIS-based, not naive index compare (design Section 2).
- Downstream execution after structural apply must key off post-apply ids for added cells.
