# Push Notification API Documentation

## Overview

The Django Mojo Push Notification API provides a simple system for managing and sending push notifications to mobile and web clients. The system uses **Firebase Cloud Messaging (FCM)** for all platforms (iOS, Android, Web), with built-in template support, delivery tracking, and organizational configuration.

### Key Features

- **FCM for All Platforms**: Single unified service for iOS, Android, and Web
- **Simple Architecture**: `device.send()` does everything, `user.push_notification()` loops devices
- **Template System**: Optional reusable templates with variable substitution
- **Device Management**: Registration and preference management for user devices  
- **Delivery Tracking**: Complete audit trail of notification attempts and results
- **Organization Support**: Per-organization push configurations and templates
- **Test Mode**: Safe testing with fake notifications during development

### Architecture

```
User.push_notification() 
  └─> loops devices → device.send()
      └─> FCM delivery + tracking
```

The push system consists of four main components:

1. **Device Registration**: Apps register FCM tokens and preferences
2. **Configuration**: Per-organization FCM credentials and settings
3. **Templates**: Optional reusable notification formats with variables
4. **Delivery Tracking**: Complete history of sent notifications and their status

## Authentication & Permissions

All push API endpoints require authentication via the standard Django Mojo auth system. Specific permissions are enforced per endpoint as documented below.

## Device Registration

### Register Device

Register a device for push notifications with an FCM token. Call this when a user installs/opens your app.

```http
POST /api/account/devices/push/register
Authorization: Bearer <token>
Content-Type: application/json
```

**Request Body:**
```json
{
    "device_token": "FCM device token from Firebase",
    "device_id": "unique-app-device-id", 
    "platform": "ios|android|web",
    "device_name": "iPhone 14 Pro",
    "app_version": "1.2.0",
    "os_version": "iOS 16.1",
    "push_preferences": {
        "orders": true,
        "marketing": false,
        "system": true
    }
}
```

**Response:**
```json
{
    "id": 123,
    "device_id": "unique-app-device-id",
    "platform": "ios",
    "device_name": "iPhone 14 Pro", 
    "app_version": "1.2.0",
    "os_version": "iOS 16.1",
    "push_enabled": true,
    "push_preferences": {
        "orders": true,
        "marketing": false,
        "system": true
    },
    "last_seen": "2024-01-15T10:30:00Z",
    "user": {
        "id": 456,
        "username": "john.doe"
    }
}
```

## Device Management

### List Registered Devices

Get all registered devices for the authenticated user.

```http
GET /api/account/devices/push
Authorization: Bearer <token>
```

**Query Parameters:**
- `platform`: Filter by platform (ios, android, web)
- `push_enabled`: Filter by push enabled status (true, false)
- `search`: Search device names or IDs

**Response:**
```json
{
    "results": [
        {
            "id": 123,
            "device_id": "unique-device-1",
            "platform": "ios",
            "device_name": "iPhone 14 Pro",
            "push_enabled": true,
            "last_seen": "2024-01-15T10:30:00Z"
        }
    ],
    "count": 1
}
```

### Get Device Details

```http
GET /api/account/devices/push/123
Authorization: Bearer <token>
```

### Update Device

```http
PUT /api/account/devices/push/123
Authorization: Bearer <token>
Content-Type: application/json
```

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

```http
DELETE /api/account/devices/push/123
Authorization: Bearer <token>
```

## Sending Notifications

### Send Direct Notification

Send notifications with explicit content (most common use case).

```http
POST /api/account/devices/push/send
Authorization: Bearer <token>
Content-Type: application/json
Permission: send_notifications
```

**Request Body:**
```json
{
    "title": "Order Ready",
    "body": "Your order #12345 is ready for pickup",
    "category": "orders",
    "action_url": "myapp://orders/12345",
    "data": {
        "order_id": 12345,
        "custom_field": "value"
    }
}
```

**Response:**
```json
{
    "success": true,
    "sent_count": 2,
    "failed_count": 0,
    "deliveries": [
        {
            "id": 790,
            "title": "Order Ready", 
            "category": "orders",
            "status": "sent",
            "sent_at": "2024-01-15T10:30:00Z"
        }
    ]
}
```

