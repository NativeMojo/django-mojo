For building your own simple notification framework in a multi-tenant Django system, I'd recommend using the low-level libraries directly:

- **`pyfcm`** for Firebase/Android notifications
- **`apns2`** for iOS notifications

These are lightweight, well-maintained, and give you full control without the overhead of existing Django packages.

Here's a simple architecture using Django models:

```python
# models.py
from django.db import models
from django.contrib.auth.models import User

class NotificationConfig(models.Model):
    """Per-tenant notification configuration"""
    tenant_id = models.CharField(max_length=100)  # or ForeignKey to your tenant model
    fcm_server_key = models.TextField(blank=True)
    apns_key_id = models.CharField(max_length=100, blank=True)
    apns_team_id = models.CharField(max_length=100, blank=True)
    apns_bundle_id = models.CharField(max_length=255, blank=True)
    apns_key_file = models.TextField(blank=True)  # Store key content or path

class RegisteredDevice(models.Model):
    DEVICE_TYPES = [
        ('ios', 'iOS'),
        ('android', 'Android'),
        ('web', 'Web'),
    ]

    user = models.ForeignKey(User, on_delete=models.CASCADE)
    tenant_id = models.CharField(max_length=100)
    device_token = models.TextField()
    device_type = models.CharField(max_length=10, choices=DEVICE_TYPES)

    # Notification preferences
    receive_marketing = models.BooleanField(default=True)
    receive_alerts = models.BooleanField(default=True)
    receive_updates = models.BooleanField(default=True)

    created_at = models.DateTimeField(auto_now_add=True)
    last_active = models.DateTimeField(auto_now=True)
    is_active = models.BooleanField(default=True)

    class Meta:
        unique_together = ['user', 'device_token']

# Simple notification service
from pyfcm import FCMNotification
from apns2.client import APNsClient
from apns2.payload import Payload
from apns2.credentials import TokenCredentials

class NotificationService:
    def __init__(self, tenant_id):
        self.tenant_id = tenant_id
        self.config = NotificationConfig.objects.get(tenant_id=tenant_id)

    def send_notification(self, user, message, notification_type='alert'):
        devices = RegisteredDevice.objects.filter(
            user=user,
            tenant_id=self.tenant_id,
            is_active=True
        )

        for device in devices:
            # Check if user wants this type of notification
            if not self._should_send(device, notification_type):
                continue

            if device.device_type == 'android':
                self._send_fcm(device, message)
            elif device.device_type == 'ios':
                self._send_apns(device, message)

    def _should_send(self, device, notification_type):
        type_mapping = {
            'marketing': device.receive_marketing,
            'alert': device.receive_alerts,
            'update': device.receive_updates,
        }
        return type_mapping.get(notification_type, True)

    def _send_fcm(self, device, message):
        if not self.config.fcm_server_key:
            return False

        push_service = FCMNotification(api_key=self.config.fcm_server_key)
        result = push_service.notify_single_device(
            registration_id=device.device_token,
            message_title="Notification",
            message_body=message
        )
        return result

    def _send_apns(self, device, message):
        if not all([self.config.apns_key_id, self.config.apns_team_id]):
            return False

        credentials = TokenCredentials(
            auth_key=self.config.apns_key_file,
            auth_key_id=self.config.apns_key_id,
            team_id=self.config.apns_team_id
        )

        client = APNsClient(credentials=credentials, use_sandbox=False)
        payload = Payload(alert=message, sound="default", badge=1)

        client.send_notification(device.device_token, payload, self.config.apns_bundle_id)
        return True

# Usage
def send_user_notification(user, tenant_id, message, notification_type='alert'):
    service = NotificationService(tenant_id)
    service.send_notification(user, message, notification_type)
```

**Key benefits of this approach:**
- All configuration stored in Django models (no settings.py needed)
- Multi-tenant support built-in
- Per-device notification preferences
- Simple interface: just call `send_user_notification()`
- Easy to extend with more notification types
- Handles both iOS and Android with minimal dependencies

**To use it:**
```python
# Register a device
RegisteredDevice.objects.create(
    user=request.user,
    tenant_id="tenant_123",
    device_token="device_token_here",
    device_type="ios"
)

# Send notification
send_user_notification(
    user=some_user,
    tenant_id="tenant_123",
    message="Your order is ready!",
    notification_type="alert"
)
```
