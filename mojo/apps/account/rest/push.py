import mojo.decorators as md
from mojo.apps.account.models import (
    RegisteredDevice, NotificationTemplate, PushConfig,
    NotificationDelivery
)
from mojo.apps.account.services.push import (
    send_push_notification, send_direct_notification
)
from mojo.helpers import response


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

    return device.on_rest_get(request, 'default')


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
    template = request.DATA.get('template')
    title = request.DATA.get('title')
    body = request.DATA.get('body')

    if template:
        # Templated sending
        context = request.DATA.get('context', {})
        user_ids = request.DATA.get('user_ids')
        results = send_push_notification(
            user=request.user,
            template_name=template,
            context=context,
            user_ids=user_ids
        )
    elif title and body:
        # Direct sending
        category = request.DATA.get('category', 'general')
        action_url = request.DATA.get('action_url')
        user_ids = request.DATA.get('user_ids')
        results = send_direct_notification(
            user=request.user,
            title=title,
            body=body,
            category=category,
            action_url=action_url,
            user_ids=user_ids
        )
    else:
        return response.error('Must provide either template or both title and body')

    return response.success({
        'success': True,
        'sent_count': len([r for r in results if r.status == 'sent']),
        'failed_count': len([r for r in results if r.status == 'failed']),
        'deliveries': [r.to_dict("basic") for r in results]
    })


@md.POST('account/devices/push/test')
@md.requires_auth()
def test_push_config(request):
    """
    Test push configuration by sending a test notification to requesting user's devices.

    POST /api/account/devices/push/test
    {
        "message": "Custom test message" # optional
    }
    """
    test_message = request.DATA.get('message', 'This is a test notification')

    results = send_direct_notification(
        user=request.user,
        title="Push Test",
        body=test_message,
        category="test"
    )

    if not results:
        return response.error('No registered devices found for testing')

    return response.success({
        'success': True,
        'message': 'Test notifications sent',
        'results': [r.to_dict('basic') for r in results]
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
