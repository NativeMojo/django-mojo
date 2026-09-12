"""Real hosted HTTP + enforcing authentication/token contracts."""
import json
import re
import uuid
from urllib.parse import urlsplit

from testit import helpers as th
from testit.client import RestClient


def config(response, name='mbg-config'):
    assert response.status_code == 200, f'hosted page must render, got {response.status_code}'
    found = re.search(r'<script id="' + name + r'"[^>]*>(.*?)</script>', response.text, re.S)
    assert found, f'hosted page must contain {name}'
    return json.loads(found.group(1))


def operation(client, descriptor, op, signals=None, **fields):
    body = {'hosted_gate': {'version': 1, 'descriptor': descriptor,
            'operation': op, 'request_id': uuid.uuid4().hex}, 'signals': signals or {}}
    body['hosted_gate'].update(fields)
    response = client.post('/api/account/bouncer/assess', body)
    assert response.json and response.json.get('data'), f'hosted operation must return structured outcome: {response.status_code}'
    return response.json.data


def pass_gate(client, path='/auth'):
    challenge = config(client.get(path))
    outcome = operation(client, challenge['descriptor'], 'check', {'behavior': {'mouse_move_count': 0}})
    if outcome.next_action == 'slider':
        outcome = operation(client, challenge['descriptor'], 'submit', answer=outcome.target)
    assert outcome.next_action == 'check_cookie', f'eligible visitor must reach cookie confirmation: {outcome}'
    assert operation(client, challenge['descriptor'], 'confirm').next_action == 'allow', 'returning pass must be confirmed'
    return config(client.get(path), 'mat-hosted-bouncer')


@th.django_unit_test('real hosted login keeps device binding and obtains a fresh token after wrong credentials')
def test_hosted_login_token_retry(opts):
    from mojo.apps.account.models import User
    from mojo.apps.account.services.bouncer.token_manager import TokenManager
    email = 'test-hosted-recovery-login@example.com'
    User.objects.filter(username=email).delete()
    user = User.objects.create_user(email=email, username=email, password='Bouncer-Recovery-4309!')
    user.is_active = user.is_email_verified = True
    user.save()
    client = RestClient(opts.client.host)
    try:
        form = pass_gate(client)
        client.session.headers['X-Mojo-UID'] = 'hosted-login-device'
        first = operation(client, form['descriptor'], 'token').token
        payload = TokenManager.validate(first, '127.0.0.1', 'hosted-login-device')
        assert payload['duid'] == 'hosted-login-device', 'hosted token must retain the device identity sent by MojoAuth'
        try:
            TokenManager.validate(first, '127.0.0.1', 'other-device')
        except ValueError as error:
            assert str(error) == 'duid_mismatch', 'token transfer must fail for the device mismatch'
        else:
            raise AssertionError('same-IP token transfer to another device must fail')
        enforce = {'X-Mojo-Test-Bouncer-Require-Token': '1'}
        bad = client.post('/api/auth/login', {'username': email, 'password': 'wrong', 'bouncer_token': first}, headers=enforce)
        assert bad.status_code != 200, 'incorrect credentials must remain rejected after recovery'
        assert not TokenManager.consume(payload['nonce']), 'credential failure must still consume the single-use token'
        second = operation(client, form['descriptor'], 'token').token
        assert second and second != first, 'retry must obtain a fresh single-use token'
        good = client.post('/api/auth/login', {'username': email, 'password': 'Bouncer-Recovery-4309!', 'bouncer_token': second}, headers=enforce)
        assert good.status_code == 200 and good.json.status, f'correct credentials and fresh token must authenticate: {good.json}'
    finally:
        User.objects.filter(pk=user.pk).delete()
        client.session.close()


@th.django_unit_test('hosted form purpose, returning cookies and sticky bot verdict remain authoritative')
def test_hosted_scope_and_restrictions(opts):
    from mojo.apps.account.models import BouncerDevice
    from mojo.apps.account.services.bouncer.token_manager import TokenManager
    client = RestClient(opts.client.host)
    try:
        form = pass_gate(client, '/register')
        token = operation(client, form['descriptor'], 'token', page_type='login').token
        assert TokenManager.validate(token, '127.0.0.1')['page_type'] == 'registration', 'client purpose cannot override server render scope'
        assert TokenManager.validate_and_consume(token, '127.0.0.1')['page_type'] == 'registration', 'registration token remains accepted by the existing manager'
        strong = {'environment': {'webdriver_flag': True, 'playwright_artifacts': True, 'puppeteer_artifacts': True}}
        denied = operation(client, form['descriptor'], 'token', strong)
        assert denied.next_action == 'decoy', f'current strong evidence must route to decoy: {denied}'
        assert operation(client, form['descriptor'], 'token').next_action == 'decoy', 'omitting evidence must not erase a previously observed restriction'
        muid = client.session.cookies.get('_muid')
        assert not BouncerDevice.objects.filter(muid=muid, risk_tier='blocked').exists(), 'hosted decoy/recovery must not promote history or feed scanner learning'
        other = RestClient(opts.client.host)
        missing = operation(other, form['descriptor'], 'token')
        assert missing.next_action == 'recovery' and missing.reason == 'cookies', 'a middleware-generated identity is not a returning cookie'
        other.session.close()
    finally:
        client.session.close()


