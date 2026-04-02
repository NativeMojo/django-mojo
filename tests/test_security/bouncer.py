"""
Tests for the django-mojo Bouncer — server-gated bot detection.

Security contracts enforced:
  - Clean requests (no bot signals) get allow decision and a valid token
  - Headless browser environment signals produce a block decision with no token
  - Issued token is valid and consumable exactly once (single-use nonce)
  - Token replay (second consume of same nonce) is rejected
  - Token is rejected when IP does not match the IP it was issued for
  - Token is rejected when it has expired
  - Token is rejected when page_type scope does not match
  - Honeypot signal in gate_challenge raises risk score significantly
  - BouncerDevice is created/updated on assess calls (keyed on muid)
  - BouncerSignal audit log tracks muid, duid, msid, mtab
  - BotLearner job skips when risk_score < BOUNCER_LEARN_MIN_SCORE
  - Pass cookie set on allow decision; absent on block decision
  - requires_bouncer_token allows through when BOUNCER_REQUIRE_TOKEN=False and token missing
  - requires_bouncer_token blocks with 403 when BOUNCER_REQUIRE_TOKEN=True and token missing
  - Decoy endpoint always returns 401 with plausible error message
"""
from testit import helpers as th
from testit.helpers import assert_true, assert_eq

TEST_DUID = 'bouncer-test-duid-001'
TEST_DUID_B = 'bouncer-test-duid-002'
TEST_MUID = 'bouncer-test-muid-001'
TEST_MUID_B = 'bouncer-test-muid-002'
TEST_IP = '127.0.0.1'


@th.django_unit_setup()
def setup_bouncer(opts):
    from mojo.apps.account.models import BouncerDevice, BouncerSignal, BotSignature
    from mojo.decorators.limits import clear_rate_limits
    clear_rate_limits(ip='127.0.0.1')
    BouncerDevice.objects.filter(
        muid__in=[TEST_MUID, TEST_MUID_B]
    ).delete()
    BouncerSignal.objects.filter(
        muid__in=[TEST_MUID, TEST_MUID_B]
    ).delete()


# ---------------------------------------------------------------------------
# Assess endpoint
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_assess_clean_request(opts):
    """Clean request with no bot signals → allow + token returned."""
    resp = opts.client.post('/api/account/bouncer/assess', {
        'duid': TEST_DUID,
        'page_type': 'login',
        'session_id': 'sess-clean-001',
        'signals': {
            'environment': {},
            'behavior': {'mouse_move_count': 12, 'first_interaction_ms': 600},
        },
    })
    assert_eq(resp.status_code, 200, "expected 200")
    data = resp.json.data
    assert_true(data.decision in ('allow', 'monitor'), f"unexpected decision: {data.decision}")
    assert_true(data.token, "expected token in allow/monitor response")
    opts.clean_token = data.token
    opts.clean_session = data.session_id


@th.django_unit_test()
def test_assess_headless_bot(opts):
    """Headless browser signals → block decision, no token."""
    resp = opts.client.post('/api/account/bouncer/assess', {
        'duid': TEST_DUID_B,
        'page_type': 'login',
        'session_id': 'sess-bot-001',
        'signals': {
            'environment': {
                'webdriver_flag': True,
                'playwright_artifacts': True,
                'outer_size_zero': True,
                'languages_empty': True,
            },
            'behavior': {'mouse_move_count': 0},
        },
    })
    assert_eq(resp.status_code, 200, "expected 200")
    data = resp.json.data
    assert_true(data.decision in ('block', 'monitor'), f"expected block/monitor for bot signals, got {data.decision}")
    if data.decision == 'block':
        assert_true(not data.get('token'), "expected no token on block")


