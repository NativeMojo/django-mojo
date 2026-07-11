---
# id is assigned by /scope on pickup — leave it blank
id:
type: bug
title: REST batch save ignores CAN_UPDATE / CAN_CREATE flags (immutability bypass when CAN_BATCH is enabled)
priority: P3
effort: S
owner: backend
opened: 2026-07-10
depends_on: []
related: [ITEM-032]
links: []
---

# REST batch save ignores CAN_UPDATE / CAN_CREATE flags

## What & Why
`on_rest_handle_batch` (mojo/models/rest.py) enforces per-row *permission*
checks (ITEM-032) but never evaluates the per-verb feature flags: update rows
skip `CAN_UPDATE` (checked by `on_rest_handle_save`, rest.py ~:446-455) and
create rows skip `CAN_CREATE` (checked by `on_rest_handle_create`,
rest.py ~:584-585). A future model that sets `CAN_UPDATE = False` to make rows
immutable (e.g. an audit/ledger record) — or `CAN_CREATE = False` — and also
opts into `CAN_BATCH = True` would have that explicit control bypassed via the
batch endpoint, even though the single-instance verb is hard-disabled.

**Latent, not live:** no shipped model sets `CAN_BATCH = True`, so the gap
cannot be exercised today. It was deliberately scoped out of ITEM-032 (see
that item's "Design decisions" — `CAN_UPDATE`'s default/`CAN_SAVE`-alias
semantics make blanket per-row enforcement a behavior change) and flagged by
ITEM-032's security review as a real immutability bypass, not just a policy
inconsistency.

## Acceptance Criteria
- [ ] A model with `CAN_BATCH = True` and `CAN_UPDATE = False` refuses batch
      update rows (or refuses to enable batch at all — decide during scope).
- [ ] Same for `CAN_CREATE = False` and batch create rows.
- [ ] Decide the mechanism during scope: per-row flag enforcement in the batch
      loop (mind `CAN_UPDATE` default/`CAN_SAVE` alias semantics from
      `on_rest_handle_save`) vs. a guard that refuses `CAN_BATCH = True` on a
      model whose `CAN_UPDATE`/`CAN_CREATE` is False, so the combination can't
      be armed silently.
- [ ] Regression test on a runtime-`CAN_BATCH` model (in-process pattern from
      tests/test_models/batch_row_permissions.py).
- [ ] Existing batch and single-instance behavior otherwise unchanged; suite
      green.

## Repro — bugs only
1. On any model, set `RestMeta.CAN_BATCH = True` and `CAN_UPDATE = False`
   (no shipped model does this today — repro is via a test/dev model).
2. As a caller holding SAVE_PERMS, POST `{"batched": [{"id": <pk>, ...}]}` to
   the list endpoint.
- Expected: row refused — the single-instance `POST /api/<model>/<pk>` raises
  `feature_disabled`/`can_update_false`.
- Actual: row is updated — batch never reads `CAN_UPDATE`/`CAN_CREATE`.

## Plan
<!-- PLAN PENDING — /scope fills this section. While this marker is present the item
is UNPLANNED and /build MUST refuse it. Delete this comment when the plan is complete. -->

## Notes
- Filed from ITEM-032's security review (post-build agent, 2026-07-10),
  which rated it INFO/latent. Sibling of the per-row permission fix.
- The same review noted (no change required) that batch concentrates the
  pre-existing create-vs-denied pk-enumeration signal into one request when
  `CREATE_PERMS` is broad — worth remembering if batch is ever enabled on a
  high-value tenant boundary.

## Resolution
- closed: YYYY-MM-DD
- branch:
- files changed:
- tests added:
