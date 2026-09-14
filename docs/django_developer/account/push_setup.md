# Push Notifications — FCM Setup Guide

This guide covers setting up Firebase Cloud Messaging (FCM) for push notifications in a django-mojo project. FCM handles all platforms — iOS (via Apple's APNs bridge), Android, and Web — from a single integration.

---

## Overview

Push notifications use FCM v1 API with a service account JSON credential. There is no APNs direct integration — FCM routes iOS notifications through Apple's servers automatically.

**No `settings.py` credentials are needed.** All FCM configuration is stored in the `PushConfig` model, encrypted via `MojoSecrets`. This allows per-organization FCM projects without a deployment.

---

## Step 1 — Create a Firebase Project

1. Go to [console.firebase.google.com](https://console.firebase.google.com)
2. Click **Add project**
3. Give it a name (e.g. `my-app-production`)
4. Disable Google Analytics if not needed → **Create project**

---

## Step 2 — Register Your Apps

### Android

1. In the Firebase console, click **Add app** → Android
2. Enter your Android package name (e.g. `com.example.myapp`)
3. Download `google-services.json` → add to your Android project at `app/google-services.json`
4. Follow the SDK setup steps in the console

### iOS

1. Click **Add app** → Apple (iOS+)
2. Enter your iOS bundle ID (e.g. `com.example.myapp`)
3. Download `GoogleService-Info.plist` → add to your Xcode project
4. Follow the SDK setup steps in the console

> **APNs bridge:** FCM handles the iOS APNs connection automatically. You do not need to configure APNs certificates or keys separately — just upload your APNs key in Firebase:
> Project settings → Cloud Messaging → Apple app configuration → **Upload APNs Authentication Key** (`.p8` file from Apple Developer portal)

### Web

1. Click **Add app** → Web
2. Register your app and copy the Firebase config object
3. Use the Firebase JS SDK to get the FCM registration token (see [Web Client Setup](#web-client-setup) below)

---

## Step 3 — Get a Service Account JSON

The FCM v1 API uses a Google service account instead of a legacy server key.

1. In Firebase console → **Project settings** → **Service accounts** tab
2. Click **Generate new private key**
3. Download the JSON file — it looks like:

```json
{
  "type": "service_account",
  "project_id": "my-app-production",
  "private_key_id": "abc123...",
  "private_key": "-----BEGIN RSA PRIVATE KEY-----\n...\n-----END RSA PRIVATE KEY-----\n",
  "client_email": "firebase-adminsdk-xxxxx@my-app-production.iam.gserviceaccount.com",
  "client_id": "123456789",
  "auth_uri": "https://accounts.google.com/o/oauth2/auth",
  "token_uri": "https://oauth2.googleapis.com/token",
  ...
}
```

> **Keep this file secret.** Never commit it to source control. Store it in a secrets manager or environment variable.

---

## Step 4 — Create a PushConfig in Django

Once the project is running, create a `PushConfig` and load the service account credentials.

### System-wide config (all users)

```python
from mojo.apps.account.models import PushConfig
import json

config = PushConfig.objects.create(
    group=None,     # None = system default for all users
    name="Production FCM",
    is_active=True,
    test_mode=False,
)

# Load service account JSON from file or environment variable
with open("/path/to/service-account.json") as f:
    service_account = json.load(f)

config.set_fcm_service_account(service_account)
config.save()
```

### Per-organization config

```python
from mojo.apps.account.models import PushConfig, Group

org = Group.objects.get(name="Acme Corp")

config = PushConfig.objects.create(
    group=org,
    name="Acme FCM",
    is_active=True,
    test_mode=False,
)
config.set_fcm_service_account(service_account)
config.save()
```

When sending to a user, `PushConfig.get_for_user(user)` resolves the config: the user's org config takes priority, falling back to the system default.

---

## Step 5 — Test the Configuration

### Via Django shell

```python
from mojo.apps.account.models import PushConfig

config = PushConfig.objects.get(name="Production FCM")

# Authenticate with FCM and validate project send permission, without delivery
result = config.test_fcm_connection()
print(result)
# {'success': True, 'outcome': 'validated', 'validation_only': True, ...}

# Legacy real-token send — requires test_mode=False; confirm receipt on the device
result = config.test_fcm_connection(test_token="<real-device-token>")
print(result)
# {'success': True, 'outcome': 'accepted', 'validation_only': False, 'message_id': '...', ...}
```

The no-token check uses FCM `validate_only=True` and still contacts FCM when `test_mode=True`. It never delivers a notification. Missing credentials, authentication failures, and provider rejections return `success=False` with a safe `error_code` and `message`; a dummy-token rejection is never proof that the credentials work.

### Via REST API

```
POST /api/account/devices/push/config/1/test
Authorization: Bearer <token>
Content-Type: application/json

{}
```

Requires global `manage_push_config` or `comms`. Success returns HTTP 200 with `{"status":true,"data":result}`; the result includes `success`, `outcome="validated"`, `validation_only=true`, `test_mode`, `fcm_version`, `message_id`, `error_code`, and `message`. Failed checks return HTTP 400 with `{"status":false,"error":"...","data":result}`. This validates the selected saved config, including an inactive one; it does not activate it.

For a tracked device test, use the numeric `id` returned by device registration/listing:

```
GET /api/account/devices/push/test?device_id=42

POST /api/account/devices/push/test
{"device_id":42,"message":"Please confirm receipt"}
```

Both calls require global `send_notifications` or `comms` and permission to view device 42. GET reports local readiness without FCM traffic; POST sends to that one registered token using the device owner's active org config or system fallback. Test mode, inactive/disabled registration, a disabled `test` category, missing token/config/credentials block the real send. Inspect `data.outcome`: `accepted` means FCM accepted the request; `unknown` means acceptance is uncertain and you should check the device before sending again. Neither confirms receipt. See the [REST reference](../../web_developer/account/push.md#test-endpoint) for the full response contract.

The config endpoint still accepts `{"device_token":"<real-token>"}` for legacy callers (max 4096 characters). A nonempty token requests a real send, blocks under test mode, and does not create a delivery-history record.

---

## Step 6 — Enable Test Mode for Development

Set `test_mode=True` on the config to simulate ordinary sends during development. Notifications are logged instead.

```python
config.test_mode = True
config.save()
```

Eligible ordinary `RegisteredDevice.send()` calls return a delivery record with legacy `status="sent"` and `push_outcome="simulated"`, without FCM traffic. The caller-wide test endpoint reports these in `simulated_count`, with `sent_count=0` and `success=false` if every result is simulated. Real device tests block in this mode. No-token configuration verification remains a real FCM call, so mock the provider for offline tests.

---

## Web Client Setup

To register a web browser for push notifications:

```javascript
import { initializeApp } from "firebase/app";
import { getMessaging, getToken } from "firebase/messaging";

const app = initializeApp({
  apiKey: "...",
  authDomain: "...",
  projectId: "my-app-production",
  messagingSenderId: "...",
  appId: "..."
});

const messaging = getMessaging(app);

// Get FCM token (requires notification permission)
const token = await getToken(messaging, {
  vapidKey: "<your-web-push-vapid-key>"   // Firebase console → Project settings → Web Push certificates
});

// Register with the server
await fetch('/api/account/devices/push/register', {
  method: 'POST',
  headers: {
    'Content-Type': 'application/json',
    'Authorization': `Bearer ${jwtToken}`,
  },
  body: JSON.stringify({
    device_token: token,
    device_id: navigator.userAgent,   // or your own stable device ID
    platform: 'web',
    device_name: 'Chrome on Desktop',
  }),
});
```

---

## See Also

- [Push Notifications — Django Developer Reference](push.md) — models, service layer, permissions
- [Push Notifications — REST API Reference](../../web_developer/account/push.md) — all endpoints, mobile examples
