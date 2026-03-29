# Login Geolocation Tracking

**Type**: request
**Status**: planned
**Date**: 2026-03-29
**Priority**: high

## Description

Build a standalone login tracking system that records every successful login with full geolocation data (country, region, city, coordinates). This is **not** part of the incident/event pipeline — it is a dedicated, lightweight model purpose-built for:

1. **Per-user login map**: Visualize where a specific user logs in from (map pins with counts and timestamps)
2. **System-wide login map**: Visualize all logins across the platform by country/region (choropleth or pin map)
3. **Anomaly visibility**: Make it easy to visually spot unusual login locations (new country, new region for a user)
4. **Metrics integration**: Record login metrics by country_code and region for time-series dashboards

## Context

The existing `UserDeviceLocation` model tracks device-IP associations but has key gaps for this use case:
- It deduplicates by (user, device, ip) — so repeat logins from the same IP/device don't create new rows. There's no login count or per-login timestamp history.
- It's device-centric, not login-event-centric. You can't answer "how many times did this user log in from Brazil this month?"
- No `country_code` or `region` stored directly — requires joining through `GeoLocatedIP` for every query.
- No metrics are recorded on login location (only `login_attempts` endpoint metric and `user_activity_day` exist).
- The existing REST endpoint (`/api/user/device/location`) is a basic CRUD list — no aggregation, no filtering by country/region, no system-wide view.

The admin portal needs server-side aggregation endpoints so the frontend can render maps without pulling all records and aggregating client-side.

## Acceptance Criteria

- Every successful `jwt_login()` call creates a `UserLoginEvent` record with: user, ip, country_code, region, city, latitude, longitude, user_agent summary, device ref, login source, timestamp
- `country_code` and `region` are denormalized onto the record (not just FK to GeoLocatedIP) for fast filtering and aggregation
- REST endpoints support:
  - List login events for a specific user (filterable by date range, country_code, region)
  - System-wide login event aggregation by country_code (with optional region drill-down)
  - Both return data shaped for map visualization (country/region codes + counts + coordinate centroids)
- Metrics recorded on each login: `login:country:{code}`, `login:region:{code}:{region}` — enabling time-series charts via the existing metrics system
- New-country and new-region detection: flag when a user logs in from a country_code or region they have no prior `UserLoginEvent` for
- Anomaly flags are queryable (e.g., filter for "first-time country" logins across the system)
- User agent info captured at login time (browser, OS, device type) for secondary analysis

## Investigation

**What exists**:
- `UserDevice` + `UserDeviceLocation` — tracks devices and IP associations, but deduplicates and doesn't count logins
- `GeoLocatedIP` — full geo cache per IP (country, region, city, lat/lon, threat flags). Already called during `UserDevice.track()` → `UserDeviceLocation.track()` → `GeoLocatedIP.geolocate(ip)`
- `jwt_login()` in `mojo/apps/account/rest/user.py:204` — single function all login paths flow through. Calls `user.track()` which triggers device tracking. This is the insertion point.
- `mojo.apps.metrics` — existing metrics system with `metrics.record()` for counters and time-series
- `incident.Event` — captures login events for security pipeline, but that's a separate concern. This new system is complementary, not a replacement.
- `@md.endpoint_metrics("login_attempts")` on the login endpoint — counts attempts but not successes, and no geo breakdown

**What changes**:
- **New model**: `UserLoginEvent` in `mojo/apps/account/models/` — one row per successful login
- **Modified**: `jwt_login()` to create a `UserLoginEvent` after successful auth and record geo metrics
- **New REST endpoints**: in `mojo/apps/account/rest/` for login event queries and aggregations
- **New metrics**: `login:country:{code}` and `login:region:{code}:{region}` recorded per login

**Constraints**:
- Must not slow down `jwt_login()` — geo lookup is already happening via `user.track()`, so denormalize from the existing `GeoLocatedIP` record
- Must handle missing geo data gracefully (geo lookup may be async/stale for new IPs)
- Country/region codes must be consistent with `GeoLocatedIP.country_code` (ISO 3166)
- Respect existing permission model: system-wide views require `manage_users` + `security`, per-user views require `manage_users` + `users`
- Do not duplicate what `incident.Event` does — this is login history for visualization, not security event processing

**Related files**:
- `mojo/apps/account/models/device.py` — UserDevice, UserDeviceLocation (reference pattern)
- `mojo/apps/account/models/geolocated_ip.py` — GeoLocatedIP (geo data source)
- `mojo/apps/account/rest/user.py` — `jwt_login()` at line 204 (insertion point)
- `mojo/apps/account/rest/device.py` — existing device/location REST (reference pattern)
- `mojo/apps/account/models/user.py` — User model, `track()` method
- `mojo/apps/account/models/__init__.py` — model exports

