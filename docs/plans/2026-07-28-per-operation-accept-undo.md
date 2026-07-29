# Design: Per-Operation Accept / Undo

Adds an explicit, individually addressable **operation ledger** for each agent
turn, so a user can keep or undo one hunk at a time — alongside the existing
whole-turn checkpoint undo.

Branch: `worktree-per-op-undo-design` (worktree). Design only; no code yet.

Spec sections touched: `## Undo And Checkpoints`, `## UI Behavior`,
`## Backend Domains`, `## API Surface`.

---

## 1. Where we are today

Two reversal mechanisms exist, both in `backend/app/agent_turns/service.py`:

| | Granularity | Guard | Restores |
|---|---|---|---|
| `undo(turn_id)` (`service.py:412`) | whole turn | `_latest_applied_turn_id == turn_id` **and** `applied_revision == snapshot.revision` | full checkpoint: sources, outputs, execution counts |
| `revert_cell(turn_id, cell_id)` (`service.py:441`) | one cell | `sha256(current_source) == sha256(change.next_source)` | that cell's source only |

There is **no accept**. Per the spec's apply-then-review model
(`notebook-agent-editor-spec.md:583-585`), changes land immediately and a
"pending change the user must accept" state was explicitly deferred. Review
state is therefore never settled by the user — it is *derived* on the client by
`reconcileTurnChanges` (`frontend/src/App.tsx:442`), which drops a change once
the cell's source stops matching `nextSource`. The diff stripe simply lingers
until something incidentally invalidates it.

Four concrete gaps:

1. **No "I've reviewed this" gesture.** The only way to clear a diff overlay is
   to reject it, edit the cell, or select a different turn.
2. **No sub-cell granularity.** A turn that changes three unrelated things in
   one cell is all-or-nothing.
3. **Rejecting one cell silently kills whole-turn undo.** `revert_cell` calls
   `apply_source_changes_under_lease`, the revision advances past
   `applied_revision`, and the next `is_undo_eligible` call returns `False`
   *and drops the checkpoint* (`service.py:217-222`). Reject one cell of a
   five-cell turn and you can no longer undo the other four.
4. **Per-cell revert is effectively invisible.** See §1.1 — the affordance
   exists and is tested, but users do not find it.

### 1.1 The existing revert control is undiscoverable

Worth stating plainly, because it reframes the work: the per-cell revert button
(`NotebookCell.tsx:158`) is real, wired, and covered by unit
(`Remediation.test.tsx:149`) and e2e (`notebook-editor.spec.ts:232`) tests — and
users still report not knowing it exists. The cause is presentation:

```css
/* styles.css:75 */
.cell-actions { position: absolute; right: 6px; top: 6px; ... opacity: 0; }
.notebook-cell:hover .cell-actions, .notebook-cell:focus-within .cell-actions { opacity: 1; }
```

- Hidden until the cell is hovered or focused.
- An unlabeled 28×28 `RotateCcw` icon, tooltip-only.
- Last of up to six visually identical icon buttons (bot / book / play / pencil
  / save / revert) crammed into the cell's top-right corner.
- Positioned nowhere near the diff it acts on. The diff is rendered as inline
  CodeMirror decorations mid-cell; the control that reverts it is in a corner.

Two consequences beyond discoverability:

- **`opacity: 0` does not disable pointer events.** The button is invisible but
  still clickable and still in the accessibility tree (which is exactly why the
  tests pass). A stray click in a cell's top-right corner fires a real document
  mutation that today also destroys the turn's undo checkpoint (gap #3).
- The control renders only when `selectedTurn` still has a surviving change for
  that cell. Clicking an older turn in the chat history makes every current
  revert button vanish with no explanation.

**Design consequence:** adding ledger endpoints without fixing presentation
would reproduce the same failure at finer granularity — more invisible buttons.
Phase 1 must therefore treat *co-locating review controls with the diff* as a
requirement, not polish. See §4.6.

---

## 2. What "operation" means here

An **operation** is one atomic, individually reviewable unit of an applied turn.
The granularity ladder becomes:

```
turn  ──▶  cell change  ──▶  operation (hunk)          ← new addressable unit
                       └──▶  operation (cell outputs)  ← Phase 3
```

Turn and cell become roll-ups over the ledger rather than separate mechanisms.

### 2.1 Accept does not mutate the document

This is the load-bearing decision. Because changes are already applied:

- **Accept** = ledger-only state transition. No document mutation, no revision
  bump, no lineage break, no lease required. It means *"reviewed, stop showing
  me the diff."*
- **Reject / undo** = a real mutation. Bumps the revision, takes a lease, guarded
  like every other mutation in the system.

Keeping accept out of the mutation path is what makes this feature cheap and
safe: the entire accept surface can never corrupt document state.

**Naming.** "Accept" implies committing something not yet live, which is not
what happens. Recommend the UI reads **Keep** / **Undo** while the API verbs
stay `accept` / `reject`. (`Keep` also reads correctly for markdown cells, where
"accept" sounds like a code review.)

### 2.1.1 Cross-check: this matches how Cursor works

Checked because "accept" invites the Cursor comparison, and because decision A
(§10.1) turns on it. Cursor runs the **same apply-then-review model**, with the
same two tiers this design proposes:

