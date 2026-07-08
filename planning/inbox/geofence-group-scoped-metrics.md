---
# id is assigned by /scope on pickup — leave it blank
id:
type: feature
title: Geofence evidence metrics — dual-write group-scoped accounts (group-<id>) alongside global
priority: P2
effort: S
owner:
opened: 2026-07-08
depends_on: []
related: [ITEM-017]
links: []
---

# Geofence evidence metrics — dual-write group-scoped accounts alongside global

## What & Why

ITEM-017's evidence plane records geofence metrics (`geofence:blocks`,
`geofence:blocks:country:{CC}`, `geofence:blocks:region:{code}`,
`geofence:exempt`) with the default `account="global"` only
(`mojo/apps/account/services/geofence/evidence.py::_record_block_metrics`).
Consumer portals chart per-tenant metrics with `account="group-<id>"`
(established convention across mverify/wmx dashboards) — for geofence
that account is always empty, so per-group block charts are impossible.

Owner ruling (2026-07-08): add group-based metrics, `group-<id>` style.
The group is already in hand at record time (block metadata carries
`group_id/group_name` when present).

## Acceptance Criteria

- [ ] When a group is associated with the decision, block/exempt recording
      dual-writes the **base** slugs (`geofence:blocks`, `geofence:exempt`)
      to `account=f"group-{group.id}"` in addition to `global`. Global
      recording is unchanged (platform dashboards keep working).
- [ ] **Cardinality guard**: per-country/per-region suffixed slugs stay
      global-only unless measured cheap — do not create a
      groups × countries × regions key cross-product by default. If /scope
      decides to include them per-group, justify the key growth.
- [ ] No-group decisions record exactly as today (global only).
- [ ] Tests extend `tests/test_geofence/evidence_plane.py`: group present →
      both accounts incremented; no group → global only; suffixed slugs
      unchanged.
- [ ] Doc touch: `docs/web_developer/account/geofence.md` metrics note
      gains the group-account line so portal builders know both exist.

## Plan
<!-- PLAN PENDING — /scope fills this section. While this marker is present the item
is UNPLANNED and /build MUST refuse it. Delete this comment when the plan is complete. -->

## Notes

- Consumer rider: mverify_portal ITEM-014 (scoped) ships its charts with
  `account:'global'` and a note to flip/augment to the active group's
  account once this lands — a one-line widget change portal-side, plus an
  optional global/group toggle.
- Same convention as the rest of the platform's metrics
  (`account='group-<id>'`, e.g. VerifyDashboardPage) — no new account
  naming scheme.
