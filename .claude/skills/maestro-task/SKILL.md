---
name: maestro-task
description: >-
  Explore the codebase, clarify scope, and file a work item onto the maestro
  workspace board via the maestro MCP — the board item (markdown workspec,
  stage=inbox) is the work record, not a local file.
user-invocable: true
argument-hint: <feature/bug description>
maestro-skill-version: 1
---

# Maestro Task — File Work onto the Board

The board-backed counterpart of local request/bug intake. The work record is
a maestro board item: **state** (stage, priority, owner, due) lives in the
item's column values; **the spec** (the "workspec") is the item's markdown
description; **progress** lands on the item's activity trail. Everyone on the
workspace sees it live.

## Board Resolution (all maestro-* skills)

1. Read `.claude/maestro.json` in the repo root:
   `{"workspace": "<name or id>", "board": <board id>}`.
2. If the file is missing or the board doesn't resolve: call `whoami()` to
   confirm auth, then `list_workspaces()` and `list_boards(workspace)`, ask
   the user which board is this repo's work queue, and offer to write
   `.claude/maestro.json` so future sessions skip this step.
3. If maestro is unreachable or unauthenticated: **stop with an explicit
   notice** and offer the repo's local intake skill (e.g. `/request`) if one
   exists. Never fall back silently.
4. Call `get_board(board)` once and keep the column schema. Match `stage` /
   priority options **by value** from the schema — never assume the default
   template; warn the user if an expected stage option is missing.

## Workflow

1. Call `get_workspace_context(workspace)` — apply any `rule` docs to your
   work. Reference docs by slug in the workspec ("Apply rules: ...") instead
   of pasting their content.
2. Parse the task description from the arguments (or ask what they want).
3. Explore the codebase — what exists, what changes, constraints. Ask
   clarifying questions until scope is unambiguous: contract/shape of the
   change, permissions, edge cases, what's explicitly out of scope.
4. Compose the workspec markdown (template below).
5. Create the item:
   `create_board_item(board, title, values={"stage": "inbox", "moscow": "<must|should|could — ask or infer, default should>"}, description=<workspec>)`
   (use the board's actual priority column/options from the schema).
6. Print the item id and the portal link
   (`https://<maestro host>/workspaces/#/board/<board>?item=<id>`) and hand
   off: "run `/maestro-scope <item-id>` to scope it."

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
