"""Decorator tests — confirm @requires_geofence on built-in auth endpoints.

Per-request X-Mojo-Test-* headers replace th.server_settings() so tests run
fast and in parallel.
"""
import uuid as _uuid
from testit import helpers as th
from tests.test_geofence._helpers import headers, GEO_RU, GEO_US


@th.django_unit_setup()
def setup_decorator(opts):
    from mojo.apps.account.models import User

    # Per-setup unique users so tests in this file can run in parallel with
    # tests in other modules without colliding on usernames/emails.
    suffix = _uuid.uuid4().hex[:8]
    opts.test_email = f"geofence_user_{suffix}@geofence.test"
    opts.test_password = "Geo##fence99"
    opts.bypass_email = f"geofence_bypass_{suffix}@geofence.test"
    opts.bypass_password = "Geo##bypass99"

    user = User.objects.create_user(
        username=opts.test_email, email=opts.test_email, password=opts.test_password)
    user.is_email_verified = True
    user.requires_mfa = False
    user.save()
    opts.test_user_id = user.pk

    bypass_user = User.objects.create_user(
        username=opts.bypass_email, email=opts.bypass_email, password=opts.bypass_password)
    bypass_user.is_email_verified = True
    bypass_user.requires_mfa = False
    bypass_user.add_permission("bypass_geofence")
    bypass_user.save()
    opts.bypass_user_id = bypass_user.pk


def _login(opts, username, password, **header_kwargs):
    from mojo.decorators.limits import clear_rate_limits
    # All tests share IP 127.0.0.1; clear per-IP login bucket so parallel
    # tests / repeated calls don't hit the strict login limiter.
    clear_rate_limits(ip="127.0.0.1", key="login")
    return opts.client.post(
        "/api/auth/login", {"username": username, "password": password},
        headers=headers(**header_kwargs))


@th.django_unit_test("decorator: login blocked by country rule returns 403")
def test_login_blocked_by_country(opts):
    resp = _login(opts, opts.test_email, opts.test_password,
                  geo=GEO_RU, system_rules={"country": {"in": ["US"]}})
    assert resp.status_code == 403, \
        f"RU login must be blocked, got {resp.status_code}: {opts.client.last_response.body}"


@th.django_unit_test("decorator: 403 body has reason+detail ONLY (no country/region/abuse leak)")
def test_403_body_omits_signals(opts):
    """SECURITY: 403 must not leak detection capabilities."""
    resp = _login(opts, opts.test_email, opts.test_password,
                  geo={"country_code": "US", "region_code": "US-CA",
                       "is_tor": True, "is_vpn": False,
                       "is_proxy": False, "is_datacenter": False},
                  system_rules={"abuse": {"tor": False}})
    assert resp.status_code == 403, f"Tor block must 403, got {resp.status_code}"
    body = opts.client.last_response.body
    assert "reason" in body, f"403 body must include 'reason', got: {body}"
    for leaked in ("country_code", "country", "region_code", "region", "abuse"):
        assert leaked not in body, \
            f"SECURITY: 403 body must NOT leak {leaked!r}, got: {body}"


@th.django_unit_test("decorator: login passes when country is allowed")
def test_login_passes_when_allowed(opts):
    resp = _login(opts, opts.test_email, opts.test_password,
                  geo=GEO_US, system_rules={"country": {"in": ["US"]}})
    assert resp.status_code == 200, \
        f"US login must pass, got {resp.status_code}: {opts.client.last_response.body}"


@th.django_unit_test("decorator: bypass_geofence permission bypasses block")
def test_bypass_permission(opts):
    """User with bypass_geofence passes through even when rules would block."""
    from mojo.decorators.limits import clear_rate_limits
    clear_rate_limits(ip="127.0.0.1", key="login")
    assert opts.client.login(opts.bypass_email, opts.bypass_password), \
        "bypass user login failed (needed for auth'd /auth/handoff test)"

    resp = opts.client.post(
        "/api/auth/handoff", {},
        headers=headers(geo=GEO_RU, system_rules={"country": {"in": ["US"]}}))
    assert resp.status_code == 200, \
        f"bypass_geofence user must pass /auth/handoff even with RU IP, got {resp.status_code}: {opts.client.last_response.body}"

    opts.client.logout()


@th.django_unit_test("decorator: register blocked by country rule returns 403")
def test_register_blocked_by_country(opts):
    """Confirms the decorator is applied to /auth/register too."""
    from mojo.decorators.limits import clear_rate_limits
    clear_rate_limits(ip="127.0.0.1", key="register")
    h = headers(geo=GEO_RU, system_rules={"country": {"in": ["US"]}})
    h["X-Mojo-Test-Allow-User-Registration"] = "1"
    blocked_email = f"blocked_{_uuid.uuid4().hex[:8]}@geofence.test"
    resp = opts.client.post(
        "/api/auth/register",
        {"email": blocked_email, "password": "Block##99pw"},
        headers=h)
    assert resp.status_code == 403, \
        f"RU register must be blocked, got {resp.status_code}: {opts.client.last_response.body}"


@th.django_unit_test("decorator: passes through when no rules configured (fast path)")
def test_no_rules_passthrough(opts):
    """No-op default state: no rules → no blocking."""
    resp = _login(opts, opts.test_email, opts.test_password,
                  system_rules={})  # explicitly no rules
    assert resp.status_code == 200, \
        f"login with no rules must pass, got {resp.status_code}: {opts.client.last_response.body}"


@th.django_unit_test("decorator: GEOFENCE_ENABLED=False bypasses everything")
def test_master_kill_via_decorator(opts):
    resp = _login(opts, opts.test_email, opts.test_password,
                  geo=GEO_RU, system_rules={"country": {"in": ["US"]}},
                  enabled=False)
    assert resp.status_code == 200, \
        f"GEOFENCE_ENABLED=False must allow even blocked countries, got {resp.status_code}"
