---
# id is assigned by /scope on pickup — leave it blank
id:
type: bug
title: Group.get_member_for_user parent-walk ignores each parent group's is_active — deactivating a parent doesn't revoke access it grants to active children
priority: P2
effort:
owner: backend
opened: 2026-07-16
depends_on: []
related: [DM-039, DM-037, DM-025]
links: []
---

# Group.get_member_for_user parent-walk ignores parent group is_active

## What & Why
`Group.get_member_for_user(user, check_parents=True)`
(`mojo/apps/account/models/group.py:264-309`) walks up to 8 parent levels and, at
each level, filters the **membership row's** `is_active` (`current.members.filter(
user=user, is_active=True)`) but never checks the **parent group's** `is_active`.
So an active membership in a *deactivated parent* still resolves — a user hits
`GET /api/group/<active-child-pk>/member` (child passes `Group.get_active`, so
DM-039's own gate is satisfied), the parent walk finds their membership in the
deactivated parent, and they get a real 200 + `touch()`.

This is in tension with the framework-wide "inactive == nonexistent" invariant
DM-025 established and DM-039 leans on, and with DM-037 ("deactivating a group
instantly suspends its API keys") — parent deactivation currently does NOT revoke
the access that parent grants to active children through the membership chain.
Surfaced by DM-039's post-build security review (2026-07-16). Pre-existing; not
introduced by DM-039. **Unverified — /scope must confirm the parent-walk actually
skips `current.is_active` and repro the access before treating as confirmed.**

## Acceptance Criteria
- [ ] Decide the contract: either (a) the parent walk skips deactivated parent
      groups (`current.is_active` required at each level), making parent
      deactivation revoke chain-granted access — consistent with DM-025/DM-037; OR
      (b) document deliberately that parent deactivation is scoped to the parent
      group only and does not cascade to children's inherited access.
- [ ] If (a): a membership in a deactivated ancestor no longer authorizes on any
      surface that relies on `get_member_for_user(check_parents=True)`.
- [ ] Active-parent / active-membership inheritance is unchanged.
- [ ] Regression test covering the chosen contract.

## Repro — bugs only
1. Active child group C under deactivated parent P; user U is an active member of P
   only. U calls `GET /api/group/<C.pk>/member`.
- Expected (if contract (a)): denied — deactivating P revokes the inherited path.
- Actual (as reported, unverified): 200 with U's P-membership record; C/member
  `touch()`ed.

## Plan
<!-- PLAN PENDING — /scope fills this section. While this marker is present the item
is UNPLANNED and /build MUST refuse it. Delete this comment when the plan is complete. -->

## Notes
- Source: DM-039 post-build security-review (2026-07-16).
- Scope-wide: `get_member_for_user` backs several call sites (member lookup,
  permission checks) — the contract choice affects all of them, so scope must
  enumerate the blast radius, not just the group/<pk>/member endpoint.
- Minor adjacent finding from the same review (fold in or park): the DM-039 deny
  paths are wire-identical but not equal-cost — an active-group-non-member runs the
  parent walk (up to 8 queries) while nonexistent/inactive short-circuit after one,
  a low-severity timing side channel on `GET /api/group/<pk>/member`.
