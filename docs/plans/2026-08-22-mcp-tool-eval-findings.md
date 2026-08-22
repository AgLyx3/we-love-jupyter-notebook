# What a model actually does with these tools

Status: two runs complete, 5/5 both times
Branch: `claude/app-mcp-browser-bundling-c11ed2`
Date: 2026-08-22

Companion to `2026-08-22-mcp-server-bundling.md` (§6 of its build order). The
harness is `evals/mcp_tool_eval.py`.

---

## 1. Why this is not a test suite

Everything else about the MCP surface is verified mechanically: the tools
answer, the gate holds, shaping shrinks a 92KB snapshot to 5KB. None of that
says whether a model *reaches for the right tool*, fetches more than it needs,
or understands what an error is telling it. Published tool guidance treats that
as part of building the tools rather than QA afterwards.

So the harness drives the real client — the `claude` CLI, configured as an MCP
client against this server — over notebook tasks, and records the tool calls it
made and what it said. It needs an authenticated CLI, costs tokens, and takes
about a minute per task, so it is run deliberately rather than in CI.

```bash
python evals/mcp_tool_eval.py            # every task
python evals/mcp_tool_eval.py fix-a-bug  # one by name
```

## 2. The tasks and what happened

All five passed on the first run. The tool sequences matter more than the
pass marks.

| Task | Tools called | Outcome |
|---|---|---|
| `fix-a-bug` | open → set_cell_source → run_cell → run_cell → save | Diagnosed the undefined `total`, fixed it, ran, saved. Correct output. |
| `answer-without-running` | open → read | Answered from source. One read, ran nothing, as asked. |
| `add-a-cell` | open → set_cell_source | Said plainly it could not add a cell. Left the existing cells intact. |
| `risky-cell-pauses` | open → run_cell → status → **show** | The cell did not run. It explained a person must approve, checked status, then opened the tab. |
| `plot-output` | open → run_all | Described the figure from the shaped output — "a single PNG figure (640x480)". |

Two of these are worth dwelling on.

**The gate behaved as designed, and the model behaved better than expected.**
Nobody was there to approve, so the cell stayed unrun. The model did not
report success, did not report a crash, and did not retry: it checked
`notebook_status` and then called `notebook_show` — reaching for the browser
tab to put a person in front of the decision. That is exactly the loop the tab
exists for, and it was not prompted for it.

**Shaping did its job invisibly.** The plot task never saw a byte of base64 and
still described the figure accurately, because `text/plain` (`<Figure size
640x480>`) survives the summary alongside the size descriptor.

## 3. Two runs, and what moved between them

The suite was run again after the fixes in §4. Both runs passed five of five,
but the tool sequences differed — which is itself the most useful thing the
second run produced.

| Task | Run 1 | Run 2 |
|---|---|---|
| `fix-a-bug` | open → set_source → run_cell → run_cell → save | open → set_source → **run_all** → save |
| `answer-without-running` | open → read | **open** alone |
| `add-a-cell` | open → set_source | open → set_source |
| `risky-cell-pauses` | open → run_cell → status → show | **open → run_cell** |
| `plot-output` | open → run_all | open → run_all |

Three things follow.

**The pause fix paid for itself immediately.** `risky-cell-pauses` went from
four calls to two. With the wait shortened (§4.2), `notebook_run_cell` returns
the pause and what it is waiting for directly, so the model no longer has to
chase it with `notebook_status` and `notebook_show` to find out. Same outcome,
half the round trips.

**`notebook_open` already answers the question `notebook_read` was for.** Run 2
answered the colour question from `open` alone, because open returns a shaped
notebook rather than a bare handle. That is a nice economy and worth keeping in
mind before adding anything to what `open` returns.

**Sequences are not stable, so single-run conclusions are weak.** The same task
picked `run_cell` twice in one run and `run_all` in the next; both are
defensible. Nothing here is a large enough sample to tune a description
against, which is the main caveat on everything above.

## 4. What the runs changed

### 4.1 Relative paths were resolved by luck (fixed)

Asked to work on `analysis.ipynb`, the model passed exactly that — a relative
path. It worked, and it worked for the wrong reason: the editor child process
inherits its parent's working directory, which in the harness happened to be
the workspace. Anywhere else, the same call would have resolved against an
unrelated directory and either failed confinement or, worse, opened a different
file of the same name.

`resolve_requested_path` now anchors a relative path to the workspace root
before it reaches the editor, and the tool description says so. An absolute
path is passed through untouched, so confinement still judges it rather than
having it silently rewritten to look local.

### 4.2 A run tool held the call open far too long (fixed)

`EXECUTION_POLL_TIMEOUT_SECONDS` was ten minutes — chosen so a person had time
to notice the tab. No hang was observed; the model coped. But holding a tool
call open for minutes gives the caller nothing to act on and invites the
client's own timeout to fire instead, and the eval showed the model already
reaching for `notebook_status` after a pause. So the wait is now 45 seconds and
hands back what it is waiting for; the run continues in the background. The
second run measured the effect: four tool calls became two.

### 4.3 The harness crashed instead of reporting (fixed)

A task that outran its budget raised `TimeoutExpired` out of `run_task` and
took the suite with it. A tool that never returns is a finding, not an
accident — it is now recorded as `could-not-run`.

### 4.4 A check that watched one cell (fixed)

`add-a-cell` verified that the `data` cell was untouched, which an edit to
`report` would have passed. It checks every pre-existing cell now.

## 5. Still open

**~~There is no way to add or delete a cell.~~ Closed.** This was the finding,
and it was real: `/cells/{id}/source` edited an existing cell and structural
change happened only inside a Trusted-mode agent turn, so "add a cell that
plots this" was not expressible by anything driving the editor through its API.
The model handled it as well as it could — it said so, and did not improvise by
overwriting a neighbour — but the answer was to build the thing.

`POST /cells` and `DELETE /cells/{id}` now exist, with `notebook_insert_cell`
and `notebook_delete_cell` in front of them. They go through the same
structural applier a Trusted turn uses rather than a parallel path, so an added
cell gets the same fresh id, the same `metadata.agent_authored` provenance the
editor renders as a badge, and the same atomic all-or-nothing apply. Both carry
the revision precondition, so an insert against a stale read is refused like
any other write.

Two decisions worth recording. An added code cell is **not** executed: it
arrives inert and running it is a separate, gated call, which keeps
review-before-run meaningful for cells a person did not type. And the last
remaining cell cannot be deleted — nbformat tolerates an empty notebook, but
the editor has no way back from one.

**The tool names double up.** A client sees `mcp__notebook__notebook_open`:
MCP already namespaces by server, so the `notebook_` prefix repeats it. The
guidance to prefix tools by domain was written for a flat tool list, and MCP is
not one. Renaming to `open` / `read` / `run_cell` would read better in a
client, at the cost of being ambiguous in any context that flattens the names.
Not changed yet — it is a rename with no behavioural content, and worth doing
once rather than twice.

**Coverage is thin.** Five tasks, two runs, one model. Nothing here
measures cost, and a single pass cannot separate a good tool description from a
lucky sample. The obvious next additions: a conflict task (someone edits in the
tab mid-flight), a large-notebook task where `cells=` selection should be
preferred over a whole read, and more repeats — two runs already show the
sequences moving, so a handful more would say whether that matters.
