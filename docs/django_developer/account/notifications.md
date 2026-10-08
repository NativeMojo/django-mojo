# Notifications — Django Developer Reference

The notification system delivers messages to users via WebSocket and device push, and persists them in a queryable inbox. Users can fetch unread notifications via REST and mark them read.

## Quick Usage

```python
# Notify a single user (most common)
user.notify("Your order shipped", action_url="/orders/123")

# With body and custom data
user.notify(
    "New message",
    body="John sent you a message.",
    kind="message",
    data={"thread_id": 42},
    action_url="/messages/42",
)

# Persistent notification (stays until user reads it)
user.notify("Action required", expires_in=None)

# Notify all members of a group
from mojo.apps.account.models.notification import Notification
Notification.send("Maintenance in 10 min", group=group)
```

## `user.notify()`

```python
user.notify(
    title,
    body="",
    kind="general",
    data=None,
    action_url=None,
    expires_in=3600,   # seconds until expiry, None = persistent
    push=True,         # send device push notification
    ws=True,           # send WebSocket message
)
```

Returns a list of `Notification` instances created.

## `Notification.send()`

Lower-level classmethod used by `user.notify()`. Use directly when you need group fan-out or don't have a User instance.

```python
Notification.send(
    title,
    body="",
    user=None,         # target User instance
    group=None,        # fans out to all active group members
    kind="general",
    data=None,
    action_url=None,
    expires_in=3600,
    push=True,
    ws=True,
)
```

If both `user` and `group` are provided, the user receives one notification (no duplicate even if they are a group member).

## Delivery channels

Each call delivers via three channels simultaneously:

| Channel | Mechanism | Behaviour |
|---|---|---|
| **Inbox** | `Notification` DB row | Always created; persists until expired/read |
| **WebSocket** | `realtime.send_to_user` | Best-effort; silently skipped if user is offline |
| **Device push** | `user.push_notification` | APNs/FCM; delivered when offline via platform |

Pass `push=False` or `ws=False` to suppress individual channels.

## Notification model

| Field | Type | Description |
|---|---|---|
| `user` | FK → User | Recipient |
| `group` | FK → Group (nullable) | Source group context |
| `title` | CharField | Notification title |
| `body` | TextField | Optional body text |
| `kind` | CharField | Category for client routing (default `"general"`) |
| `data` | JSONField | Arbitrary payload |
| `action_url` | CharField | Deep-link URL |
| `is_unread` | BooleanField | `True` until marked read |
| `expires_at` | DateTimeField | `None` = persistent; set by `expires_in` |

## Expiry

Notifications with `expires_in` set (default 3600 seconds / 1 hour) are pruned automatically by a cron job that runs hourly. Persistent notifications (`expires_in=None`) remain until the user marks them read.

Override the default expiry globally:

```python
# settings.py
NOTIFICATION_DEFAULT_EXPIRY = 86400  # 24 hours
```

## Marking read

```python
notification.on_action_mark_read(True)
```

Or via REST — see the [Notification API](../../../web_developer/account/notifications.md).

## Device push only (no inbox)

If you need a silent push with no DB record (e.g. background data refresh):

```python
user.push_notification(title="Refresh", data={"type": "refresh"})
```

## Notification preferences

Users opt out per kind and per channel (`in_app`, `email`, `push`). Every
delivery path calls the same check before sending:

```python
from mojo.apps.account.services.notification_prefs import is_notification_allowed

is_notification_allowed(user, "marketing", "email")  # -> bool
```

Preferences live in `user.metadata["notification_preferences"]` as
`{kind: {channel: bool}}`. Default is **allow**. The decision order is:

1. `user` is `None`, or no preferences are stored → allowed.
2. `kind` is falsy (transactional — password reset, verification, …) → allowed.
3. **Master switch** — the reserved kind `"*"`: if `prefs["*"][channel]` is
   present and false → **suppressed**, whatever the per-kind entry says.
4. `prefs[kind][channel]` present → that value; absent → allowed.

```python
{
    "*":         {"email": False},   # no email of any kind
    "billing":   {"email": True},    # still suppressed: master off wins
    "marketing": {"push": False},    # per-kind opt-out on push
}
```

A master switch that is `true` (or absent) adds nothing — it defers to the
per-kind entries, so `{"*": {"email": True}, "marketing": {"email": False}}`
still suppresses marketing email. `"*"` is reserved: never use it as a real
notification kind.

`get_preferences(user)` returns the stored dict; `set_preferences(user, incoming)`
partial-merges `{kind: {channel: bool}}` (including `"*"`) and saves.

### Kinds registry

Register the kinds your project sends so clients can render a preferences
screen without hard-coding them. Call it once at startup (e.g. in your app's
`AppConfig.ready()`) so every process sees the same catalogue:

```python
from mojo.apps.account.services.notification_kinds import (
    register_notification_kinds, list_notification_kinds,
)

register_notification_kinds([
    {"kind": "billing", "label": "Billing", "description": "Invoices and receipts",
     "channels": ["email", "in_app"]},
    {"kind": "marketing", "label": "News & offers"},
])

list_notification_kinds()
# [{"kind": "general", "label": "General", "description": "Messages from this service", "channels": None},
#  {"kind": "billing", ...}, {"kind": "marketing", ...}]
```

| Key | Required | Notes |
|---|---|---|
| `kind` | yes | Lowercase slug `[a-z0-9_.-]+`, max 64 chars. `"*"` is rejected (reserved). |
| `label` | yes | Non-empty display name. |
| `description` | no | Defaults to `""`. |
| `channels` | no | `None` (all channels) or a list of channel names the kind is sent on. |

- `general` (the default `kind` of `user.notify()` / `Notification.send()`) is
  pre-registered as "General" / "Messages from this service". Re-register it
  to change its label.
- Re-registering a kind replaces its entry but keeps its original position;
  `list_notification_kinds()` returns entries in registration order.
- A batch is validated as a whole — on any bad entry `ValueError` is raised
  and nothing is registered.
- The registry is descriptive only. It does not gate delivery: preferences for
  unregistered kinds are still stored and enforced.

The REST surface (`GET`/`POST /api/account/notification/preferences`) is
documented in [User Self-Management § Notification Preferences](../../web_developer/account/user_self_management.md#11-notification-preferences).
