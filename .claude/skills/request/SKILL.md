---
name: request
description: >-
  File a request for new work from chat — a feature, bug, or chore. Determines the
  type itself, explores/clarifies (for a bug: best-effort confirms the root
  cause), and writes a structured, un-ID'd item to planning/inbox/. Does not
  implement or allocate an id; /scope picks it up next.
allowed-tools: Read, Grep, Glob, Write, Task
---

# Request — File New Work

## Role
Turn a natural-language ask into one structured **inbox** item — a request for new
work, whether that's a feature, a bug fix, or a chore. You decide the type, then
capture it. Do **not** implement, allocate an id, or move folders — `/scope` runs
intake next. Read `CLAUDE.md` for conventions first.

## Arguments
$ARGUMENTS — what to file. If empty, ask the user what they want to request.

## 1. Determine the type
Classify from the description; only ask the user if it's genuinely ambiguous:
- **bug** — something is broken or behaves wrong (errors, regressions, wrong output)
- **feature** — a new capability or enhancement ("add", "support", "allow")
- **chore** — refactor, cleanup, deps, tooling; no user-facing behavior change

State the type you chose (one line) before continuing.

## 2. Explore (read-only, via the Explore subagent)
Keep wide recon out of your main context; work from Explore's summary.
- **bug**: trace the code path; narrow to a root cause or 2–3 candidates;
  best-effort confirm by analysis and state confidence
  (`confirmed | high | medium | speculative`). Don't write or run a test —
  `/build` writes the failing regression test first.
- **feature / chore**: what exists to reuse, what would change (file-level),
  constraints (security, permissions, backwards compatibility).

Point Explore at `docs/django_developer/README.md` and `mojo/helpers/` so the item
doesn't propose reinventing existing features/utilities.

## 3. Clarify
Resolve real ambiguity with the user before writing — the API contract,
permissions, edge cases, and what's out of scope (features); the repro and
expected-vs-actual (bugs). Don't write a vague item; a good inbox item is
unambiguous enough to scope against.

## 4. Write the item
Create `planning/inbox/<slug>.md` from `planning/_template.md` (slug = short,
lowercased, hyphenated title). Fill:
- frontmatter: `id:` **blank**, `type: <chosen>`, `title`, `priority` (P0–P3),
  `opened: <today>`, `depends_on/related/links` as known
- `## What & Why`, `## Acceptance Criteria`
- `## Repro` — bugs only (steps, Expected, Actual)
- `## Investigation` — bug: root cause / confidence / code path (file:line) /
  regression-test feasibility; feature/chore: what exists / what changes /
  constraints / related files

## 5. Hand off
Print the file path, the chosen type, and
`To scope it: /scope planning/inbox/<slug>.md (a fresh session is ideal).`

## Forbidden
- Writing implementation code (a bug fix included).
- Allocating an `id`, editing `planning/.next_id`, or running `scripts/intake.sh`
  — leave `id:` blank (that's `/scope`'s job).
- Moving the file out of `planning/inbox/`.
- Writing a vague item instead of resolving ambiguity with the user.
- For a bug you can't confirm: say so and set confidence to `speculative` — don't
  force it.
