from django.db import models
from mojo.models import MojoModel
from mojo.apps import metrics


class RegisteredDevice(models.Model, MojoModel):
    """
    Represents a device explicitly registered for push notifications via REST API.
    Separate from UserDevice which tracks browser sessions via duid/user-agent.
    """
    created = models.DateTimeField(auto_now_add=True, editable=False, db_index=True)
    modified = models.DateTimeField(auto_now=True, db_index=True)

    user = models.ForeignKey("account.User", on_delete=models.CASCADE, related_name='registered_devices')

    # Device identification
    device_token = models.TextField(db_index=True, help_text="Push token from platform")
    device_id = models.CharField(max_length=255, db_index=True, help_text="App-provided device ID")
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
    push_preferences = models.JSONField(default=dict, blank=True,
                                      help_text="Category-based notification preferences")

    # Status tracking
    is_active = models.BooleanField(default=True, db_index=True)
    last_seen = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = [('user', 'device_id'), ('device_token', 'platform')]
        ordering = ['-last_seen']

    class RestMeta:
        SENSITIVE_FIELDS = ['device_token']
        VIEW_PERMS = ["view_devices", "manage_devices", "comms", "owner", "manage_users"]
        SAVE_PERMS = ["manage_devices", "comms", "owner"]
        # user pinned — a push-device token must bind to the registering
        # caller. Otherwise an admin could redirect another user's
        # notifications to an attacker-controlled token.
        NO_SAVE_FIELDS = ["user"]
        SEARCH_FIELDS = ["device_name", "device_id"]
        GRAPHS = {
            "basic": {
                "fields": ["id", "device_id", "platform", "device_name", "push_enabled", "is_active", "last_seen"]
            },
            "default": {
                "fields": ["id", "device_id", "platform", "device_name", "app_version",
                          "os_version", "push_enabled", "is_active", "push_preferences", "last_seen"],
                "graphs": {
                    "user": "basic"
                }
            },
            "full": {
                "graphs": {
                    "user": "default"
                }
            }
        }

    def __str__(self):
        return f"{self.device_name or self.device_id} ({self.platform}) - {self.user.username}"

    def send(self, title=None, body=None, data=None, category="general", action_url=None,
             config=None, require_live=False, client_factory=None):
        """
        Send push notification to this device via FCM.
        Simple and stupid - just send it.

        Args:
            title: Notification title (optional for silent notifications)
            body: Notification body (optional for silent notifications)
            data: Custom data payload dict
            category: Notification category (for user preferences)
            action_url: URL to open when notification is tapped

        Returns:
            NotificationDelivery object
        """
        from mojo.helpers import logit, dates
        from mojo.apps.account.models import PushConfig, NotificationDelivery

        # Check if device wants this category
        preferences = self.push_preferences or {}
        if not preferences.get(category, True):
            logit.info(f"Device {self.device_id} has disabled category {category}")
            return None

        # Get push config
        config = config if config is not None else PushConfig.get_for_user(self.user)
        if not config:
            logit.info(f"No push config available for user {self.user.username}")
            return None

        if require_live and (config.test_mode or not config.is_active
                             or not self.is_active or not self.push_enabled):
            return None

        # Create delivery record
        delivery = NotificationDelivery.objects.create(
            user=self.user,
            device=self,
            title=title,
            body=body,
            category=category,
            action_url=action_url,
            data_payload=data or {}
        )

        try:
            # Test mode - fake it
            if config.test_mode:
                self._send_test(delivery, config)
                delivery.mark_sent()
                return delivery

            # Real FCM send
            success = self._send_fcm(delivery, config, client_factory=client_factory)
            if success:
                metrics.record("push_sent")
                delivery.mark_sent()
            elif delivery.platform_data.get("outcome") == "unknown":
                delivery.error_message = "FCM acceptance unknown; check the device before sending again."
                delivery.save(update_fields=["error_message"])
            else:
                metrics.record("push_failed")
                from mojo.apps.account.services.push import provider_test_result
                delivery.mark_failed(provider_test_result(delivery.platform_data)["message"])

        except Exception:
            error_msg = "Push notification could not be completed. Check server logs."
            logit.error(error_msg)
            delivery.mark_failed(error_msg)

        return delivery

    def _send_test(self, delivery, config):
        """Fake notification for testing."""
        from mojo.helpers import logit, dates

        log_parts = []
        if delivery.title:
            log_parts.append(f"Title: {delivery.title}")
        if delivery.body:
            log_parts.append(f"Body: {delivery.body}")
        if delivery.data_payload:
            log_parts.append(f"Data: {delivery.data_payload}")

        log_message = f"TEST MODE: Push to {self.platform} device '{self.device_name}'"
        if log_parts:
            log_message += f" - {' | '.join(log_parts)}"

        logit.info(log_message)

        delivery.platform_data = {
            'test_mode': True,
            'platform': self.platform,
            'device_name': self.device_name,
            'timestamp': dates.utcnow().isoformat(),
        }
        delivery.save(update_fields=['platform_data'])

    def _send_fcm(self, delivery, config, client_factory=None):
        """Send once and retain bounded evidence, including ambiguous acceptance."""
        from mojo.helpers.fcm import FCMv1Client
        from mojo.apps.account.services.push import provider_test_result
        try:
            client = (client_factory or FCMv1Client)(config.get_fcm_service_account())
            data = dict(delivery.data_payload or {})
            if delivery.action_url:
                data['action_url'] = delivery.action_url
        except Exception:
            result = {'success': False, 'outcome': 'blocked', 'error_code': 'invalid_credentials'}
        else:
            try:
                result = client.send(token=self.device_token, title=delivery.title, body=delivery.body,
                                     data=data or None,
                                     sound=config.default_sound if (delivery.title or delivery.body) else None)
            except Exception:
                result = {'success': False, 'outcome': 'unknown', 'error_code': 'acceptance_unknown'}
        safe = provider_test_result(result)
        delivery.platform_data = {
            'fcm_version': 'v1', 'message_id': safe['message_id'], 'success': safe['success'],
            'outcome': safe['outcome'], 'error_code': safe['error_code'], 'config_id': config.pk,
            'status_code': result.get('status_code') if isinstance(result, dict) else None,
        }
        delivery.save(update_fields=['platform_data'])
        return safe['success']
