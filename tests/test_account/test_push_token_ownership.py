"""#9109: authenticated token ownership, rollback and REST confidentiality."""
import json
import uuid
from concurrent.futures import ThreadPoolExecutor
from testit import helpers as th

PREFIX = 'pto_'
PASSWORD = 'PushToken##2026'


@th.django_unit_setup()
def setup_ownership(opts):
    from mojo.apps.account.models import User, Group, PushConfig
    User.objects.filter(username__startswith=PREFIX).delete()
    Group.objects.filter(name__startswith=PREFIX).delete()
    group = Group.objects.create(name=PREFIX + uuid.uuid4().hex, kind='organization')
    opts.pto_users = []
    for label in ('a', 'b', 'c', 'viewer'):
        user = User.objects.create(username=PREFIX + label + uuid.uuid4().hex,
                                   display_name=label, org=group, is_active=True)
        user.save_password(PASSWORD)
        user.save()
        opts.pto_users.append(user)
    viewer = opts.pto_users[-1]
    viewer.add_permission('view_devices')
    viewer.save()
    PushConfig.objects.create(name=group.name, group=group, test_mode=True)


def row(user, token=None, platform='ios', **kwargs):
    from mojo.apps.account.models import RegisteredDevice
    return RegisteredDevice.objects.create(user=user, device_token=token or uuid.uuid4().hex,
        device_id=PREFIX + uuid.uuid4().hex, platform=platform, **kwargs)


def register(opts, user, token, device_id=None, legacy=False):
    from mojo.decorators.limits import clear_rate_limits
    clear_rate_limits(ip='127.0.0.1', key='login')
    assert opts.client.login(user.username, PASSWORD), 'Test account must authenticate'
    device_id = device_id or PREFIX + uuid.uuid4().hex
    payload = {'cmf_token' if legacy else 'device_token': token, 'device_id': device_id}
    if not legacy:
        payload['platform'] = 'ios'
    path = 'member/device/register' if legacy else 'devices/push/register'
    result = opts.client.post('/api/account/' + path, json=payload)
    assert result.status_code == 200, f'Registration must succeed, got {result.status_code}'
    from mojo.apps.account.models import RegisteredDevice
    holders = list(RegisteredDevice.objects.filter(device_token=token))
    assert len(holders) == 1 and holders[0].user_id == user.pk, 'Only current account may hold token'
    assert holders[0].device_id == device_id, 'Token must follow current app identifier'
    assert holders[0].is_active and holders[0].push_enabled, 'Registration must reactivate pushes'
    return holders[0]


@th.django_unit_test()
def test_cross_account(opts):
    from mojo.apps.account.models import RegisteredDevice
    a, b, *_ = opts.pto_users
    old, other = row(b), row(b)
    new = register(opts, a, old.device_token)
    assert not RegisteredDevice.objects.filter(pk=old.pk).exists(), 'Stale registration must be removed'
    assert RegisteredDevice.objects.filter(pk=other.pk).exists(), 'Other phones must remain untouched'


@th.django_unit_test()
def test_same_account_changed_identifier(opts):
    old = row(opts.pto_users[0])
    register(opts, old.user, old.device_token)


@th.django_unit_test()
def test_existing_caller_row_keeps_pk(opts):
    a, b, *_ = opts.pto_users
    own, old = row(a, is_active=False, push_enabled=False), row(b)
    new = register(opts, a, old.device_token, own.device_id)
    assert new.pk == own.pk, 'Existing caller device must retain its primary key'


@th.django_unit_test()
def test_legacy_cross_account(opts):
    a, b, *_ = opts.pto_users
    token = uuid.uuid4().hex
    old = register(opts, b, token, legacy=True)
    new = register(opts, a, token, legacy=True)
    assert new.device_name == f'{a.display_name} {old.platform.title()}' or (
        new.device_name.startswith(a.display_name + ' ') and '{platform}' not in new.device_name
    ), 'Legacy device name must contain actual platform'


@th.django_unit_test()
def test_legacy_changed_identifier(opts):
    a = opts.pto_users[0]
    token = uuid.uuid4().hex
    register(opts, a, token, legacy=True)
    register(opts, a, token, legacy=True)


