# Push Notifications Setup Guide

This guide covers setting up push notifications in Django-MOJO applications.

## Overview

Django-MOJO uses **Firebase Cloud Messaging (FCM)** for all push notifications. FCM supports iOS, Android, and Web platforms through a single unified API, making setup and maintenance simple.

## Dependencies

Install the FCM library:

```bash
pip install pyfcm==1.5.4
```

## Database Migration

After installing Django-MOJO with push notification support, create and run migrations to add the push notification tables.

This adds the following models:
- `RegisteredDevice` - Devices registered for push notifications
- `PushConfig` - FCM configuration (system/org level)
- `NotificationTemplate` - Reusable notification templates (optional)
- `NotificationDelivery` - Delivery tracking and history

## Configuration

### 1. Get FCM Server Key

1. Go to [Firebase Console](https://console.firebase.google.com/)
2. Select your project (or create one)
3. Go to Project Settings > Cloud Messaging
4. Copy your **Server Key** (or create a new one)
5. Note your **Sender ID**

### 2. System-Wide Push Config

Create a system-wide configuration:

```python
from mojo.apps.account.models import PushConfig

# Create system default config
config = PushConfig.objects.create(
    group=None,  # System-wide
    name="Default FCM Config",
    fcm_sender_id="YOUR_SENDER_ID",
    test_mode=False  # Set to True for development
)

# Set the FCM server key (encrypted automatically)
config.set_fcm_server_key("YOUR_FCM_SERVER_KEY")
config.save()
```

### 3. Organization-Specific Config (Optional)

For multi-tenant setups, create org-specific configs:

```python
from mojo.apps.account.models import Group, PushConfig

org = Group.objects.get(name="Acme Corp")

config = PushConfig.objects.create(
    group=org,  # Organization-specific
    name="Acme Corp FCM Config",
    fcm_sender_id="ORG_SENDER_ID",
    test_mode=False
)

config.set_fcm_server_key("ORG_FCM_SERVER_KEY")
config.save()
```

### 4. Test Mode

Enable test mode for development (notifications are faked):

```python
config = PushConfig.objects.create(
    name="Development Config",
    test_mode=True,  # No real notifications sent
    fcm_sender_id="dev_sender_id"
)
```

### 5. User Organization Assignment

Assign users to organizations for automatic config resolution:

```python
from mojo.apps.account.models import User, Group

user = User.objects.get(username="john@example.com")
org = Group.objects.get(name="Acme Corp")

user.org = org
user.save()
```

Config resolution priority: **User's org config → System default config**

## Notification Templates (Optional)

Create reusable templates for consistent messaging:

```python
from mojo.apps.account.models import NotificationTemplate

# System template (available to all)
template = NotificationTemplate.objects.create(
    group=None,  # System template
    name="order_ready",
    title_template="Order Ready!",
    body_template="Hi {customer_name}, order #{order_id} is ready at {location}.",
    action_url="myapp://orders/{order_id}",
    category="orders",
    priority="normal",
    variables={
        "customer_name": "Customer's display name",
        "order_id": "Order number", 
        "location": "Pickup location"
    }
)

# Organization-specific template
org_template = NotificationTemplate.objects.create(
    group=org,
    name="welcome",
    title_template="Welcome to {org_name}!",
    body_template="Hi {name}, welcome to our platform.",
    category="onboarding"
)
```

## Permissions

Add push notification permissions to your users/groups:

```python
# For users who can send notifications
user.add_permission("send_notifications")

# For admins who can manage push configs
user.add_permission("manage_push_config") 

# For viewing notification history
user.add_permission("view_notifications")
```

## Device Registration

### Frontend Integration

#### iOS (Swift) - FCM
```swift
import FirebaseMessaging

// Configure Firebase
FirebaseApp.configure()

// Request authorization
UNUserNotificationCenter.current().requestAuthorization(options: [.alert, .sound, .badge]) { granted, error in
    if granted {
        DispatchQueue.main.async {
            UIApplication.shared.registerForRemoteNotifications()
        }
    }
}

// Get FCM token
Messaging.messaging().token { token, error in
    if let token = token {
        registerDevice(token: token, platform: "ios")
    }
}
```

#### Android (Kotlin) - FCM
```kotlin
import com.google.firebase.messaging.FirebaseMessaging

FirebaseMessaging.getInstance().token.addOnCompleteListener { task ->
    if (task.isSuccessful) {
        val token = task.result
        registerDevice(token, "android")
    }
}
```

#### Web (JavaScript) - FCM
```javascript
import { getMessaging, getToken } from "firebase/messaging";

const messaging = getMessaging();
getToken(messaging, { vapidKey: 'YOUR_VAPID_KEY' }).then((token) => {
    registerDevice(token, "web");
});
```

### REST API Registration

```javascript
// Register device via REST API
fetch('/api/account/devices/push/register', {
    method: 'POST',
    headers: {
        'Authorization': 'Bearer ' + userToken,
        'Content-Type': 'application/json'
    },
    body: JSON.stringify({
        device_token: fcmToken,
        device_id: uniqueDeviceId,
        platform: 'ios', // or 'android', 'web'
        device_name: 'John\'s iPhone',
        app_version: '1.2.3',
        os_version: '17.0',
        push_preferences: {
            orders: true,
            marketing: false,
            alerts: true
        }
    })
});
```

## Sending Notifications

### Simple Direct Notifications

```python
# Send to a single user (all their devices)
user.push_notification(
    title="Hello!",
    body="Your order is ready",
    category="orders"
)

# Send to a specific device
device.send(
    title="Hello!",
    body="Your order is ready",
    data={"order_id": 123}
)
```

### Using Helper Functions

```python
from mojo.apps.account.services.push import send_to_user, send_to_users

# Send to one user
send_to_user(
    user,
    title="Order Ready",
    body="Your order #12345 is ready",
    category="orders",
    action_url="myapp://orders/12345"
)

# Send to multiple users
send_to_users(
    user_ids=[1, 2, 3],
    title="System Alert",
    body="Maintenance in 10 minutes",
    category="system"
)

# Silent notification with data only
send_to_user(
    user,
    data={"action": "sync", "timestamp": 1234567890}
)
```

### Via REST API

```bash
# Direct notification
curl -X POST /api/account/devices/push/send \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "title": "Hello!",
    "body": "Your order is ready",
    "category": "orders",
    "action_url": "myapp://orders/12345"
  }'
```

## Testing

### Test Configuration
```bash
curl -X POST /api/account/devices/push/test \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"message": "Test notification"}'
```

### View Statistics
```bash
curl -X GET /api/account/devices/push/stats \
  -H "Authorization: Bearer YOUR_TOKEN"
```

## Architecture (KISS)

The push notification system follows a simple architecture:

```
User.push_notification(title, body, data)
  └─> Loops through user.registered_devices (active, push_enabled)
      └─> device.send(title, body, data)
          ├─> Gets PushConfig for user
          ├─> Creates NotificationDelivery record
          ├─> Sends via FCM (or test mode)
          └─> Returns delivery object
```

**Key Points:**
- `RegisteredDevice.send()` does all the work
- `User.push_notification()` just loops through devices
- Helper functions in `services/push.py` for convenience
- Everything uses FCM - simple and unified

## Security

1. **Credentials**: FCM server keys are automatically encrypted using MojoSecrets
2. **Permissions**: All endpoints require appropriate permissions
3. **Device Ownership**: Users can only manage their own devices
4. **Audit Trail**: All notifications are logged in NotificationDelivery

## Troubleshooting

### Common Issues

1. **"No push config available"**: 
   - Ensure a PushConfig exists (system or org-level)
   - Check that `is_active=True`

2. **"pyfcm not installed"**: 
   - Run `pip install pyfcm==1.5.4`

3. **"No FCM server key configured"**: 
   - Set the key: `config.set_fcm_server_key("YOUR_KEY")`

4. **"No devices found"**: 
   - Ensure devices are registered
   - Check `is_active=True` and `push_enabled=True`

5. **Notifications not received on device**:
   - Check FCM console for errors
   - Verify device token is valid
   - Check device push preferences for the category

### Debug Logging

The push system logs to standard mojo logs:

```python
from mojo.helpers import logit

# Info messages go to mojo.log
logit.info("Push notification sent")

# Errors go to error.log
logit.error("FCM send failed")
```

Check logs at:
- `logs/mojo.log` - General push activity
- `logs/error.log` - Push errors and failures

## Migration from APNS

If you're migrating from APNS to FCM-only:

1. **Update Firebase Console**: Add iOS app to your Firebase project
2. **Update iOS App**: Integrate Firebase SDK instead of raw APNS
3. **Remove APNS Config**: Old APNS fields will be ignored
4. **Update Device Tokens**: Re-register devices with FCM tokens
5. **Test**: Use test mode to verify everything works

FCM handles iOS notifications just as well as APNS, with the added benefit of unified code across platforms.
