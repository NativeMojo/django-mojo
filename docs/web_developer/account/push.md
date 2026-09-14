# Push Notifications — REST API Reference

Push notifications let your app send alerts to users' iOS, Android, and Web devices. The server uses Firebase Cloud Messaging (FCM) for all platforms — iOS is supported via FCM's APNs bridge, no separate APNs integration needed.

---

## Flow Overview

```
1. Client obtains FCM device token from platform SDK
2. POST /api/account/devices/push/register  →  register device with server
3. Server sends push via FCM when triggered
4. GET  /api/account/devices/push/deliveries  →  view delivery history
```

---

## Device Registration

### Register Device

**POST** `/api/account/devices/push/register`

Registers or updates a device for push notifications. Safe to call on every app launch — uses `device_id` as the stable identifier (upsert).

**Request:**

```json
{
  "device_token": "fcm-registration-token-from-sdk",
  "device_id": "your-stable-device-identifier",
  "platform": "ios",
  "device_name": "Alice's iPhone",
  "app_version": "2.1.0",
  "os_version": "17.4",
  "push_preferences": {
    "orders": true,
    "marketing": false,
    "system": true
  }
}
```

| Field | Required | Description |
|---|---|---|
| `device_token` | Yes | FCM registration token from the platform SDK |
| `device_id` | Yes | Your app's stable device identifier (used for upsert) |
| `platform` | Yes | `ios`, `android`, or `web` |
| `device_name` | No | Human-readable device label |
| `app_version` | No | App version string |
| `os_version` | No | OS version string |
| `push_preferences` | No | Per-category opt-in/out (default: all enabled) |

**Response:**

```json
{
  "status": true,
  "data": {
    "id": 42,
    "device_id": "your-stable-device-identifier",
    "platform": "ios",
    "device_name": "Alice's iPhone",
    "app_version": "2.1.0",
    "os_version": "17.4",
    "push_enabled": true,
    "is_active": true,
    "push_preferences": {"orders": true, "marketing": false, "system": true},
    "last_seen": "2026-03-17T10:30:00Z"
  }
}
```

---

### Unregister Device

**POST** `/api/account/devices/push/unregister`

Disables push for a device (e.g. on logout). The device record is kept but marked inactive.

**Request:**

```json
{
  "device_token": "fcm-registration-token",
  "device_id": "your-stable-device-identifier",
  "platform": "ios"
}
```

**Response:**

```json
{"status": true}
```

---

## Device Management

**GET** `/api/account/devices/push` — list registered devices

**GET** `/api/account/devices/push/<id>` — device detail

**PATCH** `/api/account/devices/push/<id>` — update device (e.g. update token or preferences)

**DELETE** `/api/account/devices/push/<id>` — remove device

The `basic` and `default` device graphs include `is_active` and `push_enabled` without exposing the registration token. Use the numeric `id` for selected-device tests; the string `device_id` is the app's stable registration identifier.

### Update Push Preferences

```json
PATCH /api/account/devices/push/42
{
  "push_preferences": {
    "orders": true,
    "marketing": false
  }
}
```

### Disable All Push for a Device

```json
PATCH /api/account/devices/push/42
{
  "push_enabled": false
}
```

---

## Sending Notifications

> Requires `send_notifications` or `comms`, held as a **global** grant on
> the User — this endpoint is gated with `@md.requires_global_perms`, so a
> group/member-scoped grant does not authorize it.

**POST** `/api/account/devices/push/send`

### Direct Notification

```json
{
  "title": "Your order is ready",
  "body": "Order #123 is waiting for pickup",
  "category": "orders",
  "action_url": "myapp://orders/123",
  "data": {"order_id": 123}
}
```

Sends to all active devices for the authenticated user.

### Send to Specific Users

```json
{
  "title": "Maintenance scheduled",
  "body": "System maintenance in 5 minutes",
  "category": "system",
  "user_ids": [1, 2, 3]
}
```

### Silent Notification (Data-Only)

Omit `title` and `body` — the device receives the payload in the background without showing a visible notification.

```json
{
  "data": {"action": "sync", "timestamp": 1742212800},
  "category": "system"
}
```

**Response:**

