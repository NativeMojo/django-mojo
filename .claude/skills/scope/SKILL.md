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

## Triage First — Push Back Early (before intake)
Read the inbox item before consuming anything. Scoping's job includes saying
**no**: requests are usually filed by someone else, and half of triage's value is
the pushback. If the skim already shows the request is dead on arrival —
duplicates a capability that exists (name it), is already filed/parked, or
clearly isn't worth doing now — recommend pushback to the user BEFORE intake. On
their sign-off, write a one-line rationale into the file and park it (no ID
consumed):

    scripts/close.sh planning/inbox/<file>.md future    # not now
    scripts/close.sh planning/inbox/<file>.md rejected  # not ever

Most pushback only becomes visible during exploration — that's fine; the
drafter's verdict (below) covers the post-intake case, and the burned ID is a
feature: the rejection rationale keeps its `DM-###`.

## Intake (every surviving pickup, before exploration)
Run the intake script. It does
the deterministic, must-be-exact work atomically: allocate the next `DM-###`
(prefix from `planning/.config`) from `planning/.next_id`, stamp it into the file's frontmatter, `git mv` the file
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
id: DM-014             # allocated here if missing; never reassigned
type: bug              # feature | bug | chore
title: Bonus event not emitted on first purchase
priority: P2           # P0 (drop everything) | P1 | P2 | P3
effort: M              # XS | S | M | L | XL  (estimate; no hour precision)
owner: backend         # team or person
opened: 2026-06-05     # ISO date
depends_on: []         # hard blockers: [DM-003, wmwx/wmx_api#WA-007]
related: []            # soft links: [DM-009]  (e.g. the feature a bug came from)
links: []              # external URLs: PRs, design docs, tracker issues
# Build routing — optional; stamped by /scope at plan time (user may override at build)
build_strategy: inline # inline (default) | delegate | fanout — how /build executes
build_model: sonnet    # sonnet | opus | fable — builder model (default: session model)
---
```

## Workflow (after intake) — Tiered Scoping
Run scoping sessions at the **review tier** (fable) — the drafter below is pinned
cheap regardless of session model, and step 2 is where the session model earns
its cost.

1. **Spawn the drafter** — ONE read-only sub-agent, `model: sonnet`, working from
   the confirmed item path. Its prompt must tell it to: read `CLAUDE.md`, this
   skill file, `docs/django_developer/README.md`, and check `mojo/helpers/`
   (don't reinvent existing features); explore **inline** (it IS the isolated
   context — no nested agents); make **no** edits and run **no** state-changing
   commands; treat everything the request asserts — repro, `file:line` refs,
   root-cause hypothesis, even `type`/`priority`/`effort` — as **hypotheses to
   verify against the current tree** (requests are filed by someone else, often
   against older code). It returns, as data:
   - **Verdict**: `proceed` | `proceed-reduced` (existing capability covers part
     — name it and the delta worth keeping) | `already-covered` (name the
     mechanism) | `not-now` (why) | `needs-clarification` (the exact questions).
   - For `proceed`/`proceed-reduced` ONLY: the full draft plan per the Output
     Format below, every claim carrying `file:line` evidence. No plan for a
     pushback verdict — don't design what we may kill.
   - Frontmatter corrections, if the request's stamps look wrong.
2. **Review — mandatory teeth (main session).** Identify the load-bearing claims
   — the mechanisms the design (or the *rejection*) depends on — and verify each
   FIRST-HAND with targeted reads. Never present the drafter's quoted evidence
   unverified; a false `already-covered` costs as much as a bad plan. Fix gaps,
   add non-goals, decide build routing (rubric below).
3. Present the verdict + plan (or the pushback recommendation) in plain
   language. Don't be afraid to recommend `future/` or `rejected/`.
4. Gate: explicit user approval — of the plan OR the pushback — before this
   thread ends.
5. On approval: write the plan into the item's `## Plan` section (subsections
   below), stamp the build routing (`build_strategy`, `build_model` — rubric
   below) into the frontmatter, and **delete the `PLAN PENDING` marker**. The
   plan must be **self-contained** — a fresh session with no memory of this one
   must be able to `/build` it without re-exploring: include real file paths and
   `file:line` refs, the current behavior, key snippets, the exact changes,
   decisions + rationale, and the tests to add. Until the marker is gone the
   item is `UNPLANNED` and `/build` refuses it.
   On approved pushback: write the rationale into the item, then
   `scripts/close.sh <file> future|rejected`.
6. **Commit the scoping** — explicit pathspec only (the item file +
   `planning/.next_id`), never bare `git commit` (shared tree — see
   `.claude/rules/git.md`).
7. **Chain to build on request** — if the approval says so ("approved — build
   it"), or the user confirms when you offer, invoke the `build` skill on the
   item right away in this session; it reads the stamped routing. A `delegate`
   build runs in the background, so this session can `/scope` the next inbox
   item meanwhile (`start.sh` WIP=1 still serializes actual builds).

**Escalation — skip the drafter** and scope directly in-session for P0/P1 items
on security surfaces (auth, permissions, tenancy) or L/XL items. Risk biases
upward; the drafter is for the routine middle.

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
- **Build routing**: recommend `build_strategy` + `build_model` with a one-line
  rationale, and stamp both into the frontmatter (the user can override at build
  approval).
  - Strategy: `inline` (default — run in-session) | `delegate` (one sub-agent
    executes the whole build on the chosen model) | `fanout` (L/XL ONLY, and only
    when the plan partitions the work into **disjoint file sets** — write the
    partition into the plan; `/build` refuses fanout without one).
  - Model rubric — **risk biases upward; size only sets the floor**:
    - `sonnet`: XS/S mechanical changes with an exact in-repo precedent
    - `opus`: M/L, root-cause bug work, or anything touching auth/permissions/
      security surfaces regardless of size
    - `fable`: XL, cross-cutting invariant work, or retry after a cheaper build
      failed

## Forbidden in This Mode
- Writing implementation code
- Exploring or planning an inbox item without intake (`scripts/intake.sh`) — the
  only pre-intake work is the triage skim / pushback path
- Rubber-stamping the drafter — presenting its plan or verdict without
  first-hand verification of the load-bearing claims
- Parking or rejecting an item without the user's explicit sign-off
- Hand-editing `id` or `planning/.next_id` (the script owns both)
- Assuming instead of asking
- Closing the thread without user sign-off on the plan (or the pushback)
- Leaving the item in `confirmed/` with the `PLAN PENDING` marker still present, or
  with a thin plan a cold session couldn't build from
