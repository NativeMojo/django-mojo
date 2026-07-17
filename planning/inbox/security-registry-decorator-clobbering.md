---
# id is assigned by /scope on pickup — leave it blank
id:
type: bug
title: Auth decorators clobber each other's SECURITY_REGISTRY entries — enforced_endpoints under-reports
priority: P2
effort:
owner:
opened: 2026-07-17
depends_on: []
related: [DM-043]
links: []
---

# Auth decorators clobber each other's SECURITY_REGISTRY entries — enforced_endpoints under-reports

## What & Why

Ten decorators in `mojo/decorators/auth.py` register endpoints with a **full
overwrite** — `SECURITY_REGISTRY[key] = {...}` — at lines 22
(`requires_perms`), 85 (`requires_group_perms`), 158 (`requires_global_perms`),
198 (`public_endpoint`), 223 (`custom_security`), 249 (`uses_model_security`),
276 (`token_secured`), 296 (`requires_auth`), 325 (`requires_fresh_auth`),
352 (`requires_bearer`). Only `mojo/decorators/geofence.py::_apply_geofence`
uses the merge pattern (`entry = SECURITY_REGISTRY.get(key, {})` + update).

Decorators apply bottom-up, so whenever one of the ten sits **above**
`@requires_geofence` in a stack (applied later), it wipes the `geofence`
sub-entry. Consequence: `GET /api/geo/rules` → `enforced_endpoints` — sold in
the docs as the compliance artifact for WHERE geofencing is enforced — has
been silently missing most geofenced endpoints (register, forgot,
magic/email sends, sms, totp, passkeys, oauth, handoff, magic/reset/verify/
invite completes...). Roughly only `on_user_login` (and other stacks with no
overwriting decorator above the geofence line) survive.

**Enforcement itself is unaffected** — the wrapper (pre-view) and the DM-043
post-credential checks run regardless of registry state. This is an
audit/visibility bug only, but a real one: the compliance surface lies.

Presumably the same clobbering also loses `type: public` / perms info when
multiple registering decorators stack in other orders — the fix should make
ALL registrations merge, not just geofence's.

## Acceptance Criteria

- [ ] All ten registration sites in `mojo/decorators/auth.py` merge into the
      existing entry instead of overwriting (preserve the `geofence` sub-entry
      and each other's keys).
- [ ] `GET /api/geo/rules` → `enforced_endpoints` lists **every**
      `@requires_geofence` endpoint (pre-view and `after_auth`), regardless of
      decorator stacking order.
- [ ] Regression test: a view decorated `public_endpoint`-above-
      `requires_geofence` (the on_register shape) appears in
      `_enforced_endpoints()`.
- [ ] `tests/test_geofence/config_plane.py` upgraded from `len > 0` to
      asserting known members (e.g. on_register present).
- [ ] No behavior change to actual enforcement, auth, or perms checks — this
      is registry bookkeeping only.

## Repro

1. In a Django shell (or in-process test): import
   `mojo.apps.account.rest.user`, then call
   `mojo.apps.account.rest.geofence._enforced_endpoints()`.
- Expected: includes `...rest.user.on_register` (decorated
  `@md.requires_geofence(scope="auth")` at `user.py:263`).
- Actual: absent — `@md.public_endpoint()` sits above the geofence decorator
  and its registration at `auth.py:198` overwrote the entry.

## Investigation

- Root cause: **confirmed** (found during DM-043's
  `test_registry_annotates_after_auth`, which had to fall back to probe views;
  see the comment in `tests/test_geofence/post_auth.py`).
- Code path: `mojo/decorators/geofence.py::_apply_geofence` (merge, correct)
  vs the ten overwrite sites listed above.
- Fix shape: replace each `SECURITY_REGISTRY[key] = {...}` with the same
  get-merge-update pattern geofence uses. Watch for entries that
  intentionally replace (none apparent — each writes disjoint keys like
  `type`/`requires_auth`/`perms`).
- Regression-test feasibility: easy, in-process (define stacked probe views,
  assert registry contents).

## Resolution
- closed: YYYY-MM-DD
- branch:
- files changed:
- tests added:
