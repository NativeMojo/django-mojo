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
