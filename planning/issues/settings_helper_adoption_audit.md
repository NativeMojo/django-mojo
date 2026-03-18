# Bug: Runtime Settings Bypass `SettingsHelper`

**Type**: bug  
**Status**: In Progress  
**Date**: 2026-03-18

## Summary

The framework now supports DB-backed secure settings via `mojo.helpers.settings.settings`, but runtime code still has two bypass patterns:

1. Direct `django.conf.settings` usage in runtime modules.
2. `settings.get(...)` values cached at import time in module-level constants.

Both patterns can bypass or freeze DB-backed values and prevent live updates from taking effect.

## Why this is a bug

- DB-backed settings (including secrets) are intended to override file settings at runtime.
- Any direct `django.conf.settings` read bypasses DB-backed values.
- Any import-time constant (even via helper) freezes values until process restart.

## Audit Findings (Codebase)

### A) Direct `django.conf.settings` in runtime code (must migrate)

19 runtime files (excluding migrations/tests/docs) currently import `django.conf.settings`, including:

- `mojo/apps/jobs/*` (`manager.py`, `keys.py`, `local_queue.py`, `handlers/webhook.py`, `rest/control.py`, `rest/jobs.py`, `cli.py`, `asyncjobs.py`)
- `mojo/apps/fileman/renderer/base.py`
- `mojo/apps/fileman/utils/upload.py`
- `mojo/apps/filevault/services/vault.py`
- `mojo/apps/logit/asyncjobs.py`
- `mojo/apps/incident/asyncjobs.py`
- `mojo/helpers/crypto/sign.py`
- `mojo/helpers/crypto/hash.py`
- `mojo/serializers/core/manager.py`
- `mojo/serializers/core/cache/backends.py`
- `mojo/serializers/core/cache/utils.py`

### B) Import-time cached settings via helper (needs classification)

Many modules define top-level constants from `settings.get(...)` (examples):

- Auth/security flags: `mojo/apps/account/rest/user.py`
- Token/JWT TTLs: `mojo/apps/account/utils/tokens.py`, `mojo/apps/account/utils/jwtoken.py`, `mojo/models/auth.py`
- Jobs runtime knobs: `mojo/apps/jobs/__init__.py`, `job_engine.py`, `scheduler.py`
- Middleware toggles: `mojo/middleware/logging.py`, `mojo/middleware/auth.py`
- REST error behavior: `mojo/models/rest.py`
- OpenAPI version default arg captures import-time value: `mojo/rest/openapi.py`
- S3 global client config frozen at import: `mojo/helpers/aws/s3.py`

## Dynamic vs Startup Classification

### Must read dynamically (per request/call)

- Security gates and auth behavior (`REQUIRE_*`, `ALLOW_*`, bearer handler maps).
- Token/JWT TTL and crypto behavior.
- Rate-limit and incident/logging enforcement toggles.
- External credentials/endpoints (AWS/Twilio/USPS/Google) if managed via secure settings.
- Any value expected to be changed without process restart.

### Startup-load is acceptable (restart required)

- URL topology (`MOJO_PREFIX`, `REST_AUTO_PREFIX`, route module wiring).
- Serializer/backend global registration choices where singleton lifecycle is intentional.
- CLI/daemon bootstrap defaults that are only read when process starts.

## Proposed Remediation Plan

### Phase 1 (highest risk, immediate)

- Replace direct `django.conf.settings` imports with `mojo.helpers.settings.settings` in runtime request/job paths.
- Remove import-time cached auth/token/security constants in account/auth modules.
- Fix obvious frozen defaults:
  - `mojo/rest/openapi.py` default `version=settings.VERSION` capture
  - `mojo/models/rest.py` `MOJO_APP_STATUS_200_ON_ERROR` cached constant
  - `mojo/helpers/aws/s3.py` global `S3 = S3Config(...)` frozen credentials

### Phase 1 Progress (completed in this pass)

- Dynamic call-time settings applied in account auth/token runtime paths:
  - `mojo/apps/account/rest/user.py`
  - `mojo/apps/account/rest/oauth.py`
  - `mojo/apps/account/utils/jwtoken.py`
  - `mojo/apps/account/utils/tokens.py`
  - `mojo/models/auth.py`
- Frozen import-time defaults removed/fixed:
  - `mojo/rest/openapi.py`
  - `mojo/models/rest.py`
  - `mojo/helpers/aws/s3.py`
- Direct `django.conf.settings` imports replaced in active runtime modules (jobs, fileman/filevault services, async jobs, serializers, crypto helpers).  
  Remaining direct imports in runtime tree are limited to internal `SettingsHelper` implementation (`mojo/helpers/settings/helper.py`), which is intentional.

### Phase 2

- Convert jobs/logging/incident async modules to call-time settings reads where behavior is expected to be live.
- Keep daemon/CLI startup-only reads explicit and documented as restart-required.

### Phase 2 Progress (completed in this pass)

- Runtime call-time reads applied across additional modules:
  - Jobs runtime: `mojo/apps/jobs/__init__.py`, `job_engine.py`, `scheduler.py`
  - Logging/incident runtime: `mojo/middleware/logging.py`, `mojo/apps/logit/asyncjobs.py`, `mojo/apps/incident/asyncjobs.py`, `mojo/apps/incident/models/event.py`
  - Account runtime model knobs: `mojo/apps/account/models/{user,group,member,device,geolocated_ip,notification}.py`
  - Serializer/runtime helpers: `mojo/apps/metrics/utils.py`, `mojo/serializers/core/serializer.py`, `mojo/helpers/geoip/*`, `mojo/helpers/geoip/ipverify.py`
- Startup-only settings narrowed to URL/bootstrap path in `mojo/urls.py` (topology settings).

### Documentation Progress

- Added framework settings key reference (names-only): `docs/django_developer/helpers/settings_reference.md`
- Wired docs index/nav:
  - `docs/django_developer/helpers/README.md`
  - `docs/django_developer/helpers/settings.md`
  - `docs/django_developer/README.md`
  - `mkdocs.yml`

### Phase 3

- Sweep remaining module-level settings constants across helpers/apps.
- Document final policy: dynamic vs startup-safe patterns and examples.

## Acceptance Criteria

- No runtime module uses `from django.conf import settings` (except migrations and internal `SettingsHelper` implementation details).
- Security/auth/token/rate-limit settings are read at call time.
- Startup-only settings remain only in clearly intentional bootstrap paths.
- Developer docs updated with a short policy section.

## Notes

- Per current request, this issue is analysis-first and test creation is deferred.
