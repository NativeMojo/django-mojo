"""
Tests for BOUNCER_ALLOW_ANY_ORIGIN — the deploy-time flag that stops the
bouncer CORS allowlist being consulted on the public bouncer API.

Security contracts pinned here:
  - Flag unset → behavior is identical to before the flag existed (allowlist
    still consulted, non-http allowlist entries still echoed)
  - Flag on → any well-formed http(s) Origin is echoed with credentials on
    /api/account/bouncer/{assess,event,message}
  - `Access-Control-Allow-Origin: *` NEVER co-occurs with
    `Access-Control-Allow-Credentials: true`
  - `Origin: null` and malformed origins are refused even with the flag on
  - A garbage flag value fails CLOSED
  - The permission-gated admin endpoints (device/signal/signature) and
    verify_pass (sole carrier of X-Bouncer-Muid) never get the bypass
  - The OPTIONS preflight and the real request take the identical decision

assess/event/message are POST-only, so the GETs below return 404. That is
deliberate: CORSMiddleware is the outermost middleware, so the headers under
test are stamped regardless, and a 404 keeps these tests off the rate limiter
and out of the BouncerSignal table.
"""
from testit import helpers as th
from testit.helpers import assert_true, assert_eq

ALLOWED = 'https://app.example.com'
# A legal allowlist entry that is NOT a well-formed http(s) origin. Hybrid
# mobile shells send exactly this, and operators put it in the allowlist today.
# It must keep working with the flag off AND on — the allowlist is tested first,
# unconditionally, so _is_echoable_origin never sees it.
ALLOWED_CAPACITOR = 'capacitor://localhost'
UNLISTED = 'https://tenant-domain.example'

ASSESS = '/api/account/bouncer/assess'
EVENT = '/api/account/bouncer/event'
MESSAGE = '/api/account/bouncer/message'
VERIFY_PASS = '/api/account/bouncer/verify_pass'
DEVICE = '/api/account/bouncer/device'
SIGNAL = '/api/account/bouncer/signal'
SIGNATURE = '/api/account/bouncer/signature'


@th.django_unit_setup()
def setup(opts):
    from mojo.decorators.limits import clear_rate_limits
    clear_rate_limits(ip='127.0.0.1')


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


def _options(client, path, headers):
    """RestClient has no .options() helper; use its requests.Session directly."""
    url = f"{client.host}{path.lstrip('/')}"
    return client.session.options(url, headers=headers)


def _cors(client, path, origin=None):
    """GET `path` with an optional Origin; return (allow_origin, allow_credentials)."""
    headers = {'Origin': origin} if origin is not None else {}
    client.get(path, headers=headers)
    return (_hdr(client, 'Access-Control-Allow-Origin'),
            _hdr(client, 'Access-Control-Allow-Credentials'))


def _assert_no_credentials(origin, creds, where):
    assert_eq(origin, '*',
              f"{where}: expected wildcard origin, got '{origin}'")
    assert_true(creds == '' or creds.lower() == 'false',
                f"{where}: expected no Allow-Credentials, got '{creds}'")


# ---------------------------------------------------------------------------
# 1-2. Flag unset == today, exactly
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_allow_any_origin_unset_keeps_allowlist_behavior(opts):
    """Flag unset: an unlisted origin still gets the wildcard, no credentials."""
    with th.server_settings(BOUNCER_ALLOWED_ORIGINS=[ALLOWED]):
        origin, creds = _cors(opts.client, ASSESS, UNLISTED)
        _assert_no_credentials(origin, creds, 'flag unset + unlisted origin')


@th.django_unit_test()
def test_allow_any_origin_unset_allowlist_still_echoes(opts):
    """Flag unset: allowlisted origins are echoed with credentials — including
    a non-http(s) scheme, which the echo-branch guard would reject. This is the
    proof that the guard did not get hoisted above the allowlist test."""
    with th.server_settings(BOUNCER_ALLOWED_ORIGINS=[ALLOWED, ALLOWED_CAPACITOR]):
        origin, creds = _cors(opts.client, ASSESS, ALLOWED)
        assert_eq(origin, ALLOWED,
                  f"allowlisted origin should be echoed, got '{origin}'")
        assert_eq(creds, 'true',
                  f"allowlisted origin should get Allow-Credentials=true, got '{creds}'")

        origin, creds = _cors(opts.client, ASSESS, ALLOWED_CAPACITOR)
        assert_eq(origin, ALLOWED_CAPACITOR,
                  f"non-http allowlist entry '{ALLOWED_CAPACITOR}' must still be "
                  f"echoed with the flag off, got '{origin}'")
        assert_eq(creds, 'true',
                  f"non-http allowlist entry should get Allow-Credentials=true, got '{creds}'")


