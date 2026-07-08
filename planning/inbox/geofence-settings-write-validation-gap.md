---
# id is assigned by /scope on pickup — leave it blank
id:
type: bug
title: Geofence-adjacent Settings bypass write-time validation; kind= coercion silently absorbs garbage
priority: P2
effort:
owner:
opened: 2026-07-08
depends_on: []
related: [ITEM-017]
links: []
---

# Geofence-adjacent Settings bypass write-time validation; kind= coercion silently absorbs garbage

## What & Why

Found during mverify_api MVERIFY-API-014's post-build security review
(2026-07-08). ITEM-017 added write-time validation for DB-backed geofence
Settings, but `Setting.GEOFENCE_KEYS` covers only `GEOFENCE_SYSTEM_RULES`
and `GEOFENCE_ALLOWLIST` (`mojo/apps/account/models/setting.py:86,96-127`).
Two settings that now carry real enforcement stakes are writable as garbage
through the generic `POST /api/settings` (perms `manage_settings`/`groups`)
with no validation:

- `GEOFENCE_FAIL_CLOSED_SCOPES` — read `kind="list"`; a malformed value
  silently changes which scopes fail closed (money endpoints could quietly
  revert to fail-open).
- `GEOFENCE_ALLOW_PRIVATE_IPS` — read `kind="bool"`; an unrecognized string
  falls through to Python truthiness, so `bool("some-typo")` is `True`
  (allow) — the unsafe direction.

Compounding it, `settings.get(kind=...)` coercion is silently lenient
(`mojo/helpers/settings/helper.py:96-118`, `objict.from_json(...,
ignore_errors=True)`): a present-but-unparsable value coerces to the empty/
default shape with no log, so "someone wrote garbage" is indistinguishable
from "unset" at every read site. mverify_api fixed its own app-level key
(`PAYMENTS_GEOFENCE_RULES`) by reading the raw value and denying on
present-but-unparsable (`apps/mojopay/payments/services/geo_gate.py`); the
framework keys remain exposed for every deployment.

## Acceptance Criteria

- [ ] `GEOFENCE_FAIL_CLOSED_SCOPES` and `GEOFENCE_ALLOW_PRIVATE_IPS` (and
      any other geofence-posture key) get write-time validation — extend
      `Setting.GEOFENCE_KEYS` or generalize it into a per-key validator
      registry other apps can register into (mverify's
      `PAYMENTS_GEOFENCE_RULES` would use it).
- [ ] Coercion failure is observable: `settings.get(kind=...)` (or at least
      the geofence read paths) logs when a present value fails to coerce,
      instead of silently returning the default shape.
- [ ] `bool` coercion of unrecognized strings does not default to `True`
      for allow-flavored settings (decide: strict parse with log-and-default,
      or explicit truthy-string whitelist).
- [ ] Tests in `tests/test_geofence/` cover a garbage write to each key:
      rejected at write time, and read-path behavior pinned.

## Plan
<!-- PLAN PENDING — /scope fills this section. While this marker is present the item
is UNPLANNED and /build MUST refuse it. Delete this comment when the plan is complete. -->

## Notes

- Origin: mverify_api `planning/done/MVERIFY-API-014-*.md` (security-review
  WARNING; app-side fix landed there same day, commit 7a825ee).
- Sequencing hint: the validator-registry shape would let downstream apps
  stop hand-rolling raw-read guards like geo_gate's `_payments_rules()`.