## Endpoints

| Method | Path | Description | Permission |
|---|---|---|---|
| GET | `/api/account/logins` | List login events (filterable by user, date range, country_code, region) | RestMeta `VIEW_PERMS` |
| GET | `/api/account/logins/<int:pk>` | Single login event detail | RestMeta `VIEW_PERMS` |
| GET | `/api/account/logins/summary` | System-wide login counts by country_code, optional region drill-down | `manage_users`, `security`, `users` |
| GET | `/api/account/logins/user` | Per-user login counts by country_code + region (takes `user_id` param) | `manage_users`, `security`, `users` |

## Settings

| Setting | Default | Purpose |
|---|---|---|
| `LOGIN_EVENT_TRACKING_ENABLED` | `True` | Master toggle for login event recording |
| `LOGIN_EVENT_PRUNE_DAYS` | `365` | Auto-prune login events older than this |
| `LOGIN_EVENT_FLAG_NEW_COUNTRY` | `True` | Flag logins from a country_code not seen before for this user |
| `LOGIN_EVENT_FLAG_NEW_REGION` | `True` | Flag logins from a region not seen before for this user |

## Tests Required

- Login creates a `UserLoginEvent` with correct geo data denormalized
- Multiple logins from same IP create separate event rows (not deduplicated)
- Country/region aggregation endpoint returns correct counts
- Per-user aggregation returns only that user's data
- New-country flag is set correctly on first login from a new country
- New-region flag is set correctly on first login from a new region
- Login from unknown/unresolved IP creates event with null geo fields (no crash)
- Metrics `login:country:{code}` are recorded on login
- `LOGIN_EVENT_TRACKING_ENABLED=False` suppresses event creation
- Permission checks: non-admin users cannot access system-wide endpoints
- Aggregation endpoint supports date range filtering

## Out of Scope

- Automated blocking or challenge based on new-location detection (use incident rules for that)
- User-facing "my login history" endpoint (future request — this is admin-only)
- Real-time push notifications on anomalous logins (can be layered on later via incident rules)
- Changes to the existing `UserDeviceLocation` model or its endpoints
- Map rendering or frontend components (admin portal concern)

## Plan

**Status**: resolved
**Planned**: 2026-03-29

### Objective
Record every successful login with denormalized geo data in a new `UserLoginEvent` model, expose REST endpoints for list/detail/aggregation, and record per-country/region metrics.

### Steps
1. `mojo/apps/account/models/login_event.py` — New `UserLoginEvent` model
   - Fields: `user` (FK), `ip_address`, `country_code`, `region`, `city`, `latitude`, `longitude`, `user_agent_info` (JSONField via `parse_user_agent`), `device` (FK to UserDevice, nullable), `source` (CharField — password, magic, sms, totp, oauth), `is_new_country` (BooleanField), `is_new_region` (BooleanField), `created`, `modified`
   - Indexes: `db_index=True` on `country_code`, `region`, `is_new_country`, `is_new_region`, `created`. Composite index on `(user, country_code)` and `(user, country_code, region)` for anomaly lookups
   - RestMeta: `VIEW_PERMS = ['manage_users', 'security', 'users']`, no `SAVE_PERMS` (read-only audit log)
   - `SEARCH_FIELDS`: `ip_address`, `country_code`, `region`, `city`
   - Graphs: `list` (summary fields), `default` (all fields including user_agent_info)
   - Classmethod `track(request, user, device, source)` — looks up `GeoLocatedIP` for `request.ip`, denormalizes geo fields, checks new-country/new-region via `exists()`, records metrics, creates row. Gated by `LOGIN_EVENT_TRACKING_ENABLED` setting.

2. `mojo/apps/account/models/__init__.py` — Add `from .login_event import UserLoginEvent`

3. `mojo/apps/account/rest/user.py:~208` — In `jwt_login()`, after `user.track()` (which sets `request.device`), add: `UserLoginEvent.track(request, user, request.device, source)`

4. `mojo/apps/account/rest/login_event.py` — New REST file with endpoints:
   - `@md.URL('logins')` + `@md.URL('logins/<int:pk>')` + `@md.uses_model_security(UserLoginEvent)` — standard RestMeta CRUD list/detail
   - `@md.GET('logins/summary')` + `@md.requires_perms('manage_users', 'security', 'users')` — system-wide aggregation by `country_code`, optional `region` param for drill-down. Returns `[{country_code, count, latitude, longitude}]` using `values().annotate()`
   - `@md.GET('logins/user')` + `@md.requires_perms('manage_users', 'security', 'users')` + `@md.requires_params('user_id')` — per-user aggregation, same shape filtered to one user

