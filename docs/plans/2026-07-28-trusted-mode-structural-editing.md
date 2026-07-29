# Trusted Mode — Whole-Notebook Structural Editing

**Status:** Design — build-ready (brainstorming output + architect-review findings folded in; pre-implementation)
**Branch:** `feature/trusted-mode-structural-editing`
**Author:** design facilitated via `/brainstorming`, reviewed via `/architect-review`

---

## Understanding Summary

- Add a per-turn **Trusted** write mode alongside the existing **Blocking** (scoped)
  mode, selectable next to Edit/Plan and Model in the agent composer.
- **Blocking mode (unchanged):** the agent may rewrite only the *source* of cells in
  the frozen, hand-scoped editable set; the backend validator applies/rejects per cell.
- **Trusted mode (new):** the editable set is implicitly the **whole notebook**, and the
  agent may perform **full structural edits** — add, delete, reorder, change cell type —
  in addition to source edits.
- **Safety model = "keep the gate" (not "trust the diff"):** the backend boundary
  validator stays authoritative. Trusted *widens the allow-list* (whole notebook +
  structural ops) but still rejects malformed/invalid operations and anything outside
  the notebook. The core invariant — *the agent never owns notebook mutation* — holds.
- **Scoping UI:** editable/context sets persist across the toggle. In Trusted turns they
  become **attention-only highlights**; drag-and-drop stays enabled.
- **Review UX:** diffs visualize **cell-level add / delete / move** plus intra-cell source
  diffs. Revert granularity in Trusted = **whole-turn undo only** (per-cell revert stays
  for source edits; per-operation structural revert deferred to a separate branch).

## Target User / Why

The user already trusts the agent for a given change and wants to skip the per-turn
manual scoping burden, while keeping post-hoc review + undo as the safety net.

## Non-Goals

- Per-operation structural revert (deferred to a separate branch).
- Dropping the backend gate / trusting the agent's output unvalidated.
- Session-wide or global "always trusted" persistence (mode is strictly per-turn).
- Any change to OS posture — shell/terminal execution stays denied.
- Trusted behavior in Plan mode (Trusted is Edit-only).

## Assumptions

1. **"Trusted" = notebook-mutation scope only.** OS posture unchanged: V1 CLI agents
   still must not run shell commands; terminal execution stays denied.
2. **Trusted only has effect in Edit mode.** Trusted + Plan = write-nothing plan turn.
3. **Risky-cell execution approval flow is unchanged** by this feature.
4. **New cells get fresh nbformat IDs assigned by the backend** at apply time; the agent
   references existing cells by ID and uses the literal `"new"` placeholder for adds.
5. **Whole-turn apply is atomic** — a Trusted turn's structural + source changes apply as
   one unit and undo as one unit.
6. **Non-functional:** single local notebook, loopback-only, one active notebook in
   memory — no new scale/perf concern beyond diffing a whole-notebook change set. No new
   dependencies without approval.
7. **Process:** work on this worktree/branch off `main`; spec + Decision Log updated in
   the same change; `/code-review` before any PR (per AGENTS.md).

---

## Design

### Section 1 — Architecture & data flow (Trusted + Edit turn)

