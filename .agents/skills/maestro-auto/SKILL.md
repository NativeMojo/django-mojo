---
name: maestro-auto
description: >-
  Scope and build a batch of maestro board items in one mostly-autonomous run:
  parallel sub-agent scoping, a cross-item coherence pass, ONE consolidated
  approval gate, then sequential sub-agent builds verified by ONE closing test
  run. Interrupts the user once instead of twice per item, and only for
  decisions that change the shape of the work.
---

<!-- Generated from .claude/skills/maestro-auto/SKILL.md (maestro-skill-version: 1). Do not edit directly. -->

# Maestro Auto — Batch Scope + Build, One Gate

`$maestro-scope` then `$maestro-build`, run over several items as one batch.
The engineering discipline is unchanged — two costs drop: the user is
interrupted **once**, at a single approval gate, instead of twice per item; and
the batch is verified by **one** suite run at the end instead of one per item.

You are the orchestrator, not the builder. Two roles, kept strictly apart:

- **Sub-agents touch code.** Scoping, red-teaming, and building each run in a
  fresh-context sub-agent (frontier model, high reasoning). They read, plan, edit, test,
  and commit; they do not write to the board.
- **You own the board and the batch.** Every `update_board_item`,
  `comment_on_item`, stage flip and claim goes through you. A sub-agent that
  dies mid-task therefore never leaves a half-updated board, and stage/owner
  state stays consistent with what actually happened.

Use this when the user hands you several already-filed items and wants them
worked through with minimal supervision. Do **not** use it for a single item
(`$maestro-scope` + `$maestro-build` are clearer), for anything unfiled (that
is `$maestro-task`), or for work too small to track (`$maestro-vibe`).

## Board Resolution

Same as `maestro-task`: read `.claude/maestro.json`; on any miss, resolve via
`whoami()` / `list_workspaces()` / `list_boards()`, ask, offer to write the
file. Maestro unreachable or unauthenticated → stop with an explicit notice.
Never fall back silently. Call `get_board(board)` once and keep the column
schema; match stage/priority options **by value**.

## Pre-Flight — admit the batch

1. **Resolve the items.** Item ids from the arguments; with none, call
   `get_board(board)` and list `inbox` + `planned` items (id, title, stage,
   priority) and ask which to run. Never pick silently.
2. `get_board_item(item)` for each. Classify:
   - `inbox`, no `## Plan` → **scope then build**
   - `planned`, has `## Plan` → **build only** (skip phases 1-2)
   - `parked` → **reject**, always. Parking is a deliberate "not now" and its
     plan is presumed stale; it is resumed and re-scoped first.
   - already `building`, or owned by someone else → ask before including it.
3. **Cap the batch.** More than about six items in one run is not autonomy,
   it is an unreviewable diff: propose a first batch of the highest-priority
   items and say what you left for the next run.
4. `get_workspace_context(workspace)` once — the `rule` docs apply to every
   item in the batch.
5. **Green baseline, once for the whole run**, per the repo's test conventions.
   This is the run's only up-front suite run and the reference point for the
   closing one. Red baseline → stop and tell the user; do not build a batch on
   red.
6. Tell the user the run is starting: the item list as `#<id> — <title>`, which
   items will be scoped vs built-only, and that the next thing they hear from
   you is the single approval gate.

## Phase 1 — Parallel Scoping

Spawn one sub-agent **per item, all in one message** so they run concurrently.
Scoping is read-only, so it parallelizes cleanly — this is where the wall-clock
saving comes from.

Each agent's brief: follow the `maestro-scope` workflow for exactly one item —
read every file the workspec references, check existing patterns and helpers in
the target app, fetch framework docs when framework features are involved — and
**return the complete `## Plan` markdown as its result**: objective, ordered
steps with file paths, design decisions with rationale, edge cases, testing
plan, documentation plan. Also require it to return, separately:

- **Files it intends to create or modify** — the input to phase 3.
- **Schema/migration impact** — models and tables touched, or "none".
- **Contract changes** — endpoints, signatures, or serialization shapes whose
  behavior changes for existing callers.
- **Premise check** — anything in the workspec that turned out to be false,
  already shipped, or impossible as written.

Give each agent the workspec, the workspace rule docs, and read access to the
repo. Forbid edits: phase 1 writes nothing but its own answer.