@th.django_unit_test('historical blocked devices receive recovery and keep their restriction')
def test_history_recovery_and_cookie_rejection(opts):
    from mojo.apps.account.models import BouncerDevice
    client = RestClient(opts.client.host)
    try:
        first = config(client.get('/auth'))
        muid = client.session.cookies.get('_muid')
        BouncerDevice.objects.filter(muid=muid).delete()
        device = BouncerDevice.objects.create(muid=muid, risk_tier='blocked')
        outcome = operation(client, first['descriptor'], 'check')
        assert outcome.next_action == 'recovery', f'unknown blocked provenance must retain denial with recovery: {outcome}'
        device.refresh_from_db()
        assert device.risk_tier == 'blocked', 'recovery must not clear historical blocks'
        device.delete()
        new = config(client.get('/auth'))
        grant = operation(client, new['descriptor'], 'check')
        if grant.next_action == 'slider':
            grant = operation(client, new['descriptor'], 'submit', answer=grant.target)
        assert grant.next_action == 'check_cookie', 'eligible visitor must receive only a provisional grant'
        client.session.cookies.set('mbp', None)
        assert operation(client, new['descriptor'], 'confirm').reason == 'cookies', 'missing pass must prevent final confirmation'
        malformed = client.post('/api/account/bouncer/assess', {'hosted_gate': False})
        assert malformed.status_code == 400 and malformed.json.data.next_action == 'error', 'malformed hosted mode cannot fall into legacy issuance'
    finally:
        client.session.close()


@th.django_unit_test('hosted registration token supports phone start then fresh registration and scoped contact')
def test_registration_phone_and_contact(opts):
    from django.test import RequestFactory
    from objict import objict
    from mojo.apps.account.models import User, Group, PublicMessage
    from mojo.apps.account.rest.sms import on_phone_register_start
    from mojo.middleware.mojo import ANONYMOUS_USER
    from mojo.apps.account.services.bouncer.token_manager import TokenManager
    email = 'test-hosted-register@example.com'
    group_uuid = '4309bouncercontactscope'
    User.objects.filter(username=email).delete()
    Group.objects.filter(uuid=group_uuid).delete()
    group = Group.objects.create(uuid=group_uuid, name='Hosted recovery test group', kind='operator')
    client = RestClient(opts.client.host)
    try:
        form = pass_gate(client, '/register')
        first = operation(client, form['descriptor'], 'token').token
        request = RequestFactory(REMOTE_ADDR='127.0.0.1').post('/api/auth/phone/register/start', HTTP_X_MOJO_TEST_BOUNCER_REQUIRE_TOKEN='1')
        request.DATA = objict(phone='+15550004309', bouncer_token=first)
        request.ip, request.user_agent, request.user = '127.0.0.1', 'Mozilla/5.0', ANONYMOUS_USER
        request.group = request.duid = request.bearer = None
        request.muid = client.session.cookies.get('_muid')
        sent = []
        def send(phone, body):
            sent.append(phone)
            return objict(status='sent', id='test-sms')
        phone = on_phone_register_start(request, send=send)
        assert phone.status_code == 200 and sent == ['+15550004309'], 'enforcing phone-start must consume a registration token and reach its SMS boundary'
        payload = TokenManager.validate(first, '127.0.0.1')
        assert not TokenManager.consume(payload['nonce']), 'phone-start consumes its own token'
        fresh = operation(client, form['descriptor'], 'token').token
        registered = client.post('/api/auth/register', {'email': email, 'password': 'Bouncer-Registration-4309!', 'bouncer_token': fresh}, headers={'X-Mojo-Test-Bouncer-Require-Token': '1', 'X-Mojo-Test-Allow-User-Registration': '1'})
        assert registered.status_code == 200 and registered.json.status, f'fresh registration token must reach real account creation: {registered.json}'
        contact = config(client.get('/contact', params={'kind': 'support', 'group_uuid': group_uuid}), 'mat-hosted-bouncer')
        contact_token = operation(client, contact['descriptor'], 'token').token
        assert TokenManager.validate(contact_token, '127.0.0.1')['page_type'] == 'public_message', 'contact renderer must bind the public-message purpose'
        submitted = client.post('/api/account/bouncer/message', {'kind': 'contact_us', 'group_uuid': group_uuid, 'name': 'Recovery Test', 'email': email, 'message': 'Automated hosted recovery integration test.', 'bouncer_token': contact_token}, headers={'X-Mojo-Test-Bouncer-Require-Token': '1'})
        assert submitted.status_code == 200 and submitted.json.status, f'contact must submit with token enforcement enabled: {submitted.json}'
        message = PublicMessage.objects.get(pk=submitted.json.data.id)
        assert message.group_id == group.pk, 'contact submission must preserve the resolved group'
    finally:
        PublicMessage.objects.filter(group=group).delete()
        Group.objects.filter(pk=group.pk).delete()
        User.objects.filter(username=email).delete()
        client.session.close()


