import mojo.decorators as md
from mojo.apps.account.models import (
    RegisteredDevice, NotificationTemplate, PushConfig,
    NotificationDelivery, User
)
from mojo.apps.account.services.push import send_to_user, send_to_users
from mojo.helpers import response
from mojo.helpers import request as rhelper

@md.POST('account/devices/push/register')
@md.requires_auth()
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
        "os_version": "...",
        "push_preferences": {"orders": true, "marketing": false}
    }
    """
    device, created = RegisteredDevice.objects.update_or_create(
        user=request.user,
        device_id=request.DATA.get('device_id'),
        defaults={
            'device_token': request.DATA.get('device_token'),
            'platform': request.DATA.get('platform'),
            'device_name': request.DATA.get('device_name', ''),
            'app_version': request.DATA.get('app_version', ''),
            'os_version': request.DATA.get('os_version', ''),
            'push_preferences': request.DATA.get('push_preferences', {}),
            'is_active': True,
            'push_enabled': True
        }
    )

    if not created:
        if not device.push_enabled:
            device.is_active = True
            device.push_enabled = True
            device.save()

    return device.on_rest_get(request, 'default')


@md.POST('account/member/device/register')
@md.requires_auth()
def register_legacy_device(request):
    device_id = request.DATA.get("device_id", request.duid)
    if not device_id:
        device_id = request.device.id
    cmf_token = request.DATA.get(["cmf_token", "cm_token"])
    meta = request.DATA.get("device_metadata", {})
    ua_info = rhelper.parse_user_agent(request.user_agent)
    platform = ua_info.os.family
    device, created = RegisteredDevice.objects.update_or_create(
        user=request.user,
        device_id=device_id,
        defaults={
            'device_token': cmf_token,
            'platform': platform.lower(),
            'device_name': f"{request.user.display_name} {{platform}}",
            'app_version': meta.get('app_version', ''),
            'os_version': meta.get('os_version', ''),
            'push_preferences': request.DATA.get('push_preferences', {}),
            'is_active': True,
            'push_enabled': True
        }
    )

    if not created:
        if not device.push_enabled:
            device.is_active = True
            device.push_enabled = True
            device.save()
    return {"status": True}


@md.POST('account/devices/push/unregister')
@md.requires_auth()
@md.requires_params(['device_token', 'device_id', 'platform'])
def unregister_device(request):
    device =RegisteredDevice.objects.filter(
        user=request.user, device_id=request.DATA.get('device_id')).last()
    if device and device.push_enabled:
        device.is_active = False
        device.push_enabled = False
        device.save()
    return {"status": True }


@md.URL('account/devices/push')
@md.URL('account/devices/push/<int:pk>')
def on_registered_devices(request, pk=None):
    """Standard CRUD for registered devices."""
    return RegisteredDevice.on_rest_request(request, pk)


@md.URL('account/devices/push/templates')
@md.URL('account/devices/push/templates/<int:pk>')
def on_notification_templates(request, pk=None):
    """Standard CRUD for notification templates."""
    return NotificationTemplate.on_rest_request(request, pk)


@md.URL('account/devices/push/config')
@md.URL('account/devices/push/config/<int:pk>')
def on_push_config(request, pk=None):
    """Standard CRUD for push configuration."""
    return PushConfig.on_rest_request(request, pk)


@md.URL('account/devices/push/deliveries')
@md.URL('account/devices/push/deliveries/<int:pk>')
def on_notification_deliveries(request, pk=None):
    """Standard CRUD for notification delivery history."""
    return NotificationDelivery.on_rest_request(request, pk)


@md.POST('account/devices/push/send')
@md.requires_auth()
@md.requires_global_perms("send_notifications", "comms")
def send_notification(request):
    """
    Send push notification directly.

    POST /api/account/devices/push/send

    Direct:
    {
        "title": "Hello!",
        "body": "Your order is ready",
        "category": "orders",
        "action_url": "myapp://orders/123",
        "data": {"order_id": 123},
        "user_ids": [1, 2, 3]  # optional, defaults to requesting user
    }

    Silent (data-only):
    {
        "data": {"action": "sync", "timestamp": 123},
        "category": "system"
    }
    """
    title = request.DATA.get('title')
    body = request.DATA.get('body')
    data = request.DATA.get('data')
    category = request.DATA.get('category', 'general')
    action_url = request.DATA.get('action_url')
    user_ids = request.DATA.get('user_ids')

    # Must have at least title, body, or data
    if not (title or body or data):
        return response.error('Must provide title, body, or data')

    # Send to specific users or just the requesting user
    if user_ids:
        results = send_to_users(
            user_ids=user_ids,
            title=title,
            body=body,
            data=data,
            category=category,
            action_url=action_url
        )
    else:
        results = send_to_user(
            user=request.user,
            title=title,
            body=body,
            data=data,
            category=category,
            action_url=action_url
        )

    return response.success({
        'success': True,
        'sent_count': len([r for r in results if r and r.status == 'sent']),
        'failed_count': len([r for r in results if r and r.status == 'failed']),
        'deliveries': [r.to_dict("basic") for r in results if r]
    })


@md.requires_global_perms("send_notifications", "comms")
def _test_registered_device(request):
    from mojo.apps.account.services.push import device_test_readiness, test_registered_device
    value = request.DATA.get('device_id')
    if (isinstance(value, bool) or len(str(value)) > 19 or not str(value).isdigit()
            or not 1 <= int(value) <= 9223372036854775807):
        return response.error('device_id must be a positive registered-device ID')
    device = RegisteredDevice.objects.select_related('user__org').filter(pk=int(value)).first()
    if device is None or not RegisteredDevice.rest_check_permission(request, 'VIEW_PERMS', instance=device):
        return response.error('Registered device not found', status=404)
    if request.method == 'GET':
        return response.success(device_test_readiness(device, PushConfig.get_for_user(device.user)))
    title = request.DATA.get('title', 'Push Test')
    message = request.DATA.get('message', 'This is a test notification')
    if not isinstance(title, str) or not title.strip() or len(title) > 200:
        return response.error('Title must contain 1–200 characters')
    if not isinstance(message, str) or not message.strip() or len(message) > 1000:
        return response.error('Message must contain 1–1000 characters')
    return response.success(test_registered_device(device, title.strip(), message.strip()))


@md.GET('account/devices/push/test')
@md.POST('account/devices/push/test')
@md.requires_auth()
def test_push_config(request):
    """Test caller devices, or one registered device with explicit admin permission."""
    if request.DATA.get('device_id') is not None:
        return _test_registered_device(request)
    if request.method == 'GET':
        return response.error('device_id is required for readiness')
    if not rhelper.is_request_user(request) or rhelper.is_key_backed_session(request):
        return response.error('Permission denied', status=403)
    message = request.DATA.get('message', 'This is a test notification')
    if not isinstance(message, str) or not message.strip() or len(message) > 1000:
        return response.error('Message must contain 1–1000 characters')
    results = send_to_user(user=request.user, title="Push Test", body=message, category="test")
    if not results:
        return response.error('No eligible devices or push configuration found for testing')
    outcomes = [r.push_outcome for r in results if r]
    accepted = outcomes.count('accepted') + outcomes.count('delivered')
    simulated = outcomes.count('simulated')
    unknown = outcomes.count('unknown')
    failed = len(outcomes) - accepted - simulated - unknown
    return response.success({
        'success': accepted == len(outcomes) and accepted > 0,
        'message': f'{accepted} accepted by FCM, {failed} failed, {simulated} simulated, {unknown} unknown. Confirm receipt on the devices.',
        'sent_count': accepted, 'failed_count': failed, 'simulated_count': simulated,
        'unknown_count': unknown, 'results': [r.to_dict('basic') for r in results if r],
    })


@md.GET('account/devices/push/stats')
@md.requires_auth()
def push_stats(request):
    """
    Get push notification statistics for the requesting user.
    """
    user_deliveries = NotificationDelivery.objects.filter(user=request.user)

    stats = {
        'total_sent': user_deliveries.filter(status='sent').count(),
        'total_failed': user_deliveries.filter(status='failed').count(),
        'total_pending': user_deliveries.filter(status='pending').count(),
        'registered_devices': request.user.registered_devices.filter(is_active=True).count(),
        'enabled_devices': request.user.registered_devices.filter(
            is_active=True, push_enabled=True
        ).count()
    }

    return response.success(stats)


@md.POST('account/devices/push/config/<int:pk>/test')
@md.requires_auth()
@md.requires_global_perms("manage_push_config", "comms")
def test_push_config_connection(request, pk):
    """
    Test FCM configuration by attempting to send a test notification.

    POST /api/account/devices/push/config/123/test
    {
        "device_token": "optional_real_token_to_test_with"
    }
    """
    try:
        config = PushConfig.objects.get(id=pk)
    except PushConfig.DoesNotExist:
        return response.error('Push configuration not found', status=404)

    # Optional: test with a real device token
    test_token = request.DATA.get('device_token')

    if test_token is not None and (not isinstance(test_token, str) or len(test_token) > 4096):
        return response.error('device_token must be a string of at most 4096 characters')
    result = config.test_fcm_connection(test_token=test_token)

    if result['success']:
        return response.success(result)
    else:
        return response.JsonResponse({'status': False, 'error': result['message'], 'data': result}, status=400)
