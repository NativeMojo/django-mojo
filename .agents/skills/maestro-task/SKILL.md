---
name: maestro-task
description: >-
  Explore the codebase, clarify scope, and file a work item onto the maestro
  workspace board via the maestro MCP — the board item (markdown workspec,
  stage=inbox) is the work record, not a local file.
---

<!-- Generated from .claude/skills/maestro-task/SKILL.md (maestro-skill-version: 5). Do not edit directly. -->

# Maestro Task — File Work onto the Board

The board-backed counterpart of local request/bug intake. The work record is
a maestro board item: **state** (stage, priority, owner, due) lives in the
item's column values; **the spec** (the "workspec") is the item's markdown
description; **progress** lands on the item's activity trail. Everyone on the
workspace sees it live.

## Board Resolution (all maestro-* skills)

1. Read `.claude/maestro.json` in the repo root:
   `{"workspace": "<name or id>", "board": <board id>, "project": <project id>}`.
   `project` is **optional** and matters when several repos share one board:
   it is the Project column value stamped on every item these skills file from
   this repo, so work filed from one checkout is never mistaken for a sibling
   repo's. Store the numeric id — it is the column value verbatim, so there is
   nothing to resolve and nothing to drift. Omit the key when the board serves
   a single repo.
2. If the file is missing or the board doesn't resolve: call `whoami()` to
   confirm auth, then `list_workspaces()` and `list_boards(workspace)`, ask
   the user which board is this repo's work queue, and offer to write
   `.claude/maestro.json` so future sessions skip this step.
   - `list_workspaces()` returning `[]` is **normal for a new account, not an
     error**. The personal workspace `whoami()` reports holds the key and
     credits and cannot hold a board. Say so, then offer
     `create_workspace(name)` — it makes them admin and comes with a default
     MoSCoW board. Ask first; never create one unprompted. (Names are claimed
     globally, so a taken name fails — suggest a distinctive one.)
3. If maestro is unreachable or unauthenticated: **stop with an explicit
   notice** and offer the repo's local intake skill (e.g. `/request`) if one
   exists. Never fall back silently.
4. Call `get_board(board)` once and keep the column schema. Match `stage` /
   priority options **by value** from the schema — never assume the default
   template; warn the user if an expected stage option is missing.
5. **Stamp `project` on every item you create.** When the config carries a
   `project` and the board's schema has a `project` column, put it in the
   `values` of every `create_board_item` call — top-level items, sub-items,
   incidental findings and vibe history rows alike. Never ask the user which
   project a repo belongs to; that is what the config is for. If the config
   names a project but the board has no project column, file the item anyway
   and say the label was dropped — do not silently discard it.
6. **Keep the board's `name` and `workspace.name` from that call, and use them
   in everything you say to the user.** Ids are internal keys — MCP takes and
   returns them, but "board 8" and "#517" tell a reader nothing and force them
   to go look it up. Whenever you report filing, moving, commenting on or
   merely mentioning something, lead with the human name:
   - board → `board "Internal" (Maestro workspace, id 8)`, not `board 8`
   - item → `#586 "An agent cannot see what it deployed"`, not `#586`
   - parent → say it is one: `filed under #516 "Sites + domains (epic)"`
   - several items → a table of id, title and the values you set
   - always include the item URL — `create_board_item` returns one, and
     `get_board` returns `item_url_template` for items you did not create
   If you have only an id, look the title up (`get_board_item`) before writing
   the sentence. That lookup is far cheaper for you than for the user.

## Workflow

1. Call `get_workspace_context(workspace)` — apply any `rule` docs to your
   work. Reference docs by slug in the workspec ("Apply rules: ...") instead
   of pasting their content.
2. Parse the task description from the arguments (or ask what they want).
3. **Size check — ask before filing.** Not every request belongs on the
   board. If the description reads like a small, single-session change (a
   typo, a one-file fix, a small bug, a config tweak — the kind of thing
   that's faster to just do than to write a workspec for), stop and ask the
   user: "This looks small enough to vibe-code directly — want me to run
   `$maestro-vibe` on it now instead of filing a board item?" Proceed
   straight to filing without asking only when the task is clearly
   multi-session/cross-cutting, or the user has already indicated (by
   invoking this skill with that intent, or in the conversation) that they
   specifically want it tracked. When in doubt, ask — a cluttered board of
   silly small items is worse than one extra question. If the user opts to
   vibe it, switch to the `maestro-vibe` skill and do not create a board
   item.
4. Explore the codebase — what exists, what changes, constraints. Ask
   clarifying questions until scope is unambiguous: contract/shape of the
   change, permissions, edge cases, what's explicitly out of scope.
5. Compose the workspec markdown (template below).
6. Create the item:
   `create_board_item(board, title, values={"stage": "inbox", "moscow": "<must|should|could — ask or infer, default should>", "project": <from .claude/maestro.json, omit if unset>}, description=<workspec>)`
   (use the board's actual priority column/options from the schema).
7. Name the new item as a **markdown link**, never a bare id — see
   "Naming an item" below — and hand off: "run `$maestro-scope <item-id>` to
   scope it."

## Naming an Item

Every time you name an item to the user — here, in a recap, anywhere — write
it as a markdown link:

```
[#<id> <title> (<stage>)](<url>)
```

Take `url` straight from the tool result (`create_board_item`,
`get_board_item` and friends return it; `get_board` returns one
`item_url_template` with `{id}` to substitute). Never hand-assemble a host.

Two reasons this is not cosmetic. A bare id is not something a user juggling
parallel sessions can place — the title is the whole point. And a bare `#123`
in a repo with a GitHub remote gets auto-linkified by the client to
`github.com/<org>/<repo>/issues/123`, which is a real link to the wrong
system; an explicit markdown link beats that guess.

## Workspec Template

```markdown
# <Title>

- **Kind**: feature | bug | chore
- **Date**: <YYYY-MM-DD>
- **Requested by**: <who asked for this>

## Description

<What is wanted, in the requester's terms>

## Context

<Why this is needed, what problem it solves>

## Acceptance Criteria

- [ ] <Specific, testable criteria>

## Investigation

- **What exists**: <current state of related code — file paths, not dumps>
- **What changes**: <high-level summary>
- **Constraints**: <framework limits, permissions, costs>
- **Related files**: <paths>
- **Out of scope**: <explicitly excluded>
```

## Rules

- Do NOT implement anything. Exploration and documentation only.
- No Status line in the workspec — stage lives on the board.
- Keep repo dumps out of the workspec: reference file paths; the scoping and
  build sessions run inside the repo and can read them.
- A work item is board-backed XOR file-backed — never create both.
