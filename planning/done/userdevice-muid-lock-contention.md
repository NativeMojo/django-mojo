# UserDevice.track() causes Aurora lock contention on every authenticated request

**Type**: bug
**Status**: planned
**Date**: 2026-04-07
**Severity**: critical

## Description

Aurora Performance Insights shows 60+ average active sessions (AAS) of LOCK wait types on this single UPDATE — 73% of total DB load:

```sql
UPDATE "account_userdevice" SET "muid" = ? WHERE "account_userdevice"."id" = ?
```

The DB instance was idle before the spike, meaning this is a burst-driven contention problem, not steady-state baseline load.

## Context

The lock contention stems from three compounding issues:

1. **`UserDevice.track()` runs on every authenticated request** — not just login
2. **A bug in the muid comparison causes unnecessary UPDATEs** on established devices
3. **Cascading writes** from `track()` create a chain of row-level locks across 4+ tables per request

This affects every authenticated user on every request. Under concurrent load (e.g., a user with multiple tabs, or many users active simultaneously), the same device rows become lock hotspots. The impact scales with concurrent authenticated users — a spike in active users can take the database from idle to lock-saturated.

## Root Cause Analysis

### Issue 1: track() called on every authenticated request

The call chain:

```
middleware/auth.py:42  →  User.validate_jwt()
  user.py:1319         →  user.track()
    user.py:291        →    self.touch()          # UPDATE account_user
    user.py:294        →    UserDevice.track()     # UPDATE account_userdevice
      device.py:200    →      UserDeviceLocation.track()  # UPDATE account_userdevicelocation
        device.py:252  →        GeoLocatedIP.geolocate()  # UPDATE account_geolocatedip
```

`validate_jwt()` is the bearer auth handler called by `AuthenticationMiddleware.process_request()` on every request with an `Authorization` header. It calls `user.track()` unconditionally (line 1319), which triggers this entire write cascade.

**Result: up to 4 UPDATE/INSERT statements per authenticated request**, even for read-only API calls.

### Issue 2: muid comparison bug (the reported SQL)

`device.py:192-195`:
```python
# Always update muid if we have one and the device doesn't yet
if muid and device.muid != muid:    # BUG: comment says "doesn't yet"
    device.muid = muid              # but code fires on ANY mismatch
    update_fields.append('muid')
```

The comment documents the intended behavior: fill in muid once when the device record doesn't have one. But the code uses `!=` instead of `not device.muid`, so it fires whenever the muid **differs** — not just when it's empty.

**When does muid differ on an established device?**
- User clears cookies → middleware generates new muid (line 48: `request.muid = uuid.uuid4().hex`)
- User switches browsers (different cookie jar, same duid if sent via header/param)
- Cookie expiry after 2 years
- Mobile/API clients that don't persist HttpOnly cookies

In all these cases, every subsequent authenticated request UPDATEs the device row's muid, creating row-level lock contention. Since the middleware generates a fresh UUID when no cookie is present, a client without cookie persistence generates a new muid **on every single request**, causing an UPDATE on every request.

### Issue 3: duid fallback creates shared hotspot rows

`device.py:163-164`:
```python
if not duid:
    duid = f"ua-hash-{ua_hash}"
```

When clients don't send a `duid` (API clients, mobile apps, any client without the mojo JS SDK), all clients sharing the same user-agent string hash to the **same device record**. Combined with Issue 2, all these clients also contend on the muid UPDATE for that shared row.

### Issue 4: Cascading write chain amplifies lock hold time

Each `track()` call triggers a cascade of `get_or_create()` + conditional `save()` across multiple tables:

| Step | Table | Operation | Condition |
|------|-------|-----------|-----------|
| 1 | `account_user` | `save()` + `transaction.commit()` | Every 300s per user (`touch()`) |
| 2 | `account_userdevice` | `get_or_create()` | Every request |
| 3 | `account_userdevice` | `save(update_fields=...)` | When muid differs (bug) or IP/staleness |
| 4 | `account_userdevicelocation` | `get_or_create()` | Every request |
| 5 | `account_userdevicelocation` | `save(update_fields=...)` | When stale (>300s) |
| 6 | `account_geolocatedip` | `filter().first()` | Every request |
| 7 | `account_geolocatedip` | `save(update_fields=['last_seen'])` | Every request (unconditional) |

**Note on step 7**: `GeoLocatedIP.geolocate()` (line 526-529) updates `last_seen` **unconditionally** on every request for known IPs — no staleness check. This means every authenticated request also locks a `geolocatedip` row.

**Note on step 1**: `User.touch()` calls `atomic_save()` which calls `save()` + `transaction.commit()`. The explicit `transaction.commit()` forces an immediate flush, preventing Django from batching writes. This happens every 300 seconds per user, but under concurrent load from multiple users, it adds up.

