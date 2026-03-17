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

# Test with no token — validates credentials only
result = config.test_fcm_connection()
print(result)
# {'success': True, 'message': 'FCM v1 credentials valid (dummy token rejected by FCM)', ...}

# Test with a real device token — validates end-to-end delivery
result = config.test_fcm_connection(test_token="<real-device-token>")
print(result)
# {'success': True, 'message_id': '...', ...}
```

### Via REST API

```
POST /api/account/devices/push/config/1/test
Authorization: Bearer <token>
{ "device_token": "<optional-real-token>" }
```

---

## Step 6 — Enable Test Mode for Development

Set `test_mode=True` on the config to skip real FCM calls during development. Notifications are logged instead.

```python
config.test_mode = True
config.save()
```

All `RegisteredDevice.send()` calls will succeed (returning a delivery record with `status=sent`) without making any FCM HTTP requests. Useful for local development and CI.

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
- [Push Notifications — REST API Reference](../../../web_developer/account/push.md) — all endpoints, mobile examples