### Send Silent Notification (Data Only)

Send data-only notification without title/body.

```json
{
    "data": {
        "action": "sync",
        "timestamp": 1234567890,
        "user_id": 123
    },
    "category": "system"
}
```

### Send to Multiple Users

```json
{
    "title": "System Maintenance",
    "body": "The system will be down 2-4 AM EST",
    "category": "system",
    "user_ids": [456, 789, 123]
}
```

### Send Templated Notification (Optional)

Send notifications using predefined templates with variable substitution.

```json
{
    "template": "order_ready",
    "context": {
        "customer_name": "John Doe",
        "order_number": "ORD-12345",
        "pickup_time": "3:30 PM"
    }
}
```

### Test Push Configuration

Send a test notification to verify push setup is working.

```http
POST /api/account/devices/push/test
Authorization: Bearer <token>
Content-Type: application/json
```

**Request Body:**
```json
{
    "message": "Testing push notifications!"
}
```

**Response:**
```json
{
    "success": true,
    "message": "Test notifications sent to 2 devices",
    "results": [
        {
            "id": 792,
            "title": "Push Test",
            "category": "test",
            "status": "sent"
        }
    ]
}
```

## Notification Templates (Optional)

Templates are optional but useful for recurring notification types.

### List Templates

```http
GET /api/account/devices/push/templates
Authorization: Bearer <token>
Permission: manage_notifications
```

**Response:**
```json
{
    "results": [
        {
            "id": 10,
            "name": "order_ready",
            "category": "orders",
            "priority": "high",
            "is_active": true
        }
    ]
}
```

### Get Template Details

```http
GET /api/account/devices/push/templates/10
Authorization: Bearer <token>
Permission: manage_notifications
```

**Response:**
```json
{
    "id": 10,
    "name": "order_ready", 
    "title_template": "Order Ready for {customer_name}",
    "body_template": "Your order #{order_number} is ready for pickup at {pickup_time}",
    "action_url": "myapp://orders/{order_number}",
    "category": "orders",
    "priority": "high",
    "variables": {
        "customer_name": "Customer's display name",
        "order_number": "Order reference number", 
        "pickup_time": "Estimated pickup time"
    },
    "is_active": true,
    "group": {
        "id": 5,
        "name": "Pizza Palace"
    }
}
```

### Create Template

```http
POST /api/account/devices/push/templates
Authorization: Bearer <token>
Permission: manage_notifications
Content-Type: application/json
```

**Request Body:**
```json
{
    "name": "welcome",
    "title_template": "Welcome {username}!",
    "body_template": "Thanks for joining {app_name}.",
    "category": "onboarding",
    "priority": "normal",
    "variables": {
        "username": "User's display name",
        "app_name": "Application name"
    }
}
```

### Update Template

```http
PUT /api/account/devices/push/templates/10
Authorization: Bearer <token>
Permission: manage_notifications
Content-Type: application/json
```

### Delete Template

```http
DELETE /api/account/devices/push/templates/10
Authorization: Bearer <token>
Permission: manage_notifications
```

## Push Configuration

### List Push Configurations

```http
GET /api/account/devices/push/config
Authorization: Bearer <token>
Permission: manage_push_config
```

**Response:**
```json
{
    "results": [
        {
            "id": 1,
            "name": "Production Config",
            "test_mode": false,
            "is_active": true
        }
    ]
}
```

### Get Configuration Details

```http
GET /api/account/devices/push/config/1
Authorization: Bearer <token>
Permission: manage_push_config
```

**Response:**
```json
{
    "id": 1,
    "name": "Production FCM Config",
    "test_mode": false,
    "fcm_sender_id": "123456789",
    "default_sound": "default",
    "is_active": true,
    "group": {
        "id": 5,
        "name": "Pizza Palace"
    }
}
```

**Note:** Sensitive credentials (FCM server keys) are encrypted and not exposed via API. Set them using `config.set_fcm_server_key()` method.

### Create/Update Configuration