@th.django_unit_test()
def test_legacy_missing_token(opts):
    register(opts, opts.pto_users[0], uuid.uuid4().hex)
    result = opts.client.post('/api/account/member/device/register', json={'device_id': PREFIX + uuid.uuid4().hex})
    assert result.status_code == 400, 'Missing legacy token must return 400'


@th.django_unit_test()
def test_cross_platform(opts):
    a, b, *_ = opts.pto_users
    old = row(b, platform='mac os x')
    register(opts, a, old.device_token)


@th.django_unit_test()
def test_previous_owner_no_delivery_and_audit(opts):
    from mojo.apps.account.models import RegisteredDevice, NotificationDelivery
    from mojo.apps.account.services.push import send_to_device
    from mojo.apps.incident.models import Event
    a, b, *_ = opts.pto_users
    # Keep this case independent of earlier cases that created other phones.
    RegisteredDevice.objects.filter(user__in=[a, b]).delete()
    old = row(b)
    NotificationDelivery.objects.create(user=b, device=old, title='Old', category='general')
    new = register(opts, a, old.device_token)
    assert b.push_notification(title='Previous owner') == [], 'Old account must have no eligible device'
    assert send_to_device(old.pk, title='Old pk') is None, 'Old device ID must no longer deliver'
    assert not NotificationDelivery.objects.filter(user=b).exists(), 'Removed phone history must not point at new owner'
    deliveries = a.push_notification(title='Current owner')
    assert len(deliveries) == 1 and deliveries[0].device_id == new.pk, 'Only current account must deliver'
    events = list(Event.objects.filter(category='push:token_reassigned', model_id=a.pk))
    matching = [e for e in events if str(old.pk) in json.dumps(e.metadata)]
    assert matching, 'Cross-account takeover must leave an audit event'
    assert old.device_token not in json.dumps([e.metadata for e in matching]), 'Audit must omit token'


@th.django_unit_test()
def test_insert_race_retries(opts):
    from django.db import connections
    from mojo.apps.account.services.push import register_push_device
    from mojo.apps.account.models import RegisteredDevice
    a, _, c, _ = opts.pto_users
    token = uuid.uuid4().hex
    calls = []
    def hook():
        calls.append(1)
        if len(calls) == 1:
            def insert():
                try:
                    row(c, token)
                finally:
                    connections.close_all()
            with ThreadPoolExecutor(max_workers=1) as pool:
                pool.submit(insert).result(timeout=10)
    device, _ = register_push_device(a, device_id=PREFIX + uuid.uuid4().hex,
        device_token=token, platform='ios', fields={}, _before_write=hook)
    assert len(calls) == 2, 'Committed concurrent insert must trigger one retry'
    assert list(RegisteredDevice.objects.filter(device_token=token).values_list('pk', flat=True)) == [device.pk], 'Retry must leave one current holder'


@th.django_unit_test()
def test_exhausted_conflict_rolls_back(opts):
    from django.db import IntegrityError
    from mojo.apps.account.services.push import register_push_device, PushRegistrationConflict
    from mojo.apps.account.models import RegisteredDevice
    a, b, *_ = opts.pto_users
    old = row(b)
    calls = []
    def conflict():
        calls.append(1)
        raise IntegrityError('synthetic concurrent conflict')
    try:
        register_push_device(a, device_id=PREFIX + uuid.uuid4().hex,
            device_token=old.device_token, platform='ios', fields={}, _before_write=conflict)
    except PushRegistrationConflict:
        pass
    else:
        assert False, 'Persistent collision must be a controlled conflict'
    assert len(calls) == 2, 'Retry must be bounded to two attempts'
    assert RegisteredDevice.objects.filter(pk=old.pk).exists(), 'Failed takeover must restore original registration'


@th.django_unit_test()
def test_full_graph_hides_token(opts):
    a, _, _, viewer = opts.pto_users
    device = row(a)
    register(opts, viewer, uuid.uuid4().hex)
    result = opts.client.get('/api/account/devices/push', params={'graph': 'full', 'search': PREFIX})
    assert result.status_code == 200, 'Authorized device viewer must list full graph'
    assert result.response.data, 'Fixture devices must be returned'
    assert all('device_token' not in d for d in result.response.data), 'No full graph may disclose token'
    assert 'device_token' not in device.to_dict('full'), 'Owner graph must also omit token'
