"""
Tests for CORS on bouncer endpoints — the BOUNCER_ALLOW_ANY_ORIGIN default and
the BOUNCER_ALLOWED_ORIGINS allowlist. Both live here because both are decided
by the one CORS middleware, in the same handful of settings states.

BOUNCER_ALLOW_ANY_ORIGIN defaults to **True**: this is an open REST API
platform, so credentialed cross-origin access to the public bouncer API is on
out of the box. Setting the flag False is the opt-OUT that restricts those
endpoints to BOUNCER_ALLOWED_ORIGINS.

Default (flag unset → permissive) contracts:
  - Any well-formed http(s) Origin is echoed with credentials on
    /api/account/bouncer/{assess,event,message}
  - The permission-gated admin endpoints (device/signal/signature) and
    verify_pass (sole carrier of X-Bouncer-Muid) are never covered
  - `Origin: null` and malformed origins are refused
  - `Access-Control-Allow-Origin: *` NEVER co-occurs with
    `Access-Control-Allow-Credentials: true`
  - The OPTIONS preflight and the real request take the identical decision
  - A garbage flag value degrades to the declared default, i.e. permissive

Allowlist contracts (the allowlist is tested FIRST, unconditionally, so it is
additive under the permissive default and authoritative under the opt-out):
  - Bouncer path + allowlisted Origin → specific-origin + credentials headers
  - Flag False + allowlist populated → listed origin echoed, unlisted gets the
    wildcard fallback with no credentials (the opt-out escape hatch)
  - Non-bouncer paths keep the existing wildcard behavior (regression)

Every `th.server_settings()` context is a full server reload (~5s), so tests
are grouped by SETTINGS STATE rather than one context per behavior. Four
states genuinely differ and must stay separate contexts: default/unset, flag
explicitly on, flag explicitly off, and a garbage flag value. Do not add a
fifth for a behavior that already has a home in one of them.

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
EVIL = 'https://evil.example'

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
# BOUNCER_ALLOWED_ORIGINS allowlist
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_bouncer_path_allowlisted_origin_credentialed(opts):
    """Allowlisted Origin on bouncer path → specific-origin + Allow-Credentials.

    Uses verify_pass, which is NOT in the any-origin public set, so this asserts
    the allowlist on its own regardless of what BOUNCER_ALLOW_ANY_ORIGIN
    defaults to.
    """
    with th.server_settings(BOUNCER_ALLOWED_ORIGINS=['https://app.example.com']):
        resp = opts.client.get(
            '/api/account/bouncer/verify_pass',
            headers={'Origin': 'https://app.example.com'},
        )
        origin = _hdr(opts.client, 'Access-Control-Allow-Origin')
        creds = _hdr(opts.client, 'Access-Control-Allow-Credentials')
        assert_eq(origin, 'https://app.example.com',
                  f"expected specific Origin, got '{origin}' (status={resp.status_code})")
        assert_eq(creds, 'true',
                  f"expected Allow-Credentials=true, got '{creds}'")


@th.django_unit_test()
def test_bouncer_path_non_allowlisted_origin_wildcard(opts):
    """Non-allowlisted Origin on an uncovered bouncer path → wildcard, no creds.

    The path is verify_pass, which the any-origin default never covers, so this
    holds under the permissive default WITHOUT setting the flag — and that is
    the point: it proves verify_pass stays excluded even when any-origin is on.
    Do not add BOUNCER_ALLOW_ANY_ORIGIN=False here; it would cost a server
    reload and assert strictly less.
    """
    with th.server_settings(BOUNCER_ALLOWED_ORIGINS=['https://app.example.com']):
        resp = opts.client.get(
            '/api/account/bouncer/verify_pass',
            headers={'Origin': 'https://attacker.example.com'},
        )
        origin = _hdr(opts.client, 'Access-Control-Allow-Origin')
        creds = _hdr(opts.client, 'Access-Control-Allow-Credentials')
        assert_eq(origin, '*',
                  f"non-allowlisted origin should get wildcard, got '{origin}' (status={resp.status_code})")
        assert_true(creds == '' or creds.lower() == 'false',
                    f"non-allowlisted should not get credentials, got '{creds}'")


@th.django_unit_test()
def test_options_preflight_allowlisted_credentialed(opts):
    """OPTIONS preflight from allowlisted origin returns credentialed headers."""
    with th.server_settings(BOUNCER_ALLOWED_ORIGINS=['https://app.example.com']):
        response = _options(opts.client, '/api/account/bouncer/assess', {
            'Origin': 'https://app.example.com',
            'Access-Control-Request-Method': 'POST',
            'Access-Control-Request-Headers': 'Content-Type',
        })
        origin = response.headers.get('Access-Control-Allow-Origin', '')
        creds = response.headers.get('Access-Control-Allow-Credentials', '')
        assert_eq(origin, 'https://app.example.com',
                  f"preflight should return specific origin, got '{origin}'")
        assert_eq(creds, 'true',
                  f"preflight should set Allow-Credentials=true, got '{creds}'")


@th.django_unit_test()
def test_non_bouncer_path_unchanged(opts):
    """Regression: non-bouncer paths still get the wildcard origin."""
    with th.server_settings(BOUNCER_ALLOWED_ORIGINS=['https://app.example.com']):
        opts.client.get(
            '/api/version',
            headers={'Origin': 'https://app.example.com'},
        )
        origin = _hdr(opts.client, 'Access-Control-Allow-Origin')
        if origin:
            assert_eq(origin, '*',
                      f"non-bouncer path should keep wildcard, got '{origin}'")


# ---------------------------------------------------------------------------
# BOUNCER_ALLOW_ANY_ORIGIN — state 1 of 4: flag unset (== the default, permissive)
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_allow_any_origin_unset_echoes_any_origin_on_public_endpoints(opts):
    """Flag UNSET is permissive: unlisted origins are echoed on the public API.

    This is the default-behavior proof. BOUNCER_ALLOW_ANY_ORIGIN defaults to
    True, so a deployment that never mentions the setting answers credentialed
    cross-origin requests from any well-formed origin on assess/event/message —
    while the uncovered paths (admin endpoints, verify_pass) still do not.

    One settings state, so one context: the echo case, the allowlist-additive
    case and the exclusion cases share it rather than reloading the server.
    """
    with th.server_settings(BOUNCER_ALLOWED_ORIGINS=[ALLOWED, ALLOWED_CAPACITOR]):
        # An unlisted origin IS echoed with credentials on every public
        # endpoint, with no flag set anywhere.
        for path in (ASSESS, EVENT, MESSAGE):
            origin, creds = _cors(opts.client, path, UNLISTED)
            assert_eq(origin, UNLISTED,
                      f"default/{path}: an unlisted origin should be echoed "
                      f"under the permissive default, got '{origin}'")
            assert_eq(creds, 'true',
                      f"default/{path}: expected Allow-Credentials=true, "
                      f"got '{creds}'")

        # Allowlisted origins are echoed with credentials — including a
        # non-http(s) scheme, which the echo-branch guard would reject. This is
        # the proof that the guard did not get hoisted above the allowlist test.
        for value in (ALLOWED, ALLOWED_CAPACITOR):
            origin, creds = _cors(opts.client, ASSESS, value)
            assert_eq(origin, value,
                      f"default + allowlisted {value!r}: should be echoed, "
                      f"got '{origin}'")
            assert_eq(creds, 'true',
                      f"default + allowlisted {value!r}: should get "
                      f"Allow-Credentials=true, got '{creds}'")

        # The exclusions hold under the default, not just under an explicit
        # flag: the permission-gated admin endpoints stay uncovered.
        for path in (DEVICE, SIGNAL, SIGNATURE, f'{DEVICE}/5'):
            origin, creds = _cors(opts.client, path, UNLISTED)
            _assert_no_credentials(origin, creds, f"default + admin path {path}")

        # ...and so does verify_pass, the sole carrier of X-Bouncer-Muid.
        origin, creds = _cors(opts.client, VERIFY_PASS, UNLISTED)
        _assert_no_credentials(origin, creds, "default + verify_pass")


# ---------------------------------------------------------------------------
# BOUNCER_ALLOW_ANY_ORIGIN — state 2 of 4: flag explicitly on
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_allow_any_origin_enabled_echo_exclusions_and_invariants(opts):
    """Flag on: every contract that holds in the single "flag enabled" state.

    Covers, in order — each block labels its own assertions so a failure names
    the exact case that broke:
      1. the echo itself, on all three public endpoints
      2. the allowlist stays additive rather than shadowed
      3. `Origin: null` is refused
      4. malformed origins are refused
      5. no Origin header at all → wildcard, no credentials
      6. the permission-gated admin endpoints never get the bypass — they return
         device fingerprints, IPs, muids and geo, so the bypass must stay
         strictly narrower than an operator-curated allowlist (headers only; the
         body is 401/403 and that is fine)
      7. verify_pass never gets the bypass — it is the sole carrier of
         X-Bouncer-Muid (a stable device id, exposed via
         Access-Control-Expose-Headers) and exists for nginx auth_request, which
         is server-to-server and ignores CORS
      8. `*` and Allow-Credentials: true can never co-occur
      9. the OPTIONS preflight and the real request take the identical decision —
         the test is path-based, never method-based

    These were nine separate tests, i.e. nine server reloads (~5s each), all in
    one settings state. The allowlist here holds exactly one entry, and it is a
    non-http(s) one that none of the origins below can match: it therefore
    behaves as an empty allowlist for every deny case, while additionally
    proving the allowlist is still consulted first when the flag is on.
    """
    with th.server_settings(BOUNCER_ALLOW_ANY_ORIGIN=True,
                            BOUNCER_ALLOWED_ORIGINS=[ALLOWED_CAPACITOR]):
        # 1. An unlisted origin is echoed with credentials on every public
        #    endpoint.
        for path in (ASSESS, EVENT, MESSAGE):
            origin, creds = _cors(opts.client, path, UNLISTED)
            assert_eq(origin, UNLISTED,
                      f"echo/{path}: expected the request origin echoed, got '{origin}'")
            assert_eq(creds, 'true',
                      f"echo/{path}: expected Allow-Credentials=true, got '{creds}'")

        # 2. The allowlist remains additive rather than shadowed.
        origin, creds = _cors(opts.client, ASSESS, ALLOWED_CAPACITOR)
        assert_eq(origin, ALLOWED_CAPACITOR,
                  f"allowlist-wins/{ALLOWED_CAPACITOR}: allowlist must still win "
                  f"with the flag on, got '{origin}'")
        assert_eq(creds, 'true',
                  f"allowlist-wins/{ALLOWED_CAPACITOR}: expected "
                  f"Allow-Credentials=true, got '{creds}'")

        # 3. `Origin: null` (sandboxed iframe, data:, file://) is refused.
        origin, creds = _cors(opts.client, ASSESS, 'null')
        _assert_no_credentials(origin, creds, "flag on + Origin: null")

        # 4. Anything that is not a bare scheme://host[:port] is refused.
        for value in ('not-a-url',
                      'https://a.example/path',
                      'https://a.example/?q=1',
                      'javascript:alert(1)',
                      'https://a.example evil.example',
                      'https://'):
            origin, creds = _cors(opts.client, ASSESS, value)
            _assert_no_credentials(origin, creds,
                                   f"flag on + malformed Origin {value!r}")

        # 5. No Origin header at all → wildcard, no credentials.
        origin, creds = _cors(opts.client, ASSESS, None)
        _assert_no_credentials(origin, creds, "flag on + no Origin header")

        # 6. The permission-gated admin endpoints never get the bypass.
        for path in (DEVICE, SIGNAL, SIGNATURE, f'{DEVICE}/5'):
            origin, creds = _cors(opts.client, path, EVIL)
            _assert_no_credentials(origin, creds, f"flag on + admin path {path}")

        # 7. verify_pass never gets the bypass.
        origin, creds = _cors(opts.client, VERIFY_PASS, EVIL)
        _assert_no_credentials(origin, creds, "flag on + verify_pass")

        # 8. `*` and Allow-Credentials: true can never co-occur — checked
        #    against an echoed origin, refused origins, and excluded paths.
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
                            f"wildcard-invariant/{label}: Allow-Credentials=true was "
                            f"sent together with Allow-Origin '*' — a credentialed "
                            f"wildcard")
            if origin == '*':
                assert_true(creds == '' or creds.lower() == 'false',
                            f"wildcard-invariant/{label}: Allow-Origin '*' was sent "
                            f"together with Allow-Credentials '{creds}'")

        # 9. The preflight and the real request must agree, on a bypassed path
        #    and an excluded one.
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
                      f"preflight/{path}: preflight Allow-Origin '{pre_origin}' "
                      f"disagrees with the actual request's '{actual_origin}'")
            assert_eq(pre_creds, actual_creds,
                      f"preflight/{path}: preflight Allow-Credentials '{pre_creds}' "
                      f"disagrees with the actual request's '{actual_creds}'")


@th.django_unit_test()
def test_is_echoable_origin_rejects_crlf_and_whitespace(opts):
    """The CR/LF/tab origins, asserted directly against `_is_echoable_origin`.

    Not over HTTP, and so deliberately outside any settings context: `requests`
    refuses to transmit a header value containing CR/LF, so the wire path cannot
    reach the server-side guard. The guard still matters — Django raises
    BadHeaderError on CR/LF, which from inside middleware would be an unhandled
    500 rather than a graceful deny. Calling it in-process costs no reload.
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