```http
POST /api/account/devices/push/config
PUT /api/account/devices/push/config/1
Authorization: Bearer <token>
Permission: manage_push_config
Content-Type: application/json
```

**Request Body:**
```json
{
    "name": "Development Config",
    "test_mode": true,
    "fcm_sender_id": "dev-123456789",
    "default_sound": "default"
}
```

## Delivery Tracking

### List Notification Deliveries

```http
GET /api/account/devices/push/deliveries
Authorization: Bearer <token>
Permission: view_notifications
```

**Query Parameters:**
- `status`: Filter by status (pending, sent, delivered, failed)
- `category`: Filter by notification category
- `date_from`: Filter deliveries from date (ISO format)
- `date_to`: Filter deliveries to date (ISO format)

**Response:**
```json
{
    "results": [
        {
            "id": 789,
            "title": "Order Ready for John Doe",
            "category": "orders",
            "status": "sent",
            "sent_at": "2024-01-15T10:30:00Z",
            "created": "2024-01-15T10:29:55Z",
            "user": {
                "id": 456,
                "username": "john.doe"
            },
            "device": {
                "id": 123,
                "platform": "ios",
                "device_name": "iPhone 14 Pro"
            }
        }
    ]
}
```

### Get Delivery Details

```http
GET /api/account/devices/push/deliveries/789
Authorization: Bearer <token>
Permission: view_notifications
```

**Response:**
```json
{
    "id": 789,
    "title": "Order Ready for John Doe",
    "body": "Your order #ORD-12345 is ready for pickup at 3:30 PM",
    "category": "orders",
    "action_url": "myapp://orders/ORD-12345",
    "data_payload": {
        "order_id": 12345
    },
    "status": "sent",
    "sent_at": "2024-01-15T10:30:00Z",
    "delivered_at": null,
    "error_message": null,
    "created": "2024-01-15T10:29:55Z",
    "platform_data": {
        "multicast_id": "123456789",
        "success": 1,
        "failure": 0
    },
    "user": {
        "id": 456,
        "username": "john.doe",
        "email": "john@example.com"
    },
    "device": {
        "id": 123,
        "device_id": "unique-device-1",
        "platform": "ios",
        "device_name": "iPhone 14 Pro"
    },
    "template": {
        "id": 10,
        "name": "order_ready"
    }
}
```

## Push Statistics

### Get Push Statistics

Get delivery statistics for the authenticated user.

```http
GET /api/account/devices/push/stats
Authorization: Bearer <token>
```

**Response:**
```json
{
    "total_sent": 1247,
    "total_failed": 23,
    "total_pending": 5,
    "registered_devices": 3,
    "enabled_devices": 2
}
```

## Error Responses

All endpoints return standard HTTP status codes with detailed error information:

### 400 Bad Request
```json
{
    "error": "Must provide title, body, or data",
    "code": "INVALID_PARAMETERS"
}
```

### 401 Unauthorized
```json
{
    "error": "Authentication required",
    "code": "AUTH_REQUIRED"
}
```

### 403 Forbidden
```json
{
    "error": "Permission denied: send_notifications required",
    "code": "PERMISSION_DENIED"
}
```

### 404 Not Found
```json
{
    "error": "Device not found",
    "code": "NOT_FOUND"
}
```

## Integration Examples

### iOS Swift Example (FCM)

```swift
import FirebaseMessaging

// Configure Firebase
FirebaseApp.configure()

// Get FCM token
Messaging.messaging().token { token, error in
    guard let token = token else { return }
    
    let parameters: [String: Any] = [
        "device_token": token,
        "device_id": UIDevice.current.identifierForVendor?.uuidString ?? "",
        "platform": "ios",
        "device_name": UIDevice.current.name,
        "app_version": Bundle.main.infoDictionary?["CFBundleShortVersionString"] as? String ?? "",
        "os_version": UIDevice.current.systemVersion,
        "push_preferences": [
            "orders": true,
            "marketing": false,
            "system": true
        ]
    ]
    
    APIClient.shared.post("/api/account/devices/push/register", parameters: parameters)
}
```

### Android Kotlin Example (FCM)

```kotlin
import com.google.firebase.messaging.FirebaseMessaging

FirebaseMessaging.getInstance().token.addOnCompleteListener { task ->
    if (!task.isSuccessful) return@addOnCompleteListener
    
    val token = task.result
    val preferences = mapOf(
        "orders" to true,
        "marketing" to false,
        "system" to true
    )
    
    val requestBody = mapOf(
        "device_token" to token,
        "device_id" to getDeviceId(),
        "platform" to "android",
        "device_name" to getDeviceName(),
        "app_version" to BuildConfig.VERSION_NAME,
        "os_version" to Build.VERSION.RELEASE,
        "push_preferences" to preferences
    )
    
    apiService.registerDevice(requestBody)
}
```

### Web JavaScript Example (FCM)

```javascript
import { getMessaging, getToken } from "firebase/messaging";

async function registerForPush() {
    const messaging = getMessaging();
    const token = await getToken(messaging, { 
        vapidKey: 'YOUR_VAPID_KEY' 
    });
    
    const response = await fetch('/api/account/devices/push/register', {
        method: 'POST',
        headers: {
            'Authorization': `Bearer ${getAuthToken()}`,
            'Content-Type': 'application/json'
        },
        body: JSON.stringify({
            device_token: token,
            device_id: getDeviceId(),
            platform: 'web',
            device_name: navigator.userAgent,
            app_version: APP_VERSION,
            push_preferences: {
                orders: true,
                marketing: false,
                system: true
            }
        })
    });
    
    return response.json();
}
```

## Best Practices

### Device Registration
- Register devices on app start when user is authenticated
- Update device tokens when they change (FCM may refresh them)
- Allow users to manage notification preferences in app settings
- Use unique, persistent device_id values

### Notification Sending
- Use descriptive categories for preference filtering
- Include action_url for deep linking into your app
- Use data payload for silent/background notifications
- Test with small user groups before broad deployments

### Template Usage (Optional)
- Use templates for recurring notification types
- Keep template variables descriptive
- Document expected variables
- Test templates with realistic data

### Error Handling
- Handle failed notifications gracefully (tokens may expire)
- Monitor delivery statistics to identify issues
- Implement retry logic for temporary failures
- Log errors for debugging

### Security
- Never expose FCM server keys in client apps
- Use test mode during development
- Validate user permissions before sending on behalf of users
- Monitor for suspicious notification patterns

### Performance
- Send to multiple users efficiently using user_ids
- Respect user notification preferences via categories
- Monitor delivery rates and adjust patterns
- Use silent notifications sparingly

## Development & Testing

### Test Mode
Enable test mode to send fake notifications during development:

```python
config = PushConfig.objects.create(
    name="Dev Config",
    test_mode=True  # No real FCM calls
)
```

Test mode notifications:
- Don't call FCM (fake delivery)
- Always succeed
- Log detailed debug info
- Store test metadata in platform_data

### Local Development Setup

1. Create test config with `test_mode=True`
2. Register development devices
3. Use test endpoint to verify registration
4. Enable real FCM when ready for production

### Testing Checklist

- [ ] Device registration works on all platforms
- [ ] Notifications arrive on devices
- [ ] Deep links work (action_url)
- [ ] User preferences are respected
- [ ] Silent notifications work (data only)
- [ ] Failed deliveries are logged correctly
- [ ] Test mode works without FCM credentials

## Architecture Summary

Simple KISS architecture:

```
┌─────────────────────────────────────┐
│ User.push_notification()            │
│  - Simple loop through devices      │
└────────────┬────────────────────────┘
             │
             ▼
┌─────────────────────────────────────┐
│ RegisteredDevice.send()             │
│  - Get FCM config                   │
│  - Create delivery record           │
│  - Send via FCM                     │
│  - Track result                     │
└─────────────────────────────────────┘
```

**Key Points:**
- FCM only - no APNS complexity
- `device.send()` does all the work
- Simple helper functions for convenience
- Complete delivery tracking
- Encrypted credential storage

This comprehensive API enables building robust notification systems for mobile and web applications with minimal complexity.
