# Design: Agent Thread Memory

Status: proposed
Date: 2026-07-31
Branch: `worktree-agent-thread-memory-design`
Related: `docs/plans/2026-07-28-per-operation-accept-undo.md` (the operation ledger)

---

## 1. Where we are today

Every agent turn is a cold start.

`AgentWorkspaceBuilder.build()` creates a fresh `tempfile.mkdtemp()`, writes the
notebook plus `INSTRUCTIONS.md`, runs the agent, and destroys the directory.
`INSTRUCTIONS.md` is built from exactly one prompt:

```python
instructions = [scope.prompt, "", *notebook_context, ...]   # workspace_builder.py:101
```

No prior prompts, no prior replies, no record of prior operations.

Statelessness is not an oversight — it is asserted at the process boundary.
`ClaudeAgentAdapter.run()` passes `--no-session-persistence`
(`adapters.py:137`), so the CLI does not even write a session file that could be
resumed.

The only backward-looking channel that exists is `correction`
(`workspace_builder.py:121`), which feeds `"Previous boundary violation to
correct: ..."` back in. That is scoped to retries *within* a single turn. The
plumbing for feeding history back exists; it was never wired to conversation
history.

### 1.1 The gap this causes

What the agent knows each turn is: the current notebook, the scoped cells, and
one message.

Crucially, **kept edits are not the problem.** The agent receives a full
read-only `notebook.ipynb` every turn, so anything it changed and you accepted
is visible in the source it can already read. What is invisible is:

1. That *it* was the author of the current content.
2. Anything it proposed that you **undid** — that content exists nowhere in the
   notebook, in any form.

(2) is the actual defect. It is why the agent re-proposes work you have already
rejected, and it cannot be fixed by showing it the notebook more clearly.

### 1.2 The interface already promises what the backend lacks

`frontend/src/agentChat/AgentChatPanel.tsx` renders turns as a chat. The user is
looking at a conversation. The backend has no conversation. The mismatch, not
the statelessness, is what makes the behaviour feel like a bug.

### 1.3 The storage already exists

`AgentTurnService` retains, per turn: `prompt`, `final_output`, `changes` (with
`previous_source` and `next_source`), `error`, `applied_revision`, `state`, and
`created_at` — pruned to 50 terminal turns / 2 MB
(`service.py:582`, `_prune_history_locked`).

This design adds almost no storage. It adds a **projection** of storage that
already exists, plus one genuinely missing field (§5.1).

---

## 2. Understanding summary

- **What:** a per-turn *thread memory feed* injected into `INSTRUCTIONS.md`, so
  the agent begins a turn knowing what it already did in this notebook session —
  in particular, what it proposed that the user undid.
- **Why:** rejected work is invisible in the notebook. That is the blind spot
  behind "it forgets what it did."
- **Who:** a user working a notebook across several turns — the retry/refine loop.
- **Shape:** a chronological thread, like a normal Claude conversation. Not
  per-cell, not scope-filtered.
- **Content per entry:** the user's prompt, which cell, what the operation did,
  status (kept / undone / failed), the full diff for undone operations, and the
  agent's reply *only* for turns that changed nothing.
- **Lifetime:** in-memory, session-scoped, cleared by the existing
  `_on_session_replaced`. Nothing new is written to disk.
- **Non-goal:** execution errors and tracebacks. Different lifetime, different
  source of truth — its own design.

---

## 3. Assumptions

| # | Assumption | Status |
|---|---|---|
| A1 | **Budget:** last 10 turns, feed capped at ~16 KB, evicted from the oldest end only — never the middle — with an explicit `(earlier turns omitted)` marker. Individual diffs truncated at ~60 lines with an elision marker. Replies capped at ~500 chars, except plan-mode turns at ~1500 (§4.5). Replayed prompts carry the lead only (§4.6). | proposed |
| A2 | **Performance:** built in-process from existing `AgentTurn` records. No I/O, no new process, sub-millisecond. Turn latency unchanged. | proposed |
| A3 | **Privacy:** undone source already lives in memory in `change.previous_source` / `next_source`. Writing it into the per-turn temp workspace and passing it to the CLI sends nothing that was not already sent when the code was first proposed. No new class of data crosses a boundary. | **confirmed by user** |
| A4 | **Reliability:** the feed is advisory and best-effort. If history is missing, pruned, or raises, the turn runs exactly as it does today. A memory failure must never fail a turn. | proposed |
| A5 | **Staleness:** status is derived at build time, never cached in the feed — the same rule as `stale` in the per-op design (§3.1.1 of that doc). | proposed |
| A6 | **Scope integrity:** the *frozen scope is unchanged*. Memory widens what the agent can **recall**, never what it can **write**. The boundary validator and workspace auditor are untouched. | proposed |
| A7 | **Ownership:** `workspace_builder.py`, one read-only query method on `AgentTurnService`, and one new field on `AgentTurn`. No new module, no new persistence. | proposed |
| A8 | **Dependency:** assumes the per-op ledger (C1) eventually lands. Until then, "operation" degrades to the whole-cell entry in `turn.changes`. P0 and P1 do not have to wait for it. | proposed |

### 3.1 Inherited constraint

**C1 (from the per-op branch):** accept/undo is **per-operation only**; per-cell
revert goes away. When an operation spans an entire cell it still presents as
one operation, with the highlight covering the whole cell. This design describes
past work in units of operations, so it inherits C1.

---

## 4. Decisions

### 4.1 The feed is hand-built, not `--resume`

The Claude CLI can remember on its own: normally it saves each `-p` run to a
session file and a later run replays it with `--resume <session_id>`. Our
adapter disables this with `--no-session-persistence`.

Once the design moved to a thread shape (§4.2), `--resume` became the obvious
candidate — it *is* a Claude thread. It was still rejected:

- **False memory.** The CLI transcript records its `Edit` tool call *succeeding*.
  It has no representation of the user undoing it afterward. A resumed thread
  would confidently believe rejected code is live. That is worse than amnesia,
  and it defeats the single thing this design exists to fix.
- **Disk.** `--resume` requires dropping `--no-session-persistence`, which writes
  transcripts under `~/.claude`. The chosen lifetime is session-only, in-memory.
- **Stale paths.** Each turn is a fresh `mkdtemp`; a resumed transcript
  references workspace directories that no longer exist.
- **No filtering.** We could not exclude anything even if we wanted to.

A hybrid — resume the session *and* inject undo corrections — inherits the disk
persistence and the stale paths while duplicating the hand-built work anyway.

**`--no-session-persistence` stays.** The statelessness is not the bug; the
missing undo record is.

### 4.2 Thread-shaped, not cell-shaped

An earlier iteration filtered memory to operations touching cells in the current
turn's frozen scope, grouped by cell. That was reversed in favour of a plain
chronological thread — "like how you chat with Claude in a thread."

Consequences accepted:

- A scoped turn now sees operations on cells it cannot edit. The isolation
  guarantee protects **what the agent may write**, not what it may recall (A6).
- Feed size scales with conversation length rather than scope, so a turn cap is
  now load-bearing (A1).
- Per-cell grouping, a per-cell budget split, and "no prior attempts"
  placeholders are all dropped.

### 4.3 What each entry carries

- The user's original prompt. Without it, the agent knows code was rejected but
  not what goal it was serving, so it can avoid the code and repeat the
  misunderstanding.
- Cell id, index resolved at build time, and a one-line operation description.
- Status: `KEPT`, `UNDONE`, `FAILED (<code>)`, `CANCELLED`, or `STALE` (§4.7).
- **Full diff for undone operations only.** Kept results are already readable in
  `notebook.ipynb`; duplicating them inflates the feed for nothing.
- **The agent's reply only for turns that produced no operations.** For turns
  with a diff the reply largely restates it. For read-only and Q&A turns the
  reply is the entire content of that turn — omitting it would record that the
  user spoke and discard what was said, which is the one thing a thread is for.

### 4.4 Excluded

- **Pending operations.** With per-op accept, operations can sit unreviewed
  indefinitely. Telling the agent "you proposed this, the user has not decided"
  invites it to re-litigate rather than wait.