# ---------------------------------------------------------------------------
# BOUNCER_ALLOW_ANY_ORIGIN — state 3 of 4: flag explicitly off (the opt-out)
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_allow_any_origin_false_restores_strict_allowlist(opts):
    """Flag False + populated allowlist → strict allowlist enforcement.

    This is the escape hatch: the ONLY way to restrict the public bouncer API to
    a curated set of origins now that any-origin is the default. Listed origin
    echoed with credentials; unlisted origin gets the wildcard and no
    credentials. If this ever stops holding, operators who deliberately opted
    out are silently back to permissive and have no way to tell.
    """
    with th.server_settings(BOUNCER_ALLOW_ANY_ORIGIN=False,
                            BOUNCER_ALLOWED_ORIGINS=[ALLOWED, ALLOWED_CAPACITOR]):
        # A listed origin is still echoed with credentials on the public API.
        for value in (ALLOWED, ALLOWED_CAPACITOR):
            origin, creds = _cors(opts.client, ASSESS, value)
            assert_eq(origin, value,
                      f"opt-out + allowlisted {value!r}: should still be "
                      f"echoed, got '{origin}'")
            assert_eq(creds, 'true',
                      f"opt-out + allowlisted {value!r}: expected "
                      f"Allow-Credentials=true, got '{creds}'")

        # An unlisted origin is refused on every public endpoint — the whole
        # point of opting out.
        for path in (ASSESS, EVENT, MESSAGE):
            origin, creds = _cors(opts.client, path, UNLISTED)
            _assert_no_credentials(origin, creds,
                                   f"opt-out + unlisted origin on {path}")


