"""
Simple push notification helpers.

Core logic is in RegisteredDevice.send() and User.push_notification().
These are just convenience functions for common use cases.
"""

from mojo.apps.account.models import User, RegisteredDevice, PushConfig


class PushRegistrationConflict(Exception):
    """A concurrent registration prevented reconciliation; the caller can retry."""


def register_push_device(user, *, device_id, device_token, platform, fields,
                         request=None, _before_write=None):
    """Atomically replace stale token holders with the current app registration."""
    from django.db import IntegrityError, transaction
    from django.db.models import Q

    defaults = {key: fields[key] for key in (
        'device_name', 'app_version', 'os_version', 'push_preferences') if key in fields}
    defaults.update(device_token=device_token, platform=platform,
                    is_active=True, push_enabled=True)
    for attempt in (1, 2):
        try:
            with transaction.atomic():
                # Lock the caller row together with token holders in a single
                # order: opposite token swaps otherwise lock each other's row.
                locked = list(RegisteredDevice.objects.select_for_update().filter(
                    Q(device_token=device_token) | Q(user=user, device_id=device_id)
                ).order_by('pk'))
                stale = [previous for previous in locked
                         if previous.device_token == device_token
                         and not (previous.user_id == user.pk and previous.device_id == device_id)]
                moves = []
                same_user = False
                for previous in stale:
                    facts = dict(previous_user_id=previous.user_id,
                                 previous_device_pk=previous.pk,
                                 previous_device_id=previous.device_id,
                                 platform=previous.platform,
                                 deliveries_removed=previous.notification_deliveries.count())
                    if previous.user_id != user.pk:
                        moves.append(facts)
                    else:
                        same_user = True
                    previous.delete()
                if _before_write is not None:
                    _before_write()
                device, created = RegisteredDevice.objects.update_or_create(
                    user=user, device_id=device_id, defaults=defaults)
                for facts in moves:
                    transaction.on_commit(lambda facts=facts: user.report_incident(
                        'Push token moved from another account', 'push:token_reassigned',
                        request=request, **facts))
                if same_user:
                    transaction.on_commit(lambda: device.log('Push registration moved to current device ID'))
            return device, created
        except IntegrityError:
            if attempt == 2:
                raise PushRegistrationConflict('Push registration changed concurrently; retry.') from None


TEST_ERRORS = {
    'missing_credentials': 'No usable FCM service account is stored.',
    'invalid_credentials': 'The stored FCM service account is invalid.',
    'authentication_failed': 'FCM authentication failed. Check the service account and private key.',
    'provider_unavailable': 'FCM could not be reached. Connection was not verified.',
    'acceptance_unknown': 'FCM acceptance is unknown. Check the device before sending again.',
    'invalid_provider_response': 'FCM returned an unrecognized response. Acceptance was not confirmed.',
    'provider_rejected': 'FCM rejected the request.',
    'test_mode': 'This configuration simulates sends. Turn off test mode to send a real test push.',
    'no_config': 'No active push configuration applies to this user.',
    'inactive_device': 'This device registration is inactive.',
    'push_disabled': 'Push notifications are disabled for this device.',
    'category_disabled': 'This device has disabled test notifications.',
    'missing_token': 'This device has no registration token. Register it again from the app.',
    'INVALID_ARGUMENT': 'FCM rejected the notification or device token as invalid.',
    'UNREGISTERED': 'The device token is no longer registered. Open the app to register again.',
    'SENDER_ID_MISMATCH': 'The device token belongs to a different Firebase project.',
    'QUOTA_EXCEEDED': 'The FCM sending quota was exceeded.',
    'UNAVAILABLE': 'FCM is temporarily unavailable.',
    'INTERNAL': 'FCM reported an internal error.',
    'THIRD_PARTY_AUTH_ERROR': 'FCM could not authenticate with the platform push service. Check APNs or Web Push credentials.',
    'PERMISSION_DENIED': 'The service account cannot send for this Firebase project. Check its permission and enabled API.',
    'UNAUTHENTICATED': 'FCM rejected the authentication credentials.',
    'NOT_FOUND': 'FCM could not find the project or target.',
}


def provider_test_result(result, validation=False):
    """Project provider evidence into a safe, explicit operator result."""
    result = result if isinstance(result, dict) else {}
    accepted = (result.get('success') is True and result.get('status_code') == 200
                and isinstance(result.get('message_id'), str)
                and result['message_id'].startswith('projects/'))
    outcome = 'validated' if validation else 'accepted'
    if accepted:
        return {'success': True, 'outcome': outcome, 'error_code': None,
                'message': 'FCM authenticated and validated this project. No notification was delivered.' if validation
                else 'FCM accepted the notification. Confirm receipt on the device.',
                'message_id': result['message_id'][:500]}
    outcome = result.get('outcome')
    if outcome not in ('blocked', 'rejected', 'unknown'):
        outcome = 'blocked' if validation else 'unknown'
    code = result.get('error_code')
    if not isinstance(code, str) or code not in TEST_ERRORS:
        code = 'invalid_provider_response'
    return {'success': False, 'outcome': outcome, 'error_code': code,
            'message': TEST_ERRORS[code], 'message_id': None}


def device_test_readiness(device, config):
    """Local eligibility only. This does not contact FCM."""
    code = None
    if not device.is_active:
        code = 'inactive_device'
    elif not device.push_enabled:
        code = 'push_disabled'
    elif not (device.push_preferences or {}).get('test', True):
        code = 'category_disabled'
    elif not device.device_token:
        code = 'missing_token'
    elif config is None or not config.is_active:
        code = 'no_config'
    elif config.test_mode:
        code = 'test_mode'
    elif not config.get_fcm_service_account():
        code = 'missing_credentials'
    return {
        'ready': code is None, 'error_code': code,
        'message': TEST_ERRORS[code] if code else 'Ready to attempt a real push. Device receipt is not yet verified.',
        'device_id': device.pk,
        'config': None if config is None else {
            'id': config.pk, 'name': config.name,
            'group': {'id': config.group_id, 'name': config.group.name} if config.group_id else None,
            'fcm_project_id': config.fcm_project_id,
            'has_fcm_credentials': config.has_fcm_credentials,
            'test_mode': config.test_mode, 'is_active': config.is_active,
        },
    }


def test_registered_device(device, title, message, client_factory=None):
    """Resolve once, then reuse the normal delivery path with simulation forbidden."""
    config = PushConfig.get_for_user(device.user)
    readiness = device_test_readiness(device, config)
    if not readiness['ready']:
        return dict(readiness, success=False, outcome='blocked', delivery_id=None)
    delivery = device.send(title=title, body=message, category='test', config=config,
                           require_live=True, client_factory=client_factory)
    if delivery is None:
        return dict(readiness, success=False, outcome='blocked', delivery_id=None,
                    message='Device eligibility changed. Refresh before trying again.')
    result = provider_test_result(delivery.platform_data)
    return dict(readiness, **result, delivery_id=delivery.pk)


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
