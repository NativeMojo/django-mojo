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
- The item must be in `planning/confirmed/` (scoped). If it's still in `inbox/`,
  stop and run `/scope` first.
- Run `scripts/ready.sh planning/confirmed/<file>.md`. If it reports `BLOCKED`,
  stop and say so; only proceed on `READY`.

## Workflow
1. State what you're about to build (one sentence; include the ITEM id).
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
   `scripts/close.sh planning/confirmed/<file>.md` (stamps closed/branch/files
   changed and moves it to `planning/done/`).
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
- Building an item not in `confirmed/`, or that `scripts/ready.sh` reports BLOCKED
- Expanding scope beyond the current item
- Writing code before confirming the plan
- Skipping tests ("I'll add them later")
- For a bug: writing the fix before the failing regression test, or refactoring
  while fixing (open a separate `chore` item instead)
- Touching files not in the plan without flagging it first
- Pushing to remote, or staging with `git add -A` / `git add .`