```json
{
  "status": true,
  "data": {
    "success": true,
    "sent_count": 2,
    "failed_count": 0,
    "deliveries": [
      {"id": 101, "title": "Your order is ready", "category": "orders", "status": "sent", "push_outcome": "accepted", "sent_at": "2026-03-17T10:31:00Z", "created": "2026-03-17T10:31:00Z"},
      {"id": 102, "title": "Your order is ready", "category": "orders", "status": "sent", "push_outcome": "accepted", "sent_at": "2026-03-17T10:31:00Z", "created": "2026-03-17T10:31:00Z"}
    ]
  }
}
```

This legacy `/send` response counts stored `sent`/`failed` statuses; simulations are included in `sent_count`. Inspect each delivery's `push_outcome` for evidence of provider acceptance.

---

## Test Endpoint

### Test One Registered Device

**GET** `/api/account/devices/push/test?device_id=42` — check local readiness

**POST** `/api/account/devices/push/test`

Both methods require global `send_notifications` or `comms` **and** permission to view the selected device: ownership or the normal `view_devices`, `manage_devices`, `manage_users`, or `comms` grant. A send grant alone does not grant device visibility. Missing and inaccessible devices both return 404; group/member grants cannot satisfy the global send gate. API keys and key-backed sessions are denied.

| Field | Required | Description |
|---|---|---|
| `device_id` | Yes for GET or selected-device POST | Positive registered-device database `id`, as an integer or digit string. This is not the app-provided string `device_id` used during registration. |
| `title` | No; POST only | Nonblank string, max 200 characters. Default: `Push Test`. |
| `message` | No; POST only | Nonblank string, max 1000 characters. Default: `This is a test notification`. |

**POST request:**

```json
{
  "device_id": 42,
  "title": "Push Test",
  "message": "Please confirm receipt on this device"
}
```

GET only checks local eligibility; it does not authenticate with FCM or prove device reachability. POST resolves the device owner's active org config, falling back to the active system config, then attempts one real send to that registration using category `test`. It never broadcasts or simulates a successful test.

**GET response:**

```json
{
  "status": true,
  "data": {
    "ready": true,
    "error_code": null,
    "message": "Ready to attempt a real push. Device receipt is not yet verified.",
    "device_id": 42,
    "config": {
      "id": 7,
      "name": "Acme FCM",
      "group": {"id": 3, "name": "Acme"},
      "fcm_project_id": "acme-production",
      "has_fcm_credentials": true,
      "test_mode": false,
      "is_active": true
    }
  }
}
```

`config` is null when none applies; `config.group` is null for the system default. No device token, service account JSON, or raw provider response is exposed.

POST returns the same context plus `success`, `outcome`, `delivery_id`, and (after an attempted send) `message_id`. A successful attempt has `success=true`, `outcome="accepted"`, a delivery ID, and the message `FCM accepted the notification. Confirm receipt on the device.` Local blockers have `success=false`, `outcome="blocked"`, and `delivery_id=null`.

| Outcome | Meaning / UI behavior |
|---|---|
| `accepted` | FCM accepted the notification. Ask the operator to confirm receipt on the device. |
| `blocked` | Local eligibility, credentials, or authentication prevented sending. Show `error_code`/`message`. |
| `rejected` | FCM rejected the request. Show the safe diagnostic. |
| `unknown` | Acceptance is uncertain, such as a timeout after submission. Check the device before sending again; do not label this failed or retry automatically. |

Handled results use HTTP 200 and outer `status=true`, including blocked/rejected/unknown results with `data.success=false`. Inspect `data.outcome`; HTTP success does not prove notification acceptance. `ready=true` is only the local preflight result and can accompany provider failure. Refresh readiness before another attempt.

Readiness blockers are `inactive_device`, `push_disabled`, `category_disabled` (the device disabled category `test`), `missing_token`, `no_config`, `test_mode`, and `missing_credentials`. Provider diagnostics include `invalid_credentials`, `authentication_failed`, `provider_unavailable`, `acceptance_unknown`, `invalid_provider_response`, and safe FCM codes such as `UNREGISTERED`, `SENDER_ID_MISMATCH`, `INVALID_ARGUMENT`, `PERMISSION_DENIED`, and `THIRD_PARTY_AUTH_ERROR`.

### Test the Caller's Devices

**POST** `/api/account/devices/push/test` without a non-null `device_id`

Requires a normal authenticated User session; API keys and key-backed sessions are denied. Sends through the ordinary push path to the caller's eligible devices, honoring category `test` preferences. The optional `message` has the same default and 1000-character limit as above; title is always `Push Test`.

```json
{"message": "Custom test message"}
```

**Response data (example with two simulated sends):**