# ---------------------------------------------------------------------------
# 3-4. Flag on: the feature, and the wildcard/credentials invariant
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_allow_any_origin_echoes_unlisted_origin(opts):
    """Flag on: an unlisted origin is echoed with credentials on all three
    public endpoints, and the allowlist remains additive rather than shadowed."""
    with th.server_settings(BOUNCER_ALLOW_ANY_ORIGIN=True,
                            BOUNCER_ALLOWED_ORIGINS=[ALLOWED_CAPACITOR]):
        for path in (ASSESS, EVENT, MESSAGE):
            origin, creds = _cors(opts.client, path, UNLISTED)
            assert_eq(origin, UNLISTED,
                      f"{path}: expected the request origin echoed, got '{origin}'")
            assert_eq(creds, 'true',
                      f"{path}: expected Allow-Credentials=true, got '{creds}'")

        origin, creds = _cors(opts.client, ASSESS, ALLOWED_CAPACITOR)
        assert_eq(origin, ALLOWED_CAPACITOR,
                  f"allowlist must still win with the flag on, got '{origin}'")
        assert_eq(creds, 'true',
                  f"allowlisted origin should get Allow-Credentials=true, got '{creds}'")


@th.django_unit_test()
def test_allow_any_origin_never_sends_wildcard_with_credentials(opts):
    """Flag on: `*` and Allow-Credentials: true can never co-occur. Checked
    against an echoed origin, a refused origin, and an excluded path."""
    with th.server_settings(BOUNCER_ALLOW_ANY_ORIGIN=True,
                            BOUNCER_ALLOWED_ORIGINS=[ALLOWED]):
        cases = [
            (ASSESS, UNLISTED, 'echoed origin'),
            (ASSESS, 'null', 'refused null origin'),
            (ASSESS, 'not-a-url', 'refused malformed origin'),
            (ASSESS, None, 'no Origin header'),
            (DEVICE, UNLISTED, 'excluded admin path'),
            (VERIFY_PASS, UNLISTED, 'excluded verify_pass path'),
        ]
        for path, origin_header, label in cases:
            origin, creds = _cors(opts.client, path, origin_header)
            if creds.lower() == 'true':
                assert_true(origin != '*',
                            f"{label}: Allow-Credentials=true was sent together with "
                            f"Allow-Origin '*' — a credentialed wildcard")
            if origin == '*':
                assert_true(creds == '' or creds.lower() == 'false',
                            f"{label}: Allow-Origin '*' was sent together with "
                            f"Allow-Credentials '{creds}'")


# ---------------------------------------------------------------------------
# 5-7. Fail-closed cases
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_allow_any_origin_rejects_null_origin(opts):
    """Flag on: `Origin: null` (sandboxed iframe, data:, file://) is refused."""
    with th.server_settings(BOUNCER_ALLOW_ANY_ORIGIN=True,
                            BOUNCER_ALLOWED_ORIGINS=[]):
        origin, creds = _cors(opts.client, ASSESS, 'null')
        _assert_no_credentials(origin, creds, "flag on + Origin: null")


@th.django_unit_test()
def test_allow_any_origin_rejects_malformed_origin(opts):
    """Flag on: anything that is not a bare scheme://host[:port] is refused.

    The CR/LF/tab cases are asserted directly against `_is_echoable_origin`
    rather than over HTTP: `requests` refuses to transmit a header value
    containing CR/LF, so the wire path cannot reach the server-side guard. The
    guard still matters — Django raises BadHeaderError on CR/LF, which from
    inside middleware would be an unhandled 500 rather than a graceful deny.
    """
    from mojo.middleware.cors import _is_echoable_origin

    for value in ('https://a.example\r\nX-Evil: 1',
                  'https://a.example\nX-Evil: 1',
                  'https://a.example\tevil',
                  'https://a.example evil.example',
                  ' https://a.example'):
        assert_true(not _is_echoable_origin(value),
                    f"_is_echoable_origin must reject the whitespace/CRLF-bearing "
                    f"origin {value!r}")
    assert_true(_is_echoable_origin('https://tenant.example:8443'),
                "_is_echoable_origin must accept a plain https origin with a port")

    with th.server_settings(BOUNCER_ALLOW_ANY_ORIGIN=True,
                            BOUNCER_ALLOWED_ORIGINS=[]):
        bad = [
            'not-a-url',
            'https://a.example/path',
            'https://a.example/?q=1',
            'javascript:alert(1)',
            'https://a.example evil.example',
            'https://',
        ]
        for value in bad:
            origin, creds = _cors(opts.client, ASSESS, value)
            _assert_no_credentials(origin, creds, f"flag on + malformed Origin {value!r}")


