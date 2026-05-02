# Assistant Send Notification Tool

**Type**: request
**Status**: done
**Date**: 2026-04-05
**Priority**: high

## Description

Add a `comms` tool domain to the assistant with a `send_notification` tool that lets the LLM send messages across all supported channels: SMS (Twilio), email (AWS SES), push notifications (FCM), and in-app notifications. The tool is `mutates=True` so the LLM confirms before sending. The domain is loaded on demand via `load_tools(domain="comms")`.

## Context

Django-mojo already has a complete multi-channel notification system:
- **SMS**: `SMS.send()` via Twilio (`mojo/apps/phonehub/models/sms.py`)
- **Email**: `email_service.send_email()` / `send_with_template()` via AWS SES (`mojo/apps/aws/services/email.py`)
- **Push**: `send_to_user()` / `send_to_users()` via FCM (`mojo/apps/account/services/push.py`)
- **In-app**: `Notification.send()` with WebSocket delivery (`mojo/apps/account/models/notification.py`)
- **User convenience**: `user.notify()`, `user.send_email()`, `user.push_notification()`

The assistant can already investigate incidents and query system state. Letting it send notifications closes the loop — "alert the on-call team about this incident" becomes a single conversation.

## Tool Domain Design

This fits the existing two-tier tool loading architecture:

- **Domain**: `comms` — not core, loaded on demand via `load_tools(domain="comms")`
- **Auto-load trigger**: When user says "notify", "send", "alert", "email", "text", "message" — the LLM should auto-load the `comms` domain
- **Permission**: `comms` domain permission, plus channel-specific (`manage_sms`, `manage_email`, etc.)
- **File**: `mojo/apps/assistant/services/tools/notifications.py` — single file, follows the `jobs.py` pattern

The domain registers in `DOMAIN_DESCRIPTIONS`:
```
"comms": "Send notifications via SMS, email, push, and in-app channels"
```

## Acceptance Criteria

- Tool registered as `send_notification` in `comms` domain with `mutates=True`
- Accepts: `channel` (sms/email/push/in_app), `message`/`body`, optional `subject`, `title`, `action_url`, and `recipients` spec
- Recipients spec supports: `{"usernames": ["admin@example.com"]}`, `{"permission": "is_superuser"}`, `{"group_id": 5}`, `{"metadata": {"role": "oncall"}}`, `{"email_domain": "example.com"}`
- All recipient resolution paths filter `is_active=True` — never notify deactivated users
- SMS: sends via `SMS.send()`, requires `to_number` or resolves from user profile
- Email: sends via `email_service.send_email()`, uses system default mailbox
- Push: sends via `send_to_users()`, respects device push preferences
- In-app: sends via `Notification.send()`, supports `action_url` and `kind`
- Returns delivery summary: `{"sent": N, "failed": N, "errors": [...]}`
- Permission-gated: requires `comms` + channel-specific permission
- Respects user notification preferences (`is_notification_allowed`)
- Max recipient cap (100) to prevent accidental mass messaging
- Domain appears in `load_tools()` discovery listing

## Investigation

**What exists**:
- `SMS.send(body, to_number, from_number, user, group)` — Twilio SMS with audit trail
- `email_service.send_email(from_email, to, subject, body_text, body_html)` — SES email
- `send_to_user(user, title, body, data, category)` / `send_to_users(user_ids, ...)` — FCM push
- `Notification.send(title, body, user, group, kind, action_url, push, ws)` — in-app + push + WS
- `user.notify(title, action_url)` — convenience method
- `is_notification_allowed(user, kind, channel)` — preference check
- Tool domain system: `@tool` decorator, `DOMAIN_DESCRIPTIONS`, `load_tools` discovery

**What changes**:
- `mojo/apps/assistant/services/tools/notifications.py` — **new file**: `send_notification` tool with `@tool` decorator
- `mojo/apps/assistant/services/tools/__init__.py` — add `from . import notifications`
- `mojo/apps/assistant/__init__.py` — add `"comms"` to `DOMAIN_DESCRIPTIONS`

**Constraints**:
- SMS requires phone numbers — not all users have them. Tool should report which users couldn't be reached.
- Email requires a verified mailbox/domain in SES. Tool should use system default and report if unavailable.
- Push requires registered devices. Users without devices get skipped gracefully.
- Must respect `is_notification_allowed()` preferences per user/channel.
- Recipient resolution by permission needs care — `permissions` is a JSONField, so filtering varies by database backend. For PostgreSQL, use `__has_key`.
- Rate limiting: cap at 100 recipients per tool call to prevent accidents.

**Related files**:
- `mojo/apps/phonehub/models/sms.py` — SMS.send()
- `mojo/apps/phonehub/services/twilio.py` — Twilio client
- `mojo/apps/aws/services/email.py` — SES email service
- `mojo/apps/account/services/push.py` — FCM push service
- `mojo/apps/account/models/notification.py` — in-app notifications
- `mojo/apps/account/services/notification_prefs.py` — preference checking
- `mojo/apps/account/models/user.py` — User.notify(), permissions
- `mojo/apps/assistant/services/tools/jobs.py` — reference pattern for domain tool file
- `mojo/apps/assistant/__init__.py` — DOMAIN_DESCRIPTIONS, registry

## Example Interactions

**"Send SMS to all superusers that there are system issues"**
→ LLM auto-loads `comms` domain → confirms with user → `send_notification(channel="sms", body="System issues detected", recipients={"permission": "is_superuser"})`
→ `{"sent": 2, "failed": 1, "errors": [{"user_id": 7, "reason": "no phone number on file"}]}`

**"Notify group 12 that maintenance starts in 10 minutes"**
→ `send_notification(channel="in_app", body="Scheduled maintenance begins in 10 minutes", recipients={"group_id": 12}, action_url="/status")`
→ `{"sent": 8, "failed": 0}`

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
- Verify `comms` domain appears in `load_tools()` discovery

## Out of Scope

- Template management (creating/editing email templates)
- Bulk marketing campaigns
- Scheduled/delayed notifications
- Notification preference management (that's a separate UI concern)
- Webhook or third-party integrations
- `all_active` recipient option (too dangerous)
- SQLite compatibility (all environments use PostgreSQL)

## Plan

**Status**: planned
**Planned**: 2026-04-06

### Objective

Add a `comms` tool domain with a `send_notification` tool that sends messages across SMS, email, push, and in-app channels using existing notification infrastructure.

### Steps

1. `mojo/apps/assistant/__init__.py` — Add `"comms": "Send notifications via SMS, email, push, and in-app channels"` to `DOMAIN_DESCRIPTIONS` (line ~38)

2. `mojo/apps/assistant/services/tools/notifications.py` — **New file**. Single `send_notification` tool following the `jobs.py` pattern:
   - `@tool(name="send_notification", domain="comms", permission="comms", mutates=True, ...)`
   - Input schema: `channel` (enum: sms/email/push/in_app), `body` (required), optional `subject`, `title`, `action_url`, `kind`, and `recipients` (object)
   - `_resolve_recipients(params)` — resolves recipients from one of:
     - `{"usernames": ["admin@example.com", "ops@example.com"]}` — lookup by username/email
     - `{"permission": "is_superuser"}` — `User.objects.filter(permissions__has_key=perm)` (PostgreSQL JSONField)
     - `{"group_id": 5}` — group members via `GroupMember.objects.filter(group_id=..., is_active=True)`
     - `{"metadata": {"role": "oncall"}}` — `User.objects.filter(metadata__contains={"role": "oncall"})` (PostgreSQL)
     - `{"email_domain": "example.com"}` — `User.objects.filter(email__iendswith="@example.com")`
   - All resolution paths apply `.filter(is_active=True)` — never notify deactivated users
   - Cap at 100 recipients — return error with total count if exceeded
   - Channel dispatchers (each checks `is_notification_allowed` before sending):
     - `_send_in_app(users, params)` — `Notification.send(title, body, user=user, kind=kind, action_url=action_url)`
     - `_send_push(users, params)` — `user.push_notification(title=title, body=body, category=kind)`
     - `_send_sms(users, params)` — `SMS.send(body, to_number=user.phone_number, user=user)`, skip if no phone_number
     - `_send_email(users, params)` — `email_service.send_email(from_email, to=user.email, subject=subject, body_text=body)`, from_email from system default mailbox
   - Return: `{"sent": N, "failed": N, "skipped": N, "errors": [{"user_id": ..., "username": ..., "reason": ...}]}`

3. `mojo/apps/assistant/services/tools/__init__.py` — Add `from . import notifications  # noqa: F401`

4. `mojo/apps/assistant/services/tools/discovery.py` — Update `load_tools` description string (line 29) to include `comms` in the available domains list

### Design Decisions

- **Single tool with `channel` parameter**: One `send_notification` tool instead of four per-channel tools. The LLM picks the channel from conversation context. Keeps the domain simple.
- **Recipients by username, not user_ids**: More natural for the LLM ("notify admin@example.com") and matches how users think. The LLM can look up usernames via the `users` domain tools first.
- **Metadata filtering**: Uses PostgreSQL `__contains` lookup on the User `metadata` JSONField. Enables flexible targeting like `{"role": "oncall"}` without adding new models.
- **Permission: `comms`**: Already an established permission in the codebase (used by SMS, chat, phone models). No new permissions needed.
- **Recipient cap of 100**: Enforced in `_resolve_recipients` before any sending. Returns error with total count so the LLM can inform the user.
- **System default mailbox for email**: Resolve `from_email` from first outbound-enabled Mailbox or `settings.DEFAULT_FROM_EMAIL`. No per-call mailbox selection.
- **`skipped` count separate from `failed`**: Users who opted out or lack a phone number are `skipped` (expected), not `failed` (unexpected error). This gives the LLM better context for its summary.

### Edge Cases

- **No phone number for SMS**: Skip user, add to errors with `"no phone number on file"`. Count as skipped, not failed.
- **No mailbox configured for email**: Return early with `{"error": "No outbound mailbox configured. Set up a Mailbox with allow_outbound=True."}` before attempting any sends.
- **No registered devices for push**: `push_notification()` returns empty list — count as skipped.
- **User opted out via preferences**: `is_notification_allowed(user, kind, channel)` check before each send. Opted-out users counted as skipped.
- **Empty recipients after resolution**: Return `{"error": "No active users matched the recipients filter."}`.
- **Username not found**: Include in errors with `"user not found"`.
- **Recipients object has multiple keys**: Return error — only one resolution strategy per call (usernames OR permission OR group_id OR metadata OR email_domain).
- **Inactive users**: All resolution paths filter `is_active=True`. An inactive user in a `usernames` list gets added to errors with `"user is inactive"`.

### Testing

All tests in `tests/test_assistant/19_test_notifications_tool.py`:

- `test_tool_registered_with_mutates` — verify `send_notification` in registry with `mutates=True` and `domain="comms"`
- `test_comms_domain_in_discovery` — verify `comms` appears in `DOMAIN_DESCRIPTIONS` and `get_available_domains()`
- `test_resolve_by_usernames` — resolve `{"usernames": ["user@test.com"]}` returns correct User objects
- `test_resolve_by_permission` — resolve `{"permission": "is_superuser"}` finds superusers
- `test_resolve_by_group` — resolve `{"group_id": N}` returns active group members
- `test_resolve_by_metadata` — resolve `{"metadata": {"role": "oncall"}}` filters correctly
- `test_resolve_by_email_domain` — resolve `{"email_domain": "example.com"}` finds matching active users
- `test_resolve_excludes_inactive_users` — deactivated users (is_active=False) are never included in any resolution path
- `test_recipient_cap_enforced` — >100 recipients returns error with total count
- `test_send_in_app` — send in_app to a user, verify Notification record created in DB
- `test_send_sms_missing_phone` — user without phone_number gets skipped with clean error
- `test_notification_prefs_respected` — user who opted out of channel is skipped
- `test_delivery_summary_format` — verify response has sent/failed/skipped/errors keys

### Docs

- `docs/django_developer/assistant/tools.md` — add `comms` domain to domain listing with tool description
- `docs/web_developer/assistant/tools.md` — document `send_notification` parameters and response format
- `CHANGELOG.md` — new `comms` domain with `send_notification` tool
