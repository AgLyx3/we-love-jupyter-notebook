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
- Status: `KEPT`, `APPLIED`, `UNDONE`, `FAILED (<code>)`, or `CANCELLED`, plus an
  orthogonal `STALE` qualifier (§4.7).
- **Full diff for undone operations only.** Kept results are already readable in
  `notebook.ipynb`; duplicating them inflates the feed for nothing.
- **The agent's reply only for turns that produced no operations.** For turns
  with a diff the reply largely restates it. For read-only and Q&A turns the
  reply is the entire content of that turn — omitting it would record that the
  user spoke and discard what was said, which is the one thing a thread is for.

### 4.4 Excluded

- ~~**Pending operations.**~~ **Superseded by D21.** Written when "pending" meant
  an undecided proposal that had not landed. Under the shipped ledger,
  `APPLIED_STATES = {pending, accepted}` — a pending operation is already in the
  notebook and merely unreviewed. Excluding those would empty the feed, since
  `pending` is where operations rest until the user acts.
- ~~**Manual user edits.**~~ **Superseded by D22.** `stale_cell_ids()` derives
  this from the ledger, so the fact is now available and is carried.
- ~~**`revert_cell()` records nothing.**~~ **Still true, and no longer excused.**
  D13 assumed C1 would delete per-cell revert; it did not. See §8.
- **No retention priority for undone turns.** Pruning treats them like any
  other. In practice the feed window (10 turns) is far tighter than
  `MAX_TERMINAL_TURNS` (50), so count-based eviction cannot reach a turn the
  feed still wants; only the 2 MB byte cap could, and only with unusually large
  payloads. `MAX_PENDING_REVIEW_TURNS` (10) protects unreviewed turns from
  eviction outright, which can only help the feed.
- **The content of a rejected structural add.** The ledger stores a
  `source_hash` for added cells, deliberately not the text (a fixed-size guard,
  not retained content). If the user rejects an add, the cell is deleted and its
  source is unrecoverable, so the feed can report the fact but not the code. See
  §8.

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
- **Applied is not kept.** An operation resting at `pending` is in the notebook
  but unreviewed. Rendering it as `KEPT` would manufacture an approval the user
  never gave, which is the same class of error as guessing at a cancelled turn.
  It gets its own status: `APPLIED but not yet reviewed by the user`.
- **`STALE`** is carried through from the ledger's derived staleness, as a
  qualifier *beside* the status rather than as a replacement for it — a hunk can
  be kept and since overwritten, and both facts matter. Suppressing it would be
  its own kind of inference.
- **Mixed-status cells are fine.** A cell with two hunks, one kept and one
  undone, renders one header and two status lines. This is history, not a
  verdict — it does not need to reduce to a single status. **Supersedes D7.**
- **The thread changes retroactively.** An operation reviewed between turn N+1
  and N+2 is reported as `APPLIED` in the first and `KEPT` or `UNDONE` in the
  second. Accepted and stated deliberately: the feed reflects what is known now,
  not what was known then.

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
pruning.

**Post-C1 revision.** The shipped `undo()` also settles the turn's operations to
`rejected`, and pruning does not touch `operations`. For any cell the ledger
covers, undo is therefore derivable and this field is redundant — D11's original
rationale no longer holds as stated. It survives with a narrower job: the ledger
has no kind for retyped, deleted, or moved cells (they stay whole-turn undo), so
for those `undone_at` is the only record that the change was reversed. See D11′.

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

Excluded: non-terminal turns and the in-flight turn itself. Pending operations
are *included* — see D21.

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
  status once C1 lands, so the outcome is per-op rather than per-turn.

P0 and P1 do not have to wait for the per-op branch. P2 is the merge point.

**Status: all three are done.** C1 shipped to `main` in PR #15 while this branch
sat unmerged, and P2 turned out to be required rather than optional: the ledger
made turn-level status actively wrong, not merely coarse. Rebasing also
surfaced a fourth item the phasing had not anticipated — `build()` grew a second
instruction writer for Trusted turns (`_build_trusted`), and memory reaching
only the Blocking one would have failed silently. See D23.

---

## 8. Known gaps

- ~~**Manual user edits are invisible**~~ — **closed.** It did belong with the
  ledger's staleness rules: `stale_cell_ids()` derives it, and the feed now
  carries a `STALE` qualifier (D22).
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
- ~~**`revert_cell()` records nothing**~~ — **closed, and it was never going to
  need a marker.** C1 did not delete per-cell revert as D13 predicted; it did
  something better and rewrote it to delegate to `reject_operations`, so there
  is one reject path rather than a parallel hash-guarded one. A cell reverted
  that way settles its operations to `rejected` like any other, and the feed
  reports it as `UNDONE` with its diff. D13 reached the right call for a reason
  that turned out to be wrong twice over.
