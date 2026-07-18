---
id:
type: feature
title: "List-endpoint stat aggregation — batched counts for web-mojo TableView stat strips"
priority: P2
effort:
owner: backend
opened: 2026-07-18
depends_on: []
related: [nativemojo/web-mojo#WM-037]
links: []
---

# List-endpoint stat aggregation — batched counts for web-mojo TableView stat strips

## What & Why
web-mojo is adding a `stats:` option to TableView (WM-037 in web-mojo's
pipeline): a strip of KPI chips above heavy admin tables showing live counts
under the table's current filters — e.g. an Incidents table showing
"Open 12 · High 3 · Stale 5", where each count is the row count that a named
filter bundle *would* return, and clicking a chip applies that bundle.

The frontend needs a backend aggregation contract: given a list endpoint and
the request's current filter params, return counts for N additional named
filter bundles **in one batched request** (one round trip per table, not one
per chip).

Frontend consumer details live in web-mojo `WM-037`; this item owns the
API contract and implementation. WM-037 is blocked on this item.

## Acceptance Criteria
- [ ] A list endpoint can be asked for counts of N named filter bundles in a
      single request, evaluated on top of (AND-ed with) the request's normal
      filter params — so counts always describe what the caller would see.
- [ ] Works through the standard CRUD list endpoints with query params — no
      separate admin-scoped endpoints (per REST conventions); permissions
      apply exactly as they do to the list itself.
- [ ] Response shape is stable and documented (e.g. counts keyed by the
      caller-supplied bundle keys), designed together with the web-mojo side
      so `WM-037` can consume it directly.
- [ ] Bounded cost: cap on number of bundles per request; counts are simple
      filtered `count()`s (no arbitrary aggregation language).
- [ ] Graceful behavior for endpoints/models that don't support it (clean
      error or capability flag — web-mojo degrades to label-only chips).
- [ ] Tests covering: counts respect base filters, permission scoping,
      bundle cap, unsupported-endpoint path.
- [ ] API docs updated.

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
- Filed from web-mojo's EPIC WM-038 scoping session (2026-07-18). Once this
  item gets its `DM-###` at /scope pickup, update web-mojo `WM-037`'s
  `depends_on` from `nativemojo/django-mojo#table-stats-aggregation` to the
  real id.
- Contract sketch to evaluate at /scope (not binding): request param like
  `_stats={"open":{"status":"open"},"high":{"priority__gt":7}}` on the list
  endpoint; response gains `stats: {open: 12, high: 3}` alongside the normal
  paginated payload, or a `size=0`-style counts-only variant to skip rows.

## Resolution
- closed: YYYY-MM-DD
- branch:
- files changed:
- tests added:
