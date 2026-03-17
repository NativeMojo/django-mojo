# Push Notifications — Django Developer Reference

Push notifications use Firebase Cloud Messaging (FCM) v1 for all platforms — iOS (via FCM's APNs bridge), Android, and Web. Configuration is model-based (no `settings.py` credentials needed).

> **First time?** See [push_setup.md](push_setup.md) for Firebase project creation, service account setup, and client SDK configuration.

---

## Architecture

```
REST request / internal call
        │
        ▼
services/push.py          (convenience wrappers)
  send_to_user()
  send_to_users()
  send_to_device()
        │
        ▼
User.push_notification()  (iterates user's active devices)
        │
        ▼
RegisteredDevice.send()   (checks preferences → gets config → creates delivery → sends)
        │
        ├─ test_mode=True  →  _send_test()   (log only, no HTTP)
        └─ test_mode=False →  _send_fcm()    (FCM v1 HTTP request)
                                     │
                                     ▼
                               NotificationDelivery (created, status updated)
```

Key files:

| File | Purpose |
|---|---|
| `mojo/apps/account/models/push/device.py` | `RegisteredDevice` — per-device token + preferences |
| `mojo/apps/account/models/push/config.py` | `PushConfig` — FCM credentials, test mode, per-org |
| `mojo/apps/account/models/push/template.py` | `NotificationTemplate` — reusable templates with variable substitution |
| `mojo/apps/account/models/push/delivery.py` | `NotificationDelivery` — delivery tracking and status |
| `mojo/apps/account/rest/push.py` | REST endpoints |
| `mojo/apps/account/services/push.py` | Convenience service functions |

---

## Models

### RegisteredDevice

Represents a device explicitly registered for push via the REST API. Separate from `UserDevice` (browser session tracking).

```python
from mojo.apps.account.models import RegisteredDevice

# Fields
device_token    # FCM registration token from the platform SDK
device_id       # App-provided stable device identifier
platform        # "ios", "android", or "web"
push_enabled    # Master on/off switch (default: True)
push_preferences  # JSONField: {"orders": True, "marketing": False}
is_active       # Soft-delete flag (False after unregister)
last_seen       # Auto-updated on every registration
```

**Sending to a device directly:**

```python
device = RegisteredDevice.objects.get(pk=device_id)
delivery = device.send(
    title="Your order is ready",
    body="Order #123 is waiting for pickup",
    category="orders",
    action_url="myapp://orders/123",
    data={"order_id": 123},
)
# Returns NotificationDelivery or None (if category disabled or no config)
```

**Category-based preference check:** Before sending, `send()` checks `push_preferences.get(category, True)`. If the user has opted out of a category, `send()` returns `None` without creating a delivery record.

---

### PushConfig

FCM credentials and settings, stored per-org or as a system default. Credentials are encrypted via `MojoSecrets`.

```python
from mojo.apps.account.models import PushConfig

# Get config for a user (org config → system default fallback)
config = PushConfig.get_for_user(user)

# Store FCM service account (encrypted)
config.set_fcm_service_account(service_account_dict)
config.save()

# Read back (decrypted)
sa = config.get_fcm_service_account()

# Get FCM project ID
print(config.fcm_project_id)  # extracted from service account JSON

# Test FCM credentials
result = config.test_fcm_connection(test_token=None)
```

**Config resolution:** `get_for_user(user)` checks `user.org` first, then falls back to the `group=None` system default. This allows multi-tenant projects to have separate Firebase projects per organization.

**Test mode:** When `config.test_mode = True`, all sends are faked — logged via `logit`, delivery record created with `status=sent`, no HTTP call to FCM. Safe for development and CI.

---

### NotificationTemplate

Reusable templates with Python `str.format()` variable substitution.

```python
from mojo.apps.account.models import NotificationTemplate

template = NotificationTemplate.objects.create(
    group=None,   # None = system template
    name="order_ready",
    title_template="Order #{order_number} is ready",
    body_template="Hi {customer_name}, your order is ready for pickup.",
    action_url="myapp://orders/{order_number}",
    category="orders",
    variables={"order_number": "Order ID", "customer_name": "Customer display name"},
)

# Render
title, body, action_url, data = template.render({
    "order_number": "123",
    "customer_name": "Alice",
})
```

Templates can be system-wide (`group=None`) or org-scoped. Name must be unique per `(group, name)`.

---

### NotificationDelivery

Tracks every send attempt.

```python
from mojo.apps.account.models import NotificationDelivery

# Status flow: pending → sent → delivered (or failed)
delivery.mark_sent()
delivery.mark_delivered()
delivery.mark_failed("Token expired")

# Fields
delivery.status         # "pending", "sent", "delivered", "failed"
delivery.sent_at        # Timestamp set by mark_sent()
delivery.error_message  # Set by mark_failed()
delivery.platform_data  # FCM response dict (message_id, etc.)
```

---

## Service Layer

`mojo/apps/account/services/push.py` provides convenience wrappers for the three most common patterns:

```python
from mojo.apps.account.services.push import send_to_user, send_to_users, send_to_device

# Send to all active devices for a user
deliveries = send_to_user(
    user=user,
    title="Your order is ready",
    body="Order #123 is waiting",
    category="orders",
    action_url="myapp://orders/123",
    data={"order_id": 123},
)

# Send to multiple users by ID
deliveries = send_to_users(
    user_ids=[1, 2, 3],
    title="Maintenance alert",
    body="System maintenance in 5 minutes",
)

# Send to a specific device
delivery = send_to_device(
    device_id=42,
    data={"action": "sync"},  # silent / data-only
)

# All return NotificationDelivery objects (or None if no config/device found)
```

**Silent (data-only) notifications:** Omit `title` and `body`, pass only `data`. The device receives the payload in the background without showing a visible notification.

---

## Permissions

| Permission | Purpose |
|---|---|
| `send_notifications` | Required to call `POST /api/account/devices/push/send` |
| `manage_push_config` | Manage `PushConfig` records and test FCM connections |
| `manage_devices` | Manage other users' `RegisteredDevice` records |
| `view_devices` | View device records |
| `manage_notifications` | Manage templates and view all delivery history |
| `view_notifications` | View delivery history |
| `owner` | Each user can manage their own devices and view their own delivery history |

---

## REST Endpoints

| Method | URL | Auth | Notes |
|---|---|---|---|
| `POST` | `/api/account/devices/push/register` | required | Register device |
| `POST` | `/api/account/devices/push/unregister` | required | Deactivate device |
| `GET/POST/PATCH/DELETE` | `/api/account/devices/push[/<pk>]` | required | CRUD for devices |
| `GET/POST/PATCH/DELETE` | `/api/account/devices/push/templates[/<pk>]` | required | CRUD for templates |
| `GET/POST/PATCH/DELETE` | `/api/account/devices/push/config[/<pk>]` | required | CRUD for push configs |
| `GET/POST/PATCH/DELETE` | `/api/account/devices/push/deliveries[/<pk>]` | required | CRUD for delivery history |
| `POST` | `/api/account/devices/push/send` | `send_notifications` | Send notification |
| `POST` | `/api/account/devices/push/test` | required | Test send to own devices |
| `GET` | `/api/account/devices/push/stats` | required | Delivery stats for user |
| `POST` | `/api/account/devices/push/config/<pk>/test` | `manage_push_config` | Test FCM credentials |

---

## See Also

- [Push Notifications — FCM Setup Guide](push_setup.md) — Firebase project, service account, iOS/Android/Web client SDK
- [Push Notifications — REST API Reference](../../../web_developer/account/push.md) — endpoints, request/response examples, mobile examples
