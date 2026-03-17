# Push notifications: missing documentation and incomplete tests

**Type**: issue (docs + testing gap)
**Status**: Resolved — 2026-03-17
**Date**: 2026-03-17

## Description

The push notification system is fully implemented (`models/push/`, `rest/push.py`, `services/push.py`) but has no documentation in either the `django_developer` or `web_developer` doc tracks. The only existing docs are in `legacy_docs/` which reference `pyfcm`/`apns2` — libraries that are **not used** by the current implementation (which uses FCM v1 API exclusively). The existing test file (`tests/test_accounts/push_notifications.py`) exists but needs review against the current implementation to confirm completeness.

## What Exists

- `mojo/apps/account/models/push/` — `RegisteredDevice`, `PushConfig`, `NotificationTemplate`, `NotificationDelivery`
- `mojo/apps/account/rest/push.py` — 11 endpoints
- `mojo/apps/account/services/push.py` — `send_to_user`, `send_to_users`, `send_to_device`
- `tests/test_accounts/push_notifications.py` — 24 tests (test_mode based, no real FCM calls)
- `legacy_docs/rest_api/push.md` — outdated outline (wrong libraries, old architecture)
- `legacy_docs/future/push_notify.md` — old design notes (not current implementation)

## What's Missing

1. `docs/django_developer/account/push.md` — backend developer reference
2. `docs/web_developer/account/push.md` — REST API / mobile client reference
3. Entries in both doc track README indexes
4. Test coverage review — confirm tests cover current endpoints and service functions accurately

## Acceptance Criteria

- `docs/django_developer/account/push.md` documents: architecture, models, FCM v1 setup, `PushConfig` per-group resolution, `services/push.py` functions, required settings, test mode
- `docs/web_developer/account/push.md` documents: all 11 REST endpoints with request/response examples, device registration flow, notification sending, preferences, delivery history, stats
- Both doc track READMEs updated with push entry
- Tests reviewed and any missing coverage added
- Legacy docs left in place (do not delete — they may be referenced externally)

## Plan

### 1. Docs — django_developer

File: `docs/django_developer/account/push.md`

Sections:
- Overview (FCM v1 only, no APNs direct — iOS via FCM)
- Architecture (models + service layer diagram)
- Models: `RegisteredDevice`, `PushConfig`, `NotificationTemplate`, `NotificationDelivery`
- `PushConfig.get_for_user(user)` — group → system fallback
- `services/push.py` — `send_to_user`, `send_to_users`, `send_to_device`
- FCM v1 setup: service account JSON, `set_fcm_service_account()`
- Test mode: `PushConfig.test_mode=True` skips real FCM calls, returns fake delivery
- Required settings (none required — config is model-based)
- Permissions: `send_notifications`, `manage_push_config`, `manage_devices`, `view_notifications`

### 2. Docs — web_developer

File: `docs/web_developer/account/push.md`

Sections:
- Overview and flow
- Device registration: `POST /api/account/devices/push/register` + `POST .../unregister`
- Device management CRUD: `GET/PATCH/DELETE /api/account/devices/push`
- Send notification: `POST /api/account/devices/push/send`
- Test endpoint: `POST /api/account/devices/push/test`
- Stats: `GET /api/account/devices/push/stats`
- Templates CRUD: `POST /api/account/devices/push/templates`
- Config CRUD: `POST /api/account/devices/push/config`
- Delivery history: `GET /api/account/devices/push/deliveries`
- Push preferences schema (per-category opt-out)
- Platform values: `ios`, `android`, `web`
- Mobile client examples (iOS, Android, Web)
- Error table

### 3. README indexes

- `docs/django_developer/README.md` — add push entry
- `docs/web_developer/README.md` — add push entry

### 4. Test review

- Read existing 24 tests against current REST endpoints and service layer
- Add any missing coverage (legacy endpoint, config test endpoint, per-group config resolution)