- **A rejected structural add loses its content.** The ledger keeps a
  `source_hash` for added cells, not the source, so once the user rejects an add
  the cell's text is gone everywhere. The feed reports the removal and says
  outright that the content is not recorded — the one place D2's "undone content
  exists nowhere else" argument cannot be honoured without changing the ledger's
  memory profile. Reporting the *fact* is not optional though: see D26.
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
- Relative labels count contiguously from the newest entry (§4.5).

**Post-C1 (D21–D25):**

- An unreviewed operation renders `APPLIED`, never `KEPT` (D21). This is the one
  that would otherwise have shipped as a silent lie.
- An accepted operation renders `KEPT`.
- A cell with one hunk rejected and one kept renders **one** header and both
  status lines, and carries only the rejected hunk's content (D24).
- A hand-edited cell renders `STALE` *in addition to* its outcome (D22).
- A Trusted turn's `INSTRUCTIONS.md` carries the same feed as a Blocking one,
  without displacing its structural rules (D23).

**Integration:**

- Turn 1 edits → user undoes → turn 2's `INSTRUCTIONS.md` contains the diff and
  the "do not re-propose" line.

**Manual:**

- Verified against the real Claude CLI, not `FakeAgentAdapter`. See §9.1.

### 9.1 Validation log

**2026-07-31 — real Claude CLI 2.1.220, P0 + P1, full `AgentTurnService`
pipeline.**

Scenario chosen so that the *correct* answer and the *rejected* answer are the
same code, which is the case memory has to get right:

1. Cell contains an append loop building `squares`.
2. Turn 1: "Rewrite this loop as a single list comprehension." → agent applied
   `squares = [n * n for n in range(10)]`.
3. User undid it. `undone_at` recorded; cell restored.
4. Turn 2: "Make this cell faster." — a prompt that leads almost inevitably back
   to the comprehension.

Result: the agent did not re-propose it, and named the reason unprompted —
*"That's the version you undid last turn, so I'm leaving the explicit loop in
place rather than proposing it again."* It offered numpy and generator
alternatives instead, and correctly noted that the numpy option would need an
import in a cell outside this turn's editable scope. The cell was left
byte-identical.

Before this change the same turn 2 had no way to know turn 1 had happened.

Caveats: one scenario, one run, non-deterministic model. This shows the feed
reaches the agent and is acted on; it is not a measurement of how reliably that
holds across prompts.

**2026-08-04 — real Claude CLI 2.1.220, re-validated after rebasing onto the
shipped ledger (D11′, D21–D25).**

The 2026-07-31 run went through whole-turn `undo()`. The rebase made
per-operation reject the ordinary path, and it reaches the feed through
different code — `reject_operations` settles the hunk to `rejected`, and the
undone diff is recomposed from the ledger rather than read off the turn's
changes. Same scenario, that path instead:

1. Same append loop.
2. Turn 1 applied `squares = [n * n for n in range(10)]`. Operation state
   `pending` — confirming D21's premise that completed is not reviewed.
3. `reject_operations(..., None)` — per-op undo, not whole-turn. Cell restored.
4. Turn 2: "Make this cell faster."

Result: the agent again declined and named the reason unprompted — *"The one
standard rewrite — collapsing it into a list comprehension — is what you undid
last turn, so I'm leaving it alone."* It offered the `append` local-binding
micro-optimisation and NumPy vectorisation instead, and correctly judged that
neither was worth it at n=10. Cell left byte-identical.