### Issue 5: auto_now=True extends lock scope

`last_seen = models.DateTimeField(auto_now=True)` on UserDevice (line 32) and UserDeviceLocation (line 216) means Django includes `last_seen` in the UPDATE even when `update_fields` doesn't explicitly list it... **except** when `update_fields` is explicitly provided — in that case Django only updates the listed fields.

This is actually mitigated here since the code always uses `save(update_fields=...)`. But the `get_or_create()` calls do implicit `save()` on create (no `update_fields`), so `auto_now` fires on inserts.

## Likely Spike Scenario

The "idle then spike" pattern suggests:

1. A batch of users authenticate simultaneously (e.g., after a deploy, SSO redirect, or mobile app wake-up)
2. Many share the same user-agent (mobile app or corporate browser), producing the same fallback `duid`
3. Clients either don't persist cookies or had cookies cleared, so each gets a fresh `muid`
4. Every request: `get_or_create` the shared device row → muid differs → UPDATE → row lock
5. Concurrent requests queue on the same row lock → 60 AAS of LOCK waits
6. The cascading writes to `userdevicelocation` and `geolocatedip` extend lock hold time

## Acceptance Criteria

- The muid condition must match the documented intent: only set muid when the device record doesn't have one yet (`not device.muid`), never overwrite an existing muid
- `UserDevice.track()` must not run on every authenticated request — move it to login-only (`jwt_login`) or debounce it with a staleness check (similar to `touch()`'s 300s throttle)
- `GeoLocatedIP.geolocate()` must not unconditionally UPDATE `last_seen` on every call — add a staleness check
- Under concurrent load from the same user/device, the system must not produce row-level lock contention on `account_userdevice`
- Read-only API requests must not trigger any writes to device/location tables

## Investigation

**Likely root cause**: muid comparison bug + track() running on every auth'd request + cascading unconditional writes
**Confidence**: confirmed (code analysis — all paths traced, logic error is unambiguous)

**Code path**:
- `mojo/middleware/auth.py:42` — calls `validate_jwt()` on every bearer request
- `mojo/apps/account/models/user.py:1319` — `validate_jwt()` calls `user.track()` unconditionally
- `mojo/apps/account/models/user.py:280-288` — `touch()` → `atomic_save()` with explicit commit
- `mojo/apps/account/models/device.py:167` — `get_or_create()` per request
- `mojo/apps/account/models/device.py:193` — **BUG**: `device.muid != muid` should be `not device.muid`
- `mojo/apps/account/models/device.py:200` — cascades to `UserDeviceLocation.track()`
- `mojo/apps/account/models/device.py:252` — cascades to `GeoLocatedIP.geolocate()`
- `mojo/apps/account/models/geolocated_ip.py:528-529` — unconditional `last_seen` UPDATE

**Regression test**: not feasible — requires concurrent database load and Aurora lock monitoring

**Related files**:
- `mojo/apps/account/models/device.py` — primary fix (muid condition + debounce)
- `mojo/apps/account/models/user.py` — move `track()` out of `validate_jwt()`, keep in `jwt_login()`
- `mojo/apps/account/models/geolocated_ip.py` — add staleness check to `geolocate()`
- `mojo/middleware/auth.py` — may need adjustment if track() is decoupled from validate_jwt
- `mojo/apps/account/rest/user.py` — `jwt_login()` already calls `user.track()` (line 209), so removing from `validate_jwt` won't lose login tracking

## Plan

**Status**: resolved
**Planned**: 2026-04-07

### Objective

Eliminate lock contention on `account_userdevice` by fixing the muid bug, separating login-time tracking from request-time auth, and adding staleness guards to unconditional write paths.

### Steps

1. `mojo/apps/account/models/device.py:193` — Fix muid comparison bug. Change `if muid and device.muid != muid` to `if muid and not device.muid`. This is the direct cause of the reported SQL — stops the UPDATE that's 73% of DB load.

2. `mojo/apps/account/models/user.py:1319` — Remove `user.track()` from `validate_jwt()`, replace with `user.touch()`. This separates auth (every request) from device tracking (login only). `jwt_login()` at `rest/user.py:209` already calls `user.track()`, so login-time device tracking is preserved.

3. `mojo/apps/account/models/user.py:280-286` — Replace `atomic_save()` in `touch()` with `User.objects.filter(pk=self.pk).update(last_activity=...)`. Avoids full model save + explicit `transaction.commit()`. Single UPDATE, no row lock escalation. No custom `save()` hooks on User that need to fire for `last_activity`.

4. `mojo/apps/account/models/geolocated_ip.py:526-529` — Add staleness check to `geolocate()` before updating `last_seen`. Only write if age > `GEOLOCATION_DEVICE_LOCATION_AGE` (300s), matching the pattern used in `UserDeviceLocation.track()`. Currently writes unconditionally on every call.

### Design Decisions

- **Keep `touch()` in `validate_jwt()`**: Last-activity tracking is needed for security (session timeouts, idle detection). Already debounced at 300s via `USER_LAST_ACTIVITY_FREQ`, so low-cost.
- **Move device tracking to login-only**: Device info doesn't change between logins. Per-request tracking is pure waste. `jwt_login()` already calls `user.track()`.
- **`not device.muid` instead of `!=`**: Once a device has an muid, it should be stable. Cookie changes don't mean the device identity changed.
- **No staleness guard on `UserDevice.track()`**: Moving it to login-only removes the hot path. No need to add complexity for a path that only fires at login.
- **`update()` over `atomic_save()` in `touch()`**: No model signals or `save()` overrides depend on `last_activity` changes. `update()` is a single SQL statement with no explicit commit.
- **Public/unauthenticated requests unaffected**: `AuthenticationMiddleware` returns early when no `Authorization` header is present — `validate_jwt()` is never called. API key auth has its own branch that returns before line 1319.

### Edge Cases

- **Existing devices with wrong muid from the bug**: They keep their current muid. First-assigned muid is the most accurate (closest to device creation), so this is correct.
- **API key auth path**: Separate branch (lines 1286-1305) returns before `touch()` — unaffected by this change.
- **Users with no duid (ua-hash fallback)**: Still creates shared rows, but since `track()` only runs at login now, contention window is negligible.
- **`touch()` with `update()`**: Skips model signals and `save()` overrides — confirmed no hooks depend on `last_activity` changes. The `last_activity` field on the in-memory instance will be stale after `update()`, but it's only read for the staleness check which tolerates this (worst case: one extra update in 300s).

### Testing

- Verify muid is set once and never overwritten on existing devices → `tests/test_security/device_tracking.py`
- Verify `validate_jwt()` does not trigger device/location writes → `tests/test_account/`
- Verify `GeoLocatedIP.geolocate()` skips `last_seen` update when fresh → `tests/test_account/`
- Verify login still produces full device + location + geo tracking → existing tests in `tests/test_security/device_tracking.py`

### Docs

- `docs/django_developer/account/README.md` — Note device tracking is login-only, not per-request
- `CHANGELOG.md` — Critical performance fix: eliminated per-request lock contention on `account_userdevice`

## Resolution

**Status**: resolved
**Date**: 2026-04-07

### What Was Built

Fixed Aurora lock contention (60+ AAS, 73% of DB load) caused by `UserDevice.track()` running on every authenticated request with a muid comparison bug that triggered unnecessary UPDATEs.

### Files Changed

- `mojo/apps/account/models/device.py` — Fixed muid condition: `not device.muid` instead of `device.muid != muid` (set once, never overwrite)
- `mojo/apps/account/models/user.py` — Replaced `user.track()` with `user.touch()` in `validate_jwt()` (device tracking now login-only); replaced `atomic_save()` with `queryset.update()` in `touch()`
- `mojo/apps/account/models/geolocated_ip.py` — Added 300s staleness check before updating `last_seen` in `geolocate()`
- `tests/test_security/device_tracking.py` — Updated `test_fresh_browser_keeps_muid` to assert muid stability

### Tests

- `tests/test_security/device_tracking.py` — 7/7 passed (muid stability, device creation, location, geo)
- `tests/test_auth/` — 46/46 passed (login flows still work with validate_jwt change)
- Full suite: 1,458 passed, 0 failed, 56 skipped
- Run: `bin/run_tests -t test_security.device_tracking`

### Docs Updated

- `docs/django_developer/account/user.md` — Activity tracking section: touch() vs track() usage, muid write-once behavior
- `CHANGELOG.md` — v1.1.16 entry covering all four fixes

### Security Review

No critical issues. Two warnings for consideration:
1. **Device IP visibility**: `UserDevice.last_ip` now only updates at login, not per-request. Acceptable since bouncer signals track per-request activity independently. Consider adding lightweight IP-change detection in `touch()` if forensic accuracy is needed.
2. **Settings key coupling**: `GEOLOCATION_LAST_SEEN_AGE` shares the `GEOLOCATION_DEVICE_LOCATION_AGE` settings key. Consider a dedicated key if independent tuning is needed.
3. **User.modified frozen**: `update()` skips `auto_now` on `modified` field — verify no downstream logic depends on `User.modified` advancing on activity touches.

### Follow-up

- Consider adding lightweight IP-change detection in `touch()` if per-request device IP drift matters for security
- Consider dedicated settings key for GeoLocatedIP staleness
- Verify `User.modified` is not used for cache invalidation or audit trails
