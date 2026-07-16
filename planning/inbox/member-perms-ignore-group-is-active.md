---
# id is assigned by /scope on pickup — leave it blank
id:
type: bug
title: Member-permission resolution ignores Group.is_active — deactivated-tenant grants still authorize lists, metrics, WS subscribe
priority: P2
effort:
owner:
opened: 2026-07-10
depends_on: []
related: [DM-025]
links: []
---

# Member-permission resolution ignores Group.is_active — deactivated-tenant grants still authorize lists, metrics, WS subscribe

## What & Why
DM-025 made client-supplied numeric `group=` ids resolve ACTIVE groups only
(dispatcher + `requires_perms`/`requires_group_perms`), but a parallel
member-permission resolver never considers `Group.is_active`, so a member
grant in a **deactivated** group still authorizes several surfaces:

- **RestMeta LIST fallback** (`mojo/models/rest.py:488-496`,
  `on_rest_handle_list`): when the primary check fails (request.group None),
  it authorizes via `request.user.get_groups_with_permission(perms)` and
  returns `cls.objects.filter(group__in=...)` — and `on_rest_list`
  (rest.py:659-668) applies no further narrowing when `request.group is None`.
- **`User.get_groups_with_permission`** (`mojo/apps/account/models/user.py:406-447`):
  the direct-membership branch builds group ids from `self.members` filtering
  only `GroupMember.is_active` (429-430), never `Group.is_active` (447).
  The system-perm branch (422-424) delegates to `get_groups()` which DOES
  filter — the asymmetry is internal to this one function. Same gap in
  `get_groups`/`get_group_ids` for `include_children=False` (user.py:348-354).
- **Metrics group-account gate** (`mojo/apps/metrics/rest/helpers.py:18`):
  `Group.objects.filter(id=group_id).first()` with no is_active, feeding
  `group.user_has_permission(...)` — a deactivated tenant's member keeps
  metrics view/write for that group's account.
- **WS subscribe** (`mojo/apps/account/models/user.py:1488-1495`,
  `can_subscribe_to_topic` for `group:<id>` topics): same unfiltered lookup
  feeding a membership check.

Consequence: DM-025's "a member grant in a deactivated group no longer
authorizes" holds only for the decorator paths; list endpoints (e.g.
`GET /api/group/member`), metrics accounts, and realtime topics still honor
grants in deactivated tenants. The CHANGELOG was narrowed accordingly
(commit aca2fab) — this item closes the remainder.

## Acceptance Criteria
- [ ] A member whose only grant lives in an inactive group gets NO rows from RestMeta list endpoints for that group (fallback path included), no metrics account access, and no `group:<id>` WS subscription for it.
- [ ] `get_groups_with_permission` direct-membership branch and `get_groups`/`get_group_ids` (`include_children=False`) filter `Group.is_active` consistently with the `include_children=True` path.
- [ ] Members with grants in OTHER active groups are unaffected (their lists/metrics/subscriptions keep working).
- [ ] Regression tests: inactive-group member denied on a list endpoint via the fallback path; metrics account gate; ws subscribe (feasible per existing realtime test patterns).
- [ ] Decide + document whether any admin flow legitimately needs member-grant resolution against inactive groups (recon so far says no — admin flows use global perms).

## Repro — bugs only
1. Create group G with a member M holding `view_members` (GroupMember-level only). Set `G.is_active=False`.
2. As M: `GET /api/group/member?group=<G.pk>` (or omit `group=` entirely — the fallback never depended on it).
- Expected: no rows from G (deactivated tenant's data not member-readable).
- Actual: G's member roster returns via the `get_groups_with_permission` fallback. Post-DM-025 the same request can even AGGREGATE rows from all of M's permitted groups (request.group is None → no narrowing), returning strictly more than before.

## Investigation
Root cause traced by the DM-025 post-build security review (2026-07-10) with
file:line evidence as above — confidence: **high** (code-path reading;
re-verify the exact lines during /scope). Pre-existing (reachable pre-DM-025
by omitting `group=`); not introduced by DM-025. One root cause
(`Group.is_active` absent from the member-side resolvers), four surfaces.
Regression-test feasibility: high — `tests/test_middleware/group_param_is_active.py`
(DM-025) already builds the inactive-group + member-grant fixture to copy.

## Plan
<!-- PLAN PENDING — /scope fills this section. While this marker is present the item
is UNPLANNED and /build MUST refuse it. Delete this comment when the plan is complete. -->

## Notes
- Watch interplay with caching if `get_groups_with_permission` results are memoized anywhere.
- `metrics/rest/helpers.py` already collapses nonexistent/unauthorized into one PermissionDenied — keep that anti-oracle shape when adding the filter.