## Phase 2 — Challenge

For each returned plan, spawn one fresh-context red-team agent (in parallel
with the others) whose input is the workspec, the draft plan, and the
workspace `challenge` skill doc — or, if the workspace lacks it, its core
rules: name untested assumptions, argue the strongest opposing case, never
invent a flaw to perform thoroughness. Its brief is to **refute the plan**.

Verify each objection yourself — you have repo access, the challenger may not
run code — and give every one a disposition in a `### Challenge` subsection of
that item's plan: `amended` (say what changed) or `rebutted` (with evidence,
not "considered"). "No substantive challenge" is a valid verdict.

**An objection you cannot dispose of is a gate item, not a footnote.** It goes
to the user in phase 4 as an open question.

## Phase 3 — Batch Coherence

This phase is the reason the batch exists, and only the orchestrator can do it:
each scoping agent saw one item, you see all of them. Cross-check the returned
plans for:

- **File collisions** — two plans editing the same file. Usually fine; flag it
  when they change the *same function or contract* in incompatible directions.
- **Migration collisions** — two items adding migrations against the same
  model or table. These must be ordered explicitly, never interleaved.
- **Dependency order** — item B's plan builds on a file or helper item A
  creates. Produce an explicit build order and state the reasoning.
- **Redundant work** — two plans independently adding the same helper. Pick
  one owner for it and amend the other plan to consume it.
- **Obsolete items** — an item another plan (or already-shipped work) makes
  unnecessary. Propose dropping it rather than building it.

Amend the affected plans and record what you changed and why. The build order
you produce here is the order phase 5 runs in.

## Phase 4 — The One Gate

Push each finished plan to its item first, so the record exists before any
approval: `update_board_item(item, description=<workspec + plan>,
values={"stage": "planned"})`, then `comment_on_item(item, <3-5 line plan
summary>)`. Push failures retry once, then keep the plan locally and report the
exact manual sync call — never lose a plan.

Then present **one** consolidated brief and stop for approval. This is the
user's single decision point, so it must be complete and short:

- **The batch** — one line per item: `#<id> — <title>`, plan in a sentence,
  size (commits / tests / docs).
- **Build order** — the sequence, and any dependency that forced it.
- **What scoping changed** — only findings that change the shape of the work:
  a false workspec premise, a bug found on the way, an item now obsolete.
- **Cross-item issues** — collisions, shared helpers, migration ordering.
- **Open questions** — every undisposed challenge objection and every decision
  you judged the user's to make. Name your recommendation for each.
- **What you decided without asking** — a compact list, so a wrong call is
  catchable here rather than in the diff.

Roughly 20 lines for the whole batch, not per item. Then wait. Do not start
building until the user approves; if they approve part of the batch, run that
part and say plainly what you dropped.

## Phase 5 — Sequential Builds

**Build items one at a time, in the phase-3 order.** In a repo whose test suite
targets a fixed port and a shared database — and whose work commits to one
branch in one working tree — concurrent builds collide on the port, corrupt the
shared database, and interleave commits. Sequential is not a limitation to
route around; it is correctness. Only run builds concurrently if you have
verified the repo gives each agent an isolated checkout, test port, **and**
database, and say so explicitly when you do.

For each item, in order:

1. **Claim it** yourself: `update_board_item(item, values={"stage":
   "building", "owner": [<the user's id from whoami()>]})`.
2. **Snapshot** the approved description to `planning/built/<item-id>.md`
   (create the directory if absent), first line: `<!-- generated from maestro
   item <id> — do not edit; the board item is the source of truth -->`. Commit
   it as the build-start marker.
3. **Spawn one build sub-agent** (frontier model, high reasoning) with the approved plan,
   the repo's conventions, and the batch context it needs — the build order,
   what earlier items in this run already landed, and any shared helper another
   item introduced. Its brief is the `maestro-build` workflow for one item:
   read before editing, implement one logical unit at a time with tests written
   alongside the change, commit per the repo's git conventions, update docs and
   changelog. It returns commits (hash + one line), the tests it wrote,
   deviations from the plan, and anything left open. **It does not touch the
   board, and it does not run the full suite** (see below).
4. **Post the commit trail yourself**: `comment_on_item(item, ...)` with the
   commits and any deviation from the plan. Leave the item at `building` — it
   is built, not verified.