| Cursor | This design |
|---|---|
| **Keep / Undo** per hunk, per file, accept-all (Cmd+Enter) — review over edits the agent already made | Keep / Undo per operation, per cell, per turn (§3.7) |
| **Checkpoints**, one per message; "Restore checkpoint" resets all files to that point | Per-turn checkpoint undo (`undo(turn_id)`, unchanged) |
| Checkpoints are session-scoped, auto-cleaned, don't track manual edits, "not version control" | Identical limitations, already stated in the spec's Persistence rules |
| Setting: *Agents → Applying Changes → Inline Diffs*; off = **"auto-keep mode"**, changes apply with no review UI | Not proposed — but a sensible later option, see below |

Two things follow:

- **Decision A's default is validated.** Cursor's "accept" is not a staged
  commit either. The held-proposal model (§9) is not what Cursor does and is not
  needed to match it.
- **The naming is confirmed.** Cursor labels these buttons literally **Keep** and
  **Undo**, arrived at here independently from the semantics (§2.1). Decision F
  resolves to "yes" at no cost.

Worth considering later: an **auto-keep toggle** for users who don't want to
review agent edits at all. Cheap on this architecture — it just means defaulting
operations to `accepted` at apply time — and it is a real setting Cursor ships.

*Sourcing note:* several blog results claim Cursor stages edits and "only
accepted files write to disk." That contradicts Cursor's own forum thread and a
moderator's "auto-keep mode" explanation, and is treated here as unreliable.
What remains unconfirmed from official docs is whether, with Inline Diffs on,
Cursor holds the edit in an unsaved editor buffer or writes it to disk. Moot for
this design: downstream execution requires committed source, which is the whole
"the agent's edit is validated by running it" model.

### 2.2 What this deliberately is not

A true held-proposal model — agent writes to a staging layer, accept commits —
is a much larger change: execution, save, save-as, export, autosave, the kernel
source-hash guard (`service.py:299`) and the on-disk baseline would each need to
know which layer they read. The spec deferred it for that reason and this design
keeps that deferral. Section 9 sketches what it would cost if the product later
wants it.

---

## 3. Backend design

New module `backend/app/agent_turns/operations.py` — pure functions plus the
ledger dataclasses. Diff/compose logic stays out of the already-624-line
`service.py`, matching the repo's one-concern-per-module layout.

### 3.1 Model

```python
@dataclass(frozen=True)
class SourceHunk:
    """Index ranges only — never copies of the line text.

    Both slices are half-open ranges into the *line arrays* of the sources
    already retained on `turn.changes`, so the ledger adds four ints per hunk
    and no duplicated source. See §3.6.
    """
    ordinal: int                    # position within the cell, 0-based
    prev_start: int                 # into previous_source.split("\n")
    prev_end: int
    next_start: int                 # into next_source.split("\n")
    next_end: int

@dataclass
class TurnOperation:
    operation_id: str               # f"{turn_id}:{cell_id}:{ordinal}" — deterministic
    cell_id: str
    kind: str                       # "source_hunk" | "execution_output"
    ordinal: int
    hunk: SourceHunk | None
    state: str                      # "pending" | "accepted" | "rejected" — only these three
```

`AgentTurn` gains `operations: tuple[TurnOperation, ...]`.

Operation IDs are **deterministic**, not UUIDs, so that a client refetching a
truncated turn (the `historyTruncated` path in `App.tsx:145-148`) gets stable IDs
and in-flight buttons keep working.

### 3.1.1 `stale` is derived on read, never stored

An earlier draft stored `stale` as a fourth state, written by the reject guard
when it detected drift. That was wrong, and dropping it removes real machinery:

- **It would go out of date.** A manual edit that invalidates a cell's
  operations happens through `update_cell_source`, which knows nothing about the
  ledger. Stored staleness would only become true the next time someone
  *attempted* a reject — so the UI would offer live-looking buttons on
  operations that are already dead.
- **Fixing that properly needs new plumbing** — a general mutation listener on
  `NotebookDocumentService` (only a session-replacement hook exists today,
  `service.py:76`).

Instead compute it at serialization time. For each changed cell:

```
stale := sha256(current_cell_source) != sha256(compose(previous, states))
```

One hash per changed cell per serialize — negligible, always accurate, no new
listener, no stored state that can drift from the document. `stale` becomes a
**view-level** flag on the serialized operation, and the reject guard's job
shrinks to "recompute and 409" (§3.3) rather than "mark everything stale and
409".

**State machine** (stored states only):

| From | accept | reject | notes |
|---|---|---|---|
| `pending` | → `accepted` | → `rejected` | the normal paths |
| `accepted` | no-op | → `rejected` | undoing something already kept is legitimate |
| `rejected` | **409** | no-op | accepting an undone hunk would silently re-apply it — see §7 for the deliberate un-reject |

Accept and reject are each idempotent from their own target state, so a
double-click or a retried request is harmless. The one hard error is
`rejected → accepted`, because that is a re-apply wearing the wrong verb.

### 3.2 When operations are computed

In `AgentTurnService._run`, immediately after `validator.validate` returns the
`CandidateCellSourceChange` tuple and before `apply_source_changes_under_lease`
(`service.py:363`). The backend is the single source of truth for hunk
boundaries — the client renders exactly the hunks the server will act on, so a
button can never reject a different region than the one drawn.

This requires porting the LCS line diff in `frontend/src/notebook/cellDiff.ts`
to Python, **including its bounds** (`MAX_DIFF_CHARS`, `MAX_DIFF_LINES`,
`MAX_DIFF_WORK`, `MAX_REGION_LINES`) and the `coarseDiff` fallback, so both ends
degrade identically on pathological inputs. Under the coarse fallback a cell
yields exactly one operation — correct behaviour, since a 600-line rewrite is
not usefully reviewable hunk by hunk.

### 3.3 Composition — the guard mechanism

