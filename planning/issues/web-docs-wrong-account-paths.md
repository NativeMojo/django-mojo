# Web Developer Docs Have Wrong API Paths for Account Endpoints

**Type**: bug
**Status**: open
**Date**: 2026-04-01
**Severity**: high

## Description

The `docs/web_developer/` docs assume all account app endpoints are prefixed with `/api/account/`, but the account app sets `APP_NAME = ""` in `mojo/apps/account/rest/__init__.py`, which means routes use whatever prefix is explicitly in the `@md.URL()` decorator — no automatic `account/` prefix is added.

Some account REST modules explicitly include `account/` in their decorator paths (e.g., `@md.URL('account/bouncer/device')`) and those docs are correct. But several endpoints do NOT include `account/` and the docs incorrectly add it.

## Context

This affects any admin portal developer copying API paths from the docs — their requests will 404. The geoip/firewall docs are the worst hit with 29 wrong references. This is a critical integration blocker for anyone building a security dashboard.

## Root Cause

`mojo/apps/account/rest/__init__.py` line 1: `APP_NAME = ""`. This means the framework does not auto-prefix account routes with `account/`. Each REST module in the account app chooses its own prefix explicitly. The docs were written assuming a uniform `account/` prefix.

## Wrong Paths Found

### 1. `/api/account/login` → should be `/api/login`
- `docs/web_developer/core/authentication.md:15`
- `docs/web_developer/account/admin_portal.md:11`

Actual decorators in `mojo/apps/account/rest/user.py`:
- `@md.URL('login')` → `/api/login`
- `@md.URL('auth/login')` → `/api/auth/login`
- `@md.URL('account/jwt/login')` → `/api/account/jwt/login`

None of these produce `/api/account/login`.

### 2. `/api/account/system/geoip` → should be `/api/system/geoip`
- `docs/web_developer/account/geoip.md` — 4 occurrences (+ line 5 claims "All endpoints are under the `account` app prefix" which is wrong)
- `docs/web_developer/account/firewall.md` — 22 occurrences
- `docs/web_developer/security/README.md` — 3 occurrences

**Total: 29 occurrences across 3 files**

Actual decorator in `mojo/apps/account/rest/device.py`:
- `@md.URL('system/geoip')` → `/api/system/geoip`

### 3. `/api/account/me` → should be `/api/user/me`
- `docs/web_developer/account/admin_portal.md:230`

Actual decorators in `mojo/apps/account/rest/user.py`:
- `@md.URL('user/me')` → `/api/user/me`
- `@md.URL('account/user/me')` → `/api/account/user/me`

Neither produces `/api/account/me`.

### Paths that ARE correct (no changes needed)
These modules explicitly include `account/` in their decorators:
- `/api/account/bouncer/*` — correct (48 refs across 3 files)
- `/api/account/logins` — correct (login_events.md)
- `/api/account/notification` — correct
- `/api/account/passkeys` — correct
- `/api/account/totp/*` — correct
- `/api/account/api_keys` — correct
- `/api/account/oauth_connection` — correct
- `/api/account/devices/push/*` — correct

## Acceptance Criteria

- All 32 wrong path references fixed across 5 files
- `docs/web_developer/account/geoip.md` line 5 corrected (remove false claim about account prefix)
- No regressions to the paths that are already correct
- Verify no other docs files have the same issue (check django_developer/ too)

## Investigation

**Likely root cause**: Docs author assumed `APP_NAME` prefix behavior applied to account app, but account app explicitly opts out with `APP_NAME = ""`

**Confidence**: confirmed — verified every `@md.URL` decorator in `mojo/apps/account/rest/` against the doc references

**Code path**:
- `mojo/apps/account/rest/__init__.py:1` — `APP_NAME = ""`
- `mojo/apps/account/rest/user.py` — login and me decorators
- `mojo/apps/account/rest/device.py` — geoip decorators
- `mojo/urls.py` — URL loading logic that uses APP_NAME

**Regression test**: not feasible — documentation-only issue

**Related files**:
- `docs/web_developer/core/authentication.md` — 1 fix
- `docs/web_developer/account/admin_portal.md` — 2 fixes
- `docs/web_developer/account/geoip.md` — 5 fixes
- `docs/web_developer/account/firewall.md` — 22 fixes
- `docs/web_developer/security/README.md` — 3 fixes
