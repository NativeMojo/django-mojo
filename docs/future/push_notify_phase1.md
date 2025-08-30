# Push Notification Support - Phase 1 Design

This document outlines the design for integrating push notification support into Django-MOJO, building on our existing infrastructure while maintaining the framework's core principles of simplicity, security, and separation of concerns.

## Overview

Phase 1 focuses on building a foundational push notification system that:
- Creates a dedicated `RegisteredDevice` model for push-enabled devices
- Provides system-wide and organization-level push configuration via `PushConfig`
- Uses FCM as primary service for both iOS and Android push notifications
- Includes APNS support for iOS-specific requirements (rarely needed)
- Provides test mode for development and testing workflows
- Maintains our security-first approach with proper permissions
- Follows MOJO conventions for REST API integration

## Architecture Components

### 1. User Organization Support

Add organization support to the User model to enable proper push config resolution.

**Extension to `User` Model:**
```python
# Add to existing User model
org = models.ForeignKey("account.Group", on_delete=models.SET_NULL,
                       null=True, blank=True, related_name="org_users",
                       help_text="Default organization for this user")
```

This allows the system to determine which `PushConfig` to use: user's org config or system default.

### Model Organization

All push notification models are organized in a dedicated subdirectory for cleaner imports and better organization:

```
mojo/apps/account/models/push/
├── __init__.py      # Exports all push models
├── device.py        # RegisteredDevice model
├── config.py        # PushConfig model
├── template.py      # NotificationTemplate model
└── delivery.py      # NotificationDelivery model
```

This follows the MOJO principle of keeping filenames short and organizing related functionality together.

### 2. Registered Device Model

A dedicated model for devices that have explicitly registered for push notifications via our REST APIs.

**New Model: `RegisteredDevice` (`models/push/device.py`)**
```python
class RegisteredDevice(models.Model, MojoModel):
    """
    Represents a device explicitly registered for push notifications via REST API.
    Separate from UserDevice which tracks browser sessions via duid/user-agent.
    """
    created = models.DateTimeField(auto_now_add=True, editable=False, db_index=True)
    modified = models.DateTimeField(auto_now=True, db_index=True)

    user = models.ForeignKey("account.User", on_delete=models.CASCADE, related_name='registered_devices')

    # Device identification
    device_token = models.TextField(db_index=True)  # Push token from platform
    device_id = models.CharField(max_length=255, db_index=True)  # App-provided device ID
    platform = models.CharField(max_length=20, choices=[
        ('ios', 'iOS'),
        ('android', 'Android'),
        ('web', 'Web')
    ], db_index=True)

    # Device info
    app_version = models.CharField(max_length=50, blank=True)
    os_version = models.CharField(max_length=50, blank=True)
    device_name = models.CharField(max_length=100, blank=True)

    # Push preferences
    push_enabled = models.BooleanField(default=True, db_index=True)
    push_preferences = models.JSONField(default=dict, blank=True)

    # Status tracking
    is_active = models.BooleanField(default=True, db_index=True)
    last_seen = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = [('user', 'device_id'), ('device_token', 'platform')]
        ordering = ['-last_seen']

    class RestMeta:
        VIEW_PERMS = ["view_devices", "manage_devices", "owner"]
        SAVE_PERMS = ["manage_devices", "owner"]
        SEARCH_FIELDS = ["device_name", "device_id"]
        GRAPHS = {
            "basic": {
                "fields": ["device_id", "platform", "device_name", "push_enabled", "last_seen"]
            },
            "default": {
                "fields": ["device_id", "platform", "device_name", "app_version",
                          "os_version", "push_enabled", "push_preferences", "last_seen"],
                "graphs": {
                    "user": "basic"
                }
            }
        }

    def __str__(self):
        return f"{self.device_name or self.device_id} ({self.platform}) - {self.user.username}"
```

### 3. Push Configuration Model

System-wide and organization-level push configuration support.

