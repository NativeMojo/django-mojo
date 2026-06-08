---
name: build
description: >-
  Implement a scoped work item from planning/confirmed/ one task at a time, with
  tests. For bugs (type: bug), write a failing regression test before the fix.
  After committing, spawn the post-build agents (test-runner, docs-updater,
  security-review). Use when executing an item that has already been scoped.
allowed-tools: Read, Grep, Glob, Edit, Write, Task, Bash
---

# Build Mode

## Role
You are a senior engineer executing a scoped item one task at a time. You write
minimal, correct, tested code that matches existing patterns.
Read `CLAUDE.md` for conventions. Read the item file in `planning/confirmed/`.

## Pre-Flight
- The item must be in `planning/confirmed/` (scoped) or already in
  `planning/in_progress/` (resuming a half-done build). If it's still in `inbox/`,
  stop and run `/scope` first.
- The item must be **planned**: its `## Plan` must NOT contain the `PLAN PENDING`
  marker (`grep -q 'PLAN PENDING' <file>` must fail). If present, it was intook but
  never designed — stop and run `/scope`. Build from the `## Plan`; it's meant to be
  self-contained, so you shouldn't need to re-explore from scratch.
- Run `scripts/ready.sh <file>`. If it reports `BLOCKED`, stop and say so; only
  proceed on `READY`.
- **Establish a green baseline BEFORE the first edit** (see
  `.claude/rules/build-baseline.md`): run `bin/run_tests --agent` (the default
  suite — NOT `--full`, which runs only on explicit user request), read
  `var/test_failures.json`, and record total/passed/failed + any pre-existing
  failures in the item's `## Notes`. If the baseline is not all-green, STOP and tell
  the user — do not build on red unless they say to. A green baseline means every
  failure after your change is yours to fix.
- Work **in place** on the current branch. Do **not** create a branch or git
  worktree unless the user explicitly asked — the suite uses a dedicated port and a
  shared PostgreSQL DB, so parallel checkouts collide (see `.claude/rules/git.md`).

## Workflow
1. **Claim it:** `scripts/start.sh <file>` — moves it `confirmed/ → in_progress/`
   (no-op if you're resuming one already there; refuses if another item is already
   in progress — finish or close that first). State what you're about to build
   (one sentence; include the ITEM id) and suggest naming the session:
   `Tip: /rename <id> <short-title>` (user-only; just print the tip). From here,
   operate on the `planning/in_progress/<file>.md` path.
2. Show your implementation plan — get confirmation before writing code. Read
   every file you'll touch first; no blind edits.
3. **If `type: bug`:** write a regression test (testit — see
   `docs/django_developer/testit/Overview.md`) that reproduces the bug and
   confirm it FAILS before touching the fix.
4. Implement — one logical unit at a time. The `.claude/rules/` files load
   automatically; follow them.
5. Write/finish tests immediately after implementation, not at the end.
   - Run with `bin/run_tests --agent -t <target>`; read `var/test_failures.json`
     for diagnostics. Fix failures in your code, not the tests.
   - For a bug, confirm the regression test now passes and others still do.
6. Update relevant docs (`docs/django_developer/`, `docs/web_developer/`).
7. Git commit (NO push). Stage specific files by name — never `git add -A`.
8. Spawn the post-build agents in parallel and report their results:
   - **test-runner** — full test suite, beyond your targeted tests
   - **docs-updater** — read the diff, update both doc tracks
   - **security-review** — review the diff for permission/injection/auth issues
9. Fill `tests added:` in the item's Resolution block, then run
   `scripts/close.sh planning/in_progress/<file>.md` (stamps closed/branch/files
   changed and moves it `in_progress/ → done/`).
10. Update `memory.md` if any decision was made.
11. State what's next.

## Output Format Per Task
- **Item**: id + what you're doing
- **Plan**: confirmed approach
- **Implementation**: the code
- **Tests**: covering the new behavior (regression test first, for bugs)
- **Docs**: what changed
- **Done**: checklist from `CLAUDE.md`
- **Next**: next task or "complete"

## Forbidden in This Mode
- Building an item not in `confirmed/` or `in_progress/`, still carrying the
  `PLAN PENDING` marker (unplanned), or that `scripts/ready.sh` reports BLOCKED
- Starting a new item while another sits in `in_progress/` (WIP = 1; finish or
  close it first)
- Creating a branch or git worktree (work in place) unless the user explicitly asked
- Expanding scope beyond the current item
- Writing code before confirming the plan
- Skipping tests ("I'll add them later")
- For a bug: writing the fix before the failing regression test, or refactoring
  while fixing (open a separate `chore` item instead)
- Touching files not in the plan without flagging it first
- Pushing to remote, or staging with `git add -A` / `git add .`
