from mojo.helpers.settings import settings
from mojo.helpers import logit, dates
from mojo.apps.account.models import (
    PushConfig, RegisteredDevice, NotificationTemplate,
    NotificationDelivery, User
)



# Optional imports - will be imported only if needed
try:
    from pyfcm import FCMNotification
    HAS_FCM = True
except ImportError:
    HAS_FCM = False
    logit.warn("pyfcm not installed - FCM notifications disabled")

try:
    from apns2.client import APNsClient
    from apns2.payload import Payload
    from apns2.credentials import TokenCredentials
    HAS_APNS = True
except ImportError:
    HAS_APNS = False
    logit.warn("apns2 not installed - APNS notifications disabled")


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

        Returns:
            List of NotificationDelivery objects
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
            logit.info(f"No devices to send to for template {template_name or 'direct'}")
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
        """Send notification to a specific device using template."""
        title, body, action_url = template.render(context)

        delivery = NotificationDelivery.objects.create(
            user=device.user,
            device=device,
            template=template,
            title=title,
            body=body,
            category=template.category,
            action_url=action_url
        )

        self._attempt_delivery(delivery, device, title, body, template)
        return delivery

    def _send_direct(self, device, title, body, category, action_url=None):
        """Send direct notification without template."""
        delivery = NotificationDelivery.objects.create(
            user=device.user,
            device=device,
            title=title,
            body=body,
            category=category,
            action_url=action_url
        )

        self._attempt_delivery(delivery, device, title, body, None)
        return delivery

    def _attempt_delivery(self, delivery, device, title, body, template):
        """Attempt to deliver notification to device."""
        try:
            success = False

            # Test mode - fake delivery for development/testing
            if self.config.test_mode:
                success = self._send_test(delivery, device, title, body, template)

            # FCM is primary - supports both iOS and Android
            elif self.config.fcm_enabled:
                success = self._send_fcm(delivery, device, title, body, template)

            # APNS fallback for iOS only (rarely needed)
            elif device.platform == 'ios' and self.config.apns_enabled:
                success = self._send_apns(delivery, device, title, body, template)

            else:
                error_msg = "No push service configured"
                if not self.config.fcm_enabled and not self.config.apns_enabled:
                    error_msg = "No push services enabled in config"
                elif device.platform not in ['ios', 'android', 'web']:
                    error_msg = f"Unsupported platform: {device.platform}"
                delivery.mark_failed(error_msg)
                return

            if success:
                delivery.mark_sent()
            else:
                delivery.mark_failed("Platform delivery failed")

        except Exception as e:
            error_msg = f"Push notification failed: {str(e)}"
            logit.error(error_msg)
            delivery.mark_failed(error_msg)

    def _send_apns(self, delivery, device, title, body, template):
        """Send APNS notification to iOS device."""
        if not HAS_APNS:
            logit.error("APNS support not available - apns2 package not installed")
            return False

        try:
            credentials = TokenCredentials(
                auth_key=self.config.get_decrypted_apns_key(),
                auth_key_id=self.config.apns_key_id,
                team_id=self.config.apns_team_id
            )

            client = APNsClient(credentials=credentials,
                              use_sandbox=self.config.apns_use_sandbox)

            # Build payload
            payload = Payload(
                alert={'title': title, 'body': body},
                sound=self.config.default_sound,
                badge=self.config.default_badge_count
            )

            # Add action URL if present
            if delivery.action_url:
                payload.custom = {'action_url': delivery.action_url}

            # Send notification
            response = client.send_notification(
                device.device_token,
                payload,
                self.config.apns_bundle_id
            )

            # Store platform response data
            delivery.platform_data = {
                'apns_id': response.id if hasattr(response, 'id') else None,
                'status': response.status if hasattr(response, 'status') else 'sent'
            }
            delivery.save(update_fields=['platform_data'])

            return True

        except Exception as e:
            logit.error(f"APNS send failed: {e}")
            return False

    def _send_fcm(self, delivery, device, title, body, template):
        """Send FCM notification to device (supports both iOS and Android)."""
        if not HAS_FCM:
            logit.error("FCM support not available - pyfcm package not installed")
            return False

        try:
            push_service = FCMNotification(api_key=self.config.get_decrypted_fcm_key())

            # Build data payload
            data_message = {}
            if delivery.action_url:
                data_message['action_url'] = delivery.action_url

            result = push_service.notify_single_device(
                registration_id=device.device_token,
                message_title=title,
                message_body=body,
                sound=self.config.default_sound,
                data_message=data_message if data_message else None
            )

            # Store platform response data
            delivery.platform_data = {
                'multicast_id': result.get('multicast_id'),
                'success': result.get('success', 0),
                'failure': result.get('failure', 0),
                'results': result.get('results', [])
            }
            delivery.save(update_fields=['platform_data'])

            return result.get('success', 0) > 0

        except Exception as e:
            logit.error(f"FCM send failed: {e}")
            return False

    def _send_test(self, delivery, device, title, body, template):
        """Send fake notification for testing - always succeeds."""
        logit.info(f"TEST MODE: Fake notification to {device.platform} device '{device.device_name}' - {title}: {body}")

        # Store fake test data
        delivery.platform_data = {
            'test_mode': True,
            'platform': device.platform,
            'device_name': device.device_name,
            'timestamp': dates.utcnow().isoformat(),
            'fake_delivery': 'success'
        }
        delivery.save(update_fields=['platform_data'])

        return True


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
