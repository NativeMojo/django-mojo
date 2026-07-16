---
id:
type: bug
title: AUTH_PHONE_VERIFY_DEV_BYPASS_CODE is DB/Redis-settable — full phone-verification bypass, startup warning blind to it
priority: P1
effort: XS
owner: backend
opened: 2026-07-10
depends_on: []
related: [DM-031, DM-023]
links:
  - found by DM-031 post-build security review (2026-07-10)
---

# `AUTH_PHONE_VERIFY_DEV_BYPASS_CODE` read via DB-aware `settings.get` — remotely-armable phone-verify bypass

## What & Why

`_dev_bypass_code()` (mojo/apps/account/services/phone_register.py:102) reads
`AUTH_PHONE_VERIFY_DEV_BYPASS_CODE` through the DB/Redis-aware `settings.get(...)`
(DB via Redis cache → global, before the django.conf fallback). The key is not
in `Setting.VALIDATORS`, so it's a plain arbitrary-key Setting row — writable by
anyone with `manage_settings` via the generic `POST /api/settings` REST, or by
direct Redis write to the settings cache.

`verify_code()` (phone_register.py:106-134) accepts the value returned by
`_dev_bypass_code()` **in addition to** the real generated SMS code for the
session. So an attacker who can write one Setting row (or one Redis key) sets a
known bypass code and then satisfies **any** phone-verification session with it
— a full phone-verification auth bypass, not test plumbing.

This is the exact vector DM-031 just closed for `GEOFENCE_TEST_OVERRIDE` /
`MOJO_TEST_MODE`, in the phone-verification path. It is worse than DM-031:
that one was a test knob / DoS; this is an authentication bypass.

Note the design is already half-implemented file-only: the startup warning at
`mojo/apps/account/apps.py:25` reads the SAME key via `settings.get_static(...)`.
So on a compromised deployment the boot log can read "not set" (empty file
value) while the DB/Redis vector is live and armed — the operator warning is
blind to the actual enforcement source.

## Repro

1. No `AUTH_PHONE_VERIFY_DEV_BYPASS_CODE` in the Django settings file (startup
   warning silent).
2. `POST /api/settings {"key": "AUTH_PHONE_VERIFY_DEV_BYPASS_CODE", "value": "000000"}`
   as a `manage_settings` holder (or `HSET settings:global AUTH_PHONE_VERIFY_DEV_BYPASS_CODE 000000`).
3. Start any phone-verification session, submit `000000`.
4. Expected: rejected (no dev bypass configured at deploy time). Actual:
   `verify_code` accepts it — session verified.

## Suggested fix

Change phone_register.py:102 to
`settings.get_static("AUTH_PHONE_VERIFY_DEV_BYPASS_CODE", "")`, matching
apps.py:25 and the DM-031 fix pattern (the code is a conf-file/deploy-time
knob by design). Add a regression test: a DB/Redis
`AUTH_PHONE_VERIFY_DEV_BYPASS_CODE` Setting row must NOT be accepted by
`verify_code` (only a real generated code or a conf-file bypass value works).

## Acceptance Criteria

- [ ] `AUTH_PHONE_VERIFY_DEV_BYPASS_CODE` is read conf-file-only
      (`settings.get_static`) — a DB/Redis Setting row can no longer arm a
      phone-verify bypass code.
- [ ] The conf-file bypass code still works (dev/staging knob preserved), and
      the loopback-gated `X-Mojo-Test-Phone-Verify-Bypass-Code` header path is
      unchanged.
- [ ] Regression test covering the DB-row-ignored case.

## Notes

- Found by DM-031's post-build security review (2026-07-10). Same family as
  DM-031 and DM-023 ("adjacent/remotely-writable settings bypass the
  intended file-only or validated plane"). Worth a sweep: audit every
  `settings.get(...)` read of a `*_BYPASS_*` / `*_DEV_*` / test/debug-style key
  for the same DB-vector exposure.