- **Manual user edits.** If a user silently rewrites the agent's output by hand,
  that is arguably a stronger rejection signal than pressing undo — but in a
  thread model it is not a thread event; nobody said anything. Recorded as a
  known gap (§8).
- **`revert_cell()` still records nothing.** C1 removes per-cell revert
  entirely, so adding an outcome marker there would be building something we are
  about to delete. Accepted blind spot until the per-op branch lands.
- **No retention priority for undone turns.** Pruning treats them like any
  other. In practice the feed window (10 turns) is far tighter than
  `MAX_TERMINAL_TURNS` (50), so count-based eviction cannot reach a turn the
  feed still wants; only the 2 MB byte cap could, and only with unusually large
  payloads.

### 4.5 Turns are labelled relatively, and plan turns are included

`AgentTurn` has no ordinal — only `created_at` and a UUID. Rather than inventing
a session counter that must stay stable across pruning, entries are labelled
relatively: `2 turns ago`, `5 turns ago`. Computed at build time against the
actual turn sequence. Since eviction is oldest-end only, feed entries are always
contiguous, so relative distance is never misleading.

Consequence: a label means something different next turn. That is harmless here,
because memory is frozen per turn (§5.4) and nothing cross-references a label.

**Plan-mode turns are included.** They produce no operations and a long
`final_output`, so under §4.3 the reply is carried — but at a ~1500 char cap
rather than 500, since a plan truncated to two sentences is worse than useless.

### 4.6 Replayed prompts carry the lead only

Attachments are composed into the prompt on the frontend
(`selectionEdit.ts:77`) as:

```
<lead>

Referenced selections:
<quoted cell source / traceback ...>
```

An `error`-kind attachment can be a full traceback. Replaying that in every
subsequent feed is the single largest avoidable cost in the design.

Because there is a literal `Referenced selections:` delimiter, the memory copy
splits on it and keeps only the lead — the user's actual words — replacing the
remainder with `(+N referenced selections)`. No heuristic truncation, no loss of
the user's intent, and the bulky payload never repeats.

### 4.7 Status is stated, never inferred

- A **cancelled** turn is marked `CANCELLED`, full stop. `_run` can apply changes
  and *then* return `"cancelled"` (`service.py:372`), so its edits may or may not
  be live — and the feed does not guess. It states what the turn did and that it
  was cancelled, and points at `notebook.ipynb` for current truth.
- **`STALE`** is carried through from the per-op ledger's derived staleness
  (P2). A stale operation is factual information; suppressing it would be its own
  kind of inference.
- **Mixed-status turns are fine.** A turn with three operations, two kept and one
  undone, renders one header and three status lines. This is history, not a
  verdict — it does not need to reduce to a single status. **Supersedes D7.**
- **The thread changes retroactively.** Pending operations are excluded (D6), so
  an operation that was pending during turn N+1 is invisible then and appears as
  kept or undone in turn N+2. Accepted and stated deliberately: the feed reflects
  what is known now, not what was known then.

---

## 5. Backend design

### 5.1 The missing field

**`AgentTurn` has no record of having been undone.**

`undo()` sets `checkpoint = None` and clears `_latest_applied_turn_id`
(`service.py:429`). But `_prune_history_locked()` *also* sets
`checkpoint = None` on every non-latest terminal turn (`service.py:588`). The
two are indistinguishable, and `state` remains `"completed"` either way.
`revert_cell()` records nothing at all.

So `UNDONE` is not currently derivable. This is the one genuinely new piece of
state, and it is the real dependency on the per-op branch — not a presentational
one.

```python
@dataclass
class AgentTurn:
    ...
    undone_at: datetime | None = None     # set by undo(); never cleared by pruning
```

Set in `undo()` alongside the existing bookkeeping, and never cleared by
pruning. Once C1 lands, per-operation status supersedes this at the operation
level (§7, P2).

