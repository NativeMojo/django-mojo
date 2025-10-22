"""
Simple push notification helpers.

Core logic is in RegisteredDevice.send() and User.push_notification().
These are just convenience functions for common use cases.
"""

from mojo.apps.account.models import User, RegisteredDevice


def send_to_user(user, title=None, body=None, data=None, category="general", action_url=None):
    """
    Send push notification to a single user's devices.

    Usage:
        send_to_user(user, "Hello", "Your order is ready")
        send_to_user(user, data={"action": "sync"})  # Silent notification

    Returns:
        List of NotificationDelivery objects
    """
    return user.push_notification(
        title=title,
        body=body,
        data=data,
        category=category,
        action_url=action_url
    )


def send_to_users(user_ids, title=None, body=None, data=None, category="general", action_url=None):
    """
    Send push notification to multiple users.

    Usage:
        send_to_users([1, 2, 3], "Alert", "System maintenance in 5 minutes")
        send_to_users([1, 2, 3], data={"refresh": True})

    Returns:
        List of NotificationDelivery objects
    """
    users = User.objects.filter(id__in=user_ids)

    deliveries = []
    for user in users:
        user_deliveries = user.push_notification(
            title=title,
            body=body,
            data=data,
            category=category,
            action_url=action_url
        )
        deliveries.extend(user_deliveries)

    return deliveries


def send_to_device(device_id, title=None, body=None, data=None, category="general", action_url=None):
    """
    Send push notification to a specific device.

    Usage:
        send_to_device(device_id, "Hello", "Message just for this device")

    Returns:
        NotificationDelivery object or None
    """
    try:
        device = RegisteredDevice.objects.get(id=device_id, is_active=True, push_enabled=True)
        return device.send(
            title=title,
            body=body,
            data=data,
            category=category,
            action_url=action_url
        )
    except RegisteredDevice.DoesNotExist:
        return None
