"""Mobile input and hosted pass regressions."""
from testit import helpers as th


@th.django_unit_test('unavailable measurements are unknown rather than inactivity')
def test_missing_input_is_unknown(opts):
    from mojo.apps.account.services.bouncer.scoring import BehaviorAnalyzer, GateChallengeAnalyzer, ScoringContext
    context = ScoringContext({'gate_challenge': {'honeypot_filled': False}}, {}, None, 'login')
    assert BehaviorAnalyzer.analyze(context) == (0, []), 'missing behavior must not become observed inactivity'
    assert GateChallengeAnalyzer.analyze(context) == (0, []), 'missing modality must not become desktop inactivity'


@th.django_unit_test('activation counts as interaction without movement')
def test_activation_without_movement(opts):
    from mojo.apps.account.services.bouncer.scoring import BehaviorAnalyzer, GateChallengeAnalyzer, ScoringContext
    for modality in ('touch', 'mouse', 'keyboard', 'unknown'):
        signals = {'behavior': {'activation_count': 1, 'mouse_move_count': 0, 'touch_event_count': 0},
                   'gate_challenge': {'is_touch_device': modality == 'touch', 'had_activation': True,
                                      'had_mouse_movement': False, 'had_touch_events': False}}
        context = ScoringContext(signals, {}, None, 'login')
        assert 'no_interaction' not in BehaviorAnalyzer.analyze(context)[1], f'{modality} activation is valid interaction'
        assert 'gate_no_interaction_desktop' not in GateChallengeAnalyzer.analyze(context)[1], f'{modality} needs no movement'


@th.django_unit_test('hosted passes survive network changes but require their session and scope')
def test_session_pass_binding(opts):
    import time
    from django.http import HttpResponse
    from mojo.apps.account.rest.bouncer.assess import _set_pass_cookie, verify_pass_cookie
    response = HttpResponse()
    _set_pass_cookie(response, 'mobile-fixture', '2001:db8::1', host='auth.example.test', issued_at=int(time.time()))
    cookie, session = response.cookies['mbp'].value, response.cookies['mbs'].value
    for ip in ('2001:db8::2', '198.51.100.20'):
        assert verify_pass_cookie(cookie, ip, host='auth.example.test', session_key=session) == 'mobile-fixture', 'same browser can change IPv6 address or network'
    assert verify_pass_cookie(cookie, '198.51.100.20', host='auth.example.test') is None, 'a copied pass without its session cookie must fail'
    assert verify_pass_cookie(cookie, '198.51.100.20', host='other.example.test', session_key=session) is None, 'host-only passes cannot cross sites'
    assert verify_pass_cookie(cookie, '198.51.100.20', host='auth.example.test', session_key='a' * 64) is None, 'another session must not reuse the pass'
    assert verify_pass_cookie(cookie[:-1] + ('0' if cookie[-1] != '0' else '1'), '198.51.100.20', host='auth.example.test', session_key=session) is None, 'tampering must be rejected'
    assert response.cookies['mbs']['httponly'] and response.cookies['mbs']['samesite'] == 'Lax' and not response.cookies['mbs']['max-age'], 'companion must be an HttpOnly session cookie'
    expired = HttpResponse()
    _set_pass_cookie(expired, 'mobile-fixture', '', host='auth.example.test', session_key=session, issued_at=1)
    assert verify_pass_cookie(expired.cookies['mbp'].value, '', host='auth.example.test', session_key=session) is None, 'expired passes stay expired across networks'
    legacy = HttpResponse()
    _set_pass_cookie(legacy, 'legacy-fixture', '192.0.2.10')
    assert verify_pass_cookie(legacy.cookies['mbp'].value, '192.0.2.20') == 'legacy-fixture', 'legacy /24 validation is preserved'
    assert verify_pass_cookie(legacy.cookies['mbp'].value, '198.51.100.20') is None, 'legacy passes are not silently upgraded'