```json
{
  "success": false,
  "message": "0 accepted by FCM, 0 failed, 2 simulated, 0 unknown. Confirm receipt on the devices.",
  "sent_count": 0,
  "failed_count": 0,
  "simulated_count": 2,
  "unknown_count": 0,
  "results": [
    {"id": 101, "title": "Push Test", "category": "test", "status": "sent", "push_outcome": "simulated", "sent_at": "2026-03-17T10:31:00Z", "created": "2026-03-17T10:31:00Z"},
    {"id": 102, "title": "Push Test", "category": "test", "status": "sent", "push_outcome": "simulated", "sent_at": "2026-03-17T10:31:00Z", "created": "2026-03-17T10:31:00Z"}
  ]
}
```

The outer envelope is `{"status":true,"data":...}`. `sent_count` counts FCM acceptance; simulated and uncertain attempts have separate counts. `data.success` is true only when at least one delivery exists and every result was accepted. No eligible devices/config returns 400. GET without `device_id` returns 400.

---

## Delivery History

**GET** `/api/account/devices/push/deliveries`

Returns delivery records for the authenticated user, across all statuses
(`pending`, `sent`, `delivered`, `failed`). Filter with `?status=failed`, etc.

```json
{
  "status": true,
  "count": 15,
  "data": [
    {
      "id": 101,
      "title": "Your order is ready",
      "body": "Order #123 is waiting",
      "category": "orders",
      "action_url": "myapp://orders/123",
      "data_payload": {"order_id": 123},
      "status": "sent",
      "push_outcome": "accepted",
      "sent_at": "2026-03-17T10:31:00Z",
      "delivered_at": null,
      "error_message": null,
      "created": "2026-03-17T10:31:00Z",
      "user": {"id": 5, "username": "alice"},
      "device": {"id": 42, "platform": "ios", "device_name": "Alice's iPhone"}
    }
  ]
}
```

`push_outcome` is included in the `basic` and `default` graphs. It distinguishes `accepted`, `simulated`, `rejected`, `blocked`, and `unknown`; `delivered` requires an explicit delivered status and timestamp. Older records without provider evidence fall back to their stored status. A simulated record retains `status="sent"`, while unknown acceptance remains `status="pending"`. Never treat `sent` alone as proof of FCM acceptance or device receipt.

**GET** `/api/account/devices/push/deliveries/<id>?graph=full` — detail including `platform_data`. New real sends store bounded evidence (`fcm_version`, `message_id`, `success`, `outcome`, `error_code`, `config_id`, `status_code`), without tokens, credentials, or raw provider error bodies.

---

## Statistics

**GET** `/api/account/devices/push/stats`

```json
{
  "status": true,
  "data": {
    "total_sent": 142,
    "total_failed": 3,
    "total_pending": 0,
    "registered_devices": 2,
    "enabled_devices": 2
  }
}
```

These totals use stored statuses, so `total_sent` includes simulations. For test outcomes use the test endpoint's separate counts and delivery `push_outcome`.

---

## Push Preferences

Each device has a `push_preferences` JSON object with per-category opt-in/out. The server checks preferences before every send — if a category is set to `false`, the notification is silently skipped for that device (no delivery record created).

```json
{
  "orders": true,
  "marketing": false,
  "system": true,
  "general": true
}
```

Any category not present in `push_preferences` defaults to **enabled**. Setting a category to `false` opts the device out of that category only.

---

## Notification Templates (Admin)

> Requires `manage_notifications` permission.

Templates support Python `str.format()` variable substitution.

**POST** `/api/account/devices/push/templates`

```json
{
  "name": "order_ready",
  "title_template": "Order #{order_number} is ready",
  "body_template": "Hi {customer_name}, your order is ready for pickup.",
  "action_url": "myapp://orders/{order_number}",
  "category": "orders",
  "priority": "high",
  "variables": {
    "order_number": "Order ID",
    "customer_name": "Customer display name"
  }
}
```

**GET** `/api/account/devices/push/templates` — list templates

**GET/PATCH/DELETE** `/api/account/devices/push/templates/<id>` — manage template

---

## Push Configuration (Admin)

> Config CRUD follows normal model permissions (`manage_push_config`, `manage_groups`, or `comms`). The test action requires global `manage_push_config` or `comms`.

**GET** `/api/account/devices/push/config` — list configs

