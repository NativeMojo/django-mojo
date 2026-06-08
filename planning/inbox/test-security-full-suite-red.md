---
id:
type: bug
title: Opt-in --full test_security suite is red (audit KeyError + pii_anonymize)
priority: P2
effort:
owner:
opened: 2026-06-07
depends_on: []
related: []
links: []
---

# Opt-in --full test_security suite is red (audit KeyError + pii_anonymize)

## What & Why

`bin/run_tests --full` (the opt-in pre-publish gate that includes `test_security`)
is RED at HEAD, independent of any current feature work. The default suite is
green, so this is invisible day-to-day, but it must be green for pre-publish
validation and for the build-baseline rule to be usable. Discovered 2026-06-07
while building ITEM-002 (step-up auth); confirmed these failures pre-date and are
unrelated to that change (zero `fresh_auth` registry entries; failing routes are
untouched login/pii paths).

## Acceptance Criteria
- [ ] `bin/run_tests --agent --full` is fully green (0 failed).
- [ ] The security auditor tolerates endpoints whose only security decorator is
      `@requires_geofence` (geofence-only registry entry, no `type`).
- [ ] `pii_anonymize` leaves `metadata` clear as the test expects (or the test is
      corrected if the disable-record behavior is intended).
- [ ] No regression to the default suite.

## Repro
1. `bin/run_tests --agent --full -t test_security`
- Expected: all pass.
- Actual: 4 failures —
  - `public_endpoints_security`, `generate_security_report`,
    `route_security_comprehensive`
  - `pii_anonymize: PII fields are cleared`

## Investigation

- **Audit KeyError (3 tests).** `tests/test_security/test_routes.py:235` does
  `security_type = registry_info['type']`. `SECURITY_REGISTRY['mojo.apps.account.rest.user.on_user_login']`
  is `{'geofence': {...}}` with **no `type`** key. Cause: `on_user_login` is a login
  endpoint with `@md.requires_geofence(scope="auth")` but no `@requires_auth` /
  `@public_endpoint` / etc. The geofence decorator (`mojo/decorators/geofence.py:36-44`)
  merges a `geofence` key into the entry but never sets `type`; nothing else sets it.
  Introduced by commit `24bbd4e` (geofence engine). The comprehensive test then
  counts this route as "1 insecure or broken route".
  - Likely fix options: (a) auditor uses `registry_info.get('type')` and treats a
    geofence-only/typeless entry as "needs a primary security decorator" or as
    public-by-intent; and/or (b) classify `on_user_login` explicitly (e.g. a
    public/login security marker) so the registry entry carries a `type`.
- **pii_anonymize.** `test_security` expects `metadata == {}` after anonymize, but
  gets `{'protected': {'disable': {... 'reason': 'anonymized', 'by_username': 'system' ...}}}`.
  Determine whether `pii_anonymize` should also clear the protected disable record
  it writes, or the test should accept the anonymized-disable record.
- **Not in scope here:** the `auth/exchange rate-limit` test failure seen only in the
  parallel `--full` run is a cross-module rate-limit-counter flake (passes in
  isolation / default run), tracked separately if it recurs.

## Plan
<!-- PLAN PENDING — /scope fills this section. While this marker is present the item
is UNPLANNED and /build MUST refuse it. Delete this comment when the plan is complete. -->

## Notes
Surfaced by the new `.claude/rules/build-baseline.md` workflow (run `--full` before
building). Filing per that rule's "baseline not all-green → record + file" guidance.