**New Model: `PushConfig` (`models/push/config.py`)**
```python
class PushConfig(MojoSecrets, MojoModel):
    """
    Push notification configuration. Can be system-wide (group=None) or org-specific.
    FCM is the primary service supporting both iOS and Android platforms.
    APNS is available for iOS-specific requirements but rarely needed.
    Test mode allows fake notifications for development and testing.
    """
    created = models.DateTimeField(auto_now_add=True, editable=False, db_index=True)
    modified = models.DateTimeField(auto_now=True, db_index=True)

    group = models.OneToOneField("account.Group", on_delete=models.CASCADE,
                                related_name="push_config", null=True, blank=True,
                                help_text="Organization for this config. Null = system default")

    name = models.CharField(max_length=100, help_text="Configuration name")
    is_active = models.BooleanField(default=True, db_index=True)

    # Test/Development Mode
    test_mode = models.BooleanField(default=False, db_index=True,
                                   help_text="Enable test mode - fake notifications for development")

    # FCM Configuration (Primary - supports both iOS and Android)
    fcm_enabled = models.BooleanField(default=True,
                                     help_text="FCM handles both iOS and Android notifications")
    fcm_sender_id = models.CharField(max_length=100, blank=True)

    # APNS Configuration (iOS-specific, rarely needed - use FCM instead)
    apns_enabled = models.BooleanField(default=False,
                                      help_text="APNS for iOS-specific needs. FCM is preferred.")
    apns_key_id = models.CharField(max_length=100, blank=True)
    apns_team_id = models.CharField(max_length=100, blank=True)
    apns_bundle_id = models.CharField(max_length=255, blank=True)
    apns_use_sandbox = models.BooleanField(default=False)

    # General Settings
    default_sound = models.CharField(max_length=50, default="default")
    default_badge_count = models.IntegerField(default=1)

    class Meta:
        ordering = ['group__name', 'name']

    class RestMeta:
        VIEW_PERMS = ["manage_push_config", "manage_groups"]
        SAVE_PERMS = ["manage_push_config", "manage_groups"]
        SEARCH_FIELDS = ["name"]
        GRAPHS = {
            "basic": {
                "fields": ["name", "fcm_enabled", "apns_enabled", "test_mode", "default_sound", "is_active"]
            },
            "default": {
                "exclude": ["mojo_secrets"],  # Never expose encrypted secrets
                "graphs": {
                    "group": "basic"
                }
            },
            "full": {
                "exclude": ["mojo_secrets"],  # Never expose encrypted secrets
                "graphs": {
                    "group": "default"
                }
            }
        }

    def __str__(self):
        org = self.group.name if self.group else "System Default"
        return f"{self.name} ({org})"

    @classmethod
    def get_for_user(cls, user):
        """
        Get push config for user. Priority: user's org config -> system default
        """
        if user.org:
            config = cls.objects.filter(group=user.org, is_active=True).first()
            if config:
                return config

        # Fallback to system default
        return cls.objects.filter(group__isnull=True, is_active=True).first()

    def set_fcm_server_key(self, server_key):
        """Set FCM server key (will be encrypted)."""
        self.set_secret('fcm_server_key', server_key)

    def get_fcm_server_key(self):
        """Get decrypted FCM server key."""
        return self.get_secret('fcm_server_key', '')

    def set_apns_key_file(self, key_content):
        """Set APNS private key file content (will be encrypted)."""
        self.set_secret('apns_key_file', key_content)

    def get_apns_key_file(self):
        """Get decrypted APNS private key file content."""
        return self.get_secret('apns_key_file', '')
```

### 4. Notification Templates

Reusable notification templates for consistent messaging.

**New Model: `NotificationTemplate` (`models/push/template.py`)**
```python
class NotificationTemplate(models.Model, MojoModel):
    """
    Reusable notification templates with variable substitution support.
    """
    created = models.DateTimeField(auto_now_add=True, editable=False, db_index=True)
    modified = models.DateTimeField(auto_now=True, db_index=True)

    group = models.ForeignKey("account.Group", on_delete=models.CASCADE,
                             related_name="notification_templates", null=True, blank=True,
                             help_text="Organization for this template. Null = system template")

    name = models.CharField(max_length=100, db_index=True)
    title_template = models.CharField(max_length=200)
    body_template = models.TextField()
    action_url = models.URLField(blank=True, null=True)

    # Delivery preferences
    category = models.CharField(max_length=50, default="general", db_index=True)
    priority = models.CharField(max_length=20, choices=[
        ('low', 'Low'),
        ('normal', 'Normal'),
        ('high', 'High')
    ], default='normal', db_index=True)

    # Template variables documentation
    variables = models.JSONField(default=dict, blank=True,
                               help_text="Expected template variables and descriptions")

    is_active = models.BooleanField(default=True, db_index=True)

    class Meta:
        ordering = ['group__name', 'name']
        unique_together = [('group', 'name')]

    class RestMeta:
        VIEW_PERMS = ["manage_notifications", "manage_groups", "owner"]
        SAVE_PERMS = ["manage_notifications", "manage_groups"]
        SEARCH_FIELDS = ["name", "category"]
        LIST_DEFAULT_FILTERS = {"is_active": True}
        GRAPHS = {
            "basic": {
                "fields": ["name", "category", "priority", "is_active"]
            },
            "default": {
                "graphs": {
                    "group": "basic"
                }
            },
            "full": {
                "graphs": {
                    "group": "default"
                }
            }
        }

    def __str__(self):
        org = self.group.name if self.group else "System"
        return f"{self.name} ({org})"

    def render(self, context):
        """
        Render template with provided context variables.
        """
        title = self.title_template.format(**context)
        body = self.body_template.format(**context)
        return title, body
```

### 5. Notification History & Delivery Tracking

Track all notifications sent for audit, debugging, and analytics.

**New Model: `NotificationDelivery` (`models/push/delivery.py`)**
```python
class NotificationDelivery(models.Model, MojoModel):
    """
    Track all push notification delivery attempts and results.
    """
    created = models.DateTimeField(auto_now_add=True, editable=False, db_index=True)
    modified = models.DateTimeField(auto_now=True, db_index=True)

    user = models.ForeignKey("account.User", on_delete=models.CASCADE,
                           related_name="notification_deliveries")
    device = models.ForeignKey("RegisteredDevice", on_delete=models.CASCADE,
                             related_name="notification_deliveries")
    template = models.ForeignKey("NotificationTemplate", on_delete=models.SET_NULL,
                               null=True, related_name="deliveries")

    title = models.CharField(max_length=200)
    body = models.TextField()
    category = models.CharField(max_length=50, db_index=True)
    action_url = models.URLField(blank=True, null=True)

    # Delivery tracking
    status = models.CharField(max_length=20, choices=[
        ('pending', 'Pending'),
        ('sent', 'Sent'),
        ('delivered', 'Delivered'),
        ('failed', 'Failed')
    ], default='pending', db_index=True)

    sent_at = models.DateTimeField(null=True, blank=True, db_index=True)
    delivered_at = models.DateTimeField(null=True, blank=True)
    error_message = models.TextField(blank=True, null=True)

    # Push service specific data
    platform_data = models.JSONField(default=dict, blank=True)

    class Meta:
        ordering = ['-created']

    class RestMeta:
        VIEW_PERMS = ["view_notifications", "manage_notifications", "owner"]
        SAVE_PERMS = ["manage_notifications"]
        SEARCH_FIELDS = ["title", "category"]
        LIST_DEFAULT_FILTERS = {"status": "sent"}
        GRAPHS = {
            "basic": {
                "fields": ["title", "category", "status", "sent_at"]
            },
            "default": {
                "graphs": {
                    "user": "basic",
                    "device": "basic"
                }
            },
            "full": {
                "graphs": {
                    "user": "default",
                    "device": "default",
                    "template": "basic"
                }
            }
        }

    def __str__(self):
        return f"{self.title} -> {self.device} ({self.status})"
```

### 6. Push Notification Service

Central service for sending notifications, following MOJO service patterns.