The `basic`, `default`, and `full` graphs expose `fcm_project_id`, `has_fcm_credentials`, and `fcm_client_email`, alongside config name, active state, and test mode. `default`/`full` include the group; null means system default. Credential presence does not prove credential validity. Service account JSON and encrypted secrets are excluded.

**POST** `/api/account/devices/push/config/<id>/test`

Submit `{}` to authenticate with FCM and validate project send permission using `validate_only=true`. This checks the selected saved row, including inactive configs, and still contacts FCM when `test_mode=true`. It does not activate the config, require a device, or deliver a notification. Group/member grants, API keys, and key-backed sessions cannot authorize this action.

```json
{}
```

**Response (HTTP 200):**

```json
{
  "status": true,
  "data": {
    "success": true,
    "outcome": "validated",
    "message": "FCM authenticated and validated this project. No notification was delivered.",
    "error_code": null,
    "message_id": "projects/acme-production/messages/validation-reference",
    "validation_only": true,
    "test_mode": true,
    "fcm_version": "v1"
  }
}
```

**Failure (HTTP 400):**

```json
{
  "status": false,
  "error": "No usable FCM service account is stored.",
  "data": {
    "success": false,
    "outcome": "blocked",
    "message": "No usable FCM service account is stored.",
    "error_code": "missing_credentials",
    "message_id": null,
    "validation_only": true,
    "test_mode": true,
    "fcm_version": "v1"
  }
}
```

Inspect `data.error_code` and show `data.message`. Missing/invalid credentials, authentication failure, provider rejection, and malformed responses cannot produce a verified result; an invalid device-token rejection is never credential validation success.

For legacy callers, a nonempty `device_token` string (max 4096 characters) requests a real send instead of validation. It requires `test_mode=false`, returns `validation_only=false`, and only reports success for `outcome="accepted"`. It does not create a delivery-history record or prove receipt. Prefer the selected-device test above for tracked sends. With `outcome="unknown"`, check the device before sending again even though this endpoint returned HTTP 400.

---

## Mobile Client Examples

### iOS (Swift)

```swift
import FirebaseMessaging

// Get FCM token
Messaging.messaging().token { token, error in
    guard let token = token else { return }

    // Register with server
    let body: [String: Any] = [
        "device_token": token,
        "device_id": UIDevice.current.identifierForVendor!.uuidString,
        "platform": "ios",
        "device_name": UIDevice.current.name,
        "app_version": Bundle.main.infoDictionary?["CFBundleShortVersionString"] as? String ?? "",
        "os_version": UIDevice.current.systemVersion
    ]

    // POST to /api/account/devices/push/register with Authorization header
}
```

### Android (Kotlin)

```kotlin
import com.google.firebase.messaging.FirebaseMessaging

FirebaseMessaging.getInstance().token.addOnCompleteListener { task ->
    val token = task.result

    val body = mapOf(
        "device_token" to token,
        "device_id" to Settings.Secure.getString(contentResolver, Settings.Secure.ANDROID_ID),
        "platform" to "android",
        "device_name" to "${Build.MANUFACTURER} ${Build.MODEL}",
        "app_version" to packageManager.getPackageInfo(packageName, 0).versionName,
        "os_version" to Build.VERSION.RELEASE
    )

    // POST to /api/account/devices/push/register with Authorization header
}
```

### Web (JavaScript)

```javascript
import { getMessaging, getToken } from "firebase/messaging";

const messaging = getMessaging();
const token = await getToken(messaging, { vapidKey: "your-vapid-key" });

await fetch('/api/account/devices/push/register', {
  method: 'POST',
  headers: {
    'Content-Type': 'application/json',
    'Authorization': `Bearer ${jwtToken}`,
  },
  body: JSON.stringify({
    device_token: token,
    device_id: crypto.randomUUID(),  // store in localStorage for stability
    platform: 'web',
    device_name: navigator.userAgent,
  }),
});
```

---

## Error Responses

| Status | Cause |
|---|---|
| `400` | Invalid input; missing readiness selector; no eligible caller devices/config; failed config verification (safe details in `data`) |
| `401` | Not authenticated |
| `403` | Missing required global permission or a disallowed machine/key-backed session |
| `404` | Device absent/inaccessible or config not found |

---

## See Also

- [Push Notifications — Django Developer Reference](../../django_developer/account/push.md) — models, service layer, permissions
- [Push Notifications — FCM Setup Guide](../../django_developer/account/push_setup.md) — Firebase project setup, service account, iOS/Android/Web client SDK