Same caveats as above. What this adds over the first run is only that the
ledger-derived feed carries the rejection as legibly as the whole-turn one did.

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
| D11 | ~~Add `undone_at` to `AgentTurn`~~ | Derive undo from existing fields | **Rationale falsified by C1** — see D11′ |
| D12 | No retention priority for undone turns | Protect them from pruning like `_latest_applied_turn_id` | The feed window (10) is far tighter than `MAX_TERMINAL_TURNS` (50), so count-based eviction cannot reach a turn the feed wants. Not worth complicating pruning for the byte-cap edge case |
| D13 | ~~`revert_cell()` records nothing~~ | Add an outcome marker there too | **Premise wrong, conclusion right.** C1 neither removed per-cell revert nor left it unrecorded — it re-expressed it on the ledger, so a revert settles operations to `rejected` and reaches the feed for free. Building a marker would have been wasted either way |
| D14 | Backend-only, no frontend change | Expose `undone_at` in the turn API | Not needed by any surface. Holds for free — `agent_turn_routes.py:42` serializes explicit keys, so nothing leaks |
| D15 | Relative labels (`3 turns ago`) | Stable session ordinal | `AgentTurn` has no ordinal; a synthetic counter would have to survive pruning. Labels are computed at build time and memory is frozen per turn, so drift is harmless |
| D16 | State status, never infer it: `CANCELLED` is its own status, mixed-status turns render per-op lines, `STALE` is carried through | Collapse cancelled into `KEPT`/`FAILED`; reduce mixed turns to one verdict; suppress `STALE` | `_run` can apply changes and *then* cancel (`service.py:372`), so any collapse is a guess. The feed is factual history, not a verdict. Supersedes D7 |
| D17 | Replay the prompt lead only; drop the attachment payload | Replay the whole composed prompt; truncate by character count | Chosen for latency. `selectionEdit.ts:77` provides a literal `Referenced selections:` delimiter, so the split is exact — full intent preserved, an `error`-kind traceback never replayed |
| D18 | Include plan-mode turns, reply capped at ~1500 chars | Exclude them; apply the 500-char cap | A plan is the entire content of a plan turn; truncating it to two sentences is worse than omitting it |
| D19 | The thread may change retroactively | Freeze each turn's view of history permanently | Pending ops are excluded (D6), so an op becomes visible only once decided. The feed reflects what is known now — stated deliberately rather than left as an accident |
| D20 | Escape hatch ("clear memory" / "new thread") deferred | Build it with P1 | Needs a UI surface; this change is backend-only. Its own worktree |

### 10.1 Revisions after rebasing onto the shipped ledger

C1 (per-operation accept/undo) landed on `main` in PR #15 while this branch was
unmerged. These supersede or add to the decisions above.

| # | Decision | Alternatives considered | Why |
|---|---|---|---|
| D11′ | Keep `undone_at`, but as the marker for cells the ledger cannot represent | Delete it as redundant; keep D11's original framing | `undo()` now settles operations to `rejected` and pruning leaves `operations` alone, so undo *is* derivable for covered cells. But retyped/deleted/moved cells get no operations by design, and for those the field is the only record |
| D21 | Include pending operations, as `APPLIED` rather than `KEPT` | Exclude them (D6); fold them into `KEPT` | `pending` no longer means "undecided proposal" — `APPLIED_STATES = {pending, accepted}`, so the change is already in the notebook. Excluding it empties the feed, since `pending` is where operations rest until the user acts. Folding it into `KEPT` invents an approval. **Supersedes D6** |
| D22 | Carry `STALE` as a qualifier beside the status | A fourth status value; suppress it | A hunk can be kept *and* since hand-edited; those are two facts, and a single field would have to drop one. Closes the §8 manual-edit gap using `stale_cell_ids()` |
| D23 | Render memory in both instruction writers | Blocking only; extract one shared writer | `_build_trusted()` is a second `INSTRUCTIONS.md` path. Memory reaching only one fails *silently* — a Trusted turn would just quietly forget the thread. Extracting a shared writer is the better end state but a larger change than this branch should carry |
| D24 | Split a mixed cell into two entries under one header | One verdict per cell; one entry per hunk | Rounding a half-rejected cell to a single verdict has to be wrong in one direction, and rounding towards `KEPT` tells the agent its rejected code is live. Per-hunk entries were the alternative; per-outcome keeps the common single-outcome rendering unchanged |
| D26 | Structural adds get their own memory entry, described rather than diffed | Leave them out (they are not in `changes`); synthesise a diff from `""` | Found in review: an add-only Trusted turn rendered as "It made no changes", contradicting its own reply and inviting the agent to add the cell again. Adds carry no before/after pair and the ledger keeps a hash rather than the text, so the entry states what happened and, when rejected, that the content is gone |
| D25 | Recover undone content by composing the ledger with rejected hunks restored | Store the rejected text on the operation | `compose` is already a pure function of pre-turn source and states, so the content is recomputable. Storing it would duplicate what `changes` already holds and contradict the ledger's "index ranges only, never copies of the line text" rule |

---

## 11. Spec changes required

All applied to `docs/notebook-agent-editor-spec.md`:

- **Turn Scope → Rules.** Isolation constrains **write scope**, not recall; a
  turn's `INSTRUCTIONS.md` may reference operations on cells outside its frozen
  scope.
- **Undo And Checkpoints → whole-turn undo.** Undo is a recorded outcome, not
  merely the absence of a checkpoint — the ledger records it for covered cells
  and `undone_at` for the rest, and pruning clears neither.
- **Per-operation review.** The ledger is what the agent is told: outcomes reach
  the next turn through thread memory, and `pending` is reported as applied
  rather than kept.