**New Service: `mojo/apps/account/services/push_notifications.py`**
```python
from mojo.helpers.settings import settings
from mojo.helpers import logit, dates
from pyfcm import FCMNotification
from apns2.client import APNsClient
from apns2.payload import Payload
from apns2.credentials import TokenCredentials

class PushNotificationService:
    """
    Central push notification service for account-specific push functionality.
    
    FCM is the primary service supporting both iOS and Android platforms.
    APNS is available for iOS-specific requirements but rarely needed.
    Test mode allows fake notifications for development and testing.
    """

    def __init__(self, user):
        self.user = user
        self.config = self._get_push_config()

    def _get_push_config(self):
        """Get push config for user's organization or system default."""
        from mojo.apps.account.models import PushConfig
        return PushConfig.get_for_user(self.user)

    def send_notification(self, template_name=None, context=None, devices=None, user_ids=None,
                         title=None, body=None, category="general", action_url=None):
        """
        Send notification using template or direct content.

        Args:
            template_name: Name of notification template (for templated sending)
            context: Variables for template rendering
            devices: Specific RegisteredDevice queryset/list
            user_ids: List of user IDs to send to (uses their active devices)
            title: Direct title (for non-templated sending)
            body: Direct body (for non-templated sending)
            category: Notification category
            action_url: Direct action URL
        """
        if not self.config:
            logit.info(f"No push config available for user {self.user.username}")
            return []

        # Support both templated and direct sending
        template = None
        if template_name:
            template = self._get_template(template_name)
            if not template:
                logit.error(f"Template {template_name} not found")
                return []
        elif not (title and body):
            logit.error("Must provide either template_name or both title and body")
            return []

        target_devices = self._resolve_devices(devices, user_ids)
        if not target_devices:
            logit.info(f"No devices to send to for template {template_name}")
            return []

        results = []
        for device in target_devices:
            notification_category = template.category if template else category
            if self._should_send_to_device(device, notification_category):
                if template:
                    result = self._send_to_device(device, template, context or {})
                else:
                    result = self._send_direct(device, title, body, notification_category, action_url)
                results.append(result)

        return results

    def _get_template(self, template_name):
        """Get template by name, preferring user's org templates."""
        from mojo.apps.account.models import NotificationTemplate

        # Try user's org first
        if self.user.org:
            template = NotificationTemplate.objects.filter(
                group=self.user.org, name=template_name, is_active=True
            ).first()
            if template:
                return template

        # Fallback to system templates
        return NotificationTemplate.objects.filter(
            group__isnull=True, name=template_name, is_active=True
        ).first()

    def _resolve_devices(self, devices, user_ids):
        """Resolve target devices from various inputs."""
        from mojo.apps.account.models import RegisteredDevice, User

        if devices is not None:
            return devices

        if user_ids:
            users = User.objects.filter(id__in=user_ids)
            return RegisteredDevice.objects.filter(
                user__in=users, is_active=True, push_enabled=True
            )

        # Default to current user's devices
        return self.user.registered_devices.filter(
            is_active=True, push_enabled=True
        )

    def _should_send_to_device(self, device, category):
        """Check if device should receive this category of notification."""
        preferences = device.push_preferences or {}
        return preferences.get(category, True)  # Default to enabled

    def _send_to_device(self, device, template, context):
        """Send notification to a specific device."""
        from mojo.apps.account.models import NotificationDelivery

        title, body = template.render(context)

        delivery = NotificationDelivery.objects.create(
            user=device.user,
            device=device,
            template=template,
            title=title,
            body=body,
            category=template.category,
            action_url=template.action_url
        )

        try:
            success = False

            # Test mode - fake delivery for development/testing
            if self.config.test_mode:
                success = self._send_test(device, title, body, template)

            # FCM is primary - supports both iOS and Android
            elif self.config.fcm_enabled:
                success = self._send_fcm(device, title, body, template)

            # APNS fallback for iOS only (rarely needed)
            elif device.platform == 'ios' and self.config.apns_enabled:
                success = self._send_apns(device, title, body, template)

            else:
                delivery.status = 'failed'
                if not self.config.fcm_enabled and not self.config.apns_enabled:
                    delivery.error_message = "No push services enabled in config"
                else:
                    delivery.error_message = f"Unsupported platform: {device.platform}"

            if success:
                delivery.status = 'sent'
                delivery.sent_at = dates.utcnow()
            else:
                delivery.status = 'failed'

        except Exception as e:
            delivery.status = 'failed'
            delivery.error_message = str(e)
            logit.error(f"Push notification failed: {e}")

        delivery.save()
        return delivery

    def _send_direct(self, device, title, body, category, action_url=None):
        """Send direct notification without template."""
        from mojo.apps.account.models import NotificationDelivery

        delivery = NotificationDelivery.objects.create(
            user=device.user,
            device=device,
            title=title,
            body=body,
            category=category,
            action_url=action_url
        )

        try:
            success = False
            if device.platform == 'ios' and self.config.apns_enabled:
                success = self._send_apns(device, title, body, None)
            elif device.platform == 'android' and self.config.fcm_enabled:
                success = self._send_fcm(device, title, body, None)
            else:
                delivery.error_message = f"Unsupported platform: {device.platform}"

            if success:
                delivery.status = 'sent'
                delivery.sent_at = dates.utcnow()
            else:
                delivery.status = 'failed'

        except Exception as e:
            delivery.status = 'failed'
            delivery.error_message = str(e)
            logit.error(f"Push notification failed: {e}")

        delivery.save()
        return delivery

    def _send_apns(self, device, title, body, template):
        """Send APNS notification to iOS device."""
        try:
            credentials = TokenCredentials(
                auth_key=self.config.apns_key_file,
                auth_key_id=self.config.apns_key_id,
                team_id=self.config.apns_team_id
            )

            client = APNsClient(credentials=credentials,
                              use_sandbox=self.config.apns_use_sandbox)
            payload = Payload(
                alert={'title': title, 'body': body},
                sound=self.config.default_sound,
                badge=self.config.default_badge_count
            )

            client.send_notification(device.device_token, payload,
                                   self.config.apns_bundle_id)
            return True

        except Exception as e:
            logit.error(f"APNS send failed: {e}")
            return False

    def _send_fcm(self, delivery, device, title, body, template):
        """Send FCM notification to device (supports both iOS and Android)."""
        try:
            push_service = FCMNotification(api_key=self.config.fcm_server_key)
            result = push_service.notify_single_device(
                registration_id=device.device_token,
                message_title=title,
                message_body=body,
                sound=self.config.default_sound
            )
            return result.get('success', 0) > 0

        except Exception as e:
            logit.error(f"FCM send failed: {e}")
            return False


# Convenience functions for easy usage
def send_push_notification(user, template_name, context=None, devices=None, user_ids=None):
    """
    Send templated push notification.

    Usage:
        send_push_notification(user, 'welcome', {'name': user.display_name})
        send_push_notification(user, 'alert', user_ids=[1, 2, 3])
    """
    service = PushNotificationService(user)
    return service.send_notification(template_name=template_name, context=context,
                                   devices=devices, user_ids=user_ids)

def send_direct_notification(user, title, body, category="general", action_url=None,
                           devices=None, user_ids=None):
    """
    Send direct push notification without template.

    Usage:
        send_direct_notification(user, "Hello!", "Your order is ready", "orders")
        send_direct_notification(user, "Alert", "System maintenance", user_ids=[1, 2, 3])
    """
    service = PushNotificationService(user)
    return service.send_notification(title=title, body=body, category=category,
                                   action_url=action_url, devices=devices, user_ids=user_ids)
```