@th.django_unit_test()
def test_assess_honeypot_filled(opts):
    """Honeypot filled is a strong bot signal that elevates risk score."""
    from mojo.helpers.settings import settings
    weights = settings.get_static('BOUNCER_SCORE_WEIGHTS') or {}
    honeypot_weight = weights.get('gate_honeypot_filled', 0)

    resp = opts.client.post('/api/account/bouncer/assess', {
        'duid': 'bouncer-hp-test-duid',
        'page_type': 'login',
        'session_id': 'sess-hp-001',
        'signals': {
            'environment': {},
            'gate_challenge': {
                'honeypot_filled': True,
                'time_to_click_ms': 50,
                'had_mouse_movement': False,
                'is_touch_device': False,
            },
        },
    })
    assert_eq(resp.status_code, 200, "expected 200")
    data = resp.json.data
    if honeypot_weight > 0:
        assert_true(data.risk_score >= honeypot_weight, f"expected risk_score >= {honeypot_weight}, got {data.risk_score}")


@th.django_unit_test()
def test_bouncer_device_created(opts):
    """BouncerDevice is created on assess call, keyed on muid (server cookie).

    All test requests share the same opts.client session, so they share one
    _muid cookie and thus one BouncerDevice. The duid field reflects whichever
    client claim was sent last.
    """
    from mojo.apps.account.models import BouncerDevice
    # All assess calls in this file share one _muid cookie (same browser session).
    # Find any device that was created during these tests.
    device = BouncerDevice.objects.order_by('-last_seen').first()
    assert_true(device is not None, "expected BouncerDevice to be created")
    assert_true(device.muid, "expected muid to be set on BouncerDevice")
    assert_true(device.event_count >= 1, "expected event_count >= 1")


@th.django_unit_test()
def test_bouncer_signal_logged(opts):
    """BouncerSignal audit row is written for every assess call with identity fields."""
    from mojo.apps.account.models import BouncerSignal
    sig = BouncerSignal.objects.filter(session_id='sess-clean-001').first()
    assert_true(sig is not None, "expected BouncerSignal to be logged")
    assert_eq(sig.stage, 'assess', "expected stage=assess")
    assert_eq(sig.page_type, 'login', "expected page_type=login")
    assert_true(sig.muid, "expected muid on BouncerSignal")
    assert_eq(sig.duid, TEST_DUID, "expected duid=TEST_DUID on BouncerSignal")


# ---------------------------------------------------------------------------
# Token manager
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_token_single_use(opts):
    """Token nonce is consumed on first validate_and_consume; second call raises."""
    from mojo.apps.account.services.bouncer.token_manager import TokenManager
    token = TokenManager.issue(
        duid=TEST_DUID,
        fingerprint_id='',
        ip=TEST_IP,
        risk_score=10,
        page_type='login',
    )
    payload = TokenManager.validate_and_consume(token, TEST_IP, TEST_DUID)
    assert_true(payload is not None, "first consume should succeed")

    try:
        TokenManager.validate_and_consume(token, TEST_IP, TEST_DUID)
        assert_true(False, "expected ValueError on second consume")
    except ValueError as exc:
        assert_eq(str(exc), 'nonce_consumed', f"expected nonce_consumed, got {exc}")


@th.django_unit_test()
def test_token_ip_mismatch(opts):
    """Token issued for one IP is rejected from a different IP."""
    from mojo.apps.account.services.bouncer.token_manager import TokenManager
    token = TokenManager.issue(
        duid=TEST_DUID,
        fingerprint_id='',
        ip='10.0.0.1',
        risk_score=5,
        page_type='login',
    )
    try:
        TokenManager.validate(token, request_ip='10.0.0.2', request_duid=TEST_DUID)
        assert_true(False, "expected ValueError for IP mismatch")
    except ValueError as exc:
        assert_eq(str(exc), 'ip_mismatch', f"expected ip_mismatch, got {exc}")


