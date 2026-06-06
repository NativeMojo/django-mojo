# SMS Channel Parity in Notification Preferences

**Type**: request
**Status**: open
**Date**: 2026-06-05
**Priority**: medium

## Description

`is_notification_allowed(user, kind, channel)` already understands `channel="sms"`
(the assistant tool calls it that way in
[mojo/apps/assistant/services/tools/notifications.py](mojo/apps/assistant/services/tools/notifications.py)),
but SMS is the only delivery channel without first-class preference support. The
result: a marketing SMS cannot be suppressed by user preference the way a
marketing email or push can.

Two structural gaps:

### 1. `SMS.send()` / `send_sms()` take no `kind` and never gate

Email and push gate **inside** the framework send method when a `kind` is supplied,
and skip the gate entirely (always send) when it is omitted — the convention that
keeps transactional messages unsuppressible:

- [mojo/apps/account/models/user.py](mojo/apps/account/models/user.py) `send_template_email(..., kind=None)`:
  ```python
  if kind:
      if not is_notification_allowed(self, kind, "email"):
          return None
  ```
- Same shape in `push_notification(..., kind=None)` and `Notification.send(..., kind="general")`.

But [mojo/apps/phonehub/models/sms.py](mojo/apps/phonehub/models/sms.py) `SMS.send()`
and [mojo/apps/phonehub/__init__.py](mojo/apps/phonehub/__init__.py) `send_sms()`
have no `kind` parameter and never consult `is_notification_allowed`. There is no
way to mark an SMS as marketing-suppressible. Of ~12 SMS call sites across the
framework, only the assistant tool gates (it rolls its own check); the rest send
unconditionally — correct for the transactional ones (OTP, password reset, phone
change), but there is simply no marketing lane.

### 2. The prefs REST endpoint drops `"sms"`

[mojo/apps/account/rest/notification_prefs.py](mojo/apps/account/rest/notification_prefs.py)
hard-codes `VALID_CHANNELS = {"in_app", "email", "push"}` and silently strips any
`"sms"` key from an incoming POST. So even though `is_notification_allowed` reads
`user.metadata["notification_preferences"][kind]["sms"]` correctly, a client can
never store that preference through the public endpoint. (The
`set_preferences` *service* has no such restriction — it is purely the REST layer.)

## Proposed Change

1. Add `kind=None` to `SMS.send()` and `phonehub.send_sms()`. When `kind` **and**
   `user` are present, return early if `not is_notification_allowed(user, kind, "sms")`.
   Omitting `kind` preserves today's always-send behavior, so every existing
   transactional call site is unaffected. Byte-for-byte the email/push pattern.
2. Add `"sms"` to `VALID_CHANNELS` in `account/rest/notification_prefs.py` so SMS
   preferences round-trip through the existing
   `GET`/`POST /api/account/notification/preferences` endpoint.
3. (Optional) Ship a reusable phonehub inbound handler that maps the standard
   carrier keywords (STOP/UNSUBSCRIBE/CANCEL → opt-out, START/UNSTOP → opt-in) to
   `set_preferences(user, {"marketing": {"sms": False/True}})`, wired via the
   existing `SMS_INBOUND_HANDLER` hook. Carrier opt-out is a universal SMS
   requirement, not an app-specific one.

## Acceptance Criteria

- `SMS.send(..., kind="marketing")` for a user who has opted out of marketing SMS
  is suppressed and returns without dispatching; with no `kind` it always sends.
- `POST /api/account/notification/preferences` with `{"marketing": {"sms": false}}`
  persists and is reflected by a subsequent GET and by `is_notification_allowed`.
- Existing transactional SMS call sites (OTP, password reset, phone change,
  verification) continue to send unconditionally (no `kind` passed).
- Tests cover: gated marketing SMS, ungated transactional SMS, and SMS pref
  round-trip through the REST endpoint.

## Out of Scope

- **Opt-out-by-default policy.** `is_notification_allowed` defaults to allow
  (opt-in); regulated opt-out-by-default (e.g. TCPA marketing) is the integrating
  app's policy, layered on top of this gate.
- **Consent audit ledgers.** Timestamped opt-in/opt-out records with source/IP are
  an application compliance concern, not a framework one.
- **Per-group preference scoping.** Preferences remain per-user; brand-scoped lists
  are a separate, larger proposal.