A new per-turn field `writeScope: "blocking" | "trusted"` rides alongside `mode` and
`model` in the turn request. Default `blocking` (today's behavior). `plan` ignores it.

Turn lifecycle:

1. **Freeze scope.** The backend freezes a snapshot of the **whole notebook** as the
   editable set — an ordered list of `(cellId, cellType)`. Editable/context highlights are
   recorded as *attention hints only*.
2. **Build workspace.** Write a per-cell source file for **every** cell, plus the
   **ordered manifest** the agent will edit. Full notebook still provided as read-only
   context.
3. **Launch adapter.** CLI runs with edit/write tools over the source files *and* the
   manifest; shell/terminal denied.
4. **Collect + derive.** After the agent exits, the backend reads the returned manifest +
   source files, diffs the manifest against the frozen snapshot by cell ID, and derives an
   ordered op list: `edit-source`, `add`, `delete`, `reorder`, `retype`. Move detection
   uses a stable-subsequence (LIS) pass — see Section 2 — so only genuinely relocated
   cells are marked `reorder`.
5. **Validate (gate).** Each op checked for notebook validity (well-formed nbformat, IDs
   resolvable, no ops outside the notebook). Invalid → hard error (no retry).
6. **Concurrency check + apply atomically.** The frozen snapshot carries a notebook
   version stamp; if live notebook state changed during the turn (manual edit or
   auto-save), the turn **aborts with a "notebook changed during turn" error and applies
   nothing** (optimistic-abort — see Section 5). Otherwise all ops apply as one unit → new
   notebook state.
7. **Diff + record.** Cell-level diff rendered; whole turn is one undo unit.

The core invariant holds: the agent writes only workspace files; the Notebook Document
domain derives, validates, and applies.

### Section 2 — Workspace protocol & manifest schema

Workspace layout for a Trusted turn:

```
workspace/
  notebook.readonly.ipynb        # full notebook, read-only context (unchanged)
  cells/
    cell_<id>.py  |  .md         # one source file per EXISTING cell (ext by type)
  manifest.json                  # the ordered, agent-editable structure file
  INSTRUCTIONS.md                # turn guidance (unchanged mechanism)
```

`manifest.json` (agent edits this):

```json
{
  "cells": [
    { "cellId": "abc123", "cellType": "code",    "source": "cells/cell_abc123.py" },
    { "cellId": "def456", "cellType": "markdown", "source": "cells/cell_def456.md" }
  ]
}
```

How each op is expressed by the agent:

- **Edit source:** change the referenced source file.
- **Add cell:** insert an entry with `"cellId": "new"`, a `cellType`, and a new source file
  the agent creates under `cells/`; position = array position.
- **Delete cell:** remove the entry (a leftover source file is ignored).
- **Reorder:** change entry order in the array.
- **Change type:** change `cellType` on an existing entry.

Backend handling (the gate):

- The **frozen original manifest** is held immutably in memory; the backend diffs the
  returned manifest against its own snapshot, never trusting a read-back for the original
  order (mirrors the existing "never trusts a manifest read back" rule).
- `"new"` entries get fresh nbformat IDs at apply time; duplicate/unknown IDs → hard error.
- Source-file content stays plain UTF-8, no metadata/outputs.

**Op derivation (precise semantics):**

- **Content is keyed by the entry, not the filename.** A cell's new source is read from the
  entry's `source` path, but identity is the entry's `cellId`; the agent pointing
  `cellId: abc123` at a different file just means "abc123 now has that content." Two entries
  referencing the **same** source file is allowed (a copy); each still resolves by its own
  `cellId`/`"new"`.
- **`cellType` is authoritative; the file extension is advisory.** A `cellType` that
  disagrees with the source file's extension is not an error — the manifest wins.
- **Move detection** compares the surviving cells' order against the frozen order via a
  longest-increasing-subsequence pass: cells on the LIS keep their position; only cells
  *off* the LIS are emitted as `reorder`. This prevents a single insert/delete from
  falsely marking every downstream cell as moved.
- **Zero-cell floor:** a Trusted turn may not reduce the notebook below one cell. A returned
  manifest with an empty `cells` array is a hard error (also indistinguishable from a lost
  file — see Section 8).

### Section 3 — Boundary validation (the gate) for structural ops

In Trusted mode the validator's job shifts from **scope-checking** to
**structural-validity + integrity checking**, run on the derived op list before anything
touches live state. All failures are hard errors (no retry — retry-on-violation stays
Blocking-only).

Rules:

- **Manifest well-formedness:** valid JSON; `cells` is an array; each entry has `cellId`,
  a supported `cellType` (`code|markdown|raw`), and a resolvable `source` under `cells/`.
- **ID integrity:** every non-`"new"` `cellId` exists in the frozen snapshot; no duplicate
  IDs; `"new"` is only ever a literal placeholder.
- **Containment:** referenced source files live under `cells/`; no path escapes, no writes
  to `notebook.readonly.ipynb` or outside the workspace (reuses the protected-path audit).
- **Type-change legality:** allowed; a change to a non-code type drops outputs/exec-count.
- **Derived-op sanity:** delete/reorder referencing a phantom ID → error (defensive).

On failure: the turn terminates with a user-visible validation error naming the offending
entry; **nothing is applied**. The workspace is preserved in in-session history for audit.

What is **not** checked: semantic correctness or whether an edit is a good idea — that is
the human's job via the diff. The gate guarantees a well-formed, in-bounds mutation, not a
*safe* one (consistent with existing Security Limits).

### Section 4 — Diff rendering & UI

Composer:

- A `writeScope` toggle (**Blocking** ⇄ **Trusted**) next to Edit/Plan. Disabled/greyed
  when `mode = plan`. A short inline caption states what Trusted grants ("agent may add,
  delete, reorder, and edit any cell; review the diff before keeping").
- On Trusted turns, editable/context gutter controls stay visible and functional but render
  as **attention highlights**, not permission gates. A one-line banner clarifies "whole
  notebook is editable this turn."

Diff view — cell-level status badges layered on the existing source diff:

- **Added** — new cell rendered with an "added" badge; whole body shown as insertion.
- **Deleted** — cell shown as a tombstone/struck row with a "deleted" badge (kept visible
  until the turn is undone or accepted).
- **Moved** — cell tagged with a "moved" badge and its from→to index; source diff still
  shown if it also changed.
- **Edited** — existing intra-cell source diff (unchanged).
- **Retyped** — badge noting `code → markdown` (etc.).

**Frontend data-model note:** the "deleted" tombstone requires the diff UI to render a
**ghost cell** that no longer exists in live notebook state. The current cell view model
assumes rendered-cell ⇒ live-cell; Trusted diffs break that assumption, so the diff view
needs an explicit ghost/tombstone cell state with its own tests. This is a real state-model
change, not a styling tweak.

Undo affordances: **whole-turn undo** control for the turn; **per-cell revert** offered
only on source-only edits (structural ops are all-or-nothing this iteration).

**Failure-path salvage:** on a hard validation or concurrency abort, nothing is applied, but
the workspace and derived diff are already retained in in-session history — the UI surfaces
them read-only so the user can manually salvage the agent's work rather than losing the
whole turn.

### Section 5 — Atomic apply, concurrency & whole-turn undo

- **Optimistic-abort concurrency.** The frozen pre-turn snapshot carries a notebook version
  stamp (monotonic revision counter or content hash). At apply time the backend compares
  the live notebook's stamp to the frozen one; if they differ — the user manually edited or
  auto-save fired during the turn — the turn **aborts and applies nothing**, surfacing a
  "notebook changed during turn" error. This is required because a Trusted turn's apply
  targets the *whole* notebook, so a stale-base apply would clobber the user's concurrent
  edits (blast radius = entire notebook, unlike Blocking's few scoped cells). We reject over
  auto-rebasing; the user re-issues the turn against fresh state. Locking live edits during
  the turn was considered and rejected as higher-friction.
- Apply builds the **entire next notebook state** off the derived op list and swaps it in
  as one transaction. A validation or apply error anywhere aborts the whole turn — no
  partial notebook.
- The **pre-turn notebook snapshot** is retained as the single undo unit. Whole-turn undo
  restores that snapshot verbatim (structure + source + outputs/exec-count), which cleanly
  handles adds (removed), deletes (restored at original index), reorders (restored), and
  retypes (restored) without per-op inverse logic.
- Redo is out of scope for this iteration (matches current undo-only behavior; confirm
  against existing capability during implementation).
- Kernel state is **not** part of undo — restoring source/structure does not roll back
  executed side effects (documented limitation, same as today).

### Section 6 — CLI adapter tool grants for Trusted turns

- The adapter is launched with write/edit tools scoped to the workspace `cells/` dir and
  `manifest.json`. `notebook.readonly.ipynb` remains read-only via the protected-path
  audit.
- Shell/terminal tools stay **denied** — Trusted changes *notebook* scope only, never OS
  scope. An adapter that cannot start with terminal execution denied is unsupported.
- Instructions (`INSTRUCTIONS.md`) explicitly tell the agent: the whole notebook is
  editable this turn; edit `manifest.json` to add/delete/reorder/retype; use `"new"` for
  adds; do not treat edit permission as an obligation to change code. This is guidance
  only; the enforced boundary is the backend gate.

**Security posture (must update README/spec Security Limits in the same change):** Trusted
mode grants a capability Blocking mode does not — the agent can **introduce new executable
code cells the user never scoped, and reorder execution order.** OS posture is unchanged
(no shell, loopback-only), but review-before-run becomes load-bearing: the agent can author
code that later runs under the user's permissions. The risky-cell execution approval flow is
unchanged and still gates execution of any new/edited cell.

### Section 7 — Testing strategy

- **Backend unit:** manifest diff → op derivation (each op type + combinations);
  validator hard-fail cases (malformed JSON, dup IDs, unknown ID, path escape, bad type);
  `"new"` ID assignment; atomic apply + whole-turn undo round-trip.
- **Boundary tests:** attempt writes outside `cells/`, edits to `notebook.readonly.ipynb`,
  and a manifest referencing a phantom ID — all rejected with nothing applied.
- **Concurrency:** a manual edit / auto-save during a Trusted turn → optimistic-abort,
  nothing applied, "notebook changed during turn" surfaced, workspace retained for salvage.
- **Move detection:** a single insert at the top marks *only* the inserted cell as added
  (no false "moved" on downstream cells); a genuine relocation is the only cell badged
  `reorder` (LIS pass).
- **Zero-cell floor:** an empty returned manifest is a hard error, nothing applied.
- **Security:** a Trusted-added executable cell still requires risky-cell approval before it
  can run.
- **Mode isolation:** a Blocking turn still rejects out-of-scope edits and still retries
  ×2; a Trusted turn does neither.
- **Frontend unit:** `writeScope` toggle state, plan-mode disabling, diff badges for
  add/delete/move/retype.
- **E2E (Playwright):** a Trusted turn that adds, deletes, reorders, and edits cells;
  verify diff badges and whole-turn undo restores the original notebook.

### Section 8 — Edge cases

- **Empty manifest returned / manifest deleted by agent:** treat as no structural change
  is *not* safe → hard error (can't distinguish "delete everything" from "lost file");
  require the manifest to be present and parseable.
- **Agent adds a cell but leaves no source file:** validation error (unresolvable
  `source`).
- **Duplicate `"new"` entries:** each is a distinct add; each gets its own fresh ID.
- **Reorder + edit + delete on the same turn:** all derived from one manifest diff, applied
  atomically in snapshot order.
- **Notebook with zero cells → agent adds first cell:** supported (empty frozen snapshot).
- **Cell type change on a cell with outputs:** outputs/exec-count dropped on apply.
- **Very large notebook:** whole-notebook source-file materialization is O(n cells); fine
  for local single-notebook scale.

---

## Decision Log

| # | Decision | Alternatives | Why |
|---|----------|-------------|-----|
| 1 | Trusted unlocks full structural editing (add/delete/reorder/retype) | Source-only wider set; in-between | Genuine capability, matches intent |
| 2 | Keep the backend gate, widen allow-list | Drop gate, trust agent + human diff | Preserves "agent never owns mutation" |
| 3 | Per-turn `writeScope` toggle | Session-wide; global flag | Keeps turn-level permission invariant |
| 4 | Editable/context persist across toggle; attention-only in Trusted | Hide scoping UI on Trusted turns | UX continuity; drag/drop survives toggle |
| 5 | Whole-turn undo only for structural | Per-op structural revert | YAGNI first cut; per-op on separate branch |
| 6 | Retry-on-violation stays Blocking-only | Apply to Trusted too | Trusted has no out-of-scope cell to retry |
| 7 | Protocol: ordered manifest + per-cell source files (Approach 1) | (2) all-cells + ops log; (3) flattened doc + inferred diff | Precise, backend-derived ops; fits the gate |
| 8 | Optimistic-abort on mid-turn notebook change | Lock live edits; auto-rebase apply | Whole-notebook blast radius; reject-over-clobber is safe + low-friction |
| 9 | LIS-based move detection | Naive index compare | Avoids false "moved" badges on downstream cells after an insert/delete |
| 10 | Zero-cell floor (turn can't empty the notebook) | Allow empty manifest | Empty manifest is indistinguishable from a lost file; reject for safety |

### Recorded alternative protocols (fallbacks if Approach 1 hits trouble)

- **Approach 2 — all-cells source files + explicit `operations.jsonl`.** Structural intent
  as discrete append-only records (`add`/`delete`/`move`). Pros: intent explicit and
  auditable per-op. Cons: two channels can disagree (stale source file for a deleted cell);
  multi-op ordering semantics must be defined.
- **Approach 3 — single flattened notebook doc, backend infers the diff.** Agent rewrites
  one ID-tagged document; backend matches IDs to infer ops. Pros: most natural for an LLM.
  Cons: ambiguous inference (dropped ID = delete+add); weak precision conflicts with the
  gate; whole-notebook rewrite is token-heavy and drop-prone.

## Multi-Agent Review — Resolutions & Decision Log additions

Phase 2 review (Skeptic, Constraint Guardian, User Advocate) surfaced material defects.
Designer resolutions below; Arbiter verdict at the end.

### Accepted — design-changing (must land before/with build)

| # | Objection (reviewer) | Resolution |
|---|----------------------|-----------|
| R1 | **`"new"` sentinel collides with a real cell id `new`** — `_VALID_CELL_ID` accepts `"new"`; silent delete+add corruption (Skeptic 3) | **Drop the in-band `"new"` string.** An add is an entry with **no `cellId` key** (or `cellId: null`) plus `"op": "add"`. Backend never interprets any id string as a sentinel. |
| R2 | **Salvage promise is unimplementable** — `_run` `finally` unconditionally `destroy(workspace)` before the raise, so nothing remains to surface (Skeptic 1) | Capture the collected manifest + source contents + derived ops into the turn record **before** `destroy` on failure; the UI surfaces *that captured intent*, not the deleted workspace. |
| R3 | **No-retry ⇒ routine total loss** of large edits on one malformed-JSON/typo (Skeptic 2, UA 5) | **Refine Decision #6:** keep "no retry for scope violations" (meaningless in Trusted), but **add a bounded ×2 structural-format retry** that feeds the parse/validation error back as a correction string (mirrors the Blocking correction loop). Format errors are correctable; scope violations are not. |
| R4 | **Trusted auto-runs the whole notebook** — insert-at-top ⇒ downstream re-exec of everything with side effects; reorder-only/retype-only leave stale/never-run state; undo can't roll back side effects (Skeptic 9/10/11, Guardian 6) | **Trusted structural turns apply structure only and do NOT auto-execute.** Execution is user-initiated after review. This resolves 9/10/11 together and makes whole-turn undo clean (the turn itself runs nothing). |
| R5 | **Applier keys on revision only** — session replacement mid-turn could reset `_revision` and coincidentally match ⇒ apply to the *wrong notebook* (Guardian 5, Skeptic 5) | New `apply_structural_changes_under_lease` checks **session_id AND revision** (via `check_snapshot_preconditions`), not revision alone. Also correct Decision #8 rationale: the coordinator lease is the *primary* guard; the revision/session check is defense-in-depth (the abort branch is rarely hit because the lease already blocks mutations). |
| R6 | **Toggle stickiness** (UA 2) | **Rejected auto-revert per user decision.** The toggle **persists its last-selected write scope across turns** (sticky): choose Trusted and every subsequent turn stays Trusted until switched back to Blocking, and vice-versa. Initial default is Blocking. The accidental-sticky-Trusted risk UA raised is instead mitigated by the **send-time signal (R9)**, **visual distinction of attention-only marks (R8)**, and the **persistent provenance marker (R7)** — not by auto-revert. |
| R7 | **Agent-authored cells run later with no provenance** — "added" badge is review-time only; Run-All executes unread agent code (UA 8) | Add a **persistent `metadata.agent_authored` marker** on cells the agent creates, with a subtle persistent UI indicator until the user edits/accepts-runs it. Makes review-before-run reliable. |
| R8 | **Editable/context marks silently reverse meaning** in Trusted (UA 3); **ghost tombstones + live drag/drop = index bugs** (Skeptic 13, UA 7) | Visually distinguish attention-only marks from permission gates in Trusted; **disable live editing/drag-drop while a structural diff is pending review** (review is a resolve-or-undo state), removing the ghost/live-index reconciliation hazard. |
| R9 | **No send-time signal** the whole notebook can change (UA 1) | Trusted turns show an explicit send-time affordance (button label reflects Trusted / a first-use confirm), not just an ambient banner. |

### Accepted — security hardening (spec the guards concretely, do not just "keep them")

| # | Objection | Resolution |
|---|-----------|-----------|
| R10 | **Writable `manifest.json` read is unguarded** (Guardian 1) | Read the agent manifest through the **same hardened primitive** as cell files: `O_NOFOLLOW`, `S_ISREG`, `st_nlink == 1`, size cap. |
| R11 | **Widened `cells/` allow-list is hardlink-blind** — `O_NOFOLLOW`/symlink checks miss hardlinks (Skeptic 6) | Reject any file under `cells/` with `st_nlink != 1`. |
| R12 | **`source` containment is prose only** — `startswith` leaks `cells/../x` (Skeptic 7, Guardian 2) | Containment is `Path.resolve()` + `is_relative_to(cells/)`, applied to every agent `source` string pre-open; add test `source: "cells/../notebook.readonly.ipynb"`. |
| R13 | **Aggregate cap bypassed by unreferenced files** under `cells/` (Guardian 3) | Cap **total `cells/` bytes and file count** (not just referenced files); reject over-budget workspaces. |
| R14 | **Two parallel security pipelines drift** + confusingly similar manifest names (Guardian 7) | Extract one shared hardened `read_workspace_file` helper used by both Blocking and Trusted collect/audit. **Rename** the writable file to `structure.json` (agent-owned) to avoid collision with the read-only `AGENT_CELL_MANIFEST.json`. |
| R15 | **Shared-source "copy" duplicates content silently** (Skeptic 14) | Disallow two entries referencing the same `source` file; each entry references a distinct file (an intended copy writes identical content to two files). |

### Accepted — smaller / documented

- R16 (Skeptic 4): a cancel that loses the race to a committed atomic apply yields a
  **completed, undoable** turn (not a "cancelled" label implying nothing happened); undo is
  always offered on an applied turn.
- R17 (Skeptic 8): move badges use a **deterministic** LIS (patience-sort, prefer lower
  original index) and are labeled advisory — the applied order is authoritative.
- R18 (Skeptic 12, Guardian 10): count `structural_ops` in `_history_size`; note the retained
  checkpoint is bounded to the single latest applied turn; correct the design's "no perf
  concern" line to "bounded whole-notebook copies per turn."
- R19 (Guardian 4): the 5 MB gate error (committed notebook = sources + preserved outputs)
  must be a clear, distinct user message, not a generic failure.
- R20 (UA 4/12): keep per-op structural revert deferred (Decision #5), but the UI must set the
  expectation up front — structural changes are **accept-all / undo-all**, not per-cell.
- R21 (UA 6/10/11): plain-language errors + why-Trusted-is-disabled-in-Plan tooltip + a
  one-time Trusted explainer on first use.

### Rejected / not adopted

- None outright. The one **governance gate** (Guardian 8) is escalated, not resolved by the
  designer: per AGENTS.md, weakening "only cells in the editable set may receive agent writes"
  requires **explicit user approval**, and the spec + Decision Log must land in the same change.
  This is a hard precondition on *building*, not a design flaw.

### Decision Log additions

| # | Decision | Why |
|---|----------|-----|
| 11 | Adds carry no `cellId` (+`op:"add"`); no string sentinel | Kills the `"new"`-collision corruption (R1) |
| 12 | Bounded ×2 structural-format retry; scope-retry still N/A | Prevents routine total loss (R3) |
| 13 | Trusted applies structure only; execution is user-initiated | Resolves whole-notebook auto-reexec + undo/side-effect gap (R4) |
| 14 | Applier checks session_id + revision | Prevents wrong-notebook apply on session replacement (R5) |
| 15 | Write-scope toggle is **sticky** (persists last-selected scope across turns; initial default Blocking) | User decision; accidental-Trusted risk covered by send-time signal + attention-mark distinction + provenance marker instead (R6) |
| 16 | `metadata.agent_authored` provenance marker | Makes review-before-run reliable (R7) |
| 17 | One shared hardened file-read helper; rename writable file to `structure.json` | Prevents boundary drift + name confusion (R14) |
| 18 | Disable live edits while a structural diff is pending | Removes ghost/live index hazard (R8) |

### Arbiter verdict: **REVISE → conditionally APPROVED**

The original design was **not build-ready** (HIGH defects: `"new"` corruption, unimplementable
salvage, whole-notebook auto-reexec, revision-only apply). With resolutions R1–R21 folded in,
the design is architecturally sound and the objections are resolved or consciously deferred
(per-op revert). **Two hard preconditions remain before backend build may start:**

1. **Explicit user approval** to weaken the editable-set permission guarantee (AGENTS.md
   governance, Guardian 8), with the spec + Decision Log landing in the same change.
2. The security-hardening resolutions (R10–R15) are treated as **build requirements**, not
   follow-ups — they are the boundary itself.

Until precondition 1 is granted, this stays at design; no boundary-weakening backend code is written.

## Spec impact (must land in the same change)

Per AGENTS.md, `docs/notebook-agent-editor-spec.md` must be updated alongside
implementation: the Permission Model, Turn Scope, Agent Workspace Protocol, Boundary
Validation, and CLI Agent Execution Policy sections all change for Trusted mode, and a
Decision Log entry for the write/edit-boundary change is required.
