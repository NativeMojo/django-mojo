---
# id is assigned by /scope on pickup — leave it blank
id:
type: chore
title: GET /api/group/<pk>/member deny paths are wire-identical but not equal-cost (timing side channel)
priority: P3
effort: S
owner: backend
opened: 2026-07-16
depends_on: []
related: [DM-039, DM-048]
links: []
---

# group/<pk>/member deny timing side channel

## What & Why
DM-039 made every deny outcome on `GET /api/group/<pk>/member` wire-identical
(one `PermissionDeniedException` raise site), but the deny paths are not
equal-cost: nonexistent/inactive short-circuit after one query while an
active-group non-member runs the parent walk (up to 8 queries) — a low-severity
timing oracle for group existence. Surfaced by DM-039's post-build security
review (2026-07-16); parked from DM-048 scope (2026-07-17) because equalizing
cost is a different kind of change and severity is low. Note DM-048's
`is_effectively_active` gate adds its own (bounded) walk to resolution paths —
re-measure before designing a fix.

## Acceptance Criteria
- [ ] Decide whether timing equalization is worth doing at all (measure first).
- [ ] If yes: deny paths on the member endpoint are statistically
      indistinguishable by response time.

## Plan
<!-- PLAN PENDING — /scope fills this section. While this marker is present the item
is UNPLANNED and /build MUST refuse it. Delete this comment when the plan is complete. -->

## Notes
- Source: DM-039 post-build security-review; split out of DM-048.