```python
def compose(previous_source: str, operations: Sequence[TurnOperation]) -> str:
    """Rebuild a cell's source from its pre-turn source and per-op states.

    An operation in "pending" or "accepted" contributes its added lines;
    "rejected" contributes its removed lines instead.
    """
```

`compose` is a pure function of `previous_source` + operation states, so:

- The cell's current expected source is always recomputable — nothing is stored
  twice, and the ledger cannot drift from the document.
- **Reject is reversible for free.** Flipping a state back to `pending` and
  recomposing un-rejects a hunk. See §7 (optional).

**Reject guard** (replaces the whole-cell hash check, which cannot work once
partial rejection exists — after rejecting hunk 1 the cell no longer hashes to
`next_source`, so hunk 2 would be permanently unrejectable):

1. Acquire coordinator lease (`operation_type="agent_reject"`).
2. `check_snapshot_preconditions(snapshot, session_id, expected_revision)` —
   unchanged, existing helper.
3. `sha256(current_cell_source) == sha256(compose(previous, current_states))`.
   On mismatch → raise `OperationConflict` (409). Nothing is written; the
   client's next read sees the cell's operations as `stale` because staleness is
   derived from this same comparison (§3.1.1).
4. Flip the target operation to `rejected`; write
   `compose(previous, new_states)` via `apply_source_changes_under_lease` with
   owner `f"reject:{turn_id}:{operation_id}"`.
5. Advance the turn's lineage (§3.4), then publish `notebook.updated`.

The guard is "the cell is exactly what the ledger says it should be." Any manual
edit anywhere in the cell freezes that cell's remaining operations rather than
risking a mis-placed patch. This is strictly stronger than fuzzy context
re-matching and matches the codebase's existing philosophy: clean 409 over
silent misapplication. It also **degenerates to today's behaviour** when a cell
has one operation and none are rejected.

### 3.4 Undo must survive its own turn's rejections

Fixing gap #3. `is_undo_eligible` currently requires
`applied_revision == snapshot.revision`. Change to: eligibility holds while
every mutation since the checkpoint is owned by the turn **or by that turn's own
rejections**. Concretely, `reject_operation` sets
`turn.applied_revision = updated.revision` after its own commit.

Rationale: rejecting a subset of a turn's changes *is reviewing that turn*, not
unrelated later work. The checkpoint still exactly represents the pre-turn
state, so full restore stays correct regardless of how many hunks were rejected
first. Risk is nil.

> **Spec amendment required.** `notebook-agent-editor-spec.md:1240-1242` lists
> "revert" among the actions that make checkpoint undo ineligible. That clause
> must be narrowed to *manual* cell edits, import, manual execution, and new
> agent turns. Flagging explicitly because it inverts a documented rule.

**Implementation hazard — verified, and worse than it looks.**
`is_undo_eligible` clears the checkpoint *as a side effect* when it returns
`False` (`service.py:216-222`: it nulls `_latest_applied_turn_id` and sets
`stored.checkpoint = None`). It is called from **five** places:

```
agent_turn_routes.py:135   start_turn
agent_turn_routes.py:142   get_turn
agent_turn_routes.py:154   cancel_turn
session_routes.py:30       session status — turn history
session_routes.py:43       session status — active turn
```

The two in `session_routes.py` are the critical ones: that endpoint is polled by
the client on every `refresh()`, and `refresh()` runs after most mutations. So a
reject that commits without first advancing `applied_revision` leaves a window
in which **any concurrent status poll permanently destroys the checkpoint** —
not merely marks it ineligible. This is a race, so it will pass a naive
single-threaded test and fail in use.

Requirements that follow:
- Update `applied_revision` and commit the ledger inside the same `_lock`
  acquisition that the document mutation is published from — never after.
- Add a test that polls `is_undo_eligible` concurrently with a reject and
  asserts the checkpoint survives.
- Consider making the checkpoint-clearing side effect explicit
  (`_invalidate_checkpoint()`) rather than hiding it inside a predicate named
  `is_*`. A query that mutates state is the root cause here and will keep
  producing bugs like this one.

### 3.5 `revert_cell` is reimplemented, not kept alongside

`revert_cell` becomes a thin alias for "reject every pending operation in this
cell", composing once and committing once. The endpoint and its 409 semantics
survive; the duplicate hash-guard code path does not. One mutation path is worth
more than backwards-compatible internals.

### 3.6 Memory

Near-free, **but only because §3.1 stores indices rather than line text**. The
full sources are already retained on `turn.changes`; duplicating hunk text into
the ledger would roughly double per-turn source memory against a 2 MB budget
that already drives pruning. Four ints plus a state enum per hunk keeps the
delta at tens of bytes.

`_history_size` (`service.py:615`) must still count the operation payload so the
`MAX_TURN_HISTORY_BYTES` accounting stays honest.

Consequence to respect in review: `compose` and any serializer must slice the
sources on demand and must never be "optimised" by caching materialised hunk
text on the operation.

### 3.6.1 Pruning can evict a turn the user is still reviewing

`_prune_history_locked` (`service.py:582-613`) evicts whole turns —
`self._turns.pop(expired.turn_id, None)` — once the retained set exceeds
`MAX_TERMINAL_TURNS` (50) or the 2 MB budget. Its `protected` set contains only
`_latest_applied_turn_id`.

Today that is survivable: eviction kills whole-turn undo, and per-cell revert
degrades to a 404 the client already treats as "the change is gone." With a
ledger it is worse — a turn with **pending operations** can be evicted while the
notebook is still drawing its diffs, leaving visible overlays whose Keep/Undo
buttons 404. The user sees unreviewed changes they cannot act on.

Fix: extend `protected` to turns holding any `pending` operation.

That trades one bound for another, so it needs an explicit cap rather than an
unbounded exception — a turn with pending operations is retained until either
its operations are all settled or a hard ceiling (suggest 10 turns' worth of
pending ledgers) is hit, after which the oldest is force-settled to `accepted`
and dropped. Accepting is the safe direction: it discards *review state*, never
document content, and the change stays in the notebook exactly as applied.

`log`-style visibility matters here too — silently force-settling reads as "the
user reviewed it." Surface it as "older changes were auto-kept" rather than
letting the ledger quietly shrink.

### 3.7 API surface

```
POST /agent-turns/{turnId}/operations/{operationId}/accept   → { operations, turn }
POST /agent-turns/{turnId}/operations/{operationId}/reject   → NotebookSnapshot + operations
POST /agent-turns/{turnId}/operations/accept-all             → { operations, turn }
POST /agent-turns/{turnId}/operations/reject-all             → NotebookSnapshot + operations
POST /agent-turns/{turnId}/cells/{cellId}/revert             → unchanged (alias, §3.5)
GET  /agent-turns/{turnId}                                   → gains turn.operations[]
```

**Preconditions, deliberately asymmetric:**

- **Reject / reject-all**: `sessionId` + `expectedDocumentRevision` + lease +
  composition hash. Identical rigour to every other mutation.
- **Accept / accept-all**: `sessionId` only. Idempotent. No revision
  precondition — accept mutates nothing, so a stale revision cannot cause harm,
  and returning 409 for "I read this diff" is user-hostile. *Alternative
  considered:* require the revision for uniformity with the spec's blanket
  precondition rule; rejected because that rule is scoped to document mutations
  and accept is not one.

`reject-all` is distinct from `undo(turn)`: it reverts only still-pending
operations and preserves ones the user already accepted, whereas checkpoint undo
restores everything including outputs. Both stay available.

### 3.7.1 Two consequences that need UI copy, not code

**Undo-turn discards accepted work too.** Checkpoint undo restores the pre-turn
notebook, so it also reverses hunks the user explicitly kept. That is correct
for "undo the turn", but after partial review the button's label is a lie. It
should read **"Undo entire turn"** and, once any operation is accepted, confirm:
*"This also reverses the N change(s) you kept."* Cheap to do; surprising if not.

**Several turns can hold live ledgers at once.** An older turn's operations stay
valid as long as no later turn touched their cells — strictly better than
today's behaviour, but the notebook renders only `selectedTurn`'s changes
(`NotebookView.tsx:124`). So a cell can carry unreviewed operations that are
invisible because a different turn is selected. Today this affects one icon;
with a finer ledger it hides real pending review state. Phase 1 needs at minimum
a per-cell indicator that unreviewed operations exist on another turn, and the
chat history should mark turns with outstanding operations. Full multi-turn
overlay rendering is out of scope.

### 3.8 Serialization

`serialize_turn` gains `operations`. `serialize_turn_summary`
(`agent_turn_routes.py:63`) already truncates aggressively under a 128 KB cap —
serialize operations there as **ranges, states, and counts only, never hunk
text**. Hunk text is reconstructible from `changes`, and when `changes` are
themselves truncated the existing `historyTruncated` → refetch-detail path in
`App.tsx:145-148` covers it. No new mechanism needed.

### 3.9 Events

Reuse `turn.updated` rather than adding an event type — the client's `fetchTurn`
already subscribes and refetches. One wrinkle: `fetchTurn` triggers a full
`refresh()` for terminal turns (`App.tsx:118`), so each accept would refetch the
whole notebook. Acceptable for Phase 1; if it shows up as jank, gate the
`refresh()` on the event carrying a revision change.

---

## 4. Frontend design — modelled on Cursor

### 4.1 The model being copied

Cursor's review surface, mapped onto notebook concepts. Their **file** tier is
this app's **cell** tier; everything else transfers directly.

| Cursor | Adopt as | Notes |
|---|---|---|
| Inline red/green diff decorations in the editor | Already exists (`CellEditor.tsx` `diffField`) | Switch to backend hunks (§3.2) |
| Per-hunk **Keep / Undo** on the decoration | Per-operation Keep/Undo | Phase 2 |
| Floating **Review panel** at the bottom of the editor, opened by a **Review** button on the agent's reply | Sticky **review bar** over the notebook surface, opened from the turn message | Phase 1 |
| **Keep All / Undo All / Review Next File** in that panel | **Keep all / Undo all / Next change** | Phase 1 |
| Review UI appears live while the agent is still running | Defer — the app already blocks mutations during a turn (`mutationsDisabled`) | Not Phase 1 |
| Changed-file list in the chat message | Changed-cell list in the turn message, click to jump | Phase 1; `onFocusCell` plumbing already exists (`App.tsx` `requestCellFocus`) |
| Per-message **checkpoint restore** | Per-turn checkpoint undo | Already exists |
| Setting: *Inline Diffs* off = "auto-keep mode" | Optional later (§7) | Not Phase 1 |

The shape is two tiers — granular review of applied changes, plus a coarse
whole-turn restore — which is what this design already had. Cursor's specific
contribution is the **review-session UI**: a persistent bar that tracks progress
through the change set and moves you to the next one.

### 4.2 What NOT to copy — Cursor's documented failures

Cursor's users report four concrete problems with this exact surface. Copying
the structure without these guards would import the bugs.

1. **Undo All sits next to Keep All in a layout that shifts as items are
   reviewed**, so users repeatedly destroy work by mis-clicking. There is no
   confirmation and no recovery.
   → **Guard:** fixed button positions that do not reflow as the count drops;
   Undo-all visually separated from Keep-all and styled destructive; a
   confirmation once anything has been kept (§3.7.1); reject stays recoverable
   via whole-turn undo (§3.4), which is precisely why B matters.

2. **`Cmd/Ctrl+Backspace` = reject-all collides with delete-word** while typing
   in the chat box — users wipe out an entire turn mid-sentence. Several
   separate forum reports.
   → **Guard:** no destructive action on a bare, common editing chord. See §4.5.

3. **Jump-to-next-change was lost** when arrow navigation was replaced by the
   floating panel; users now scroll manually to find hunks.
   → **Guard:** "Next change" is a Phase 1 requirement, not a nicety. The
   `focusRequest` → `scrollIntoView` plumbing in `NotebookView` already does
   exactly this for chat-to-cell jumps and can be reused.

4. **Review controls appear when nothing is pending.**
   → **Guard:** the bar renders only when the ledger has non-terminal
   operations for the selected turn; the settled state (§4.6) hides it.

Failure 1 is the same class of bug as §1.1's invisible-but-clickable revert
button. Two independent products hit it; treat "destructive review control that
is easy to hit by accident" as the primary hazard of this feature.

### 4.3 The riskiest change: reconciliation

`reconcileTurnChanges` (`App.tsx:442`) keeps a change visible only while
`cell.source === change.nextSource`. Once a single hunk is rejected that
equality breaks and **the entire cell's diff vanishes** — the remaining
un-reviewed hunks would disappear silently. This function must be rewritten to
reconcile against the ledger: a change stays visible while it has non-terminal
operations and the server-supplied `composedSourceHash` matches the cell's
current source.

This is the one place where a partial implementation is worse than none. It
should land in the same commit as the backend ledger.

**Rendering rule — the overlay shows pending operations only.** Undefined in
earlier drafts, and everything downstream depends on it:

| Operation state | Diff decoration |
|---|---|
| `pending` | green added lines + red removed markers — the current behaviour |
| `accepted` | **nothing** — reviewed means stop showing it |
| `rejected` | nothing; the lines are back to `previousSource` anyway |
| `stale` (derived) | decoration shown, controls disabled, tooltip explains why |

So accepting is what visually "clears" a diff, which is precisely the missing
gesture from gap #1. A cell settles when it has no pending operations left, and
the turn settles when no cell does.

**`turn.changes` becomes a historical record, not a render source.** After any
partial reject, `changes[].nextSource` no longer describes the cell — it
describes what the turn originally proposed. Keep it for history, audit, and
`previousSource`, but the notebook must render from `operations` plus the
server's composed hash. Any code still diffing `previousSource → nextSource` for
display is a bug after Phase 1; `CellEditor`'s `diffField` is the one that
matters (`CellEditor.tsx:28-35`).

### 4.4 Surfaces and components

Three surfaces, mirroring §4.1:

**A. Inline, on the hunk** (Phase 2) — Keep/Undo widgets on the CodeMirror
decoration, so the control sits on the change it acts on.

**B. Review bar** (Phase 1) — sticky footer over the notebook surface while the
selected turn has unreviewed operations:

```
┌──────────────────────────────────────────────────────────────┐
│  3 of 7 changes reviewed        [ Next change ]   [ Keep all ]│
│                                                    [ Undo all ]│
└──────────────────────────────────────────────────────────────┘
```
Counter, navigation, and the two roll-ups. Positions fixed regardless of the
counter (§4.2 guard 1); Undo-all separated and styled destructive.

**C. Changed-cell list in the turn message** (Phase 1) — the agent's reply lists
the cells it changed with per-cell Keep/Undo and click-to-jump, matching
Cursor's changed-file list. Reuses `onFocusCell`.

Component-level work:

- **`api/client.ts`** — `AgentChange` gains `operations: AgentOperation[]` and
  `composedSourceHash`; new `acceptOperation` / `rejectOperation` /
  `acceptAll` / `rejectAll` calls following the existing `mutation(snapshot)`
  pattern.
- **`CellEditor.tsx`** — `diffField` switches from
  `cellDiffRanges(previousSource, nextSource)` to backend-supplied hunks, and
  gains a per-hunk `Decoration.widget` carrying ✓ Keep / ↺ Undo. The existing
  `RemovedWidget` block-widget is the template; this is the bulk of the UI work
  and is why it is phased separately.
- **`NotebookCell.tsx`** — the single `RotateCcw` button (`NotebookCell.tsx:158`)
  moves *out* of the hover-only `.cell-actions` cluster and becomes a persistent
  per-cell review control (§4.6) with Keep-all / Undo-all and an `n of m` counter.
- **`AgentChatPanel.tsx`** — turn footer (`AgentChatPanel.tsx:105`) gains
  "Keep all changes"; "Undo turn" stays and is now *still enabled* after partial
  rejects (§3.4).
- **`App.tsx`** — new handlers threaded through `mutate()`. Accept handlers
  should pass `refreshAfter: false` since nothing document-level changed.

### 4.5 Keyboard

Out of scope; mouse-driven review is the target. Two constraints to respect
whenever bindings are added later:

- **Do not copy Cursor's `Cmd+Enter` (accept all)** — `Mod-Enter` already runs a
  cell (`CellEditor.tsx:53-56`). `Alt+Arrow` (cell nav, `NotebookView.tsx:115`)
  and `Cmd+S` (save, `App.tsx:232`) are likewise taken.
