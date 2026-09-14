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

The `basic` and `default` REST graphs include `is_active` alongside `push_enabled`. Neither graph exposes the device token.

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

**Config resolution:** `get_for_user(user)` selects an active `user.org` config first, then falls back to an active `group=None` system default. Device tests resolve the target device owner's config, regardless of the operator's organization.

**Safe metadata:** The `basic`, `default`, and `full` REST graphs expose `fcm_project_id`, `has_fcm_credentials`, and `fcm_client_email`. Project ID and email are bounded to 200 and 254 characters respectively. `has_fcm_credentials` reports stored credential presence, including malformed credentials; it does not prove validity. The service account JSON and `mojo_secrets` remain private.

**Credential verification:** `test_fcm_connection()` authenticates with FCM and submits a topic message with `validate_only=True`, even when `test_mode=True`. No device token is needed and no notification is delivered. Success requires a valid provider response and returns `success=True`, `outcome="validated"`, `validation_only=True`, `test_mode`, `message`, `message_id`, `error_code=None`, and `fcm_version="v1"`. An invalid-token rejection never counts as successful validation.

Passing a nonempty `test_token` retains the legacy real-send behavior: `validation_only=False`, success means `outcome="accepted"`, and test mode blocks the send. It does not create a `NotificationDelivery`; use `test_registered_device()` for tracked device tests. Both modes return safe `error_code`/`message` values on failure, without raw provider errors. `client_factory` accepts an FCM client factory for provider-isolated tests.

**Test mode:** Ordinary `RegisteredDevice.send()` calls simulate sends through `logit` without FCM traffic. Their delivery records retain `status="sent"` for compatibility, but `push_outcome="simulated"` identifies them. A real device test blocks in this mode; a credential verification still contacts FCM.

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
delivery.push_outcome   # Derived evidence, exposed in basic/default graphs
```

New real sends store bounded provider evidence in `platform_data`: `fcm_version`, `message_id`, `success`, `outcome`, `error_code`, `config_id`, and `status_code`. Tokens, credentials, and raw provider error bodies are excluded.

| `push_outcome` | Meaning / stored status |
|---|---|
| `accepted` | FCM accepted the request; `status="sent"`. Device receipt still needs confirmation. |
| `simulated` | No provider send; legacy `status="sent"`. |
| `rejected` / `blocked` | Provider rejection or inability to submit; `status="failed"`. |
| `unknown` | Acceptance could not be determined; `status="pending"`. Check the device before sending again; do not treat this as failed or automatically retry. |
| `delivered` | A real delivery was explicitly marked delivered with `delivered_at`. FCM acceptance alone does not do this. |

Older records without explicit evidence fall back to their stored `status`. A simulation remains `simulated` even if its status changes.

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

### Operator device tests

```python
from mojo.apps.account.services.push import device_test_readiness, test_registered_device

readiness = device_test_readiness(device, PushConfig.get_for_user(device.user))
result = test_registered_device(device, "Push Test", "Please confirm receipt")
```

`device_test_readiness()` only checks local eligibility: active registration, push enabled, `test` category allowed, token present, an active config, live mode, and usable stored credential JSON. It does not authenticate with FCM. The response includes `ready`, `device_id` (database PK), `error_code`, `message`, and a safe `config` projection (`id`, `name`, `group`, `fcm_project_id`, `has_fcm_credentials`, `test_mode`, `is_active`).

`test_registered_device()` resolves the config once and sends to that one registration through `device.send(config=config, require_live=True)`, using category `test`. It returns the readiness fields plus `success`, `outcome`, `delivery_id`, and provider `message_id` when attempted. Local blockers return `outcome="blocked"` and `delivery_id=None`; no simulated send is allowed. Service functions assume their caller has authorized access; the REST handler applies the permission checks below.

---

## Permissions

| Permission | Purpose |
|---|---|
| `send_notifications` | Global grant for sending and testing a selected device |
| `manage_push_config` | Manage `PushConfig` records and test FCM connections |
| `manage_devices` | Manage other users' `RegisteredDevice` records |
| `view_devices` | View device records |
| `manage_notifications` | Manage templates and view all delivery history |
| `view_notifications` | View delivery history |
| `owner` | Each user can manage their own devices and view their own delivery history |
| `comms` | Alternative global grant for send/config-test/device-test actions; also accepted by push model permissions |

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
| `GET/POST` | `/api/account/devices/push/test?device_id=<pk>` | global send grant + device visibility | Readiness / real test to one registered device |
| `GET` | `/api/account/devices/push/stats` | required | Delivery stats for user |
| `POST` | `/api/account/devices/push/config/<pk>/test` | `manage_push_config` | Test FCM credentials |

`push/send` and `push/config/<pk>/test` are gated with `@md.requires_global_perms`
— the permission must be a global grant on the User (no group/member fallback).
The CRUD rows above (`push`, `push/templates`, `push/config`, `push/deliveries`)
are standard RestMeta endpoints and follow normal model permission rules.

### Test action contracts

- **Selected device:** `GET /api/account/devices/push/test?device_id=42` reads readiness; `POST` accepts `{"device_id":42,"title":"Push Test","message":"Please confirm receipt"}`. `device_id` is a positive database PK (an integer or digit string), distinct from the app's registration identifier. Both methods require global `send_notifications` or `comms`, plus the device's normal `VIEW_PERMS` (owner, `view_devices`, `manage_devices`, `manage_users`, or `comms`). Invisible and missing devices both return 404. The title defaults to `Push Test` (1–200 characters); message defaults to `This is a test notification` (1–1000); neither may be blank. Handled outcomes return HTTP 200 with `{"status":true,"data":result}` even for `data.success=false`; inspect `outcome` and `error_code`.
- **Caller devices:** `POST /api/account/devices/push/test` without a non-null `device_id` accepts `{"message":"Please confirm receipt"}` and requires a normal authenticated User session; API keys and key-backed sessions are denied. It sends through the ordinary path to the caller's eligible devices. `data` includes `success`, `message`, `sent_count`, `failed_count`, `simulated_count`, `unknown_count`, and basic delivery `results`. `sent_count` counts provider acceptance; success requires at least one result and all results accepted. No eligible device/config returns 400; GET without a selector also returns 400.
- **Saved configuration:** `POST /api/account/devices/push/config/<pk>/test` with `{}` requires global `manage_push_config` or `comms` and calls `test_fcm_connection()` on that saved row, including inactive rows. A nonempty `device_token` (string, max 4096 characters) invokes the legacy real send. Success uses the normal success envelope; failure returns HTTP 400 with `{"status":false,"error":result.message,"data":result}` so safe diagnostic fields are retained. Missing configs return 404.

The legacy `/send` counts and `/stats` totals still use stored delivery status, which includes simulations as `sent`. Use `push_outcome` when presenting delivery evidence, and use the test endpoint's separate counts for test results.

---

## See Also

- [Push Notifications — FCM Setup Guide](push_setup.md) — Firebase project, service account, iOS/Android/Web client SDK
- [Push Notifications — REST API Reference](../../web_developer/account/push.md) — endpoints, request/response examples, mobile examples
