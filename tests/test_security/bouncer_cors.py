"""
Tests for credentialed CORS on bouncer endpoints.

Verifies:
  - Bouncer path + allowlisted Origin → specific-origin + credentials headers
  - Bouncer path + non-allowlisted Origin → wildcard fallback (no credentials)
  - OPTIONS preflight gets the credentialed treatment for allowlisted origins
  - Non-bouncer paths keep the existing wildcard behavior (regression)
"""
from testit import helpers as th
from testit.helpers import assert_true, assert_eq


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


@th.django_unit_test()
def test_bouncer_path_allowlisted_origin_credentialed(opts):
    """Allowlisted Origin on bouncer path → specific-origin + Allow-Credentials."""
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
    """Non-allowlisted Origin → wildcard (no credentials)."""
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
