---
name: maestro-scope
description: >-
  Pull a maestro board item, scope it inside this repo with full investigation
  rigor, append a file-level ## Plan to its workspec, and push it back
  (stage=planned) via the maestro MCP.
user-invocable: true
argument-hint: <item-id (omit to pick from the board)>
maestro-skill-version: 1
---

# Maestro Scope — Design the Plan on the Item

Scoping runs **inside the target repo** with full code access; only the
storage target differs from file-based scoping — the plan is written back to
the board item's workspec, not a local planning file.

## Board Resolution

Same as `maestro-task`: read `.claude/maestro.json`; on any miss, resolve via
`whoami()` / `list_workspaces()` / `list_boards()`, ask, offer to write the
file. Unreachable or unauthenticated → stop with an explicit notice; offer
the repo's local scoping skill if one exists. Never fall back silently.

## Workflow

1. **Pick the item.** With an item-id argument, use it. Without one, call
   `get_board(board)` and list the items whose `values.stage` is `inbox` or
   `scoped` (id, title, priority) — ask the user which to scope. Never pick
   silently.
2. **Pull.** `get_board_item(item)` → write the `description` verbatim to
   `planning/.cache/<item-id>.md` (create `planning/.cache/` if absent and
   make sure it is gitignored — offer to add the entry). This scratch file is
   the working copy for the whole session; edit it, not the item. Read the
   item's activity trail too — requester comments are scope input.
3. **Context.** `get_workspace_context(workspace)` — apply `rule` docs.
4. **Deep exploration.** Read every file the workspec references; check
   existing patterns and helpers in the target app; fetch framework docs when
   framework features are involved. The investigation depth must match a
   local scoping session — the board changes storage, not rigor.
5. **Design.** Append a `## Plan` section to the scratch file:
   - Objective (exact outcome)
   - Ordered implementation steps with file paths
   - Design decisions (why this approach over alternatives)
   - Edge cases and error handling
   - Testing plan (what to add/update, run commands)
   - Documentation plan
   The plan must be complete enough that a build session can execute it
   without re-exploring. Resolve open decisions; don't leave both options.
6. **Present** the plan to the user; iterate until confirmed.
7. **Push.** `update_board_item(item, description=<full scratch file
   contents>, values={"stage": "planned"})` — description replaces whole.
   Then `comment_on_item(item, <3-5 line plan summary>)`.
8. Hand off: "run `/maestro-build <item-id>` to build it."

## Push Failures

Retry once. If it still fails, keep the scratch file and tell the user
exactly which item to sync manually (`planning/.cache/<item-id>.md` →
`update_board_item(<id>, description=...)`). Never lose the plan.

## Rules

- Do NOT implement. Planning and documentation only.
- Every endpoint designed must have fail-closed permissions.
- Keep repo dumps out of the workspec — reference file paths.
- If the item is already `building` or has an owner, ask before re-scoping.
