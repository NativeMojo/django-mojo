"""Decorator tests — verify @requires_geofence on built-in auth endpoints.

The decorator is applied to login, register, magic, OAuth begin/complete,
TOTP login, SMS login, passkey login, etc. We spot-check representative
endpoints to confirm:
  - block returns 403 with {reason, detail} ONLY (no country/region/abuse leak)
  - allow passes through
  - bypass_geofence permission bypasses block

Uses GEOFENCE_TEST_OVERRIDE to simulate country-of-origin.
"""
from testit import helpers as th


GEO_RU = {"country_code": "RU", "region_code": "RU-MOW",
          "is_tor": False, "is_vpn": False, "is_proxy": False, "is_datacenter": False}
GEO_US = {"country_code": "US", "region_code": "US-CA",
          "is_tor": False, "is_vpn": False, "is_proxy": False, "is_datacenter": False}

TEST_USER = "geofence_user@geofence.test"
TEST_PWORD = "Geo##fence99"
BYPASS_USER = "geofence_bypass@geofence.test"
BYPASS_PWORD = "Geo##bypass99"


@th.django_unit_setup()
def setup_decorator(opts):
    from mojo.apps.account.models import User
    from mojo.decorators.limits import clear_rate_limits

    clear_rate_limits(ip="127.0.0.1")

    User.objects.filter(email__icontains="@geofence.test").delete()

    user = User.objects.create_user(username=TEST_USER, email=TEST_USER, password=TEST_PWORD)
    user.is_email_verified = True
    user.requires_mfa = False
    user.save()
    opts.test_user_id = user.pk

    bypass_user = User.objects.create_user(
        username=BYPASS_USER, email=BYPASS_USER, password=BYPASS_PWORD)
    bypass_user.is_email_verified = True
    bypass_user.requires_mfa = False
    bypass_user.add_permission("bypass_geofence")
    bypass_user.save()
    opts.bypass_user_id = bypass_user.pk


def _attempt_login(opts, username, password, **settings_overrides):
    from mojo.decorators.limits import clear_rate_limits
    clear_rate_limits(ip="127.0.0.1")
    # Always include GEOFENCE_CACHE_TTL=0 so each test sees a fresh decision
    settings_overrides.setdefault("GEOFENCE_CACHE_TTL", 0)
    with th.server_settings(**settings_overrides):
        return opts.client.post("/api/auth/login", {"username": username, "password": password})


@th.django_unit_test("decorator: login blocked by country rule returns 403")
def test_login_blocked_by_country(opts):
    resp = _attempt_login(opts, TEST_USER, TEST_PWORD,
                          GEOFENCE_SYSTEM_RULES={"country": {"in": ["US"]}},
                          GEOFENCE_TEST_OVERRIDE=GEO_RU)
    assert resp.status_code == 403, \
        f"RU login must be blocked by country.in=[US], got {resp.status_code}: {opts.client.last_response.body}"


@th.django_unit_test("decorator: 403 body has reason+detail ONLY (no country/region/abuse leak)")
def test_403_body_omits_signals(opts):
    """SECURITY: a 403 from @requires_geofence must not leak detection capabilities.
    Only `reason` and `detail` go to the caller. Full decision is logged server-side."""
    resp = _attempt_login(opts, TEST_USER, TEST_PWORD,
                          GEOFENCE_SYSTEM_RULES={"abuse": {"tor": False}},
                          GEOFENCE_TEST_OVERRIDE={
                              "country_code": "US", "region_code": "US-CA",
                              "is_tor": True, "is_vpn": False,
                              "is_proxy": False, "is_datacenter": False,
                          })
    assert resp.status_code == 403, f"Tor block must 403, got {resp.status_code}"
    body = opts.client.last_response.body
    # reason MUST be present
    assert "reason" in body, f"403 body must include 'reason', got: {body}"
    # SECURITY assertions: these fields must NOT be in the 403 body
    for leaked in ("country_code", "country", "region_code", "region", "abuse"):
        assert leaked not in body, \
            f"SECURITY: 403 body must NOT leak {leaked!r}, got: {body}"


