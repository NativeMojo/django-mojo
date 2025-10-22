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
        VIEW_PERMS = ["view_devices", "manage_devices", "owner", "manage_users"]
        SAVE_PERMS = ["manage_devices", "owner"]
        SEARCH_FIELDS = ["device_name", "device_id"]
        LIST_DEFAULT_FILTERS = {"is_active": True}
        GRAPHS = {
            "basic": {
                "fields": ["id", "device_id", "platform", "device_name", "push_enabled", "last_seen"]
            },
            "default": {
                "fields": ["id", "device_id", "platform", "device_name", "app_version",
                          "os_version", "push_enabled", "push_preferences", "last_seen"],
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

    def send(self, title=None, body=None, data=None, category="general", action_url=None):
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
        config = PushConfig.get_for_user(self.user)
        if not config:
            logit.info(f"No push config available for user {self.user.username}")
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
            success = self._send_fcm(delivery, config)
            if success:
                metrics.record("push_sent")
                delivery.mark_sent()
            else:
                metrics.record("push_failed")
                delivery.mark_failed("FCM delivery failed")

        except Exception as e:
            error_msg = f"Push notification failed: {str(e)}"
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

    def _send_fcm(self, delivery, config):
        """Send via FCM v1 API."""
        from mojo.helpers import logit
        from mojo.helpers.fcm import FCMv1Client

        try:
            service_account_json = config.get_fcm_service_account()
            if not service_account_json:
                logit.error("No FCM service account configured")
                return False

            # Initialize FCM v1 client
            try:
                fcm_client = FCMv1Client(service_account_json)
            except Exception as e:
                logit.error(f"Failed to initialize FCM client: {e}")
                return False

            # Build data payload
            data_message = delivery.data_payload.copy() if delivery.data_payload else {}
            if delivery.action_url:
                data_message['action_url'] = delivery.action_url

            # Send notification via FCM v1
            result = fcm_client.send(
                token=self.device_token,
                title=delivery.title,
                body=delivery.body,
                data=data_message if data_message else None,
                sound=config.default_sound if (delivery.title or delivery.body) else None
            )

            # Store response
            delivery.platform_data = {
                'fcm_version': 'v1',
                'message_id': result.get('message_id'),
                'success': result.get('success', False),
                'status_code': result.get('status_code'),
                'error': result.get('error')
            }
            delivery.save(update_fields=['platform_data'])

            return result.get('success', False)

        except Exception as e:
            logit.error(f"FCM send failed: {e}")
            return False
