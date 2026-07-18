---
# id is assigned by /scope on pickup — leave it blank
id:
type: bug
title: Parent-group API key can disable a descendant group but never read or reactivate it — DM-037's is_group_allowed active check makes key-driven group lifecycle a one-way door
priority: P3
effort:
owner:
opened: 2026-07-16
depends_on: []
related: [DM-037, DM-025]
links: []
---

# Parent-group API key can disable a descendant group but never read or reactivate it — DM-037's is_group_allowed active check makes key-driven group lifecycle a one-way door

## What & Why
DM-037 added `if group is None or not group.is_active: return False` to
`ApiKey.is_group_allowed` (`mojo/apps/account/models/api_key.py:~202`) so a
suspended tenant's key cannot read/write its own Group row (including
flipping `is_active` back on). Correct for the suspended-key case — but the
check gates the TARGET group unconditionally, which has an undocumented
side effect on a different actor: an **ACTIVE parent group's key**.

A fleet-automation key on active parent P holding `manage_groups` can
disable child C (allowed — C is active when `Group.check_edit_permission`
runs; `on_action_disable` only requires the perm). From that moment the same
key is locked out of C entirely:

- `GET`/`PATCH` `/api/group/<C.pk>` → 403 (`Group.check_view_permission`/
  `check_edit_permission` at `group.py:585/610` route through
  `is_group_allowed`, now False for the inactive target).
- C vanishes from the key's lists (`ApiKey.get_groups` filters
  `is_active=True`).
- Explicit `group=<C.pk>` never resolves (`Group.get_active`, dispatcher
  active-only — the DM-025 contract).
- `reactivate` is unreachable (outer `check_edit_permission` gate fails
  before the action runs).

So key-driven suspend/unsuspend automation is a one-way door: only a human
`User` with global `manage_groups` (unaffected — `user_has_permission` has
no `Group.is_active` condition) can undo the disable, confirm C's state, or
even read its record. Before DM-037 the same key could read and reactivate
its descendants. No in-repo flow does parent-key group lifecycle (only
tests), so this is a downstream-automation concern, not a live in-repo
breakage — hence P3.

**The design question for /scope (needs an owner ruling):** should
`is_group_allowed` permit an inactive **strict descendant** when the key's
OWN group is active — i.e. rule = "deny when the key's own group is
inactive (suspension); allow inactive descendants for active-parent keys
(management)"? That mirrors human admins (who CAN manage inactive groups)
and restores automation, while fully preserving the DM-037 security fix
(the suspended key's own group stays denied; a suspended parent's key still
can't touch anything — its own group check fails first). Alternative: keep
the one-way door and document it as intended ("group lifecycle is a
human-admin operation").

## Acceptance Criteria
- [ ] Owner decision recorded: inactive-descendant management by an
      active-parent key is either restored or explicitly documented as
      unsupported.
- [ ] If restored: an active parent's key with `manage_groups` can GET,
      list (or at least GET by pk), and `reactivate` an inactive descendant;
      a suspended group's OWN key still cannot read/write its own row or
      self-reactivate (DM-037 regression suite stays green, notably
      test_group_self_access_denied_when_inactive).
- [ ] If restored: the dispatcher explicit-`group=` path is reconciled —
      `Group.get_active` never resolves an inactive id (DM-025 contract), so
      detail access for the parent key must come via the pk route
      (`/api/group/<pk>` instance path), not `?group=`; document which
      routes work.
- [ ] Regression test either way (restored behavior, or a test asserting
      the documented one-way door so the contract is deliberate).
- [ ] Docs updated in both tracks wherever DM-037's behavior is described.

## Repro — bugs only
1. Create active parent P and active child C (parent=P);
   `ApiKey.create_for_group(P, permissions={"groups": True, "manage_groups": True})`.
2. As the key: `POST /api/group/<C.pk>` `{"disable": {"reason": "archived"}}`
   → 200 (C was active at check time).
3. As the same key: `GET /api/group/<C.pk>` then
   `POST /api/group/<C.pk>` `{"reactivate": {}}`.
- Expected (pre-DM-037 behavior / automation expectation): 200 — the parent
  key that disabled C can inspect and reactivate it.
- Actual: 403 on both (`is_group_allowed` denies the inactive target); C is
  also absent from the key's group lists and `?group=<C.pk>` never resolves.

## Investigation
Confidence: **confirmed** — adversarially verified by the DM-037 post-close
review (2026-07-12), all mechanics quoted: `api_key.py:202` (target-active
check), `group.py:585/610` (both hooks route through it, returning early in
`mojo/models/rest.py:~260-278`), `group.py:625` (`on_action_disable` inner
gate is perm-only, so the disable itself succeeds), `Group.get_active`
(`group.py:224-232`) + dispatcher (`mojo/decorators/http.py:~83,116`)
blocking the explicit-param route, and `user_has_permission`
(`group.py:213-221`) confirming human admins are unaffected. Finding sites
unchanged as of 2026-07-16. Behavior change introduced by DM-037 commit
3a74187 (deliberate for the self-suspension case; this collateral was not
called out in its docs). Regression-test feasibility: high — extend
`tests/test_global_perms/apikey_group_inactive.py` (parent/child fixture
exists in test_apikey_active_child_still_reachable).

## Plan
<!-- PLAN PENDING — /scope fills this section. While this marker is present the item
is UNPLANNED and /build MUST refuse it. Delete this comment when the plan is complete. -->

## Notes
- If the "own group active + descendant" rule is adopted, implement it
  inside `is_group_allowed` itself (single choke point — both Group hooks
  and the dispatcher route through it) and mind the ordering: check
  `self.group.is_active` BEFORE the hierarchy walk so a suspended parent's
  key short-circuits to deny.
- Related surface in the same review: the no-param list fallback item
  (`apikey-suspension-residual-surfaces.md`) may change `get_groups`
  semantics — if both items land, decide whether an inactive descendant
  should appear in the parent key's LIST results or only be reachable by pk.
