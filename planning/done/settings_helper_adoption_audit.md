# Bug: Runtime Settings Bypass `SettingsHelper`

**Type**: bug
**Status**: Resolved
**Date**: 2026-03-18
**Resolved**: 2026-03-21

## Root Cause

Two bypass patterns existed in the codebase:

1. Direct `from django.conf import settings` in runtime modules — these never hit the DB-backed `SettingsHelper` lookup chain.
2. Module-level constants set at import time via `settings.get(...)` — these froze values at startup and ignored any DB/Redis overrides.

Additionally, `ALLOW_DB_SETTINGS` was a hardcoded `False` flag in `helper.py`, meaning DB-backed settings were never actually consulted. The correct trigger is Django's own `apps.ready` flag.

## Resolution

### Phase 1 — Replace direct `django.conf.settings` in runtime paths
- `mojo/apps/jobs/*` — `manager.py`, `keys.py`, `local_queue.py`, `handlers/webhook.py`, `rest/control.py`, `rest/jobs.py`, `cli.py`, `asyncjobs.py`
- `mojo/apps/fileman/renderer/base.py`, `utils/upload.py`
- `mojo/apps/filevault/services/vault.py`
- `mojo/apps/logit/asyncjobs.py`
- `mojo/apps/incident/asyncjobs.py`
- `mojo/helpers/crypto/sign.py`, `hash.py`
- `mojo/serializers/core/manager.py`, `cache/backends.py`, `cache/utils.py`

### Phase 2 — Remove import-time cached constants; classify static vs dynamic
- Jobs runtime knobs moved to call-time: `mojo/apps/jobs/__init__.py`, `job_engine.py`, `scheduler.py`
- Logging/incident toggles: `mojo/middleware/logging.py`, `mojo/apps/logit/asyncjobs.py`, `mojo/apps/incident/asyncjobs.py`, `mojo/apps/incident/models/event.py`
- Account model knobs: `mojo/apps/account/models/{user,group,member,device,geolocated_ip,notification}.py`
- Auth/token security gates: `mojo/apps/account/rest/user.py`, `rest/oauth.py`, `utils/jwtoken.py`, `utils/tokens.py`, `mojo/models/auth.py`
- Frozen import-time defaults fixed: `mojo/rest/openapi.py`, `mojo/models/rest.py`, `mojo/helpers/aws/s3.py`
- Serializer/geoip helpers: `mojo/apps/metrics/utils.py`, `mojo/serializers/core/serializer.py`, `mojo/helpers/geoip/*`

### Policy applied

- **`get_static()`** — for infrastructure config that doesn't change at runtime (header names, URL prefixes, logging paths, jobs engine tuning, metrics config, etc.). Safe at module level, no DB hit.
- **`settings.get()` at call time** — for credentials/secrets (Twilio, GeoIP API keys) and security gates (`ALLOW_*`, `REQUIRE_*`, token TTLs) that an admin might update via DB without restarting.
- **Startup-only** — URL topology (`MOJO_PREFIX`, `REST_AUTO_PREFIX`, route wiring) in `mojo/urls.py` only.

### Auto-detection of DB readiness
- Replaced hardcoded `ALLOW_DB_SETTINGS = False` with `_django_ready()` check in `mojo/helpers/settings/helper.py`.
- `_django_ready()` checks `django.apps.apps.ready` — DB lookups activate automatically once all `AppConfig.ready()` methods have fired. Safe to call at any time including import.

### Documentation
- Added settings key reference: `docs/django_developer/helpers/settings_reference.md`
- Updated: `docs/django_developer/helpers/README.md`, `settings.md`, `docs/django_developer/README.md`, `mkdocs.yml`

## Acceptance Criteria — Met

- No runtime module uses `from django.conf import settings` except migrations and internal `SettingsHelper` implementation.
- Security/auth/token/rate-limit settings read at call time.
- Startup-only settings confined to URL bootstrap path.
- Developer docs updated with policy section.
- DB settings auto-enable once Django is ready — no manual flag required.