@th.django_unit_test("decorator: login passes when country is allowed")
def test_login_passes_when_allowed(opts):
    resp = _attempt_login(opts, TEST_USER, TEST_PWORD,
                          GEOFENCE_SYSTEM_RULES={"country": {"in": ["US"]}},
                          GEOFENCE_TEST_OVERRIDE=GEO_US)
    assert resp.status_code == 200, \
        f"US login must pass country.in=[US], got {resp.status_code}: {opts.client.last_response.body}"


@th.django_unit_test("decorator: bypass_geofence permission bypasses block")
def test_bypass_permission(opts):
    """User with bypass_geofence permission must pass through even when rules would block.
    Uses opts.client.login() so the JWT is auto-stored and sent on subsequent requests."""
    from mojo.decorators.limits import clear_rate_limits
    clear_rate_limits(ip="127.0.0.1")

    # First, log in BYPASS_USER under permissive settings so the JWT is stored
    # on the test client for subsequent auth'd requests.
    with th.server_settings(GEOFENCE_SYSTEM_RULES={}, GEOFENCE_CACHE_TTL=0):
        assert opts.client.login(BYPASS_USER, BYPASS_PWORD), \
            "bypass user login failed (needed to obtain JWT)"

    # Now hit /auth/handoff (which is @requires_auth + @requires_geofence) under
    # rules that would block. Bypass permission must short-circuit.
    clear_rate_limits(ip="127.0.0.1")
    with th.server_settings(GEOFENCE_SYSTEM_RULES={"country": {"in": ["US"]}},
                            GEOFENCE_TEST_OVERRIDE=GEO_RU,
                            GEOFENCE_CACHE_TTL=0):
        resp = opts.client.post("/api/auth/handoff", {})
    assert resp.status_code == 200, \
        f"bypass_geofence user must pass /auth/handoff even with RU IP and US-only rule, got {resp.status_code}: {opts.client.last_response.body}"

    opts.client.logout()


@th.django_unit_test("decorator: register blocked by country rule returns 403")
def test_register_blocked_by_country(opts):
    """Confirm the decorator is applied to /auth/register too."""
    from mojo.decorators.limits import clear_rate_limits
    clear_rate_limits(ip="127.0.0.1")
    with th.server_settings(ALLOW_USER_REGISTRATION=True,
                            GEOFENCE_SYSTEM_RULES={"country": {"in": ["US"]}},
                            GEOFENCE_TEST_OVERRIDE=GEO_RU,
                            GEOFENCE_CACHE_TTL=0):
        resp = opts.client.post("/api/auth/register",
                                {"email": "blocked@geofence.test", "password": "Block##99pw"})
    assert resp.status_code == 403, \
        f"RU register must be blocked, got {resp.status_code}: {opts.client.last_response.body}"


@th.django_unit_test("decorator: passes through when no rules configured (fast path)")
def test_no_rules_passthrough(opts):
    """The decorator must be a true no-op when no rules are configured —
    no geofence side effects in the response body."""
    resp = _attempt_login(opts, TEST_USER, TEST_PWORD,
                          GEOFENCE_SYSTEM_RULES={},
                          GEOFENCE_TEST_OVERRIDE=None)
    assert resp.status_code == 200, \
        f"login with no rules must pass through, got {resp.status_code}: {opts.client.last_response.body}"


@th.django_unit_test("decorator: GEOFENCE_ENABLED=False bypasses everything")
def test_master_kill_via_decorator(opts):
    resp = _attempt_login(opts, TEST_USER, TEST_PWORD,
                          GEOFENCE_ENABLED=False,
                          GEOFENCE_SYSTEM_RULES={"country": {"in": ["US"]}},
                          GEOFENCE_TEST_OVERRIDE=GEO_RU)
    assert resp.status_code == 200, \
        f"GEOFENCE_ENABLED=False must allow even blocked countries, got {resp.status_code}"