@th.django_unit_test('hosted errors retain usable evidence and review intake without cookies')
def test_cookie_free_review_and_operator_lifecycle(opts):
    import uuid
    from testit.client import RestClient
    from mojo.apps.account.models import BouncerSignal, User
    from test_bouncer_recovery.integration import config, operation
    from mojo.apps.account.services.bouncer.hosted_recovery import _row, resolve, queue
    client = RestClient(opts.client.host)
    anonymous = RestClient(opts.client.host)
    email = 'bouncer-review-' + uuid.uuid4().hex[:10] + '@example.test'
    reviewer = None
    row = None
    try:
        initial = config(client.get('/auth'))
        # No returning _muid: a technical failure, never a training label.
        failed = operation(anonymous, initial['descriptor'], 'check')
        assert failed.reason == 'cookies' and failed.review_ticket, 'cookie failure must provide a signed review path'
        row, detail = _row(failed.reference)
        assert detail['reason'] == 'cookies' and row.decision == 'log', 'reference must resolve to the actual technical failure'
        body = {'review_ticket': failed.review_ticket, 'email': email, 'note': 'Stationary tap did not continue'}
        anonymous.session.cookies.clear()
        first = anonymous.post('/api/auth/bouncer/recovery', body)
        assert first.status_code == 200 and first.json.data.review_state == 'pending', 'review must work without any cookies or Bouncer pass'
        anonymous.session.cookies.clear()
        repeat = anonymous.post('/api/auth/bouncer/recovery', {**body, 'note': 'replacement'})
        assert repeat.status_code == 200 and repeat.json.data.reference == failed.reference, 'lost-response retry must be idempotent'
        row.refresh_from_db()
        assert row.server_signals['hosted_gate']['review']['note'] == body['note'], 'duplicate submission must not overwrite the original request'
        assert 'review_ticket' not in str(row.server_signals) and not row.raw_signals, 'ticket and raw payload must not be retained'
        assert not anonymous.session.cookies.get('mbp'), 'requesting review must never grant access'
        denied = anonymous.get('/api/auth/bouncer/recovery')
        assert denied.status_code in (401, 403), 'anonymous callers cannot read the operator queue'
        denied = anonymous.post('/api/auth/bouncer/recovery/resolve', {'reference': failed.reference, 'resolution': 'allow'})
        assert denied.status_code in (401, 403), 'anonymous callers cannot close reviews'
        invalid = anonymous.post('/api/auth/bouncer/recovery', {**body, 'review_ticket': body['review_ticket'] + 'x'})
        assert invalid.status_code == 400, 'modified tickets must fail'
        assert queue(failed.reference)['items'][0]['details']['review']['email'] == email, 'exact reference lookup must return the review evidence'
        User.objects.filter(username=email).delete()
        reviewer = User.objects.create_user(username=email, email=email, password='Review-test-4478!')
        result = resolve(failed.reference, reviewer, 'Investigated the reported cookie failure; sent configuration guidance.')
        assert result['review_state'] == 'reviewed', 'operator completion must leave pending state'
        resolve(failed.reference, reviewer, 'must not replace the audit')
        row.refresh_from_db()
        assert row.server_signals['hosted_gate']['review']['resolution'].startswith('Investigated'), 'resolution retries must preserve the first audit'
    finally:
        if row:
            row.delete()
        if reviewer:
            reviewer.delete()
        client.session.close()
        anonymous.session.close()


@th.django_unit_test('review tickets cannot cross host or expiry and inputs stay bounded')
def test_review_ticket_boundaries(opts):
    from datetime import timedelta
    from django.test import RequestFactory
    from objict import objict
    from mojo.helpers import dates
    from mojo.helpers.request import sensitive_body_label
    from mojo.apps.account.services.bouncer import hosted_recovery as service
    request = RequestFactory().post('/api/auth/bouncer/recovery', HTTP_HOST='one.test')
    request.DATA = objict()
    ticket = service.record(request, 'registration', 'recovery', 'cookies')
    row, _ = service._row(ticket['reference'])
    try:
        request.DATA = objict(review_ticket=ticket['review_ticket'], email='review@example.test', note='')
        assert sensitive_body_label(request) == 'account_auth', 'request and response bodies must use existing pre-view redaction'
        request.META['HTTP_HOST'] = 'two.test'
        try:
            service.submit(request)
        except ValueError:
            pass
        else:
            raise AssertionError('ticket must not cross hosts')
        request.META['HTTP_HOST'] = 'one.test'
        request.DATA.note = 'x' * 501
        try:
            service.submit(request)
        except ValueError:
            pass
        else:
            raise AssertionError('oversized notes must not reach storage')
        request.DATA.note = ''
        row.created = dates.utcnow() - timedelta(seconds=service.TICKET_TTL + 1)
        row.save(update_fields=['created'])
        try:
            service.submit(request)
        except ValueError:
            pass
        else:
            raise AssertionError('expired ticket must not submit a review')
    finally:
        row.delete()


@th.django_unit_test('review queue and resolution require global rather than tenant permissions')
def test_review_global_permissions(opts):
    import uuid
    from testit.client import RestClient
    from mojo.apps.account.models import User, Group, GroupMember
    email = 'review-perms-' + uuid.uuid4().hex[:10] + '@example.test'
    User.objects.filter(username=email).delete()
    user = User.objects.create_user(username=email, email=email, password='Review-permissions-4478!')
    user.is_active = user.is_email_verified = True
    user.save()
    group = Group.objects.create(name=email)
    member = GroupMember.objects.create(user=user, group=group, permissions={'security': True})
    client = RestClient(opts.client.host)
    try:
        assert client.login(email, 'Review-permissions-4478!'), 'fixture login must succeed'
        denied = client.get(f'/api/auth/bouncer/recovery?group={group.pk}')
        assert denied.status_code == 403, 'tenant security permission must not read global reviews'
        denied = client.post('/api/auth/bouncer/recovery/resolve', {'group': group.pk, 'reference': 'invalid', 'resolution': 'reviewed'})
        assert denied.status_code == 403, 'tenant security permission must not resolve global reviews'
        user.permissions = {'view_security': True}
        user.save(update_fields=['permissions'])
        assert client.get('/api/auth/bouncer/recovery').status_code == 200, 'global security viewer can read queue'
        assert client.post('/api/auth/bouncer/recovery/resolve', {'reference': 'invalid', 'resolution': 'reviewed'}).status_code == 403, 'viewer cannot resolve'
        user.permissions = {'manage_security': True}
        user.save(update_fields=['permissions'])
        assert client.post('/api/auth/bouncer/recovery/resolve', {'reference': 'invalid', 'resolution': 'reviewed'}).status_code == 400, 'global manager reaches validated resolution handler'
    finally:
        member.delete()
        group.delete()
        user.delete()
        client.session.close()
