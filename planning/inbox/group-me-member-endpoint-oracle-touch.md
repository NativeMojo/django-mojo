---
# id is assigned by /scope on pickup — leave it blank
id:
type: bug
title: GET /api/group/<pk>/member resolves + touches ANY group for any authenticated user (existence oracle, inactive touch)
priority: P3
effort:
owner:
opened: 2026-07-10
depends_on: []
related: [ITEM-025]
links: []
---

# GET /api/group/<pk>/member resolves + touches ANY group for any authenticated user (existence oracle, inactive touch)

## What & Why
`on_group_me_member` (`mojo/apps/account/rest/group.py:92-100`) does
`request.group = Group.objects.filter(pk=pk).last()` — no `is_active` filter —
and unconditionally `.touch()`es the result. It is gated only by
`@md.requires_auth()`: ANY authenticated user (member or not) can probe an
arbitrary pk and distinguish nonexistent (403) from existing-active-or-inactive
(200 with real perms or the `{"id": -1, "permissions": []}` non-member shape),
and can perturb a deactivated group's `last_activity`/`modified` from a
non-member request. This is the same oracle-plus-touch pattern ITEM-025 closed
in the dispatcher, now a visible outlier from the framework-wide
"inactive == nonexistent" contract that commit establishes. Flagged as a
deferred follow-up in ITEM-025's plan and confirmed by its post-build
security review (2026-07-10).

## Acceptance Criteria
- [ ] Decide the contract: switch to `Group.get_active(pk)` (inactive == nonexistent here too), OR document why this endpoint deliberately resolves inactive groups (e.g. member self-service pre-reactivation) — and in that case at minimum stop `touch()`ing inactive groups and remove the nonexistent/exists distinction for non-members.
- [ ] A non-member authenticated user cannot distinguish nonexistent vs existing-but-unrelated group pks via this endpoint.
- [ ] No write side effect (`last_activity`/`modified`) on inactive groups from this endpoint.
- [ ] Legitimate member self-lookup on ACTIVE groups unchanged.
- [ ] Regression test covering the chosen contract.

## Repro — bugs only
1. As any authenticated user (no membership anywhere): `GET /api/group/<pk>/member` for (a) a nonexistent pk, (b) an existing inactive group's pk.
- Expected: indistinguishable responses; no write to the inactive group.
- Actual: (a) 403 vs (b) 200 `{"id": -1, ...}`, and (b) bumps the inactive group's `last_activity`/`modified`.

## Investigation
Confidence: **high** (ITEM-025 recon + its post-build security review both
read the handler; re-verify exact response shapes during /scope — the 403 vs
200 split and the `{"id": -1}` non-member payload). `Group.get_active` (added
by ITEM-025, `mojo/apps/account/models/group.py`) is the ready-made drop-in if
the active-only contract is chosen. Regression-test feasibility: high —
inactive-group + touch-assertion fixtures exist in
`tests/test_middleware/group_param_is_active.py`.

## Plan
<!-- PLAN PENDING — /scope fills this section. While this marker is present the item
is UNPLANNED and /build MUST refuse it. Delete this comment when the plan is complete. -->

## Notes
- The endpoint's purpose is "current user's membership in group" — check what web-mojo actually calls it for before changing the non-member response shape.
