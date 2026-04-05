# Assistant Send Notification Tool

**Type**: request
**Status**: open
**Date**: 2026-04-05
**Priority**: high

## Description

Add a `send_notification` tool to the assistant that lets the LLM send messages across all supported channels: SMS (Twilio), email (AWS SES), push notifications (FCM), and in-app notifications. Recipients can be specified by user IDs, permission filter (e.g., "all superusers"), or group. The tool is marked `mutates=True` so the LLM confirms before sending.

## Context

Django-mojo already has a complete multi-channel notification system:
- **SMS**: `SMS.send()` via Twilio (`mojo/apps/phonehub/models/sms.py`)
- **Email**: `email_service.send_email()` / `send_with_template()` via AWS SES (`mojo/apps/aws/services/email.py`)
- **Push**: `send_to_user()` / `send_to_users()` via FCM (`mojo/apps/account/services/push.py`)
- **In-app**: `Notification.send()` with WebSocket delivery (`mojo/apps/account/models/notification.py`)
- **User convenience**: `user.notify()`, `user.send_email()`, `user.push_notification()`

The assistant can already investigate incidents and query system state. Letting it send notifications closes the loop — "alert the on-call team about this incident" becomes a single conversation.

## Acceptance Criteria

- Tool accepts: `channel` (sms/email/push/in_app), `message`/`body`, optional `subject`, and `recipients` spec
- Recipients spec supports: `{"user_ids": [1,2,3]}`, `{"permission": "is_superuser"}`, `{"group_id": 5}`, `{"all_active": true}`
- SMS: sends via `SMS.send()`, requires `to_number` or resolves from user profile
- Email: sends via `email_service.send_email()`, uses system default mailbox
- Push: sends via `send_to_users()`, respects device push preferences
- In-app: sends via `Notification.send()`, supports `action_url` and `kind`
- `mutates=True` — LLM must confirm before sending
- Returns delivery summary: how many sent, how many failed, any errors
- Permission-gated: requires `comms` + channel-specific permission (e.g., `manage_sms` for SMS)
- Respects user notification preferences (`is_notification_allowed`)
- Max recipient cap (e.g., 100) to prevent accidental mass messaging

## Investigation

**What exists**:
- `SMS.send(body, to_number, from_number, user, group)` — Twilio SMS with audit trail
- `email_service.send_email(from_email, to, subject, body_text, body_html)` — SES email
- `email_service.send_with_template(from_email, to, template_name, context)` — templated email
- `send_to_user(user, title, body, data, category)` / `send_to_users(user_ids, ...)` — FCM push
- `Notification.send(title, body, user, group, kind, action_url, push, ws)` — in-app + push + WS
- `user.notify(title, action_url)` — convenience method
- `User.objects.filter(is_superuser=True)` — query superusers
- `User.objects.filter(permissions__has_key="perm_name")` — query by JSON permission (PostgreSQL)
- `is_notification_allowed(user, kind, channel)` — preference check

**What changes**:
- `mojo/apps/assistant/services/tools/notifications.py` — **new file**: send_notification handler + TOOLS list
- `mojo/apps/assistant/services/tools/__init__.py` — import and register

**Constraints**:
- SMS requires phone numbers — not all users have them. Tool should report which users couldn't be reached.
- Email requires a verified mailbox/domain in SES. Tool should use system default and report if unavailable.
- Push requires registered devices. Users without devices get skipped gracefully.
- Must respect `is_notification_allowed()` preferences per user/channel.
- Recipient resolution by permission needs care — `permissions` is a JSONField, so filtering varies by database backend. For SQLite (testing), may need Python-side filtering. For PostgreSQL (production), can use `__has_key`.
- Rate limiting: cap at 100 recipients per tool call to prevent accidents.

**Related files**:
- `mojo/apps/phonehub/models/sms.py` — SMS.send()
- `mojo/apps/phonehub/services/twilio.py` — Twilio client
- `mojo/apps/aws/services/email.py` — SES email service
- `mojo/apps/account/services/push.py` — FCM push service
- `mojo/apps/account/models/notification.py` — in-app notifications
- `mojo/apps/account/services/notification_prefs.py` — preference checking
- `mojo/apps/account/models/user.py` — User.notify(), permissions

## Example Interactions

**"Send SMS to all superusers that there are system issues"**
→ `send_notification(channel="sms", body="System issues detected — please check status page", recipients={"permission": "is_superuser"})`
→ Resolves 3 superusers → sends SMS to each → `{"sent": 2, "failed": 1, "errors": [{"user_id": 7, "reason": "no phone number on file"}]}`

**"Email the security team about incident #456"**
→ `send_notification(channel="email", subject="Incident #456 requires attention", body="...", recipients={"permission": "security"})`
→ `{"sent": 5, "failed": 0}`

**"Notify group 12 that maintenance starts in 10 minutes"**
→ `send_notification(channel="in_app", body="Scheduled maintenance begins in 10 minutes", recipients={"group_id": 12}, action_url="/status")`
→ `{"sent": 8, "failed": 0}`

**"Push notification to user 42"**
→ `send_notification(channel="push", title="Your report is ready", body="Q1 metrics report has been generated", recipients={"user_ids": [42]}, action_url="/reports/q1")`
→ `{"sent": 1, "failed": 0}`

## Tests Required

- Send in-app notification to a single user and verify Notification created
- Send to a group and verify fanout to members
- Send with `permission` filter and verify correct users resolved
- Verify `mutates=True` is set on tool definition
- Verify permission gate (user without `comms` perm is denied)
- Verify recipient cap enforced (>100 returns error)
- Verify missing phone number for SMS returns clean error per user
- Verify notification preferences respected (opted-out user skipped)
- Verify delivery summary returned with sent/failed counts

## Out of Scope

- Template management (creating/editing email templates)
- Bulk marketing campaigns
- Scheduled/delayed notifications
- Notification preference management (that's a separate UI concern)
- Webhook or third-party integrations
