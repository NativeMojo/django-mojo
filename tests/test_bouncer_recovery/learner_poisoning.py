"""#5569: a caller cannot teach the Bouncer to block a shared browser identifier.

The learner used to write a `user_agent` signature after five reported blocks
and a `fingerprint` signature after three. Both values are chosen by the
caller, and the public check scores signals the caller asserts, so five
anonymous requests blocked every visitor sending that User-Agent for 7 days.

The learner is called in process with an object carrying `.payload`, as the
job engine calls it. Every User-Agent and fingerprint carries a marker unique
to the run, and every call uses an address in its own /24 of the benchmarking
range (198.18.0.0/15), so no network signature is learned as a side effect.
"""
import random
import uuid

from testit import helpers as th
from testit.helpers import assert_eq, assert_true

LEARN_FUNC = 'mojo.apps.account.services.bouncer.learner.learn_from_block'
OLD_CACHE_KEY = 'bouncer:sigs:active'
PWORD = 'Learner-poison-5569!'


def _marker():
    return 'm5569' + uuid.uuid4().hex[:16]


def _iphone_ua(marker):
    return ('Mozilla/5.0 (iPhone; CPU iPhone OS 18_6 like Mac OS X) AppleWebKit/605.1.15 '
            f'(KHTML, like Gecko) Version/18.6 Mobile/15E148 Safari/604.1 {marker}')


def _addresses(count):
    """`count` addresses, each in a different /24."""
    nets = random.sample([(second, third) for second in (18, 19) for third in range(256)], count)
    return [f'198.{second}.{third}.{random.randint(1, 254)}' for second, third in nets]


class _Job:
    def __init__(self, payload):
        self.payload = payload


def _report(count, user_agent='', fingerprint_id='', triggered_signals=None):
    """Run the learner `count` times with the payload the public check publishes."""
    from mojo.apps.account.services.bouncer.learner import learn_from_block
    for ip in _addresses(count):
        learn_from_block(_Job({
            'muid': '',
            'duid': 'duid-' + uuid.uuid4().hex[:12],
            'ip': ip,
            'fingerprint_id': fingerprint_id,
            'risk_score': 85,
            'triggered_signals': triggered_signals or [],
            'user_agent': user_agent,
        }))


def _rows(marker):
    from mojo.apps.account.models import BotSignature
    return BotSignature.objects.filter(value__contains=marker)


def _matches(user_agent='', fingerprint_id=''):
    from mojo.apps.account.services.bouncer.learner import check_signature_cache
    matched, _, _ = check_signature_cache(_addresses(1)[0], user_agent, fingerprint_id)
    return matched


def _cleanup(marker):
    from mojo.apps.account.services.bouncer.learner import refresh_sig_cache
    _rows(marker).delete()
    refresh_sig_cache()


@th.django_unit_test('#5569: five fake bot reports do not block a browser User-Agent')
def test_five_fake_bot_reports_do_not_block_a_browser_user_agent(opts):
    marker = _marker()
    user_agent = _iphone_ua(marker)
    try:
        _report(5, user_agent=user_agent)
        assert_eq(_rows(marker).count(), 0, 'five reports must not write a signature for the User-Agent')
        assert_true(not _matches(user_agent=user_agent), 'a visitor sending that User-Agent must not match a signature')
    finally:
        _cleanup(marker)


@th.django_unit_test('#5569: three reports do not learn a fingerprint')
def test_three_reports_do_not_learn_a_fingerprint(opts):
    marker = _marker()
    try:
        _report(3, user_agent=_iphone_ua(_marker()), fingerprint_id=marker)
        assert_eq(_rows(marker).count(), 0, 'three reports must not write a signature for the fingerprint')
        assert_true(not _matches(fingerprint_id=marker), 'a visitor sending that fingerprint must not match a signature')
    finally:
        from mojo.helpers.redis import get_connection
        get_connection().delete('bouncer:learn:fp:' + marker)
        _cleanup(marker)


@th.django_unit_test('#5569: a User-Agent that announces automation is not learned either')
def test_automation_user_agent_is_not_learned_either(opts):
    marker = _marker()
    user_agent = f'python-requests/2.32.3 {marker}'
    try:
        _report(5, user_agent=user_agent)
        assert_eq(_rows(marker).count(), 0, 'no User-Agent is learned, whatever it announces')
        assert_true(not _matches(user_agent=user_agent), 'a client sending that User-Agent must not match a signature')
    finally:
        _cleanup(marker)


@th.django_unit_test('#5569: an automatic User-Agent or fingerprint signature already stored is not enforced')
def test_existing_automatic_signature_is_not_enforced(opts):
    from mojo.apps.account.models import BotSignature
    from mojo.apps.account.services.bouncer.learner import refresh_sig_cache
    marker = _marker()
    user_agent = _iphone_ua(marker)
    fingerprint = 'fp-' + marker
    try:
        for sig_type, value in (('user_agent', user_agent), ('fingerprint', fingerprint)):
            BotSignature.objects.create(sig_type=sig_type, value=value, source='auto',
                                        confidence=90, is_active=True, block_count=5)
        refresh_sig_cache()
        assert_true(not _matches(user_agent=user_agent), 'a learned User-Agent signature must no longer match')
        assert_true(not _matches(fingerprint_id=fingerprint), 'a learned fingerprint signature must no longer match')
        assert_eq(_rows(marker).filter(is_active=True).count(), 2, 'the stored rows are left as they were')
    finally:
        _cleanup(marker)