@th.django_unit_test()
def test_allow_any_origin_garbage_value_fails_closed(opts):
    """An uncoercible flag value degrades to the declared default False."""
    with th.server_settings(BOUNCER_ALLOW_ANY_ORIGIN='maybe',
                            BOUNCER_ALLOWED_ORIGINS=[ALLOWED]):
        origin, creds = _cors(opts.client, ASSESS, UNLISTED)
        _assert_no_credentials(origin, creds, "BOUNCER_ALLOW_ANY_ORIGIN='maybe'")


# ---------------------------------------------------------------------------
# 8-9. Paths the bypass must never reach
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_allow_any_origin_excludes_admin_endpoints(opts):
    """Flag on: the permission-gated admin endpoints never get the bypass.
    They return device fingerprints, IPs, muids and geo; the bypass must stay
    strictly narrower than an operator-curated allowlist. Headers only — the
    body is 401/403 and that is fine."""
    with th.server_settings(BOUNCER_ALLOW_ANY_ORIGIN=True,
                            BOUNCER_ALLOWED_ORIGINS=[]):
        for path in (DEVICE, SIGNAL, SIGNATURE, f'{DEVICE}/5'):
            origin, creds = _cors(opts.client, path, 'https://evil.example')
            _assert_no_credentials(origin, creds, f"flag on + admin path {path}")


@th.django_unit_test()
def test_allow_any_origin_excludes_verify_pass(opts):
    """Flag on: verify_pass is excluded. It is the sole carrier of
    X-Bouncer-Muid (a stable device id, and exposed via
    Access-Control-Expose-Headers) and exists for nginx auth_request, which is
    server-to-server and ignores CORS — no browser client needs it."""
    with th.server_settings(BOUNCER_ALLOW_ANY_ORIGIN=True,
                            BOUNCER_ALLOWED_ORIGINS=[]):
        origin, creds = _cors(opts.client, VERIFY_PASS, 'https://evil.example')
        _assert_no_credentials(origin, creds, "flag on + verify_pass")


# ---------------------------------------------------------------------------
# 10-11. Preflight agreement, and the no-Origin case
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_allow_any_origin_preflight_matches_actual(opts):
    """The decision is path-based, never method-based: an OPTIONS preflight and
    the real request must agree, on a bypassed path and an excluded one."""
    with th.server_settings(BOUNCER_ALLOW_ANY_ORIGIN=True,
                            BOUNCER_ALLOWED_ORIGINS=[]):
        for path in (ASSESS, DEVICE):
            actual_origin, actual_creds = _cors(opts.client, path, UNLISTED)
            pre = _options(opts.client, path, {
                'Origin': UNLISTED,
                'Access-Control-Request-Method': 'POST',
                'Access-Control-Request-Headers': 'Content-Type',
            })
            pre_origin = pre.headers.get('Access-Control-Allow-Origin', '')
            pre_creds = pre.headers.get('Access-Control-Allow-Credentials', '')
            assert_eq(pre_origin, actual_origin,
                      f"{path}: preflight Allow-Origin '{pre_origin}' disagrees with "
                      f"the actual request's '{actual_origin}'")
            assert_eq(pre_creds, actual_creds,
                      f"{path}: preflight Allow-Credentials '{pre_creds}' disagrees "
                      f"with the actual request's '{actual_creds}'")


@th.django_unit_test()
def test_allow_any_origin_no_origin_header_gets_wildcard(opts):
    """Flag on, no Origin header at all → wildcard, no credentials."""
    with th.server_settings(BOUNCER_ALLOW_ANY_ORIGIN=True,
                            BOUNCER_ALLOWED_ORIGINS=[]):
        origin, creds = _cors(opts.client, ASSESS, None)
        _assert_no_credentials(origin, creds, "flag on + no Origin header")