## REST API Integration

Following MOJO patterns, REST endpoints with data in params/POST body:

### Device Registration
**File: `mojo/apps/account/rest/push.py`**

```python
import mojo.decorators as md
from mojo.apps.account.models import RegisteredDevice

@md.URL('devices/push/register')
@md.POST
@md.requires_auth
@md.requires_params(['device_token', 'device_id', 'platform'])
def register_device(request):
    """
    Register device for push notifications.

    POST /api/account/devices/push/register
    {
        "device_token": "...",
        "device_id": "...",
        "platform": "ios|android|web",
        "device_name": "...",
        "app_version": "...",
        "os_version": "..."
    }
    """
    device, created = RegisteredDevice.objects.update_or_create(
        user=request.user,
        device_id=request.POST.get('device_id'),
        defaults={
            'device_token': request.POST.get('device_token'),
            'platform': request.POST.get('platform'),
            'device_name': request.POST.get('device_name', ''),
            'app_version': request.POST.get('app_version', ''),
            'os_version': request.POST.get('os_version', ''),
            'is_active': True,
            'push_enabled': True
        }
    )

    return device.on_rest_response(request, 'default')

@md.URL('devices/push')
@md.URL('devices/push/<int:pk>')
def on_registered_devices(request, pk=None):
    """Standard CRUD for registered devices."""
    return RegisteredDevice.on_rest_request(request, pk)
```

