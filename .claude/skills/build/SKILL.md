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
  **Ordering in a shared tree:** run the claim (`scripts/start.sh`, Workflow
  step 1) BEFORE the baseline — the WIP lock doubles as the test-suite lock
  against concurrent builder sessions; a baseline run outside the claim can
  collide with another session's suite.
- Work **in place** on the current branch. Do **not** create a branch or git
  worktree unless the user explicitly asked — the suite uses a dedicated port and a
  shared PostgreSQL DB, so parallel checkouts collide (see `.claude/rules/git.md`).

## Execution Strategy (from `build_strategy` / `build_model` frontmatter)

Absent fields mean `inline` + the session model; the user can override either at
build time. **Test-lock invariant — exactly one entity ever runs tests** (shared
port + shared Postgres): the session (inline), the delegate (delegate), or the
orchestrator (fanout). Sub-agents inherit the session's permission config; a
background builder *pauses* on any command the session wouldn't already allow, so
delegate/fanout work best when the session's mode covers tests, `bin/create_testproject`,
and `git commit`.

- **inline** — run this skill in-session, exactly as the Workflow below.
- **delegate** — spawn ONE builder sub-agent (model = `build_model`) that executes
  this entire skill end-to-end: claim → baseline → implement → test → docs →
  commit → post-build agents → close. Its prompt must point it at this file,
  `CLAUDE.md`, `.claude/rules/`, and the item file, and state explicitly: the
  item's `## Plan` is user-approved (skip the interactive confirmation gate);
  work in place on main (never branch/worktree); it is the ONLY test runner; the
  commit trailer names **its own** model; commits go by explicit pathspec (see
  `.claude/rules/git.md`); never push; if the baseline is red, STOP and report
  back instead of building. While it runs, the orchestrator stays
  hands-off the working tree — no edits, no test runs. On completion, verify
  (item Resolution, `var/test_failures.json`, `git log -p` spot-check) and relay.
  If the sub-agent cannot spawn the post-build agents itself, it performs those
  three passes inline, sequentially.
- **fanout** — L/XL items ONLY, and only when the plan defines **disjoint file
  partitions** (refuse otherwise). Orchestrator: claim + record the baseline
  BEFORE spawning; spawn one builder per partition (all share this one working
  tree — worktrees are forbidden), each implements code + tests for its partition
  and **NEVER runs `bin/run_tests`** (state this in every builder prompt);
  integrate their reports, then run targeted tests and the default suite
  yourself; loop failures back to the owning builder; make the single commit;
  then post-build agents and close — all orchestrator-side.

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