@th.django_unit_test('a late stream freeze and unavailable state prevent grants without promoting device history')
def test_late_freeze_and_unavailable_state(opts):
    from mojo.helpers.redis import get_bounded_connection
    from mojo.apps.account.services.bouncer.hosted_challenge import ChallengeStore
    redis = get_bounded_connection(timeout=1, read_from_replicas=False)
    client = RestClient(opts.client.host)
    keys = []
    try:
        first = config(client.get('/auth'))
        muid = client.session.cookies.get('_muid')
        grant = operation(client, first['descriptor'], 'check')
        if grant.next_action == 'slider':
            grant = operation(client, first['descriptor'], 'submit', answer=grant.target)
        assert grant.next_action == 'check_cookie', 'pre-freeze browser must reach provisional grant'
        risk_key = 'bouncer:session_risk:' + muid
        keys.append(risk_key)
        redis.setex(risk_key, 60, '99')
        assert operation(client, first['descriptor'], 'confirm').next_action == 'recovery', 'a newly frozen session must prevent cookie confirmation'
        assert redis.get(risk_key) in ('99', b'99'), 'hosted recovery cannot reset the enforcement high-water mark'
        redis.delete(risk_key)
        assert operation(client, first['descriptor'], 'confirm').next_action == 'recovery', 'the observed restriction remains attached to this descriptor'
        store = ChallengeStore(redis, urlsplit(client.host).netloc.lower(), muid)
        keys.append(store.key)
        redis.setex(store.key, 60, 'invalid-state')
        failure = operation(client, first['descriptor'], 'check')
        assert failure.next_action == 'error' and failure.reason == 'unavailable', 'unreadable state must fail honestly without granting'
    finally:
        for key in keys:
            redis.delete(key)
        redis.close()
        client.session.close()


@th.django_unit_test('hosted capabilities are redacted and canonical contact/reset forwarding stays bounded')
def test_render_scope_and_logging(opts):
    from django.test import RequestFactory
    from objict import objict
    from urllib.parse import parse_qs
    from mojo.apps.account.rest.bouncer.views import _serve_challenge, _serve_login
    from mojo.middleware.logging import LoggerMiddleware
    from mojo.helpers.request import sensitive_body_label
    descriptor = {'descriptor': 'a' * 32, 'next_action': 'check'}
    request = RequestFactory().get('/contact', {'kind': 'support', 'returnTo': '/saved', 'force_reauth': '1', 'auth_theme': 'editorial', 'auth_appearance': 'light', 'token': 'pr:private'})
    request.DATA = objict(request.GET.dict())
    response = _serve_challenge(request, page_type='public_message', hosted_config=descriptor)
    found = re.search(rb'<script id="mbg-config"[^>]*>(.*?)</script>', response.content, re.S)
    cfg = json.loads(found.group(1))
    forwarded = parse_qs(urlsplit(cfg['redirect_url']).query)
    assert forwarded['kind'] == ['support'] and forwarded['redirect'] == ['/saved'], 'contact kind and return destination must survive the gate'
    assert forwarded['auth_theme'] == ['editorial'] and forwarded['force_reauth'] == ['1'], 'approved theme and force-reauth must survive'
    assert 'token' not in forwarded and 'private' not in json.dumps(cfg), 'reset tokens must not enter challenge state or a new redirect URL'
    middleware = LoggerMiddleware(lambda _: None)
    summary = middleware.get_response_log_content(request, response)
    assert 'hosted_bouncer_page' in summary and descriptor['descriptor'] not in summary, 'rendered descriptors must never appear in ordinary response logs'
    form = _serve_login(request, hosted_config=descriptor)
    assert descriptor['descriptor'] not in middleware.get_response_log_content(request, form), 'real form descriptors must receive the same log protection'
    api = RequestFactory().post('/api/account/bouncer/assess')
    assert sensitive_body_label(api) == 'bouncer_assessment', 'assessment request and token response bodies must be path-redacted'
