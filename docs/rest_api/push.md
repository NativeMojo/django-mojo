# Push Notification API Documentation

## Overview

The Django Mojo Push Notification API provides a comprehensive system for managing and sending push notifications to mobile applications and web clients. The system supports both iOS (via FCM/APNS) and Android (via FCM) platforms, with built-in template support, delivery tracking, and organizational configuration.

### Key Features

- **Multi-Platform Support**: FCM for both iOS/Android, APNS for iOS-specific needs
- **Template System**: Reusable notification templates with variable substitution
- **Device Management**: Registration and preference management for user devices  
- **Delivery Tracking**: Complete audit trail of notification attempts and results
- **Organization Support**: Per-organization push configurations and templates
- **Test Mode**: Safe testing with fake notifications during development

### Architecture

The push system consists of four main components:

1. **Device Registration**: Apps register device tokens and preferences
2. **Templates**: Define reusable notification formats with variables
3. **Configuration**: Per-organization push service credentials and settings
4. **Delivery Tracking**: Complete history of sent notifications and their status

## Authentication & Permissions

All push API endpoints require authentication via the standard Django Mojo auth system. Specific permissions are enforced per endpoint as documented below.

## Device Registration

### Register Device

Register a device for push notifications. This is typically called when a user first installs/opens your app.

```http
POST /api/account/devices/push/register
Authorization: Bearer <token>
Content-Type: application/json
```

**Request Body:**
```json
{
    "device_token": "FCM/APNS device token",
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

### Send Templated Notification

Send notifications using predefined templates with variable substitution.

```http
POST /api/account/devices/push/send
Authorization: Bearer <token>
Content-Type: application/json
Permission: send_notifications
```

**Request Body:**
```json
{
    "template": "order_ready",
    "context": {
        "customer_name": "John Doe",
        "order_number": "ORD-12345",
        "pickup_time": "3:30 PM"
    },
    "user_ids": [456, 789]
}
```

**Response:**
```json
{
    "success": true,
    "sent_count": 3,
    "failed_count": 0,
    "deliveries": [
        {
            "id": 789,
            "title": "Order Ready for John Doe",
            "category": "orders",
            "status": "sent",
            "sent_at": "2024-01-15T10:30:00Z"
        }
    ]
}
```

### Send Direct Notification

Send notifications with explicit content without using templates.

```http
POST /api/account/devices/push/send
Authorization: Bearer <token>
Content-Type: application/json
Permission: send_notifications
```

**Request Body:**
```json
{
    "title": "System Maintenance",
    "body": "The system will be down for maintenance from 2-4 AM EST",
    "category": "system",
    "action_url": "myapp://maintenance",
    "user_ids": [456, 789]
}
```

**Response:**
```json
{
    "success": true,
    "sent_count": 2,
    "failed_count": 1,
    "deliveries": [
        {
            "id": 790,
            "title": "System Maintenance", 
            "category": "system",
            "status": "sent",
            "sent_at": "2024-01-15T10:30:00Z"
        },
        {
            "id": 791,
            "title": "System Maintenance",
            "category": "system", 
            "status": "failed",
            "error_message": "Invalid device token"
        }
    ]
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
    "message": "Testing push notifications from my app!"
}
```

**Response:**
```json
{
    "success": true,
    "message": "Test notifications sent",
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

## Notification Templates

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
    "body_template": "Thanks for joining {app_name}. Get started by exploring our features.",
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
            "fcm_enabled": true,
            "apns_enabled": false,
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
    "name": "Production Config",
    "test_mode": false,
    "fcm_enabled": true,
    "fcm_sender_id": "123456789",
    "apns_enabled": false,
    "default_sound": "default",
    "default_badge_count": 1,
    "is_active": true,
    "group": {
        "id": 5,
        "name": "Pizza Palace"
    }
}
```

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
    "fcm_enabled": true,
    "fcm_sender_id": "dev-123456789",
    "default_sound": "default",
    "default_badge_count": 1
}
```

**Note:** Sensitive credentials (FCM server keys, APNS private keys) are set via separate secure methods, not through the REST API.

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
    "status": "sent",
    "sent_at": "2024-01-15T10:30:00Z",
    "delivered_at": null,
    "error_message": null,
    "created": "2024-01-15T10:29:55Z",
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
    "error": "Must provide either template or both title and body",
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
    "error": "Template 'invalid_template' not found",
    "code": "NOT_FOUND"
}
```

## Integration Examples

### iOS Swift Example

```swift
// Register device for push notifications
func registerForPush(deviceToken: String) {
    let parameters = [
        "device_token": deviceToken,
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
    ] as [String: Any]
    
    APIClient.shared.post("/api/account/devices/push/register", parameters: parameters) { result in
        // Handle response
    }
}
```

### Android Kotlin Example

```kotlin
// Register device for push notifications
fun registerForPush(token: String) {
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
    
    apiService.registerDevice(requestBody).enqueue { response ->
        // Handle response
    }
}
```

### JavaScript Web Example

```javascript
// Register for web push notifications
async function registerForPush() {
    const registration = await navigator.serviceWorker.register('/sw.js');
    const subscription = await registration.pushManager.subscribe({
        userVisibleOnly: true,
        applicationServerKey: FCM_VAPID_KEY
    });
    
    const response = await fetch('/api/account/devices/push/register', {
        method: 'POST',
        headers: {
            'Authorization': `Bearer ${getAuthToken()}`,
            'Content-Type': 'application/json'
        },
        body: JSON.stringify({
            device_token: subscription.endpoint,
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
- Always register devices when the app starts and the user is authenticated
- Update device tokens when they change (iOS/Android may refresh them)
- Allow users to manage notification preferences in your app settings

### Template Usage
- Use templates for recurring notification types (order updates, system alerts, etc.)
- Keep template variables descriptive and document them in the `variables` field
- Test templates in development with realistic data

### Error Handling
- Handle failed notifications gracefully - tokens may become invalid
- Implement retry logic for temporary failures
- Monitor delivery statistics to identify issues

### Security
- Never expose push configuration credentials in client applications
- Use the test endpoint during development to avoid sending notifications to real users
- Validate user permissions before sending notifications on behalf of users

### Performance
- Batch notifications when possible by using `user_ids` parameter
- Use appropriate notification categories to respect user preferences
- Monitor notification delivery rates and adjust sending patterns accordingly

## Development & Testing

### Test Mode
Enable test mode in push configuration to send fake notifications during development:

```json
{
    "test_mode": true
}
```

Test mode notifications will:
- Not attempt actual platform delivery
- Always succeed
- Log detailed information for debugging
- Store test metadata in delivery records

### Local Development Setup

1. Create a test push configuration with `test_mode: true`
2. Register your development devices
3. Use the test endpoint to verify registration is working
4. Gradually enable real push services as needed

This comprehensive API enables building robust notification systems for mobile and web applications while providing complete control over delivery tracking and user preferences.