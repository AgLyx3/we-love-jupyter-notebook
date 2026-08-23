# AGENTS.md

## Scope

These instructions apply to the entire repository.

## Project Context

This project is a local notebook-first editor for `.ipynb` files with scoped AI
agent editing. The product spec lives in:

- `docs/notebook-agent-editor-spec.md`

Read the spec before making architecture, backend, frontend, or agent-workflow
changes.

## Giving Suggestions

When suggesting an approach, option, tool, library, or fix, include **pros and
cons** for each realistic option.

- State the trade-offs honestly, including the **downsides of the option being
  recommended** — not just the drawbacks of the ones being rejected.
- Still end with a clear recommendation and the reason for it. Pros and cons are
  there to support a decision, not to replace one with a neutral list.
- Say what would change the recommendation ("pick the other one if X"), when the
  answer depends on something not yet known.
- Skip the options that are not genuinely on the table; do not pad the list to
  look thorough.
- This applies to informal suggestions in conversation, not only to formal
  written proposals or plans.

## Worktrees

- When starting a new task that is unrelated to the current work, create a fresh
  git worktree (on its own branch) and do the work there instead of the active
  checkout: `git worktree add -b <branch> <path> main`. This isolates independent
  work, keeps the main checkout and any running dev server undisturbed, and stops
  unrelated changes from mixing.
- Keep related follow-up work — fixes, reviews, and further iteration on the same
  feature — in that feature's existing worktree/branch; do not spawn a new one for
  a continuation.
- Remove a worktree once its branch is merged or abandoned: `git worktree remove <path>`.

## Out-Of-Scope Bugs

When you find a bug that is unrelated to the branch you are working on, and
fixing it belongs on a different branch, do not silently fix it in place and do
not drop it on the floor.

- Raise it to the user, and **while waiting for their response, go ahead and open
  a GitHub issue for it** (`gh issue create`) so the finding is captured rather
  than lost to the end of the session. Opening the issue does not need separate
  approval; fixing the bug still does.
- Write the issue so it stands on its own: what is broken, how to reproduce it,
  the `file:line` where it lives, and why it is out of scope for the current
  branch. Link the PR or branch that surfaced it.
- Label it (`bug`, `enhancement`, ...) and check the open issues first so a
  duplicate is not filed.
- Say in your reply to the user that the issue was opened, and give the number.
  If they would rather fold the fix into the current branch, close the issue.
- Keep the current branch focused on its own scope regardless — the issue is the
  mechanism for deferring, not a license to widen the change.

## Code Review

Run `/code-review` on the branch's changes **before handing any completed change
back to the user**, not only before a PR. "Completed" means a change presented as
done, ready to review, ready to test, or ready to commit.

- Run it on the branch diff (the work since `main`), so the review sees the change
  as a whole rather than one edit at a time.
- Report the findings and state how each was handled: fixed, or deliberately not
  fixed with a reason. Do not silently drop a finding.
- If the review surfaces nothing, say so explicitly — that is a result, not a
  reason to stay quiet.
- Exempt: pure documentation edits, and trivial one-line mechanical changes. When
  in doubt, run it.

For backend, permission-boundary, or notebook-validation changes, also run
`/security-review`. These are the areas where a defect breaks a product invariant
rather than just a feature — see "Agent And Permission Model" below.

`/code-review ultra` (multi-agent cloud review of the current branch) is stronger
but is **user-triggered and billed**; an agent cannot launch it. Suggest it for
large or risky branches instead of trying to run it.

### Pull Requests

- Do not open a PR until `/code-review` has been run on the branch and its
  findings addressed or explicitly acknowledged.
- Include a short note in the PR description confirming `/code-review` was run and
  summarizing how its findings were handled.

## Hard Rules

- Do not push, deploy, or otherwise publish backend changes without explicit user permission.
- Do not stage or commit backend changes for a GitHub push unless the user has explicitly approved those backend changes.
- Frontend UI changes may be pushed or prepared for deployment only after they have been verified locally.
- Do not treat frontend verification as approval for backend changes.
- Keep backend, frontend, agent-workspace, and notebook-validation changes clearly separated in summaries and commits.

## Backend Change Policy

Backend changes include, but are not limited to:

- FastAPI routes or services.
- Notebook parsing, mutation, validation, or checkpoint logic.
- Agent CLI invocation or temp workspace logic.
- Kernel execution and risky-cell classification.
- File upload/download behavior.
- Security, permission, or boundary-enforcement logic.

For backend changes:

- Explain the intended change before editing when practical.
- Run focused backend tests or clearly state why they could not be run.
- Ask before adding new backend dependencies.
- Ask before pushing, deploying, or including backend files in a release.

## Frontend Change Policy

Frontend UI work is allowed without separate approval when it is consistent with
the spec and does not change backend behavior.

Before preparing frontend UI work for GitHub deployment:

- Run the relevant frontend build, lint, or test command when available.
- Visually verify meaningful UI changes when a browser/dev server is available.
- Report what was verified.

## Agent And Permission Model

Preserve the core product invariant:

> Agent integration must not own notebook mutation. It can produce candidate
> cell-source changes; the Notebook Document domain applies or rejects them.

Do not weaken these guarantees without explicit user approval:

- Agent write permission is turn-scoped.
- Only cells in the current turn editable set may receive agent-written source changes.
- The live `.ipynb` document must not be edited directly by external CLI agents.
- Backend validation is authoritative; UI state is advisory.
- A CLI agent gets a shell only when it has no other way to reach files, and then
  only scoped to its own turn workspace. Claude uses a per-tool allow-list and no
  shell; Codex has no non-shell file API, so it runs under a sandbox confined to
  the turn workspace. The boundary is enforced by that sandbox plus the post-run
  workspace audit — not by the absence of a shell. See the spec's decision log
  entry "Sandboxed Shell For Adapters With No Other File API".

## Documentation

- Keep `docs/notebook-agent-editor-spec.md` aligned with major product or architecture changes.
- Any major permission-model or user-facing change must update the spec in the
  same change. This includes changes to the write/edit boundary, turn scope,
  what an agent is allowed to do (tools, read-only vs. editable turns), and
  meaningful notebook/chat UI behavior. Update the relevant section and, when the
  choice is non-obvious, add or amend a Decision Log entry.
- Prefer concise, durable documentation over scattered notes.
- Record meaningful architecture decisions in the spec decision log.
