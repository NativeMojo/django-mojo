---
# id is assigned by /scope on pickup — leave it blank
id: ITEM-017
type: feature
title: Geofence config + evidence plane — editable system rules, validation, simulate, incident events
priority: P1
effort: L
owner: backend
opened: 2026-07-07
depends_on: []
related: []
links: []
---

# Geofence config + evidence plane — editable system rules, validation, simulate, incident events

## What & Why

The geofence engine (`mojo/apps/account/services/geofence/`) is complete as a
*decision* engine but has no **config plane** (rules are a raw settings dict +
unvalidated `Group.metadata['geofence']`) and no **evidence plane** (blocks
403 silently — no event, no audit trail). Downstream products (MojoVerify,
WMX) now need both: payment-processor compliance teams ask for "geofencing
rules in an active state" with evidence, and **legal/business staff — not
engineers — will maintain the jurisdiction lists** via an admin UI (web-mojo
item, filed separately). That UI needs backend support that doesn't exist yet.

Owner rulings driving this (2026-07-07):
1. System rules must be **editable in the admin portal** (not deploy-file only).
2. Fail posture is **per-endpoint-scope**: fail-closed on money endpoints,
   fail-open on auth.
3. Rule content is set by legal/business in the portal — so validation,
   attribution, and a safe self-serve test surface are mandatory, and raw-JSON
   editing is not an acceptable interface.

What exists (recon, verified 2026-07-07):
- Engine `engine.py:186-265`, DSL `dsl.py` (`validate_rule` / `evaluate_rule`),
  Redis decision cache `cache.py` (key `geofence:dec:{ip}:{group_id}`).
- `@md.requires_geofence(scope=)` decorator (`mojo/decorators/geofence.py`) on
  23 auth endpoints; `GET /api/geo/check` pre-flight (`rest/geofence.py`).
- `settings.get` already chains DB-backed `Setting.resolve()` → Redis →
  django.conf, so a Setting-backed `GEOFENCE_SYSTEM_RULES` needs no engine
  read-path change.
- `Group.metadata['geofence']` has **no write-time validation** (a typo'd rule
  surfaces only at evaluation, as `rule_invalid`).
- `incident.report_event(details, title=, category=, level=, request=)`
  (`mojo/apps/incident/reporter.py:4`) is the established evidence sink;
  passing `request=` captures IP/UA/path context.
- Blocks currently emit **nothing** (only `logit.error` on lookup failure /
  invalid rule).

## Acceptance Criteria

- [ ] **Editable system rules**: `GEOFENCE_SYSTEM_RULES` manageable as a
      DB-backed `Setting` row via perm-gated REST (admin-level permission),
      with `validate_rule` enforced on write and change attribution
      (who/when) queryable for the admin UI's change history.
- [ ] **Group-rule validation on save**: writing `Group.metadata['geofence']`
      through REST validates the rule and rejects malformed shapes with a
      human-readable error (no more lazy `rule_invalid` at request time).
- [ ] **Cache invalidation on any rule change** (system or group): an
      emergency rule edit must not serve stale allow decisions for up to
      `GEOFENCE_CACHE_TTL`; invalidation is automatic on write, not an ops
      step.
- [ ] **Effective-rules endpoint** (perm-gated): returns the merged
      system+group ruleset plus posture (enabled, fail mode, cache TTL,
      last-changed metadata) — the machine-readable "rules in an active
      state" artifact.
- [ ] **Simulate endpoint** (perm-gated): arbitrary IP or geo dict (+ optional
      group) → full uncached `GeoDecision`, so a non-engineer can demonstrate
      "a WA IP is blocked" without owning a WA IP. Distinct from the public
      self-check `GET /api/geo/check`.
- [ ] **Block events → incidents**: every geofence block calls
      `incident.report_event(category="geofence_block", request=request)`
      with a level scheme — 3 auth-endpoint block · 5 money-endpoint block or
      abuse-flag (VPN/Tor) block · 6 lookup-failure-while-fail-open ·
      7 invalid-rule-at-evaluation (crosses typical `INCIDENT_LEVEL_THRESHOLD`
      and pages). Per-`(ip, reason)` hourly dedupe via cache so a blocked
      state hammering login cannot flood events; aggregate metrics
      (`geofence:blocks`, `geofence:blocks:region:{code}`) recorded on every
      block including deduped ones (mirror the existing
      `firewall:blocks:country:{code}` pattern).
- [ ] **Per-scope fail posture**: decorator `scope` maps to posture (e.g.
      `GEOFENCE_FAIL_CLOSED_SCOPES = ["payments"]`) so money endpoints
      fail-closed while auth stays fail-open. Reconcile with
      `geofence-hardening.md` (inbox) rather than duplicating it — that item
      owns the strict-posture allow-by-default paths; this one owns the
      scope-map shape.
- [ ] **Bypass visibility**: an endpoint listing users holding
      `bypass_geofence` (also called for in `geofence-hardening.md` — build
      once).
- [ ] Tests extend `tests/test_geofence/` (validation, invalidation, events,
      dedupe, scope posture, simulate perms).

## Plan
<!-- PLAN PENDING — /scope fills this section. While this marker is present the item
is UNPLANNED and /build MUST refuse it. Delete this comment when the plan is complete. -->

## Notes

- Sibling filing (same program, 2026-07-07): web-mojo
  `admin-geofencing-section.md` (the admin UI consuming these endpoints —
  its scope should pin a hard dependency on this item once IDs exist);
  mverify_api `geofence-enforcement-payments.md`; wmx_api
  `geofence-five-touchpoints-and-loader.md`.
- Overlap warning for /scope: `planning/inbox/geofence-hardening.md` predates
  this item and covers strict posture + bypass visibility. Merge or sequence
  explicitly; do not build twice.
- Evidence-plane volume: blocked-jurisdiction login traffic is the system
  working, not an incident — hence level 3 + dedupe + metrics, with
  escalation left to incident bundling (same-subnet probing, post-rule-change
  spikes).