- **Do not copy Cursor's `Cmd+Backspace` (reject all)** — §4.2 guard 2. Bind no
  destructive review action to a common editing chord.

### 4.6 Presentation — review controls live with the diff

Direct response to §1.1. Review affordances must not go back into the hover-only
corner cluster.

- **Persistent, not hover-revealed.** A cell with unreviewed operations shows a
  review bar unconditionally. Reviewing is a state the user is *in*; hiding its
  controls until hover is the core mistake to avoid repeating.
- **Co-located.** Per-hunk Keep/Undo render as CodeMirror widgets attached to
  the hunk (the `RemovedWidget` block-widget pattern generalises), so the
  control sits on the change it acts on. The cell-level bar anchors to the cell
  header, not the floating corner overlay.
- **Labelled, not icon-only.** "Keep" / "Undo" as text, or icon + text. Six
  interchangeable 28px glyphs is what made the current control unfindable.
- **Visually distinct from scope/run actions.** Review is a different mode from
  "add to scope" and "run cell"; it should not share their affordance language.
- **Fix `opacity: 0` clickability.** Whatever stays hover-revealed in
  `.cell-actions` needs `pointer-events: none` while transparent, or
  `visibility`/conditional rendering instead of `opacity`. A destructive
  mutation behind an invisible-but-clickable button is a bug independent of this
  feature and can ship ahead of it.
- **Explain disappearance.** When a turn is deselected or its operations go
  `stale`, say so rather than silently removing controls.

### 4.7 Interaction rules

- Per-operation controls are disabled (visibly, with a reason) rather than
  removed while `mutationsDisabled || hasDirtyDrafts`, matching the existing
  `dependentDisabled` gate.
- A `stale` operation renders its diff with no buttons and a tooltip: *"This
  cell changed after the turn — use Undo turn or edit manually."*
- Once every operation in a turn is `accepted` or `rejected`, the turn is
  settled and all overlays clear. This is the explicit end-of-review signal the
  product currently lacks.

---

## 5. Outputs (Phase 3)

Downstream execution rewrites outputs and execution counts from the first edited
cell onward (`service.py:385-398`). Undoing a source hunk leaves those outputs
describing code that no longer exists.

Recommendation: model each executed cell as an `execution_output` operation in
the same ledger, restorable from the turn checkpoint. **Do not auto-revert
outputs when a source hunk is rejected** — a hidden second mutation is
surprising, and the user often wants to keep the output they just read. Instead
dim the outputs and offer re-run. The ledger's `kind` field exists from day one
so this slots in without a migration.

**But the warning belongs in Phase 1, not Phase 3.** Rejecting a source hunk
leaves outputs produced by code that no longer exists — a correctness trap, and
notebooks are exactly where stale outputs mislead people, because the output is
the result you are reasoning about. This already happens today via `revert_cell`;
per-operation reject will make it routine rather than rare.

The indicator is cheap and needs no ledger work: if a cell has any `rejected`
operation and a non-empty `outputs`, dim them and label *"Outputs are from code
you undid — re-run this cell."* Ship it with Phase 1; the restorable
`execution_output` operation can wait for Phase 3.

---

## 6. Concurrency

Nothing new. Reject takes a coordinator lease exactly as `revert_cell` does
today, so it serializes against agent turns, executions, saves, and manual edits
by the existing mechanism. Accept touches only `AgentTurnService._lock`.
Ordering discipline from `NotebookDocumentService`'s docstring — lease, then
document lock, never the reverse — is unchanged.

---

## 7. Optional, near-free extras

- **Un-reject.** Because `compose` is pure, flipping `rejected → pending` and
  recomposing restores a hunk. Costs one new state transition and reuses the
  whole reject path. Makes hunk review non-destructive and is worth strong
  consideration once §4.3 is in place.
- **Auto-keep setting** (§2.1.1). Default operations to `accepted` at apply time
  for users who don't want to review. Costs one flag; matches Cursor's
  *Inline Diffs* off / "auto-keep mode".
- **Live review during a turn.** Cursor shows review controls while the agent is
  still working. Deferred here: the app disables mutations for the whole turn
  (`mutationsDisabled`), so enabling this means letting a reject land against a
  turn that still holds the coordinator lease. Not a small change.

---

## 8. Phasing

| Phase | Scope | Why here |
|---|---|---|
| **0a** | Frontend-only: `pointer-events: none` on transparent `.cell-actions`; make the existing revert control persistent and labelled on changed cells. | Standalone bug + usability fix (§1.1). No backend change, so it ships under the frontend-verification rule alone. |
| **0b** | `operations.py`: diff port, `compose`, ledger dataclasses, computed at apply time, serialized. No behaviour change. | Purely additive and unit-testable in isolation. |
| **1** | Accept/reject at **cell** level on the ledger; `revert_cell` reimplemented; undo-survives-reject lineage fix; pending-turn eviction protection (§3.6.1); `reconcileTurnChanges` rewrite + rendering rule (§4.3); review bar + changed-cell list + Next-change navigation (§4.4 B/C); stale-output warning (§5). | Delivers the missing primitive (accept), fixes gap #3, and ships Cursor's review-session UI — zero CodeMirror work. |
| **2** | Hunk granularity in `CellEditor` decorations + per-hunk Keep/Undo widgets (§4.4 A) + keyboard (§4.5). | Pure UI on an already-correct backend; can be iterated on safely. |
| **3** | Output operations; optionally un-reject. | Independent value, no rework. |

