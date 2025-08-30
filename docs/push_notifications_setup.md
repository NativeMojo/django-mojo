# Push Notifications Setup Guide

This guide covers setting up push notifications in Django-MOJO applications.

## Dependencies

Install the required push notification libraries:

```bash
pip install pyfcm==1.5.4  # For Android/FCM notifications
pip install apns2==0.7.2  # For iOS/APNS notifications
```

Both packages are optional - install only what you need for your platforms.

## Database Migration

After installing Django-MOJO with push notification support, create and run migrations:

```bash
python manage.py makemigrations account
python manage.py migrate
```

This adds the following models:
- `RegisteredDevice` - Devices registered for push notifications
- `PushConfig` - Push service configuration (system/org level)
- `NotificationTemplate` - Reusable notification templates
- `NotificationDelivery` - Delivery tracking and history

## Configuration

### 1. System-Wide Push Config

Create a system-wide configuration (accessible via admin or REST API):

```python
from mojo.apps.account.models import PushConfig

# Create system default config
config = PushConfig.objects.create(
    group=None,  # System-wide
    name="Default System Config",
    
    # iOS/APNS Configuration
    apns_enabled=True,
    apns_key_id="YOUR_KEY_ID",
    apns_team_id="YOUR_TEAM_ID", 
    apns_bundle_id="com.yourapp.bundle",
    apns_key_file="-----BEGIN PRIVATE KEY-----\n...\n-----END PRIVATE KEY-----",
    apns_use_sandbox=True,  # False for production
    
    # Android/FCM Configuration  
    fcm_enabled=True,
    fcm_server_key="YOUR_FCM_SERVER_KEY",
    fcm_sender_id="YOUR_SENDER_ID"
)
```

### 2. Organization-Specific Config

For multi-tenant setups, create org-specific configs:

```python
from mojo.apps.account.models import Group, PushConfig

# Assuming you have a Group/Organization
org = Group.objects.get(name="Acme Corp")

config = PushConfig.objects.create(
    group=org,  # Organization-specific
    name="Acme Corp Push Config",
    apns_enabled=True,
    fcm_enabled=True,
    # ... other config fields
)
```

### 3. User Organization Assignment

Assign users to organizations for automatic config resolution:

```python
from mojo.apps.account.models import User, Group

user = User.objects.get(username="john@example.com")
org = Group.objects.get(name="Acme Corp")

user.org = org
user.save()
```

## Notification Templates

Create reusable templates for consistent messaging:

```python
from mojo.apps.account.models import NotificationTemplate

# System template (available to all)
template = NotificationTemplate.objects.create(
    group=None,  # System template
    name="order_ready",
    title_template="Order Ready!",
    body_template="Hi {customer_name}, your order #{order_id} is ready for pickup at {location}.",
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
    body_template="Hi {name}, welcome to our platform. Get started by...",
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

#### iOS (Swift)
```swift
// Register for push notifications
UNUserNotificationCenter.current().requestAuthorization(options: [.alert, .sound, .badge]) { granted, error in
    if granted {
        DispatchQueue.main.async {
            UIApplication.shared.registerForRemoteNotifications()
        }
    }
}

// When token is received
func application(_ application: UIApplication, didRegisterForRemoteNotificationsWithDeviceToken deviceToken: Data) {
    let token = deviceToken.map { String(format: "%02.2hhx", $0) }.joined()
    
    // Register with your API
    registerDevice(token: token, platform: "ios")
}
```

#### Android (Java/Kotlin)
```kotlin
FirebaseMessaging.getInstance().token.addOnCompleteListener { task ->
    if (!task.isSuccessful) return@addOnCompleteListener
    
    val token = task.result
    registerDevice(token, "android")
}
```

#### REST API Registration
```javascript
// Register device via REST API
fetch('/api/account/devices/push/register', {
    method: 'POST',
    headers: {
        'Authorization': 'Bearer ' + userToken,
        'Content-Type': 'application/json'
    },
    body: JSON.stringify({
        device_token: pushToken,
        device_id: uniqueDeviceId,
        platform: 'ios', // or 'android'
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

### Using Templates
```python
from mojo.apps.account.services.push import send_push_notification

# Send to specific user
send_push_notification(
    user=current_user,
    template_name="order_ready",
    context={
        "customer_name": "John Doe",
        "order_id": "12345",
        "location": "Main Street Store"
    }
)

# Send to multiple users
send_push_notification(
    user=admin_user,
    template_name="system_alert", 
    context={"message": "System maintenance in 10 minutes"},
    user_ids=[1, 2, 3, 4, 5]
)
```

### Direct Notifications
```python
from mojo.apps.account.services.push import send_direct_notification

send_direct_notification(
    user=current_user,
    title="Hello!",
    body="Your order is ready for pickup",
    category="orders",
    action_url="myapp://orders/12345"
)
```

### Via REST API
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
      "location": "Main St"
    }
  }'

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

### Test Push Configuration
```bash
curl -X POST /api/account/devices/push/test \
  -H "Authorization: Bearer YOUR_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"message": "Test notification from API"}'
```

### View Statistics
```bash
curl -X GET /api/account/devices/push/stats \
  -H "Authorization: Bearer YOUR_TOKEN"
```

## Security Notes

1. **Credentials**: Push credentials are automatically encrypted using MojoSecrets
2. **Permissions**: All endpoints require appropriate permissions
3. **Device Ownership**: Users can only manage their own devices
4. **Audit Trail**: All notifications are logged in NotificationDelivery

## Troubleshooting

### Common Issues

1. **"No push config available"**: Ensure a PushConfig exists (system or org-level)
2. **"Template not found"**: Check template name and organization scope
3. **APNS/FCM errors**: Verify credentials and certificate validity
4. **No devices found**: Ensure devices are registered and push_enabled=True

### Debug Mode
Enable push notification debugging:

```python
# The push service uses the new logit convenience functions:
# logit.info() -> logs to mojo.log  
# logit.error() -> logs to error.log
# logit.warn() -> logs to mojo.log
# logit.debug() -> logs to debug.log

# No additional configuration needed - logging works out of the box
```

This will automatically log detailed information about config resolution, template matching, and delivery attempts to the appropriate log files.