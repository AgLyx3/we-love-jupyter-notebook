# Backlog

Last updated: 2026-07-22

Deferred work items that are understood but intentionally not being built yet.
Each entry records the problem, the design thinking so far, and the decision
still owed before implementation. Read alongside
`docs/notebook-agent-editor-spec.md` (product/architecture authority) and
`docs/engineering-handoff.md` (implemented state).

---

## 1. Downstream breakage vs. the agent's edit scope

Status: deferred (design decided in principle, not built)
Raised: 2026-07-22

### Problem

When an agent turn edits an in-scope **code** cell, the editor re-runs that cell
**and every downstream code cell** to validate the edit
(`agent_turns/service.py` downstream execution, "starts from the earliest edited
code cell"). If a downstream cell breaks, the whole turn is reported as
`failed` with the generic message "Cell execution failed"
(`kernel_execution/service.py`).

This is technically correct but misleading. Observed case: the agent deleted
`from collections import Counter` from the Setup cell. The Setup cell itself ran
fine ("Environment ready"), but a downstream Task cell using `Counter(...)`
raised `NameError`, so the turn showed as Failed. To the user it looked like a
false failure, because the cell they were watching worked.

### Root insight — two boundaries that don't line up

- **Edit boundary** — which cells the agent may change source of. This is the
  frozen turn scope; it is the permission the user granted.
- **Execution boundary** — which cells get re-run to validate the edit.
  Currently the edited cell plus everything downstream, which reaches cells the
  agent was never allowed to touch.

The failure is the execution boundary spilling past the edit boundary: the edit
stayed inside its permission, but validation judged it against out-of-scope
cells.

### Options considered

| Option | Behavior | Cost |
|---|---|---|
| A. Validate-only (current) | Edit in-scope; run downstream; fail if downstream breaks. Never edit the broken cell. | Failure looks like a false alarm; user is confused. |
| B. Auto-fix downstream | Agent also edits the downstream cell to repair it. | Silently violates the scope the user set. Rejected. |
| C. Notify + offer to expand scope | Edit in-scope only; detect the downstream break; surface which cell broke and offer to expand scope (and retry) or revert. | More flow/UI, but the boundary moves only when the user says so. |

### Decision (in principle)

- **The edit boundary stays sacred.** The agent never edits a cell outside the
  frozen scope without the user explicitly widening it. This rules out Option B
  as a default.
- **Distinguish two failure kinds** instead of the single "Cell execution
  failed":
  - **In-scope failure** — a cell the agent was allowed to edit errors. This is
    a real "the edit is wrong" failure; fail the turn as today.
  - **Out-of-scope breakage** — the edited cell runs fine but a downstream cell
    the agent could not touch now errors. Surface this as a distinct outcome
    (e.g. "applied with downstream breakage") that names the offending cell and
    its error, and offers **expand-scope-and-retry** or **revert** — not a plain
    failure.

This keeps the permission model clean: the agent notifies, the user decides
whether to widen the boundary. No hidden layer of behavior.

### Open decisions before building

1. Confirm in-scope vs out-of-scope downstream breaks should be treated
   differently (recommended), or whether any downstream break is equally a
   "failure".
2. For the out-of-scope case, decide between an active **"expand scope and let
   the agent fix it"** offer versus a passive notification ("this edit breaks
   cell N — revert or fix it yourself").

### Likely touch points when implemented

- Backend: downstream execution result handling and turn outcome classification
  (`agent_turns/service.py`, `kernel_execution/service.py`) to carry which cell
  failed, its error, and whether it was in or out of scope.
- Frontend: the turn failure banner in the agent chat panel, to name the
  offending cell + error and (optionally) offer expand-scope/retry.
- Spec: `docs/notebook-agent-editor-spec.md` scope/permission semantics.
