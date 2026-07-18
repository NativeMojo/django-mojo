---
# id is assigned by /scope on pickup — leave it blank
id:
type: feature          # feature | bug | chore
title: [Title]
priority: P2           # P0 (drop everything) | P1 | P2 | P3
effort:                # XS | S | M | L | XL
owner:                 # team or person
opened: YYYY-MM-DD
depends_on: []         # hard blockers: [DM-003, wmwx/wmx_api#WA-007]
related: []            # soft links: [DM-009]
links: []              # external URLs
# Build routing — optional; /scope stamps these at plan time (see scope skill rubric)
build_strategy:        # inline (default) | delegate | fanout
build_model:           # sonnet | opus | fable  (default: session model)
---

# [Title]

## What & Why
[What needs to exist / what's broken. Why it matters now.]

## Acceptance Criteria
- [ ]
- [ ]

## Repro — bugs only
1.
- Expected:
- Actual:

## Plan
<!-- PLAN PENDING — /scope fills this section. While this marker is present the item
is UNPLANNED and /build MUST refuse it. Delete this comment when the plan is complete. -->

_Write a complete, self-contained design here — enough that a fresh session can
`/build` it cold, without re-deriving anything. Fill every subsection._

### Goal
[One sentence.]

### Context — what exists
[The recon a builder would otherwise redo: relevant files with paths and
`file:line` refs, current behavior, key snippets, helpers/patterns to reuse.]

### Changes — what to do
1. `path` — [exact change and why]
2. `path` — [...]

### Design decisions
- [decision] — [rationale; alternatives rejected]

### Edge cases & risks
- [case] — [how it's handled]

### Tests
- [scenario] -> `test file`   (for a bug: the regression test to add)

### Docs
- `doc` — [what changes]

### Open questions
- [blocking unknowns, or "none"]

## Notes
[Scratch space — anything not part of the plan.]

## Resolution
- closed: YYYY-MM-DD
- branch:
- files changed:
- tests added:
