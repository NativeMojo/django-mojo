"""Truthful operator push checks, without live provider traffic."""
import json
from testit import helpers as th

PREFIX = 'pushchecks_'
PASSWORD = 'PushChecks##mojo2026'


class Provider:
    def __init__(self, result):
        self.result = result
        self.calls = []
        self.accounts = []

    def factory(self, account):
        self.accounts.append(account)
        return self

    def validate(self):
        self.calls.append(('validate', {}))
        return self.result

    def send(self, **kwargs):
        self.calls.append(('send', kwargs))
        return self.result


def accepted():
    return {'success': True, 'status_code': 200, 'message_id': 'projects/pushchecks/messages/123'}


@th.django_unit_setup()
def setup_push_checks(opts):
    from mojo.apps.account.models import User, Group, PushConfig, RegisteredDevice
    User.objects.filter(username__startswith=PREFIX).delete()
    Group.objects.filter(name__startswith=PREFIX).delete()
    PushConfig.objects.filter(name__startswith=PREFIX).delete()
    group = Group.objects.create(name=PREFIX + 'org', kind='organization')
    opts.push_owner = User.objects.create(username=PREFIX + 'owner', org=group, is_active=True)
    opts.push_admin = User.objects.create(username=PREFIX + 'admin', is_active=True)
    opts.push_admin.save_password(PASSWORD)
    opts.push_admin.add_permission(['send_notifications', 'view_devices', 'manage_push_config'])
    opts.push_admin.save()
    opts.push_sender = User.objects.create(username=PREFIX + 'sender', is_active=True)
    opts.push_sender.save_password(PASSWORD)
    opts.push_sender.add_permission('send_notifications')
    opts.push_sender.save()
    opts.push_config = PushConfig.objects.create(name=PREFIX + 'config', group=group, test_mode=True)
    opts.push_config.set_fcm_service_account({'project_id': 'pushchecks', 'client_email': 'checks@example.test',
                                             'private_key': 'secret-canary'})
    opts.push_config.save()
    opts.push_device = RegisteredDevice.objects.create(user=opts.push_owner, device_id=PREFIX + 'device',
                                                       device_token=PREFIX + 'token-secret', platform='ios')


@th.django_unit_test()
def test_simulation_is_not_credential_verification(opts):
    from mojo.apps.account.models import PushConfig
    result = PushConfig(name='pushchecks empty', test_mode=True).test_fcm_connection()
    assert result['success'] is False, 'Test mode must not turn missing credentials into verified success'


@th.django_unit_test()
def test_malformed_credentials_do_not_crash_metadata(opts):
    from mojo.apps.account.models import PushConfig
    config = PushConfig(name='pushchecks malformed')
    config.set_fcm_service_account('[1, 2]')
    assert config.fcm_project_id is None, 'Non-object credentials must not crash project metadata'


@th.django_unit_test()
def test_connection_contacts_provider_in_simulation_mode(opts):
    provider = Provider(accepted())
    result = opts.push_config.test_fcm_connection(client_factory=provider.factory)
    assert provider.calls == [('validate', {})], 'Connection check must validate with FCM even in simulation mode'
    assert result['success'] is True and result['outcome'] == 'validated', 'Real validation should be explicit'
    assert result['validation_only'] is True and result['test_mode'] is True, 'Validation and send mode are separate facts'


@th.django_unit_test()
def test_rejected_and_malformed_verdicts_fail_closed(opts):
    from mojo.apps.account.services.push import provider_test_result
    for raw in [{}, None, {'success': True}, {'success': 'yes', **{k:v for k,v in accepted().items() if k != 'success'}},
                {'success': False, 'outcome': 'rejected', 'status_code': 400, 'error_code': 'INVALID_ARGUMENT',
                 'error': {'message': 'invalid secret-canary'}}]:
        result = provider_test_result(raw, validation=True)
        assert result['success'] is False, 'Absent/malformed success or provider rejection must not validate credentials'
        assert 'secret-canary' not in json.dumps(result), 'Provider error bodies must not be exposed'
    result = opts.push_config.test_fcm_connection(client_factory=Provider({
        'success': False, 'outcome': 'rejected', 'error_code': 'INVALID_ARGUMENT', 'status_code': 400,
    }).factory)
    assert result['success'] is False, 'Invalid argument is never evidence of valid credentials'


@th.django_unit_test()
def test_real_device_test_is_single_target_and_pins_config(opts):
    from mojo.apps.account.services.push import test_registered_device
    config = opts.push_config
    config.test_mode = False
    config.save()
    try:
        provider = Provider(accepted())
        result = test_registered_device(opts.push_device, 'Check', 'Please confirm receipt', provider.factory)
        assert result['success'] is True and result['outcome'] == 'accepted', 'Provider acceptance must be reported'
        assert len(provider.calls) == 1 and provider.calls[0][1]['token'] == opts.push_device.device_token, 'Send only to selected token'
        assert result['config']['id'] == config.pk, 'Report the configuration actually used'
        assert result['delivery_id'], 'Every attempted send needs a delivery reference'
        assert 'token-secret' not in json.dumps(result) and 'secret-canary' not in json.dumps(result), 'Safe test response must not contain secrets'
    finally:
        config.test_mode = True
        config.save()


@th.django_unit_test()
def test_readiness_blocks_simulation_disabled_and_opted_out_devices(opts):
    from mojo.apps.account.services.push import device_test_readiness, test_registered_device
    device, config = opts.push_device, opts.push_config
    provider = Provider(accepted())
    result = test_registered_device(device, 'Check', 'Message', provider.factory)
    assert result['outcome'] == 'blocked' and result['error_code'] == 'test_mode', 'Device test must never simulate success'
    assert not provider.calls, 'Simulation mode must not contact FCM for device test'
    for field, value, code in [('is_active', False, 'inactive_device'), ('push_enabled', False, 'push_disabled'),
                               ('push_preferences', {'test': False}, 'category_disabled'), ('device_token', '', 'missing_token')]:
        old = getattr(device, field)
        setattr(device, field, value)
        try:
            assert device_test_readiness(device, config)['error_code'] == code, f'{field} must block the real send'
        finally:
            setattr(device, field, old)


@th.django_unit_test()
def test_unknown_and_rejected_delivery_evidence(opts):
    from mojo.apps.account.services.push import test_registered_device
    from mojo.apps.account.models import NotificationDelivery
    config = opts.push_config
    config.test_mode = False
    config.save()
    try:
        for outcome, code, status in [('unknown', 'acceptance_unknown', 'pending'), ('rejected', 'UNREGISTERED', 'failed')]:
            result = test_registered_device(opts.push_device, 'Check', 'Message', Provider({
                'success': False, 'outcome': outcome, 'error_code': code,
            }).factory)
            delivery = NotificationDelivery.objects.get(pk=result['delivery_id'])
            assert result['success'] is False and result['outcome'] == outcome, 'Preserve ambiguous vs rejected outcome'
            assert delivery.status == status and delivery.push_outcome == outcome, 'Unknown acceptance must not become failed or sent'
    finally:
        config.test_mode = True
        config.save()


@th.django_unit_test()
def test_http_permissions_readiness_and_config_failure(opts):
    path = '/api/account/devices/push/test'
    opts.client.logout()
    result = opts.client.post(path, json={'device_id': opts.push_device.pk})
    assert result.status_code in (401, 403), 'Anonymous device tests must be denied'
    opts.client.login(opts.push_sender.username, PASSWORD)
    result = opts.client.post(path, json={'device_id': opts.push_device.pk})
    assert result.status_code == 404, 'Send permission must not grant another user device inspection'
    opts.client.login(opts.push_admin.username, PASSWORD)
    result = opts.client.get(path, params={'device_id': opts.push_device.pk})
    assert result.status_code == 200 and result.response.data.ready is False, 'Readiness should explain simulation blocker'
    assert result.response.data.config.id == opts.push_config.pk, 'Org config must be selected'
    assert 'token-secret' not in json.dumps(result.response) and 'secret-canary' not in json.dumps(result.response), 'Readiness is a safe projection'
    result = opts.client.post(path, json={'device_id': opts.push_device.pk})
    assert result.status_code == 200 and result.response.data.success is False, 'A simulated device send is a handled blocked result'
    assert result.response.data.error_code == 'test_mode', 'Simulation must be explicit'
    result = opts.client.post(path, json={'device_id': True})
    assert result.status_code == 400, 'Boolean IDs are invalid'
    result = opts.client.post(path, json={'device_id': opts.push_device.pk, 'title': 'x' * 201})
    assert result.status_code == 400, 'Oversize titles must fail before send'
    from mojo.apps.account.models import PushConfig
    empty = PushConfig.objects.create(name=PREFIX + 'empty', test_mode=True, is_active=False)
    try:
        result = opts.client.post(f'/api/account/devices/push/config/{empty.pk}/test', json={})
        assert result.status_code == 400, 'Missing credentials must be a structured failure, not fake success or TypeError'
        assert result.response.data.success is False and result.response.data.error_code == 'missing_credentials', 'Failure must retain safe evidence'
    finally:
        empty.delete()


@th.django_unit_test()
def test_safe_graphs_and_org_precedence(opts):
    from mojo.apps.account.models import PushConfig
    config = opts.push_config
    assert PushConfig.get_for_user(opts.push_owner).pk == config.pk, 'Active org config must win over system defaults'
    row = config.to_dict('default')
    assert row['fcm_project_id'] == 'pushchecks' and row['has_fcm_credentials'] is True, 'Default graph must include safe metadata'
    assert row['fcm_client_email'] == 'checks@example.test', 'Account email helps identify saved credentials'
    assert 'secret-canary' not in json.dumps(row), 'Graph must not expose credential contents'
    assert opts.push_device.to_dict('default')['is_active'] is True, 'Default device graph must expose active state'


@th.django_unit_test()
def test_fcm_wire_validation_and_timeouts(opts):
    import requests
    from mojo.helpers.fcm import FCMv1Client
    class Reply:
        status_code = 200
        text = 'response'
        def json(self):
            return {'name': 'projects/pushchecks/messages/123'}
    class HTTP:
        def __init__(self):
            self.calls = []
            self.fail = False
        def post(self, url, **kwargs):
            self.calls.append((url, kwargs))
            if self.fail:
                raise requests.Timeout('secret-canary')
            return Reply()
    class Client(FCMv1Client):
        def _get_access_token(self):
            return 'test-access-token'
    http = HTTP()
    client = Client({'project_id': 'pushchecks'}, http=http)
    assert client.validate()['success'] is True, 'Well-formed provider validation should succeed'
    payload = http.calls[-1][1]
    assert payload['json']['validate_only'] is True and 'topic' in payload['json']['message'], 'Validation must never deliver a message'
    assert payload['timeout'] == (5, 20), 'Provider request must have bounded connect/read timeouts'
    client.send('device-token', title='Check')
    assert 'validate_only' not in http.calls[-1][1]['json'], 'Real send must not become validation'
    http.fail = True
    assert client.send('device-token')['outcome'] == 'unknown', 'Post-submission timeout must not claim rejection'
    assert client.validate()['success'] is False, 'Validation timeout must not report success'


@th.django_unit_test()
def test_legacy_real_token_test_refuses_simulation(opts):
    provider = Provider(accepted())
    result = opts.push_config.test_fcm_connection(test_token='legacy-token', client_factory=provider.factory)
    assert result['success'] is False and result['error_code'] == 'test_mode', 'Explicit token must not fake a real test'
    assert not provider.calls, 'Blocked real-token test must not contact the provider'


@th.django_unit_test()
def test_cleanup_push_checks(opts):
    from mojo.apps.account.models import User, Group, PushConfig
    User.objects.filter(username__startswith=PREFIX).delete()
    Group.objects.filter(name__startswith=PREFIX).delete()
    PushConfig.objects.filter(name__startswith=PREFIX).delete()


