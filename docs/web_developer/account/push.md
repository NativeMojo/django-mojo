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

> Requires the `send_notifications` permission.

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
      {"id": 101, "title": "Your order is ready", "category": "orders", "status": "sent", "sent_at": "2026-03-17T10:31:00Z", "created": "2026-03-17T10:31:00Z"},
      {"id": 102, "title": "Your order is ready", "category": "orders", "status": "sent", "sent_at": "2026-03-17T10:31:00Z", "created": "2026-03-17T10:31:00Z"}
    ]
  }
}
```

---

## Test Endpoint

**POST** `/api/account/devices/push/test`

Sends a test notification to all of the authenticated user's registered devices. Useful for verifying setup.

```json
{
  "message": "Custom test message"
}
```

`message` is optional (defaults to `"This is a test notification"`).

**Response:**

```json
{
  "status": true,
  "data": {
    "success": true,
    "message": "Test notifications sent to 2 devices",
    "results": [...]
  }
}
```

---

## Delivery History

**GET** `/api/account/devices/push/deliveries`

Returns sent delivery records for the authenticated user. Filtered to `status=sent` by default.

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

**GET** `/api/account/devices/push/deliveries/<id>` — full delivery detail including `platform_data` (FCM response).

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

> Requires `manage_push_config` permission.

**GET** `/api/account/devices/push/config` — list configs

**POST** `/api/account/devices/push/config/<id>/test`

Test FCM credentials for a config. Optionally provide a real device token to test end-to-end delivery.

```json
{
  "device_token": "optional-real-fcm-token"
}
```

Response:

```json
{
  "status": true,
  "data": {
    "success": true,
    "message": "FCM v1 credentials valid (dummy token rejected by FCM)",
    "fcm_version": "v1"
  }
}
```

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
| `400` | Missing required fields, or `title`/`body`/`data` all absent on send |
| `401` | Not authenticated |
| `403` | Missing `send_notifications` or `manage_push_config` permission |
| `404` | Device or config not found |

---

## See Also

- [Push Notifications — Django Developer Reference](../../django_developer/account/push.md) — models, service layer, permissions
- [Push Notifications — FCM Setup Guide](../../django_developer/account/push_setup.md) — Firebase project setup, service account, iOS/Android/Web client SDK
