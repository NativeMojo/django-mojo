---
name: maestro-build
description: >-
  Claim a planned maestro board item (owner + stage=building), execute its
  ## Plan inside this repo with full build discipline, keep the item's
  activity trail updated (commits, tests, blockers), and land it at
  review/done via the maestro MCP.
user-invocable: true
argument-hint: <item-id (omit to pick from the board)>
maestro-skill-version: 1
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
3. If `values.owner` is already set to someone else, stop and ask before
   taking it over.
4. **Claim** in one call:
   `update_board_item(item, values={"stage": "building", "owner": [<your user id from whoami()>]})`.
5. **Snapshot.** Write the pulled description to
   `planning/built/<item-id>.md` (create the directory if absent), first
   line: `<!-- generated from maestro item <id> — do not edit; the board item
   is the source of truth -->`. Commit it as the build-start marker.
6. Pull the description to `planning/.cache/<item-id>.md` (gitignored) — the
   working copy for the session.
7. Establish the repo's green test baseline before the first edit, per the
   repo's own test conventions. If the baseline is red, stop and tell the
   user — don't build on red without their say-so.

## Workflow

1. State what you're about to build (one sentence).
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
   "done"}`. Final comment: what changed + how to validate.
9. **On failure/blocker**: post a blocker comment, leave `stage=building`
   and the owner intact, and tell the user where it stands.

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
