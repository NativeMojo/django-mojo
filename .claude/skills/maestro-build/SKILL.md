---
name: maestro-build
description: >-
  Claim a planned maestro board item (owner + stage=building), execute its
  ## Plan inside this repo with full build discipline, keep the item's
  activity trail updated (commits, tests, blockers), and land it at
  review/done via the maestro MCP.
user-invocable: true
argument-hint: <item-id (omit to pick from the board)>
maestro-skill-version: 3
---

# Maestro Build — Execute a Planned Item

You are a senior engineer executing a scoped item one task at a time: minimal,
correct, tested code matching the repo's existing patterns and conventions
(read the repo's `CLAUDE.md` / rules first). The board item is the work
record — keep its stage and activity trail current the whole way.

## Board Resolution

Same as `maestro-task`: read `.claude/maestro.json`; on any miss, resolve via
`whoami()` / `list_workspaces()` / `list_boards()`, ask, offer to write the
file. Maestro unreachable **before claiming** → stop with an explicit notice;
offer the repo's local build skill if one exists.

## Pre-Flight

1. **Pick the item.** With an item-id argument, use it. Without one, call
   `get_board(board)` and list items whose `values.stage` is `planned` (id,
   title, priority) — ask the user which to build. Never claim silently.
2. `get_board_item(item)`. The description must contain a `## Plan` section —
   if not, stop and point at `/maestro-scope <item-id>`.
   **Refuse a `parked` item.** Parking is a deliberate "not now", and the
   plan of a parked item is presumed stale. Say so and stop: it is resumed
   from the board (or the drawer's Resume button) and re-scoped first.
3. If `values.owner` is already set to someone else, stop and ask before
   taking it over.
4. **Claim** in one call:
   `update_board_item(item, values={"stage": "building", "owner": [<your user id from whoami()>]})`.
5. **Snapshot.** Write the pulled description to
   `planning/built/<item-id>.md` (create the directory if absent), first
   line: `<!-- generated from maestro item <id> — do not edit; the board item
   is the source of truth -->`. Commit it as the build-start marker.
   (`planning/` is deliberately **not** indexed into the knowledge base —
   the snapshot is a git provenance record, not knowledge. See item 317.)
6. Pull the description to `planning/.cache/<item-id>.md` (gitignored) — the
   working copy for the session.
7. Establish the repo's green test baseline before the first edit, per the
   repo's own test conventions. If the baseline is red, stop and tell the
   user — don't build on red without their say-so.

## Workflow

1. **Orient the user before building.** They are typically running several
   sessions at once and may not remember what `<item-id>` refers to. Before
   the first edit, state in a few lines: `#<id> — <title>`, the original ask
   in one sentence in the requester's terms, what the plan will change, and
   anything in the plan you already intend to deviate from. Then build.
2. Read every file the plan touches before editing — no blind edits.
3. **If the workspec header says `Kind: bug`:** write a regression test that
   reproduces the bug and confirm it FAILS before touching the fix.
4. Implement one logical unit at a time, following the repo's conventions.
   Write/finish tests immediately after each unit, not at the end. Fix
   failures in your code, not the tests.
5. Commit each logical unit per the repo's git conventions (no push unless
   the repo's rules say otherwise).
6. **After each commit / test run / blocker**, post to the trail:
   `comment_on_item(item, ...)` — commit hash + one-line summary, test
   counts, or the blocker. If the plan itself changed during the build, push
   the updated scratch file back with `update_board_item(item,
   description=...)`.
7. Update the repo's docs and changelog per its conventions.
8. **Close.** PR opened → `update_board_item(item, values={"stage":
   "review"})`; committed straight to the main branch → `values={"stage":
   "done"}`. Final comment on the trail: what changed + how to validate.
   Then report back to the user TL;DR-first (see below).
9. **On failure/blocker**: post a blocker comment, leave `stage=building`
   and the owner intact, and tell the user where it stands — in the same
   TL;DR-first shape.

## Reporting Back

A build session is long and the user has usually been elsewhere for it. Both
the step-1 orientation and the closing summary name the item as
`#<id> — <title>`; a bare id is not something a user juggling parallel
sessions can place.

The closing summary opens with a short block, before any detail:

- **What shipped** — the item title plus the original ask in one sentence, in
  the requester's terms, not the codebase's.
- **What changed** — the commits, one line each.
- **What deviated from the plan** — anything done differently and why, or
  "followed the plan" if nothing moved. Never let a deviation surface only in
  a diff.
- **Verification** — the commands run and their result, stated plainly. If a
  test failed, was skipped, or could not run, it goes here, not omitted.
- **Stage** — where the item landed, and what remains if it is not `done`.

Keep it to roughly 15 lines. Detail lives in the commits and the item's
activity trail — point at them rather than inlining them.

A blocker report follows the same shape: name the item, what stopped it,
what is committed so far, and what you need to proceed.

## Outage Mid-Build

Never block the build on maestro. Finish locally against the scratch copy;
collect the stage flip and pending comments in your final summary as exact
tool calls for the user (or next session) to replay. Retry each push once
before queueing it.

## Forbidden

- Building an item with no `## Plan`, or claiming over someone else's owner
  without asking
- Expanding scope beyond the item; touching files outside the plan without
  flagging it first
- Skipping tests, or leaving the item's stage stale after the build ends
- Closing a build without stating what deviated from the plan and how
  verification actually went
