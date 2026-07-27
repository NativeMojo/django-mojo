---
name: maestro-scope
description: >-
  Pull a maestro board item, scope it inside this repo with full investigation
  rigor, append a file-level ## Plan to its workspec, and push it back
  (stage=planned) via the maestro MCP.
user-invocable: true
argument-hint: <item-id (omit to pick from the board)>
maestro-skill-version: 4
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
   `get_board(board)` and list the items whose `values.stage` is `inbox`
   (id, title, priority) — ask the user which to scope. Never pick silently.
   **Skip `parked` items**: parking is a deliberate "not now", so a parked
   item is never a scoping candidate unless the user names it outright.
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
6. **Challenge — an independent red-team of the draft plan.** The author
   does not grade their own homework: spawn ONE fresh-context agent (e.g.
   general-purpose, read access to the repo) whose input is the workspec,
   the draft `## Plan`, and the workspace `challenge` skill doc (slug
   `challenge`, from step 3's context; if the workspace lacks it, brief the
   agent with its core rules: name untested assumptions, argue the
   strongest opposing case, never invent a flaw to perform thoroughness).
   Its brief: **refute the plan** — the untested assumption most likely to
   be wrong, the strongest failure scenario, the weakest design decision.
   Verify the objections yourself (measure, read, re-derive — this session
   has repo access; the challenger may not run code), then give each one a
   disposition in a `### Challenge` subsection of the plan:
   - `amended` — the plan changed; say what.
   - `rebutted` — with evidence or reasoning, not "considered".
   "No substantive challenge" is a valid verdict on small items and is
   recorded as such. A plan with an undispositioned objection is not ready
   to present.
7. **Present — lead with a TL;DR.** The user is typically running many
   sessions at once and will not remember what `<item-id>` refers to. Open
   with a short block, before any detail:
   - **What this item is** — the title, plus the original ask in one
     sentence in the requester's terms, not the codebase's.
   - **What scoping changed** — only findings that change the shape of the
     work: a workspec assumption that turned out false, a bug found on the
     way, an overlapping item that already shipped. Omit if nothing moved.
   - **The plan** — 2-4 lines.
   - **Challenged** — what the red-team objected to and what it changed;
     surviving rebuttals the user might overturn go under Decisions to
     confirm. "No substantive challenge" is reportable as-is.
   - **Decisions to confirm** — the ones you resolved that the user might
     reverse; name the option you took and why.
   - **Size** — commits, tests, docs touched.

   Keep it to roughly 15 lines. The scratch file holds the full plan — link
   it, don't inline it. Then iterate until confirmed.
8. **Push.** `update_board_item(item, description=<full scratch file
   contents>, values={"stage": "planned"})` — description replaces whole.
   Then `comment_on_item(item, <3-5 line plan summary>)`.
9. Hand off: "run `/maestro-build <item-id>` to build it" — name the item
   title alongside the id; that line is often read back in a later session.

## Push Failures

Retry once. If it still fails, keep the scratch file and tell the user
exactly which item to sync manually (`planning/.cache/<item-id>.md` →
`update_board_item(<id>, description=...)`). Never lose the plan.

## Rules

- Do NOT implement. Planning and documentation only.
- The challenge exchange (step 6) travels WITH the plan — it is part of the
  workspec pushed to the board, so the build session and the user see what
  was objected to and how it was answered.
- Every summary in the session — the step-7 presentation and any recap the
  user asks for later — names the item as `#<id> — <title>`. A bare id is
  not something a user juggling parallel sessions can place.
- Every endpoint designed must have fail-closed permissions.
- Keep repo dumps out of the workspec — reference file paths.
- If the item is already `building` or has an owner, ask before re-scoping.
