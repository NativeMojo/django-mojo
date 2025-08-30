# Push Notifications REST API

This guide covers the REST API endpoints for push notifications in Django-MOJO applications. Push notifications allow you to send real-time alerts to registered mobile and web devices.

## Overview

The push notification system supports:
- **Device Registration**: Register iOS, Android, and web devices for push notifications
- **Template Management**: Create reusable notification templates with variables
- **Direct Sending**: Send notifications with custom content
- **Configuration Management**: Manage push service credentials and settings
- **Delivery Tracking**: Monitor notification delivery status and history
- **Multi-tenant Support**: Organization-level and system-wide configurations

## Authentication

All push notification endpoints require authentication. Include your authentication token in the request headers:

```http
Authorization: Bearer YOUR_AUTH_TOKEN
```

## Device Registration

### Register Device for Push Notifications

Register a device to receive push notifications.

**Endpoint:** `POST /api/account/devices/push/register`

**Permissions Required:** `authenticated user`

**Request Body:**
```json
{
    "device_token": "abc123def456...",
    "device_id": "unique-device-identifier",
    "platform": "ios|android|web",
    "device_name": "John's iPhone",
    "app_version": "1.2.3",
    "os_version": "17.0",
    "push_preferences": {
        "orders": true,
        "marketing": false,
        "alerts": true,
        "general": true
    }
}
```

**Required Fields:**
- `device_token`: Push token from the platform (FCM token, APNS token, etc.)
- `device_id`: Unique identifier for the device (from your app)
- `platform`: Device platform (`ios`, `android`, or `web`)

**Optional Fields:**
- `device_name`: Human-readable device name
- `app_version`: Version of your application
- `os_version`: Operating system version
- `push_preferences`: Category-based notification preferences (defaults to all enabled)

**Response:**
```json
{
    "id": 123,
    "device_id": "unique-device-identifier",
    "platform": "ios",
    "device_name": "John's iPhone",
    "app_version": "1.2.3",
    "os_version": "17.0",
    "push_enabled": true,
    "push_preferences": {
        "orders": true,
        "marketing": false,
        "alerts": true,
        "general": true
    },
    "last_seen": "2024-01-15T10:30:00Z",
    "user": {
        "id": 456,
        "username": "john@example.com",
        "display_name": "John Doe"
    }
}
```

**Example cURL:**
```bash
curl -X POST /api/account/devices/push/register \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "device_token": "abc123def456...",
    "device_id": "john-iphone-14",
    "platform": "ios",
    "device_name": "John'\''s iPhone",
    "app_version": "2.1.0"
  }'
```

## Device Management

### List Registered Devices

Get all devices registered by the authenticated user.

**Endpoint:** `GET /api/account/devices/push/`

**Permissions Required:** `view_devices`, `manage_devices`, or `owner`

**Query Parameters:**
- `graph`: Response graph (`basic`, `default`, `full`)
- `platform`: Filter by platform (`ios`, `android`, `web`)
- `push_enabled`: Filter by push status (`true`, `false`)

**Response:**
```json
{
    "results": [
        {
            "id": 123,
            "device_id": "john-iphone-14",
            "platform": "ios",
            "device_name": "John's iPhone",
            "push_enabled": true,
            "last_seen": "2024-01-15T10:30:00Z"
        }
    ]
}
```

### Get Device Details

**Endpoint:** `GET /api/account/devices/push/{device_id}`

### Update Device

**Endpoint:** `PUT /api/account/devices/push/{device_id}`

**Request Body:**
```json
{
    "push_enabled": false,
    "push_preferences": {
        "orders": true,
        "marketing": false
    }
}
```

### Delete Device

**Endpoint:** `DELETE /api/account/devices/push/{device_id}`

## Sending Notifications

### Send Push Notification

Send push notifications using templates or direct content.

**Endpoint:** `POST /api/account/devices/push/send`

**Permissions Required:** `send_notifications`

#### Templated Notifications

Send notifications using predefined templates:

**Request Body:**
```json
{
    "template": "order_ready",
    "context": {
        "customer_name": "John Doe",
        "order_id": "12345",
        "location": "Main Street Store"
    },
    "user_ids": [123, 456, 789]
}
```

**Fields:**
- `template`: Name of the notification template
- `context`: Variables to substitute in the template
- `user_ids`: Optional list of user IDs to send to (defaults to current user)

#### Direct Notifications

Send notifications with custom content:

**Request Body:**
```json
{
    "title": "Order Ready!",
    "body": "Hi John, your order #12345 is ready for pickup at Main Street Store.",
    "category": "orders",
    "action_url": "myapp://orders/12345",
    "user_ids": [123, 456]
}
```