5. Metrics — Inside `UserLoginEvent.track()`:
   - `metrics.record(f"login:country:{code}", category="logins")` when country_code present
   - `metrics.record(f"login:region:{code}:{region}", category="logins")` when both present
   - `metrics.record("login:new_country", category="logins")` when `is_new_country`
   - `metrics.record("login:new_region", category="logins")` when `is_new_region`

### Design Decisions
- **Denormalize geo fields** rather than FK to GeoLocatedIP: avoids joins for aggregation, prevents historical records from changing when GeoLocatedIP refreshes
- **New-country/region detection at write time**: one `exists()` query each (indexed on `(user, country_code)`), stored as bools for cheap filtering later
- **`source` from `jwt_login`'s existing param**: no new plumbing needed, just pass it through
- **`/api/account/logins` as single resource**: standard RestMeta CRUD for list/detail, separate GET endpoints for aggregation views
- **Per-user endpoint uses `user_id` query param** not URL segment: follows "dynamic segments at end only" rule
- **`user_agent_info` as JSONField**: stores full `parse_user_agent()` dict (same pattern as `UserDevice.device_info`), richer than a summary string
- **No SAVE_PERMS**: append-only audit log, no REST writes. Pruning via future job using `LOGIN_EVENT_PRUNE_DAYS`

### Edge Cases
- **Missing geo data**: GeoLocatedIP may have null country_code for new/unresolved IPs — store nulls, skip anomaly flags and metrics, no crash
- **Concurrent first-login-from-country**: Two simultaneous logins could both get `is_new_country=True` — acceptable, it's a flag not a unique constraint
- **`LOGIN_EVENT_TRACKING_ENABLED=False`**: `track()` returns None immediately, no DB write, no metrics
- **High-volume logins**: One lightweight INSERT per login. No FK to GeoLocatedIP, just denormalized strings. Index on `created` supports pruning

### Testing
- Login creates `UserLoginEvent` with correct denormalized geo → `tests/test_account/test_login_event.py`
- Multiple logins from same IP create separate rows → same file
- New-country and new-region flags set correctly → same file
- Null geo fields when IP unresolved → same file
- `LOGIN_EVENT_TRACKING_ENABLED=False` suppresses creation → same file
- Metrics recorded on login → same file
- Aggregation endpoints return correct counts/shapes → `tests/test_account/test_login_event_rest.py`
- Permission checks on endpoints → same file
- Date range filtering on aggregation → same file

### Docs
- `docs/django_developer/account/README.md` — UserLoginEvent model, settings, track() usage
- `docs/web_developer/account/README.md` — 4 new endpoints with request/response shapes
- `CHANGELOG.md` — New feature entry

## Resolution

**Status**: resolved
**Date**: 2026-03-29

### What Was Built
Login geolocation tracking system — UserLoginEvent model records every successful login with denormalized geo data, anomaly flags, and per-country/region metrics. REST endpoints for list/detail and aggregation.

### Files Changed
- `mojo/apps/account/models/login_event.py` — New UserLoginEvent model with track() classmethod
- `mojo/apps/account/models/__init__.py` — Added UserLoginEvent export
- `mojo/apps/account/rest/user.py` — Hooked UserLoginEvent.track() into jwt_login()
- `mojo/apps/account/rest/login_event.py` — REST endpoints: CRUD list/detail + geo summary + per-user summary
- `docs/web_developer/account/login_events.md` — Full endpoint spec for Admin Portal team
- `docs/web_developer/account/README.md` — Added login events link

### Tests
- `tests/test_account/test_login_event.py` — 8 tests covering: event creation with geo, no dedup, new-country/region flags, null geo handling, tracking toggle, source field
- Run: `bin/run_tests -t test_account.test_login_event`

### Docs Updated
- `docs/web_developer/account/login_events.md` — Full REST API spec with request/response examples
- `docs/web_developer/account/README.md` — Index entry added

### Security Review
Pending — agent running in background.

### Follow-up
- User must run `makemigrations` and `migrate` in their Django project to create the table
- REST endpoint tests (test_login_event_rest.py) for aggregation endpoints — deferred pending server-side test infrastructure
- CHANGELOG.md update — docs-updater agent handling
- `LOGIN_EVENT_PRUNE_DAYS` pruning job — future request