# ---------------------------------------------------------------------------
# BOUNCER_ALLOW_ANY_ORIGIN — state 4 of 4: garbage flag value
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_allow_any_origin_garbage_value_falls_back_to_permissive_default(opts):
    """An uncoercible flag value degrades to the declared default, now True.

    `_convert_value` returns the DECLARED default for an unrecognized string, and
    the declared default is True — so a typo'd value degrades OPEN, not closed.
    An operator who meant to opt out and wrote `BOUNCER_ALLOW_ANY_ORIGIN =
    'maybe'` does NOT get the restriction; they get the permissive default plus a
    coercion warning in the log. Pinned here so that is a known, tested property
    rather than a surprise.
    """
    with th.server_settings(BOUNCER_ALLOW_ANY_ORIGIN='maybe',
                            BOUNCER_ALLOWED_ORIGINS=[ALLOWED]):
        origin, creds = _cors(opts.client, ASSESS, UNLISTED)
        assert_eq(origin, UNLISTED,
                  f"BOUNCER_ALLOW_ANY_ORIGIN='maybe' should degrade to the "
                  f"declared default True and echo the origin, got '{origin}'")
        assert_eq(creds, 'true',
                  f"BOUNCER_ALLOW_ANY_ORIGIN='maybe': expected "
                  f"Allow-Credentials=true, got '{creds}'")