5. **Check the tree before moving on.** Working tree clean, commits present,
   nothing staged from the next item. A build agent's report is a claim; the
   suite settles it, once, at the end.

## Verification — One Suite Run for the Batch

Per-item suite runs are the single biggest time cost in a batch, and in a repo
that serializes tests they cannot overlap. So the run has **two** suite runs
total: the pre-flight baseline and one closing sweep.

- Build agents run **no** suite. They write tests as part of the change.
- The one exception is a bug's regression test, which proves nothing unless it
  is seen to fail: the agent runs **that test alone** before the fix and after.
  Targeted, seconds, not the suite.
- After the last item lands, **you** run the repo's full suite once.
- **Green** → flip every built item in one pass: `review` if it went to a PR,
  `done` if it went straight to the main branch. This is the first moment any
  item may be called done.
- **Red** → attribute each failure to the item that owns the touched files
  (the per-item commits make this unambiguous), fix in place, and re-run the
  full suite. One sweep per fix round, never per item. Items whose failures are
  fixed flip normally; an item you cannot get green stays at `building` with a
  blocker comment naming the failing tests.
- Report the real numbers against the baseline. A failure, a skip, or a suite
  that could not run is stated, never omitted.

Deferred verification is why nothing is marked `done` mid-run: an item's stage
tells the truth about it, and until the sweep runs the truth is "built".

## Failure Policy

- **A failed item does not fail the batch.** Post a blocker comment, leave the
  item at `building` with its owner intact, and continue with the remaining
  **independent** items.
- **Halt items that depended on the failure.** Say which, and why, rather than
  building on a missing foundation.
- **Halt the whole run** on: a red baseline, a failure that invalidates a later
  item's approved plan, or a needed change that goes beyond what the user
  approved at the gate.
- A build agent that fails outright (crash, unusable result) is retried once
  before the item is blocked — its item is the only one affected.
- Mid-run maestro outage: never block a build on it. Finish locally, retry each
  push once, then queue the pending stage flips and comments in the final
  report as exact tool calls for replay.

## Autonomy Contract

The point of this skill is deciding things the user should not have to. Decide
yourself, log it at the gate or in the final report, and move on: naming, file
placement, test organization, which existing helper to reuse, step order within
an item, doc wording — anything the repo's conventions, the workspec, or the
code already answer.

Raise it — at the gate if you know before building, immediately if you find it
mid-run:

- Two plans changing the same contract in incompatible directions
- Two items migrating the same table or model
- A workspec premise that turned out false, or an item now obsolete
- Fail-closed permission design, or any new auth surface
- An item materially bigger than filed (a multi-session build)
- A red-team objection you could not dispose of
- Any deviation from the approved plan that changes what the user agreed to

Never stop to ask a question the repo, its rules, or the workspec answers. And
never suppress a question because the run was supposed to be autonomous — an
unasked question becomes a wrong diff.

## Final Report

TL;DR-first, once, for the batch. The user has been away for the whole run and
a bare id is not something they can place — every item is `#<id> — <title>`.

- **What shipped** — one line per item: title, the original ask in the
  requester's terms, where it landed.
- **What deviated** — per item, what was done differently than the approved
  plan and why. Never let a deviation surface only in a diff.
- **Verification** — the closing suite result against the baseline, stated
  plainly, plus how many fix rounds it took. Failures, skips, and anything that
  could not run go here, not omitted.
- **Not done** — blocked, halted, or dropped items, with what each needs.
- **Queued board updates** — any push that never landed, as replayable calls.

Roughly 20 lines. Detail lives in the commits and each item's activity trail —
point at them.

## Forbidden

- Building any item the user did not approve at the gate
- Building an item with no `## Plan`, a `parked` item, or one owned by someone
  else without asking
- Concurrent builds in a repo with a shared test port, database, or working
  tree
- Letting sub-agents write to the board, or reporting a sub-agent's claimed
  test result as verified fact without checking it
- Flipping any item to `review`/`done` before the closing suite run
- Trading the closing suite run away for speed, or reporting a batch as verified
  on targeted tests alone
- Skipping the challenge phase, the coherence pass, or the gate to "save time"
- Leaving any item's stage stale when the run ends