**Fields:**
- `title`: Notification title
- `body`: Notification body text
- `category`: Notification category (for user preferences)
- `action_url`: Optional deep link or URL
- `user_ids`: Optional list of user IDs to send to

**Response:**
```json
{
    "success": true,
    "sent_count": 3,
    "failed_count": 1,
    "deliveries": [
        {
            "id": 789,
            "title": "Order Ready!",
            "category": "orders",
            "status": "sent",
            "sent_at": "2024-01-15T10:30:00Z",
            "created": "2024-01-15T10:29:58Z"
        }
    ]
}
```

**Example cURL:**
```bash
# Templated notification
curl -X POST /api/account/devices/push/send \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "template": "order_ready",
    "context": {
      "customer_name": "John",
      "order_id": "12345",
      "location": "Main St Store"
    }
  }'

# Direct notification
curl -X POST /api/account/devices/push/send \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "title": "Flash Sale!",
    "body": "50% off everything for the next 2 hours",
    "category": "marketing",
    "action_url": "https://example.com/sale"
  }'
```

## Notification Templates

### List Templates

Get available notification templates.

**Endpoint:** `GET /api/account/devices/push/templates/`

**Permissions Required:** `manage_notifications`, `manage_groups`, or `owner`

**Response:**
```json
{
    "results": [
        {
            "id": 1,
            "name": "order_ready",
            "category": "orders",
            "priority": "normal",
            "is_active": true
        }
    ]
}
```

### Create Template

**Endpoint:** `POST /api/account/devices/push/templates/`

**Request Body:**
```json
{
    "name": "welcome_user",
    "title_template": "Welcome {name}!",
    "body_template": "Hi {name}, welcome to {app_name}. Get started by exploring our features!",
    "action_url": "myapp://onboarding",
    "category": "onboarding",
    "priority": "normal",
    "variables": {
        "name": "User's display name",
        "app_name": "Application name"
    }
}
```

### Get Template Details

**Endpoint:** `GET /api/account/devices/push/templates/{template_id}`

**Response:**
```json
{
    "id": 1,
    "name": "order_ready",
    "title_template": "Order Ready!",
    "body_template": "Hi {customer_name}, your order #{order_id} is ready for pickup at {location}.",
    "action_url": "myapp://orders/{order_id}",
    "category": "orders",
    "priority": "normal",
    "variables": {
        "customer_name": "Customer's display name",
        "order_id": "Order number",
        "location": "Pickup location"
    },
    "is_active": true
}
```

## Push Configuration

### Get Push Configuration

View push service configuration.

**Endpoint:** `GET /api/account/devices/push/config/`

**Permissions Required:** `manage_push_config` or `manage_groups`

**Response:**
```json
{
    "results": [
        {
            "id": 1,
            "name": "Default System Config",
            "apns_enabled": true,
            "fcm_enabled": true,
            "default_sound": "default",
            "is_active": true
        }
    ]
}
```

**Note:** Sensitive credentials (APNS keys, FCM server keys) are never exposed in API responses.

### Create/Update Configuration

**Endpoint:** `POST /api/account/devices/push/config/`

**Request Body:**
```json
{
    "name": "My App Push Config",
    "apns_enabled": true,
    "apns_key_id": "ABC123DEF4",
    "apns_team_id": "TEAM123456",
    "apns_bundle_id": "com.myapp.bundle",
    "apns_key_file": "-----BEGIN PRIVATE KEY-----\n...\n-----END PRIVATE KEY-----",
    "apns_use_sandbox": false,
    "fcm_enabled": true,
    "fcm_server_key": "AAAAxxxxxxxx:APA91b...",
    "fcm_sender_id": "123456789012",
    "default_sound": "default"
}
```

## Notification History

### List Delivery History

View notification delivery history and status.

**Endpoint:** `GET /api/account/devices/push/deliveries/`

**Permissions Required:** `view_notifications`, `manage_notifications`, or `owner`

**Query Parameters:**
- `status`: Filter by status (`pending`, `sent`, `delivered`, `failed`)
- `category`: Filter by category
- `start_date`: Filter deliveries after date (ISO format)
- `end_date`: Filter deliveries before date (ISO format)

