"""
Tests for the verify_pass endpoint used by nginx auth_request.

Verifies:
  - Valid mbp cookie → 200 with X-Bouncer-Muid header
  - Missing/invalid cookie → 401
  - Signature cache pre-screen → 401 with X-Bouncer-Reason: signature even
    when a valid cookie is present (signature beats cookie)
  - BOUNCER_PASS_COOKIE_DOMAIN setting causes the mbp cookie to be issued
    with a Domain= attribute for cross-subdomain sharing
"""
from testit import helpers as th
from testit.helpers import assert_true, assert_eq

TEST_DUID = 'verify-pass-duid-001'
TEST_IP = '127.0.0.1'


@th.django_unit_setup()
def setup(opts):
    from mojo.apps.account.models import BouncerDevice, BouncerSignal, BotSignature
    from mojo.decorators.limits import clear_rate_limits
    clear_rate_limits(ip=TEST_IP)
    BouncerSignal.objects.filter(duid=TEST_DUID).delete()
    BouncerDevice.objects.filter(duid=TEST_DUID).delete()
    BotSignature.objects.filter(value=TEST_IP).delete()


def _hdr(client, name):
    """Case-insensitive header lookup on the RestClient's last_response."""
    lr = getattr(client, 'last_response', None)
    if not lr or not lr.headers:
        return ''
    needle = name.lower()
    for k, v in lr.headers.items():
        if k.lower() == needle:
            return v
    return ''


@th.django_unit_test()
def test_verify_pass_valid_cookie_returns_200(opts):
    """Cookie issued by /assess passes verify_pass on the same session."""
    resp = opts.client.post('/api/account/bouncer/assess', {
        'duid': TEST_DUID,
        'page_type': 'login',
        'session_id': 'sess-vp-clean',
        'signals': {
            'environment': {},
            'behavior': {'mouse_move_count': 14, 'first_interaction_ms': 600},
        },
    })
    assert_eq(resp.status_code, 200, f"assess setup must succeed, got {resp.status_code}")
    data = resp.json.data
    assert_true(data.decision in ('allow', 'monitor'),
                f"expected allow/monitor on clean signals, got {data.decision}")

    resp2 = opts.client.get('/api/account/bouncer/verify_pass')
    assert_eq(resp2.status_code, 200,
              f"valid mbp cookie should pass verify_pass, got {resp2.status_code}")
    muid_header = _hdr(opts.client, 'X-Bouncer-Muid')
    assert_true(muid_header, f"expected X-Bouncer-Muid header on 200")


@th.django_unit_test()
def test_verify_pass_missing_cookie_returns_401(opts):
    """No mbp cookie on the request → 401."""
    from testit.client import RestClient
    fresh = RestClient(opts.client.host)
    resp = fresh.get('/api/account/bouncer/verify_pass')
    assert_eq(resp.status_code, 401,
              f"no cookie should 401, got {resp.status_code}")


@th.django_unit_test()
def test_verify_pass_invalid_signature_returns_401(opts):
    """Tampered mbp cookie → 401."""
    from testit.client import RestClient
    fresh = RestClient(opts.client.host)
    fresh.session.cookies.set('mbp', 'tampered-muid:9999999999:zzzzzzzzzzzzzzzz')
    resp = fresh.get('/api/account/bouncer/verify_pass')
    assert_eq(resp.status_code, 401,
              f"tampered cookie should 401, got {resp.status_code}")


@th.django_unit_test()
def test_verify_pass_signature_match_returns_401_with_reason(opts):
    """Signature cache match → 401 with X-Bouncer-Reason: signature.

    A matched signature beats even a valid mbp cookie — the IP-level block
    takes precedence so nginx can deny access at the edge.
    """
    from mojo.apps.account.models import BotSignature
    from mojo.apps.account.services.bouncer.learner import refresh_sig_cache

    BotSignature.objects.create(
        sig_type='ip', value=TEST_IP, source='manual',
        confidence=100, is_active=True, block_count=1,
    )
    try:
        refresh_sig_cache()
        resp = opts.client.get('/api/account/bouncer/verify_pass')
        assert_eq(resp.status_code, 401,
                  f"signature match should 401 even with valid cookie, got {resp.status_code}")
        reason = _hdr(opts.client, 'X-Bouncer-Reason')
        assert_eq(reason, 'signature',
                  f"expected X-Bouncer-Reason=signature, got '{reason}'")
    finally:
        BotSignature.objects.filter(sig_type='ip', value=TEST_IP).delete()
        refresh_sig_cache()


@th.django_unit_test()
def test_pass_cookie_domain_attribute(opts):
    """BOUNCER_PASS_COOKIE_DOMAIN puts a Domain= attribute on the mbp cookie.

    Inspects the raw Set-Cookie header — `requests` would silently reject the
    cookie because the request host (127.0.0.1) doesn't suffix-match
    `.example.com`, so checking the cookie jar isn't reliable here.
    """
    with th.server_settings(BOUNCER_PASS_COOKIE_DOMAIN='.example.com'):
        from testit.client import RestClient
        fresh = RestClient(opts.client.host)
        resp = fresh.post('/api/account/bouncer/assess', {
            'duid': 'verify-pass-domain-duid',
            'page_type': 'login',
            'session_id': 'sess-vp-dom',
            'signals': {
                'environment': {},
                'behavior': {'mouse_move_count': 14, 'first_interaction_ms': 600},
            },
        })
        assert_eq(resp.status_code, 200,
                  f"assess must succeed, got {resp.status_code}")
        set_cookie = _hdr(fresh, 'Set-Cookie')
        assert_true('mbp=' in set_cookie,
                    f"expected mbp cookie in Set-Cookie header, got: {set_cookie[:200]!r}")
        # Domain attribute is case-insensitive; either form is valid.
        assert_true(
            'Domain=.example.com' in set_cookie or 'domain=.example.com' in set_cookie,
            f"expected Domain=.example.com in Set-Cookie, got: {set_cookie[:400]!r}"
        )