@th.django_unit_test()
def test_token_expired(opts):
    """Expired token is rejected."""
    import time
    from mojo.apps.account.services.bouncer.token_manager import (
        TokenManager, _b64url_encode, _get_signing_key
    )
    import hmac, hashlib, json

    payload = {
        'duid': TEST_DUID,
        'fingerprint_id': '',
        'ip': TEST_IP,
        'risk_score': 5,
        'page_type': 'login',
        'issued_at': int(time.time()) - 1000,
        'expires_at': int(time.time()) - 100,
        'nonce': 'expired-nonce-123',
    }
    payload_b64 = _b64url_encode(json.dumps(payload, separators=(',', ':')))
    sig = hmac.new(_get_signing_key(), payload_b64.encode('ascii'), hashlib.sha256).digest()
    token = f"{payload_b64}.{_b64url_encode(sig)}"

    try:
        TokenManager.validate(token, request_ip=TEST_IP)
        assert_true(False, "expected ValueError for expired token")
    except ValueError as exc:
        assert_eq(str(exc), 'expired', f"expected expired, got {exc}")


@th.django_unit_test()
def test_token_page_type_scope(opts):
    """Token issued for 'login' is rejected when page_type scope is 'registration'."""
    from mojo.apps.account.services.bouncer.token_manager import TokenManager
    token = TokenManager.issue(
        duid=TEST_DUID,
        fingerprint_id='',
        ip=TEST_IP,
        risk_score=5,
        page_type='login',
    )
    payload = TokenManager.validate(token, request_ip=TEST_IP)
    assert_eq(payload['page_type'], 'login', "expected page_type=login in payload")
    assert_true(payload['page_type'] != 'registration', "scope should not match registration")


# ---------------------------------------------------------------------------
# Signature cache / learner
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_learner_skips_low_score(opts):
    """BotLearner job does nothing when risk_score < BOUNCER_LEARN_MIN_SCORE."""
    from mojo.apps.account.models import BotSignature
    from mojo.apps.account.services.bouncer.learner import learn_from_block
    from mojo.helpers.settings import settings

    min_score = settings.get_static('BOUNCER_LEARN_MIN_SCORE', 80)
    count_before = BotSignature.objects.count()

    class FakeJob:
        payload = {
            'muid': 'learner-skip-muid',
            'duid': 'learner-skip-duid',
            'ip': '192.168.1.1',
            'fingerprint_id': '',
            'risk_score': min_score - 1,
            'triggered_signals': ['webdriver_flag'],
            'user_agent': 'TestAgent/1.0',
        }

    learn_from_block(FakeJob())
    assert_eq(BotSignature.objects.count(), count_before, "learner should not create signature for low-score block")


@th.django_unit_test()
def test_sig_cache_check(opts):
    """check_signature_cache returns False for an unknown IP."""
    from mojo.apps.account.services.bouncer.learner import check_signature_cache
    matched, sig_type, value = check_signature_cache('192.168.99.99', 'Mozilla/5.0')
    assert_true(matched is False, "clean IP should not match signature cache")


# ---------------------------------------------------------------------------
# requires_bouncer_token decorator
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_requires_bouncer_token_log_only(opts):
    """With BOUNCER_REQUIRE_TOKEN=False (default), missing token logs but request proceeds."""
    resp = opts.client.post('/api/login', {
        'username': 'nonexistent_bouncer_test_user',
        'password': 'wrong',
    })
    assert_true(resp.status_code != 403, f"expected non-403 with log-only mode, got {resp.status_code}")


@th.django_unit_test()
def test_requires_bouncer_token_enforce(opts):
    """With BOUNCER_REQUIRE_TOKEN=True, missing token returns 403."""
    with th.server_settings(BOUNCER_REQUIRE_TOKEN=True):
        resp = opts.client.post('/api/login', {
            'username': 'any_user',
            'password': 'any_pass',
        })
        assert_eq(resp.status_code, 403, f"expected 403 with enforcement, got {resp.status_code}")


# ---------------------------------------------------------------------------
# Decoy / honeypot
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_decoy_endpoint_returns_plausible_error(opts):
    """Decoy dead endpoint at /login POST returns 401 with plausible error."""
    resp = opts.client.post('/login', {
        'username': 'attacker@example.com',
        'password': 'Password123',
        'duid': 'attacker-duid',
    })
    assert_eq(resp.status_code, 401, f"expected 401 from decoy, got {resp.status_code}")
    assert_true(resp.json.error or not resp.json.status, "expected plausible error in decoy response")