**Response:**
```json
{
    "results": [
        {
            "id": 789,
            "title": "Order Ready!",
            "body": "Hi John, your order #12345 is ready...",
            "category": "orders",
            "action_url": "myapp://orders/12345",
            "status": "sent",
            "sent_at": "2024-01-15T10:30:00Z",
            "created": "2024-01-15T10:29:58Z",
            "user": {
                "id": 123,
                "username": "john@example.com",
                "display_name": "John Doe"
            },
            "device": {
                "device_id": "john-iphone-14",
                "platform": "ios",
                "device_name": "John's iPhone"
            }
        }
    ]
}
```

### Get Delivery Details

**Endpoint:** `GET /api/account/devices/push/deliveries/{delivery_id}`

## Testing & Statistics

### Test Push Configuration

Send a test notification to verify push configuration.

**Endpoint:** `POST /api/account/devices/push/test`

**Permissions Required:** `manage_push_config`

**Request Body:**
```json
{
    "message": "Custom test message"
}
```

**Response:**
```json
{
    "success": true,
    "message": "Test notifications sent",
    "results": [
        {
            "id": 790,
            "title": "Push Test",
            "category": "test",
            "status": "sent",
            "sent_at": "2024-01-15T10:35:00Z"
        }
    ]
}
```

### Get Push Statistics

View push notification statistics for the authenticated user.

**Endpoint:** `GET /api/account/devices/push/stats`

**Permissions Required:** `view_notifications`

**Response:**
```json
{
    "total_sent": 145,
    "total_failed": 12,
    "total_pending": 3,
    "registered_devices": 4,
    "enabled_devices": 3
}
```

## Graph Usage

All list endpoints support the `graph` parameter to control response detail level:

- `basic`: Minimal fields for list views
- `default`: Standard detail level
- `full`: Complete object details with relationships

**Example:**
```bash
# Get devices with full details
curl -X GET "/api/account/devices/push/?graph=full" \
  -H "Authorization: Bearer YOUR_TOKEN"
```

## Error Handling

### Common Error Responses

**400 Bad Request:**
```json
{
    "error": "Must provide either template or both title and body",
    "code": "invalid_request"
}
```

**403 Forbidden:**
```json
{
    "error": "Permission denied",
    "code": "forbidden"
}
```

**404 Not Found:**
```json
{
    "error": "Template 'unknown_template' not found",
    "code": "not_found"
}
```

### Push-Specific Errors

**No Push Configuration:**
```json
{
    "error": "No push configuration available",
    "code": "no_config"
}
```

**No Devices Found:**
```json
{
    "error": "No registered devices found for notification",
    "code": "no_devices"
}
```

## Best Practices

### Device Registration
- Register devices on app startup and when push tokens change
- Update device information when app version changes
- Set meaningful device names for easier management

### Notification Categories
- Use consistent category names across your app
- Allow users to manage preferences per category
- Common categories: `orders`, `marketing`, `alerts`, `social`, `general`

### Template Usage
- Use templates for frequently sent notifications
- Document template variables clearly
- Test templates thoroughly before use
- Keep templates organization-specific when needed

### Rate Limiting
- Be mindful of notification frequency
- Respect user preferences and local time zones  
- Consider batching notifications when appropriate

### Error Handling
- Check delivery status for important notifications
- Implement retry logic for failed deliveries
- Monitor delivery statistics regularly

## Integration Examples

### iOS Swift
```swift
import UserNotifications

// Request permission
UNUserNotificationCenter.current().requestAuthorization(options: [.alert, .sound, .badge]) { granted, error in
    if granted {
        DispatchQueue.main.async {
            UIApplication.shared.registerForRemoteNotifications()
        }
    }
}

// Register device when token received
func application(_ application: UIApplication, didRegisterForRemoteNotificationsWithDeviceToken deviceToken: Data) {
    let token = deviceToken.map { String(format: "%02.2hhx", $0) }.joined()
    registerDevice(token: token, platform: "ios")
}
```

### Android Kotlin
```kotlin
FirebaseMessaging.getInstance().token.addOnCompleteListener { task ->
    if (!task.isSuccessful) {
        Log.w("FCM", "Fetching FCM registration token failed", task.exception)
        return@addOnCompleteListener
    }

    val token = task.result
    Log.d("FCM", "FCM Registration Token: $token")
    registerDevice(token, "android")
}
```

### JavaScript (Web)
```javascript
// Register service worker and get push subscription
navigator.serviceWorker.register('/sw.js').then(registration => {
    return registration.pushManager.subscribe({
        userVisibleOnly: true,
        applicationServerKey: urlBase64ToUint8Array(vapidPublicKey)
    });
}).then(subscription => {
    registerDevice(subscription.endpoint, "web");
});
```

For more information on authentication, filtering, and general REST API usage, see the [REST API Guide](../rest_api/).