**This change is backend-only, with no frontend surface.** That holds for free:
`agent_turn_routes.py:42` builds the API response field-by-field with explicit
keys rather than serializing the dataclass, so a new field cannot leak. A test
should pin that (§9).

### 5.2 `AgentTurnService.thread_memory()`

```python
def thread_memory(self, session_id: str, *, limit: int = 10) -> tuple[MemoryEntry, ...]:
```

Read-only. Walks `self._turns` under `self._lock`, oldest → newest. Returns
immutable `MemoryEntry` values.

Excluded: non-terminal turns, the in-flight turn itself, and pending operations.

Status is derived at read time (A5), never stored on the entry.

### 5.3 `AgentWorkspaceBuilder.build(..., memory=())`

New optional keyword defaulting to empty, so every existing caller and test is
unaffected and the empty case is byte-identical to today's output.

Rendered **after line 1** and before the editable-files list.

> **Line 1 must remain `scope.prompt`.** `DevelopmentFakeAgentAdapter`
> (`adapters.py:74`) reads `instructions.splitlines()[0].lower()` as the prompt.
> Nothing currently asserts this. §9 adds a test.

The feed is part of `INSTRUCTIONS.md`, which is already in `baseline_hashes` and
`chmod`-protected, so the protected-path audit covers it with no auditor change.
No new file is added to the workspace — `workspace_auditor.audit()` treats any
undeclared path as a boundary violation (`workspace_auditor.py:87`).

### 5.4 Memory is frozen at turn start

Captured in `start()` alongside the snapshot and the frozen scope, then passed
unchanged into all three `build()` attempts in `_run`.

Boundary-violation retries must see identical memory. If memory could change
between attempts, a retry becomes a different turn — which contradicts the
frozen-scope model the whole system is built on.

---

## 6. Rendering

```
Conversation so far (this notebook session). These earlier turns are yours.
(earlier turns omitted)

--- 4 turns ago ---
You asked: "vectorize this loop" (+1 referenced selection)
It edited cell 4 (id a1b2c3): replaced lines 4-9.
STATUS: UNDONE by the user. This code is NOT in the notebook. Do not re-propose it.
    - for i in range(len(df)):
    -     out.append(df.iloc[i].x * 2)
    + out = (df.x * 2).tolist()

--- 3 turns ago ---
You asked: "clean up the loader and add a docstring"
It edited cell 4 (id a1b2c3): inserted a docstring.
  STATUS: KEPT. The result is in notebook.ipynb; read it there.
It edited cell 6 (id d4e5f6): renamed `tmp` to `frame`.
  STATUS: UNDONE by the user. Do not re-propose it.
    - tmp = pd.read_csv(path)
    + frame = pd.read_csv(path)

--- 2 turns ago ---
You asked: "stop"
It edited cell 6 (id d4e5f6): reordered the imports.
STATUS: CANCELLED. Whether this change is in the notebook is not recorded
here — read notebook.ipynb for current source.

--- 1 turn ago ---
You asked: "what does cell 7 do"
It made no changes. It replied: "Cell 7 loads the raw CSV and ..."
```

### 6.1 Edge cases

- **Cell indexes are resolved at build time** against the frozen snapshot, never
  stored. Indexes shift as cells are added or removed; a stored index would be
  quietly wrong.
- **Deleted cells keep their entry**, rendered `(cell deleted)`. A deleted cell
  is precisely when the diff is the only surviving record.
- **Failed turns are included** as `STATUS: FAILED (<code>)`, cancelled ones as
  `STATUS: CANCELLED` (§4.7). A thread with silent gaps invites the agent to
  invent what filled them.
- **`validation_incomplete`** is a terminal state (`service.py:34`) and renders
  as `FAILED (validation_incomplete)`.
- **Eviction is from the oldest end only.** Dropping from the middle would break
  thread continuity without saying so.
- **Memory failure never fails a turn** (A4): `thread_memory()` is wrapped and
  logged; the turn proceeds with an empty feed.

---

## 7. Phasing

- **P0 — outcome marker.** `undone_at` on `AgentTurn`, set by `undo()`. Small,
  independent, and valuable alone: the system currently cannot tell you whether a
  turn was undone. Touches no workspace code.