Phase 1 is shippable and coherent on its own — if hunk-level UI proves fiddly,
the product still gains explicit accept and non-destructive partial reject.

---

## 9. If the product later wants true held proposals

For completeness, since "accept" invites the question. It would require a
two-layer document (committed + proposed) and every reader choosing a layer:
execution's source-hash guard, save-to-disk, save-as, export, autosave, the
on-disk baseline, and the agent workspace builder. That is a change to the
document core, not an additive feature, and it conflicts with the current model
where the agent's edit is validated *by running it*. Not recommended without a
product-level decision to change the review model.

---

## 10. Test plan

**Backend** (`backend/tests/test_agent_turns.py`, new `test_turn_operations.py`):

- `compose` round-trip: all-pending == `next_source`; all-rejected ==
  `previous_source`; deterministic operation IDs.
- Reject a middle hunk; reject the remaining hunks in any order → final source
  equals `previous_source`.
- Manual edit between rejects → 409 `OperationConflict`, cell's operations all
  `stale`, other cells' operations unaffected.
- **Whole-turn undo still eligible after N rejects**, and the restored notebook
  equals the pre-turn checkpoint. (Directly covers gap #3.)
- Accept is idempotent, bumps no revision, breaks no lineage.
- **State machine** (§3.1.1): `accepted → rejected` allowed; `rejected → accepted`
  is 409; accept and reject each idempotent from their own state.
- **`stale` is derived, not stored** (§3.1.1): edit a cell manually, then read
  the turn — its operations serialize as `stale` with *no* intervening reject
  attempt. This is the case a stored flag would get wrong.
- **Eviction protection** (§3.6.1): a turn with pending operations survives
  `_prune_history_locked` past `MAX_TERMINAL_TURNS`; past the pending ceiling
  the oldest force-settles to `accepted` and its notebook content is unchanged.
- Coarse-diff fallback yields exactly one operation.
- `_history_size` accounts for operations; pruning still respects the 2 MB cap.
- Lease conflict during reject → `MutationConflict`.
- **Concurrent status poll during reject does not destroy the checkpoint**
  (§3.4) — the race, not the sequential case.

**Frontend**: reconciliation after partial reject (diff for un-reviewed hunks
survives); per-hunk widget renders and wires to the right operation ID; controls
disabled under `mutationsDisabled`/dirty drafts. Plus the rendering rule (§4.3):
accepting an operation removes its decoration while pending ones in the same
cell keep theirs — the assertion that gap #1 is actually fixed.

**Visibility regressions cannot be tested in vitest — verified.** `styles.css` is
imported only by `main.tsx` (`main.tsx:3`), which unit tests never render; they
mount `App`/`NotebookView` directly. jsdom therefore has **no stylesheet at
all**, so `.cell-actions { opacity: 0 }` is never applied and `toBeVisible()`
returns true for a control no user can see. An earlier draft of this plan
proposed exactly that assertion; it would have been vacuously green.

Two consequences:

1. §1.1's regression guard belongs in **Playwright** (`e2e/`, real browser, real
   CSS), not vitest. Assert the review control is visible and hit-testable
   without hovering, e.g. `toBeVisible()` + `click({ trial: true })` with no
   prior hover.
2. Prefer **structural** fixes over CSS-dependent ones — conditional rendering
   rather than `opacity` — because those *are* assertable in jsdom. This is a
   further argument for §4.6's "persistent, not hover-revealed."

The same trap applies to any future assertion about the review bar's layout
stability (§4.2 guard 1): position and reflow are CSS, so those are e2e tests.

Regression tests for the Cursor failure modes (§4.2), one each — noting which
layer each belongs in, per the jsdom limitation above:

| Guard | Assertion | Layer |
|---|---|---|
| 1 | Undo-all confirms once anything is kept | vitest |
| 1 | Undo-all / Keep-all do not reflow as the counter changes | **e2e** (layout is CSS) |
| 2 | `Mod+Enter` in a cell editor still runs the cell; no destructive action bound to `Cmd/Ctrl+Backspace` | vitest |
| 3 | "Next change" moves focus to the next unreviewed operation, including across cells | vitest (assert focus/`scrollIntoView` call, not pixels) |
| 4 | Review bar absent when the selected turn has no non-terminal operations | vitest |

**E2E** (`e2e/notebook-editor.spec.ts`): run a turn, undo one hunk, keep another,
confirm the notebook body and that "Undo turn" is still offered and works.

---

## 10.1 Decisions — RESOLVED 2026-07-29

All six are settled. A and F were settled by the Cursor cross-check (§2.1.1);
B–E were delegated to the implementer and are recorded here with rationale so
the reasoning survives the decision.

| # | Decision | Resolution |
|---|---|---|
| **A** | What "accept" means | **Review marker** on already-applied changes. Cursor works the same way (§2.1.1); the held-proposal model (§9) stays deferred. |
| **B** | Spec amendment narrowing the lineage-breaking rule (§3.4) | **Amend.** See below. |
| **C** | Strict composition guard (§3.3) | **Strict.** See below. |
| **D** | Backend implementation approval, Phases 0b–2 | **Granted, local only.** See below. |
| **E** | Scope to build now (§8) | **0a → 0b → 1.** Phases 2–3 deferred. See below. |
| **F** | Naming | **Keep / Undo** in the UI, `accept` / `reject` in the API. Cursor uses the same words. |

