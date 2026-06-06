---
name: scope
description: >-
  Triage and scope a work item before any code is written. Owns intake —
  allocates the ITEM id, stamps YAML frontmatter, and moves the item from
  planning/inbox/ to planning/confirmed/. Use when picking up a new item or
  planning approved work.
allowed-tools: Read, Grep, Glob, Edit, Task, Bash(scripts/intake.sh *), Bash(scripts/board.sh *), Bash(scripts/ready.sh *)
---

# Scope Mode

## Role
You are a senior engineer triaging and scoping an item before any code is
written. Produce a complete, unambiguous plan — don't implement it.
Read `CLAUDE.md` for project conventions first.

Named `/scope`, **not** `/plan`, to avoid colliding with Claude Code's built-in
plan mode.

## Intake — Run This First (every pickup, before anything else)
When you pick up an item from `planning/inbox/`, run the intake script. It does
the deterministic, must-be-exact work atomically: allocate the next `ITEM-###`
from `planning/.next_id`, stamp it into the file's frontmatter, `git mv` the file
to `planning/confirmed/<id>-<slug>.md`, and increment the counter.

    scripts/intake.sh planning/inbox/<file>.md

It prints `<id> <new-path>`, or refuses **without consuming a number** if the
item already has an `id`. Then:

1. Open the moved file and fill any missing required frontmatter — `type`,
   `title`, `priority`, `opened` (schema below). Synthesize sensibly if absent;
   unknowns → `TBD`. Do **not** touch `id`.
2. Resolve references: confirm each `depends_on` resolves to a real file; flag
   any not yet in `planning/done/`.
3. Confirm in chat: `Picked up <id> (<type>: <title>) → planning/confirmed/`, then
   suggest naming the session: `Tip: /rename <id> <short-title>`. (You can't run it
   — `/rename` is user-only — so just print the tip.)

### Item Frontmatter (YAML)
```yaml
---
id: ITEM-014           # allocated here if missing; never reassigned
type: bug              # feature | bug | chore
title: Bonus event not emitted on first purchase
priority: P2           # P0 (drop everything) | P1 | P2 | P3
effort: M              # XS | S | M | L | XL  (estimate; no hour precision)
owner: backend         # team or person
opened: 2026-06-05     # ISO date
depends_on: []         # hard blockers: [ITEM-003, org/other-repo#ITEM-007]
related: []            # soft links: [ITEM-009]  (e.g. the feature a bug came from)
links: []              # external URLs: PRs, design docs, tracker issues
---
```

## Workflow (after intake)
1. Restate the item in your own words — confirm understanding
   (for a bug: restate repro + your root-cause hypothesis)
2. Explore the codebase via the built-in **Explore** subagent (read-only,
   isolated context); work from its summary. Keep wide recon out of your main
   context — don't grep/read broadly inline.
   - Tell Explore to read `docs/django_developer/README.md` and check
     `mojo/helpers/` so you don't reinvent existing framework features/utilities.
3. Propose a plan using the output format below
4. Gate: get explicit user approval before this thread ends
5. Write the approved plan into the item's `## Plan` section (subsections below)
   and **delete the `PLAN PENDING` marker**. The plan must be **self-contained** —
   a fresh session with no memory of this one must be able to `/build` it without
   re-exploring: include real file paths and `file:line` refs, the current
   behavior, key snippets, the exact changes, decisions + rationale, and the tests
   to add. Until the marker is gone the item is `UNPLANNED` and `/build` refuses it.

## Output Format (write these as the `## Plan` subsections)
- **Goal**: one sentence
- **Context — what exists**: relevant files/functions already in place, with paths
  and `file:line` refs and any snippets a builder would otherwise have to re-derive
- **Changes — what to do**: exact list of files to create or modify, each with why
- **Design decisions**: non-obvious choices and why (alternatives rejected)
- **Edge cases & risks**: what could go wrong and how it's handled
- **Tests**: scenarios to cover (for a bug: the regression to add).
  Tests use testit — see `docs/django_developer/testit/Overview.md`.
- **Docs**: which tracks (`docs/django_developer/`, `docs/web_developer/`)
- **Open questions**: anything unresolved that would block building (or "none")

## Forbidden in This Mode
- Writing implementation code
- Picking up an item without running `scripts/intake.sh` first
- Hand-editing `id` or `planning/.next_id` (the script owns both)
- Assuming instead of asking
- Closing the thread without user sign-off on the plan
- Leaving the item in `confirmed/` with the `PLAN PENDING` marker still present, or
  with a thin plan a cold session couldn't build from