- **P1 — the feed.** `thread_memory()` + `build(memory=)` + rendering. Depends
  only on P0.
- **P2 — ledger reconciliation.** Replace turn-level status with per-operation
  status once C1 lands, so `KEPT` / `UNDONE` is per-op rather than per-turn.

P0 and P1 do not have to wait for the per-op branch. P2 is the merge point.

---

## 8. Known gaps

- **Manual user edits are invisible** (§4.4). If the user rewrites the agent's
  output by hand, the agent reads it as neutral current state. Likely belongs
  with the per-op ledger's staleness rules rather than here.
- **Execution errors are only *automatically* out of scope.** Tracebacks already
  reach the agent today via the `error`-kind attachment
  (`AgentChatPanel.tsx:83`), composed into the prompt by hand. What is missing is
  automatic capture of a failure the agent's own edit caused. That is a transient
  runtime fact rather than a record of what the agent did — different lifetime,
  different source of truth — so it needs its own design. Note the interaction
  with D17: an attached traceback informs the turn it was attached to, and is
  deliberately *not* replayed into later turns.
- **No escape hatch — deferred to its own worktree.** There is no "clear memory"
  or "new thread" control. When memory poisons results — the agent fixating on an
  approach you rejected — the only exit is closing the notebook. This needs a
  UI surface, which is why it is not in this backend-only change.
- **`revert_cell()` records nothing** (§4.4). Blind spot until C1 lands.
- **No cross-session memory.** Reopen the notebook tomorrow and the thread is gone.
- **No standing preferences** ("this notebook uses polars"). That is durable
  configuration, not turn history.

---

## 9. Test plan

**Regression guards (write first):**

- `build()` with empty memory produces a byte-identical `INSTRUCTIONS.md` to
  today. Everything else in this design is additive.
- Line 1 of `INSTRUCTIONS.md` is `scope.prompt` — protects the undocumented
  `DevelopmentFakeAgentAdapter` dependency (§5.3).

**Core:**

- `undo()` and `_prune_history_locked()` leave distinguishable states: an undone
  turn is still identifiable as undone *after* pruning nulls its checkpoint.
  This is the test that stops §5.1 regressing.
- `thread_memory()` ordering; exclusion of non-terminal, in-flight, and pending.
- All three boundary-violation attempts receive identical memory (§5.4).
- `thread_memory()` raising still completes the turn with an empty feed (A4).
- Budget: a 60-line diff truncates with a marker; a thread over 16 KB evicts from
  the oldest end and emits `(earlier turns omitted)`.
- Cell index resolved at build time; deleted cell renders `(cell deleted)`.
- **No frontend leak:** the agent-turn API response is byte-identical before and
  after `undone_at` is added (§5.1).
- A prompt containing `Referenced selections:` replays only its lead, with
  `(+N referenced selections)` (§4.6). A prompt without the delimiter is
  unchanged.
- A cancelled turn that applied changes renders `CANCELLED`, never `KEPT` (§4.7).
- A turn with mixed per-op outcomes renders one header and per-op status lines.
- Relative labels count contiguously from the newest entry (§4.5).

**Integration:**

- Turn 1 edits → user undoes → turn 2's `INSTRUCTIONS.md` contains the diff and
  the "do not re-propose" line.

**Manual:**

- Verified against the real Claude CLI, not `FakeAgentAdapter`.

---

## 10. Decision log