### Template Management
```python
from mojo.apps.account.models import NotificationTemplate

@md.URL('devices/push/templates')
@md.URL('devices/push/templates/<int:pk>')
def on_notification_templates(request, pk=None):
    """Standard CRUD for notification templates."""
    return NotificationTemplate.on_rest_request(request, pk)
```

### Push Configuration
```python
from mojo.apps.account.models import PushConfig

@md.URL('devices/push/config')
@md.URL('devices/push/config/<int:pk>')
def on_push_config(request, pk=None):
    """Standard CRUD for push configuration."""
    return PushConfig.on_rest_request(request, pk)
```

### Send Notifications
```python
from mojo.apps.account.services.push_notifications import send_push_notification

@md.POST('devices/push/send')
@md.requires_auth
@md.requires_perms("send_notifications")
def send_notification(request):
    """
    Send push notification using template or direct content.

    POST /api/account/devices/push/send

    Templated:
    {
        "template": "template_name",
        "context": {"key": "value"},
        "user_ids": [1, 2, 3]  # optional
    }

    Direct:
    {
        "title": "Hello!",
        "body": "Your order is ready",
        "category": "orders",
        "action_url": "myapp://orders/123",
        "user_ids": [1, 2, 3]  # optional
    }
    """
    template = request.POST.get('template')
    title = request.POST.get('title')
    body = request.POST.get('body')

    if template:
        # Templated sending
        context = request.POST.get('context', {})
        user_ids = request.POST.get('user_ids')
        results = send_push_notification(
            user=request.user,
            template_name=template,
            context=context,
            user_ids=user_ids
        )
    elif title and body:
        # Direct sending
        category = request.POST.get('category', 'general')
        action_url = request.POST.get('action_url')
        user_ids = request.POST.get('user_ids')
        results = send_direct_notification(
            user=request.user,
            title=title,
            body=body,
            category=category,
            action_url=action_url,
            user_ids=user_ids
        )
    else:
        return {'error': 'Must provide either template or both title and body'}

    return {
        'success': True,
        'sent_count': len([r for r in results if r.status == 'sent']),
        'failed_count': len([r for r in results if r.status == 'failed']),
        'deliveries': [r.on_rest_response(request, 'basic') for r in results]
    }
```

## Security Considerations

1. **Credential Storage**: Push credentials stored using existing `MojoSecrets` encryption
2. **Permissions**: Leverage existing group/user/owner permission system
3. **Device Registration**: Only authenticated users can register devices
4. **Template Access**: Templates scoped to organization with proper permissions
5. **Audit Trail**: Complete notification history via `NotificationDelivery`
6. **Rate Limiting**: Built into existing MOJO request handling

## Implementation Phase Plan

### Phase 1A: Core Models & User Extension
- Add `org` field to User model
- Create `RegisteredDevice` model
- Create `PushConfig` model with system/org support
- Database migrations

### Phase 1B: Templates & Service
- Create `NotificationTemplate` model
- Create `NotificationDelivery` tracking model
- Build `PushNotificationService` in account services
- Basic device registration REST endpoint

### Phase 1C: Full API & Platform Integration
- Complete REST API endpoints
- iOS APNS integration
- Android FCM integration
- Error handling & retry logic
- Template management endpoints

## Dependencies

**New Python Packages:**
- `pyfcm==1.5.4` - FCM notifications
- `apns2==0.7.2` - APNS notifications

**Database Changes:**
- Add `org` field to User model
- New models organized in `models/push/`: `RegisteredDevice`, `PushConfig`, `NotificationTemplate`, `NotificationDelivery`

## Success Metrics

- Users can register devices for push notifications via REST API
- System and organization-level push configuration works correctly
- Notifications sent successfully to iOS and Android devices
- Template system enables consistent messaging
- Full audit trail of notification delivery maintained
- Zero security vulnerabilities in credential handling

## Future Enhancements (Phase 2+)

- Web push notification support
- Advanced scheduling and delivery options
- A/B testing for notification templates
- Rich media notification support
- Push notification analytics dashboard
- Bulk notification sending with job queues
- User preference management UI
- Push notification open/click tracking

---

This design maintains MOJO's principles while providing a robust, scalable foundation for push notifications that supports both system-wide and organization-specific configurations.