**B — amend the spec.** The rule at `notebook-agent-editor-spec.md:1240-1242`
lists "revert" among the actions that void checkpoint undo. That was written
when revert was the *only* granular action; with a ledger it makes the feature
self-defeating, because the first hunk you undo destroys your ability to undo
the rest. The safety argument for the original rule does not apply: the
checkpoint still represents the pre-turn document exactly, so a full restore
remains correct no matter how many hunks were rejected first. Narrowed to manual
cell edits, import, manual execution, and new agent turns.

**C — strict guard.** Fuzzy context re-matching would let a hunk apply to a
region the user has since edited, which is a silent-wrong-answer failure in a
document people compute on. The strict guard's cost is a clean 409 and a
disabled button; the fuzzy guard's cost is a wrong notebook that looks right.
Consistent with every other precondition in this codebase.

**D — granted, local only.** Explicitly scoped: implement and commit backend
changes **in this worktree only**. Per AGENTS.md this does *not* authorise
pushing, deploying, or staging backend changes for a GitHub push — that needs a
separate approval, after `/code-review`.

**E — 0a → 0b → 1.** 0a is frontend-only and fixes a live bug independent of
everything else. 0b is additive and unit-testable in isolation. 1 is the first
slice that delivers user-visible value (the accept primitive + review UI) and
fixes gap #3. Phase 2 (hunk-level CodeMirror widgets) and Phase 3 (output
operations) are deferred until 1 is in use, because hunk granularity is only
worth its complexity if cell-level review proves too coarse in practice.

---

## 10.2 Validation log

Every code-dependent claim in this document was checked against the worktree
source on 2026-07-28. Claims are grouped by outcome.

**Confirmed as written:**

| Claim | Evidence |
|---|---|
| `Mod-Enter` / `Shift-Enter` run a cell — so Cursor's Cmd+Enter accept-all is unavailable (§4.5) | `CellEditor.tsx:53-56`, `Prec.highest` keymap |
| `Alt+Arrow` taken by cell navigation; `Cmd+S` by save (§4.5) | `NotebookView.tsx:115`, `App.tsx:232` |
| Jump-to-cell plumbing already exists, so "Next change" is cheap (§4.2 guard 3) | `NotebookView.tsx:23` `scrollIntoView`; `App.tsx:275` `requestCellFocus`; `App.tsx:390` `onFocusCell` |
| `is_undo_eligible` clears the checkpoint as a side effect (§3.4) | `service.py:216-222` |
| 2 MB history budget and `_history_size` exist and drive pruning (§3.6) | `service.py:36`, `service.py:616`, `service.py:597-604` |
| `.cell-actions` is `opacity: 0` until hover (§1.1) | `styles.css:75-76` |

**Corrected during validation — the design was wrong before these:**

1. **§3.1/§3.6 contradicted each other.** Hunks were modelled as tuples of line
   *text* while §3.6 claimed memory was near-free because text was not
   duplicated. Resolved by storing four indices per hunk.

2. **§3.4's hazard was understated.** `is_undo_eligible` is called from **five**
   sites, two of them in `session_routes.py` (the polled status endpoint), not
   just the three turn routes. That turns a sequencing note into a genuine race:
   a status poll landing inside the reject window destroys the checkpoint
   outright. Drove the added concurrency test and the `_invalidate_checkpoint()`
   recommendation.

3. **The proposed §1.1 regression test could not work.** `styles.css` is
   imported only by `main.tsx:3`, which unit tests never mount, so jsdom applies
   no stylesheet and `toBeVisible()` is vacuously true for an `opacity: 0`
   control. The guard moved to Playwright, and structural fixes are now
   preferred over CSS-dependent ones precisely because they are assertable in
   jsdom.

4. **`stale` as stored state was wrong** (§3.1.1). It could only become true on
   an attempted reject, because manual edits go through `update_cell_source`,
   which has no ledger awareness and no general mutation hook
   (`service.py:76` is session-replacement only). The UI would have shown live
   buttons on dead operations. Now derived at serialization from the same hash
   comparison the guard uses — accurate by construction, and it deletes a state
   and a would-be listener.

5. **Pruning could evict a turn mid-review** (§3.6.1). `_prune_history_locked`
   (`service.py:582-613`) pops whole turns and protects only
   `_latest_applied_turn_id`, so visible diffs could outlive their ledger and
   strand 404-ing buttons. Protection extended to turns with pending
   operations, with an explicit ceiling and force-settle to `accepted` rather
   than an unbounded exception.

6. **The state machine was undefined.** Accept/reject transitions were described
   as "idempotent" without saying what `rejected → accepted` does — which, taken
   literally, would silently re-apply an undone hunk. Now 409, with the
   deliberate un-reject kept in §7 where it belongs.

7. **The diff overlay's post-review behaviour was unspecified** (§4.3). Nothing
   said what an *accepted* operation renders as, which is the whole point of
   gap #1. Now: pending renders, accepted renders nothing, and `turn.changes` is
   demoted to a historical record rather than a render source.

**Not validated / open:**

- Whether Cursor holds inline-diff edits in an unsaved buffer or writes them to
  disk (§2.1.1). Unresolvable from public docs; moot for this design.
- No code was run. `pytest` and `vitest` baselines should be captured before
  Phase 0a, not assumed.

---

## 11. Spec changes required

1. `## Undo And Checkpoints` — narrow the lineage-breaking list (§3.4), add the
   operation ledger and composition guard, restate accept as review-only.
2. `## UI Behavior` (line 289) — per-operation keep/undo alongside per-cell and
   per-turn.
3. `## API Surface` (line 1360) — the four new endpoints.
4. Line 583-585 — clarify that apply-then-review now has an explicit review
   *settlement*, while held proposals remain deferred.