| # | Decision | Alternatives considered | Why |
|---|---|---|---|
| D1 | Fix "the agent forgets what it did" | Follow-up reference resolution; execution-error threading; standing preferences | The other three are either already partly solved by the notebook copy, or are separate mechanisms with different lifetimes |
| D2 | Carry undone diffs + an outcome index; drop kept diffs | Outcome index only; full diffs for everything; add a user-supplied rejection reason | Kept edits are already readable in `notebook.ipynb`. Undone content exists nowhere else. Outcome-only lets the agent re-propose the same code |
| D3 | ~~Filter to the current turn's frozen scope~~ | All ops unfiltered; scoped-full + one-line index | **Superseded by D9** |
| D4 | Session-only, in-memory | Persist to disk; survive notebook reload | Matches the spec's existing in-memory stance; no new artifact, format, or privacy surface |
| D5 | Include the user's prompt; drop the agent's reply | Op facts only; full exchange | The prompt makes a rejection interpretable. Replies mostly restate the diff — later narrowed by D10 |
| D6 | Exclude pending operations | Include them | Simplicity; and telling the agent about an undecided proposal invites re-litigation |
| D7 | ~~Whole-turn undo renders as one chunk~~ | Per-operation entries | **Superseded by D16** |
| D8 | Hand-built feed, keep `--no-session-persistence` | CLI `--resume`; hybrid resume + corrections | `--resume` gives *false* memory — the transcript records edits succeeding, with no representation of undo. Also forces disk persistence and carries stale workspace paths |
| D9 | Thread-shaped and unfiltered | Per-cell grouping (D3) | User reframe: memory should be "what it did in the whole thread, like how you chat with Claude." Accepts that a scoped turn can recall out-of-scope cells; A6 keeps write-scope unchanged |
| D10 | Include the reply for no-op turns only, truncated | Omit no-op turns; keep content-free stubs; include every reply | D5 left read-only turns as empty stubs once the model became a thread. Recording that the user spoke while discarding what was said defeats the purpose |
| D11 | Add `undone_at` to `AgentTurn` | Derive undo from existing fields | Not derivable: `undo()` and `_prune_history_locked()` both null the checkpoint and leave `state == "completed"` |
| D12 | No retention priority for undone turns | Protect them from pruning like `_latest_applied_turn_id` | The feed window (10) is far tighter than `MAX_TERMINAL_TURNS` (50), so count-based eviction cannot reach a turn the feed wants. Not worth complicating pruning for the byte-cap edge case |
| D13 | `revert_cell()` records nothing | Add an outcome marker there too | C1 deletes per-cell revert; building a marker for it now is building something we are about to remove |
| D14 | Backend-only, no frontend change | Expose `undone_at` in the turn API | Not needed by any surface. Holds for free — `agent_turn_routes.py:42` serializes explicit keys, so nothing leaks |
| D15 | Relative labels (`3 turns ago`) | Stable session ordinal | `AgentTurn` has no ordinal; a synthetic counter would have to survive pruning. Labels are computed at build time and memory is frozen per turn, so drift is harmless |
| D16 | State status, never infer it: `CANCELLED` is its own status, mixed-status turns render per-op lines, `STALE` is carried through | Collapse cancelled into `KEPT`/`FAILED`; reduce mixed turns to one verdict; suppress `STALE` | `_run` can apply changes and *then* cancel (`service.py:372`), so any collapse is a guess. The feed is factual history, not a verdict. Supersedes D7 |
| D17 | Replay the prompt lead only; drop the attachment payload | Replay the whole composed prompt; truncate by character count | Chosen for latency. `selectionEdit.ts:77` provides a literal `Referenced selections:` delimiter, so the split is exact — full intent preserved, an `error`-kind traceback never replayed |
| D18 | Include plan-mode turns, reply capped at ~1500 chars | Exclude them; apply the 500-char cap | A plan is the entire content of a plan turn; truncating it to two sentences is worse than omitting it |
| D19 | The thread may change retroactively | Freeze each turn's view of history permanently | Pending ops are excluded (D6), so an op becomes visible only once decided. The feed reflects what is known now — stated deliberately rather than left as an accident |
| D20 | Escape hatch ("clear memory" / "new thread") deferred | Build it with P1 | Needs a UI surface; this change is backend-only. Its own worktree |

---

## 11. Spec changes required

- `docs/notebook-agent-editor-spec.md` — the turn-isolation section needs to
  state that isolation constrains **write scope**, not recall, and that a turn's
  `INSTRUCTIONS.md` may reference operations on cells outside its frozen scope.
- The agent-turn lifecycle section needs `undone_at` and the statement that undo
  is a recorded outcome, not merely the absence of a checkpoint.