@th.django_unit_test('#5569: a manual User-Agent or fingerprint signature is still enforced')
def test_manual_signature_is_still_enforced(opts):
    from mojo.apps.account.models import BotSignature
    from mojo.apps.account.services.bouncer.learner import refresh_sig_cache
    marker = _marker()
    user_agent = _iphone_ua(marker)
    fingerprint = 'fp-' + marker
    try:
        for sig_type, value in (('user_agent', user_agent), ('fingerprint', fingerprint)):
            BotSignature.objects.create(sig_type=sig_type, value=value, source='manual',
                                        confidence=100, is_active=True, block_count=1)
        refresh_sig_cache()
        assert_true(_matches(user_agent=user_agent), "an operator's User-Agent signature must still match")
        assert_true(_matches(fingerprint_id=fingerprint), "an operator's fingerprint signature must still match")
    finally:
        _cleanup(marker)


@th.django_unit_test('#5569: a signature created over REST with no source is manual')
def test_rest_created_signature_without_source_is_manual(opts):
    from testit.client import RestClient
    from mojo.apps.account.models import BotSignature, User
    from mojo.apps.account.services.bouncer.learner import refresh_sig_cache
    marker = _marker()
    user_agent = _iphone_ua(marker)
    name = f'{marker}@example.test'
    user = User.objects.create_user(username=name, email=name, password=PWORD)
    user.is_active = user.is_email_verified = True
    user.permissions = {'manage_security': True}
    user.save()
    client = RestClient(opts.client.host)
    try:
        assert_true(client.login(name, PWORD), 'the security operator must authenticate')
        resp = client.post('/api/account/bouncer/signature', {'sig_type': 'user_agent', 'value': user_agent})
        assert_eq(resp.status_code, 200, f'the operator can create a signature, got {resp.status_code}')
        row = BotSignature.objects.get(sig_type='user_agent', value=user_agent)
        assert_eq(row.source, 'manual', 'a signature made by hand with no source is stored as manual')
        refresh_sig_cache()
        assert_true(_matches(user_agent=user_agent), 'and it is enforced')

        learned = 'fp-' + marker
        resp = client.post('/api/account/bouncer/signature',
                           {'sig_type': 'fingerprint', 'value': learned, 'source': 'auto'})
        assert_eq(resp.status_code, 200, f'a create that names its source is accepted, got {resp.status_code}')
        assert_eq(BotSignature.objects.get(sig_type='fingerprint', value=learned).source, 'auto',
                  'a source the request names is kept')

        resp = client.post(f'/api/account/bouncer/signature/{row.pk}', {'notes': 'edited'})
        assert_eq(resp.status_code, 200, f'the operator can edit the signature, got {resp.status_code}')
        BotSignature.objects.filter(pk=row.pk).update(source='auto')
        resp = client.post(f'/api/account/bouncer/signature/{row.pk}', {'notes': 'edited again'})
        assert_eq(resp.status_code, 200, f'the operator can edit the signature, got {resp.status_code}')
        assert_eq(BotSignature.objects.get(pk=row.pk).source, 'auto', "an edit does not change a row's source")
    finally:
        client.session.close()
        user.delete()
        _cleanup(marker)


@th.django_unit_test('#5569: a cache written under the old key is ignored')
def test_old_cache_key_is_ignored(opts):
    import json
    from mojo.helpers.redis import get_connection
    marker = _marker()
    user_agent = _iphone_ua(marker)
    redis = get_connection()
    try:
        redis.set(OLD_CACHE_KEY, json.dumps({'user_agent': [user_agent]}), ex=60)
        assert_true(not _matches(user_agent=user_agent),
                    'a cache an old job worker wrote under the old key must not be read')
    finally:
        redis.delete(OLD_CACHE_KEY)
        _cleanup(marker)


@th.django_unit_test('#5569: five fake bot reports through the public check do not block the User-Agent')
def test_public_check_cannot_poison_a_user_agent(opts):
    """No job engine runs during the suite, so the jobs the endpoint publishes
    are run here, in process, selected by this test's own User-Agent."""
    from testit.client import RestClient
    from mojo.apps.jobs.models import Job
    marker = _marker()
    user_agent = _iphone_ua(marker)
    # one different extra signal per report, so the five do not form a campaign
    extras = ['selenium_artifacts', 'phantom_globals', 'nightmare_global', 'screen_zero', 'outer_size_zero']
    client = RestClient(opts.client.host)
    mine = Job.objects.filter(func=LEARN_FUNC, payload__user_agent=user_agent)
    try:
        for ip, extra in zip(_addresses(5), extras):
            resp = client.post('/api/account/bouncer/assess', {
                'duid': 'duid-' + uuid.uuid4().hex[:12],
                'page_type': 'login',
                'session_id': 'sess-' + uuid.uuid4().hex[:12],
                'signals': {
                    'environment': {'webdriver_flag': True, 'playwright_artifacts': True,
                                    'puppeteer_artifacts': True, extra: True},
                    'behavior': {'mouse_move_count': 0},
                },
            }, headers={'User-Agent': user_agent, 'X-Real-IP': ip})
            assert_eq(resp.status_code, 200, f'the public check answers, got {resp.status_code}')
            assert_eq(resp.json.data.decision, 'block', 'three asserted automation signals are a block')
        assert_eq(mine.count(), 5, 'each blocked report publishes one learn job carrying the User-Agent')
        ran = th.run_pending_jobs(func=LEARN_FUNC, payload={'user_agent': user_agent})
        assert_eq(ran, 5, 'the five learn jobs run')
        assert_eq(mine.filter(status='completed').count(), 5, 'and all five complete')
        assert_eq(_rows(marker).count(), 0, 'five public reports must not write a signature for the User-Agent')
        assert_true(not _matches(user_agent=user_agent), 'a visitor sending that User-Agent must not match a signature')
    finally:
        client.session.close()
        _cleanup(marker)
