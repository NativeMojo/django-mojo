---
# id is assigned by /scope on pickup — leave it blank
id:
type: bug
title: Dispatcher numeric group= resolution skips is_active (group_uuid path filters it)
priority: P3
effort: XS
owner: backend
opened: 2026-07-08
depends_on: []
related: [ITEM-020]
links: []
---

# Dispatcher numeric group= resolution skips is_active (group_uuid path filters it)

## What & Why

The REST dispatcher resolves `request.group` from two client-supplied params
(`mojo/decorators/http.py:69-111`), and the two branches disagree:

- `group=<int>` (`http.py:74-91`): `modules.get_model_instance("account",
  "Group", int(...))` — **no `is_active` filter**, and it calls
  `request.group.touch()` on whatever it finds.
- `group_uuid=<uuid>` (`http.py:101-111`): `Group.objects.filter(uuid=...,
  is_active=True)` — with an explicit SECURITY comment (`http.py:96-100`)
  explaining why: inactive groups must never become `request.group` via a
  public path (touch side-effect = existence disclosure; inactive groups
  shouldn't be resolvable at all).

The numeric path contradicts the documented security rationale of its sibling
branch: an unauthenticated caller can make an **inactive** group the request
group by integer id — it gets touched (modified-timestamp side effect /
existence oracle), its geofence rules participate in the decision, and (since
ITEM-020) evidence metrics are attributed to its `group-<id>` account.

Surfaced by the ITEM-020 post-build security review. Same review's product
note, worth deciding while here: on public auth surfaces the group param has
no membership check (by design — white-label flows), so any existing group id
can be *attributed* activity by an anonymous blocked caller. ITEM-020
documented per-group geofence counters as "reported activity, not verified
counts"; if stronger integrity is ever wanted, this dispatcher choke point is
where it would go.

## Acceptance Criteria

- [ ] The numeric `group=` branch resolves only active groups (matching the
      `group_uuid` branch), or a deliberate, documented decision is recorded
      for why inactive groups must remain resolvable by id (and if so, at
      minimum the `touch()` side effect on inactive groups is removed).
- [ ] Behavior change is verified against authenticated flows that pass
      `group=<id>` legitimately (REST list/detail with group context, member
      endpoints) — no regression for active groups.
- [ ] Regression test: request with an inactive group's id → `request.group`
      is None (and no `modified` bump on the inactive group).

## Repro — bugs only

1. Create a group, set `is_active=False`.
2. Send any mojo REST request with `group=<that id>` (e.g. as a query param).
- Expected: `request.group` stays None (as it would via `group_uuid`).
- Actual: the inactive group becomes `request.group` and gets `touch()`ed.

## Plan
<!-- PLAN PENDING — /scope fills this section. While this marker is present the item
is UNPLANNED and /build MUST refuse it. Delete this comment when the plan is complete. -->

## Notes

- Blast-radius check for /scope: grep who passes numeric `group=` today
  (portals, tests) and whether any legitimate flow relies on resolving an
  inactive group (e.g. admin re-activation screens use direct model access,
  not request.group, so probably none).
- The api_key confinement inside both branches (`is_group_allowed`) is
  unaffected — it only fires for key-authenticated requests.
