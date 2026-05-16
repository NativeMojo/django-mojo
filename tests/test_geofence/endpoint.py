"""Pre-flight endpoint tests: GET /api/geo/check.

Covers:
  - Returns full GeoDecision (different shape from decorator 403)
  - group_uuid resolution
  - Unknown group_uuid → 400
  - Inactive group → system-only with detail flag
  - Endpoint itself is NOT geofenced (blocked users still get a response)
"""
from testit import helpers as th


GEO_RU = {"country_code": "RU", "region_code": "RU-MOW",
          "is_tor": False, "is_vpn": False, "is_proxy": False, "is_datacenter": False}
GEO_US = {"country_code": "US", "region_code": "US-CA",
          "is_tor": False, "is_vpn": False, "is_proxy": False, "is_datacenter": False}


@th.django_unit_setup()
def setup_endpoint(opts):
    from mojo.apps.account.models import User
    from mojo.apps.account.models.group import Group
    from mojo.decorators.limits import clear_rate_limits

    clear_rate_limits(ip="127.0.0.1")

    User.objects.filter(email__icontains="@geofence.test").delete()
    Group.objects.filter(name__startswith="Geofence Test").delete()

    grp = Group.objects.create(name="Geofence Test Group", is_active=True)
    grp.metadata = {"geofence": {"country": {"in": ["US"]}}}
    grp.save()
    opts.test_group_uuid = grp.get_uuid()
    opts.test_group_id = grp.pk

    inactive = Group.objects.create(name="Geofence Test Inactive", is_active=False)
    opts.inactive_group_uuid = inactive.get_uuid()


@th.django_unit_test("endpoint: GET /api/geo/check returns full GeoDecision shape")
def test_returns_full_decision(opts):
    from mojo.decorators.limits import clear_rate_limits
    clear_rate_limits(ip="127.0.0.1")
    with th.server_settings(GEOFENCE_SYSTEM_RULES={"country": {"in": ["US"]}},
                            GEOFENCE_TEST_OVERRIDE=GEO_RU,
                            GEOFENCE_CACHE_TTL=0):
        resp = opts.client.get("/api/geo/check")
    assert resp.status_code == 200, f"endpoint returns 200 with decision in body, got {resp.status_code}"
    d = resp.response.data
    # Full decision shape — country/region/abuse must all be present
    assert d.allowed is False, "RU blocked by country.in=[US]"
    assert "country_code" in d, "full decision must include country_code"
    assert "region_code" in d, "full decision must include region_code"
    assert "abuse" in d, "full decision must include abuse"
    assert d.abuse.tor is False, f"abuse.tor must be False, got {d.abuse.tor}"


@th.django_unit_test("endpoint: group_uuid evaluates group rules")
def test_group_uuid_evaluation(opts):
    from mojo.decorators.limits import clear_rate_limits
    clear_rate_limits(ip="127.0.0.1")
    # Group's metadata.geofence has country.in=[US]. RU IP should fail.
    with th.server_settings(GEOFENCE_SYSTEM_RULES={},
                            GEOFENCE_TEST_OVERRIDE=GEO_RU,
                            GEOFENCE_CACHE_TTL=0):
        resp = opts.client.get(f"/api/geo/check?group_uuid={opts.test_group_uuid}")
    d = resp.response.data
    assert d.allowed is False, "RU IP must be blocked by group country.in=[US]"
    assert d.rule_level == "group", f"rule_level must be 'group', got {d.rule_level!r}"


@th.django_unit_test("endpoint: unknown group_uuid → 400")
def test_unknown_group_uuid(opts):
    from mojo.decorators.limits import clear_rate_limits
    clear_rate_limits(ip="127.0.0.1")
    resp = opts.client.get("/api/geo/check?group_uuid=00000000-not-a-real-uuid")
    assert resp.status_code in [400, 422], \
        f"unknown group_uuid must return 4xx, got {resp.status_code}"


@th.django_unit_test("endpoint: inactive group → system-only eval with detail")
def test_inactive_group_falls_back(opts):
    from mojo.decorators.limits import clear_rate_limits
    clear_rate_limits(ip="127.0.0.1")
    with th.server_settings(GEOFENCE_SYSTEM_RULES={},
                            GEOFENCE_TEST_OVERRIDE=GEO_US,
                            GEOFENCE_CACHE_TTL=0):
        resp = opts.client.get(f"/api/geo/check?group_uuid={opts.inactive_group_uuid}")
    assert resp.status_code == 200, f"inactive group must not 400, got {resp.status_code}"
    d = resp.response.data
    assert d.get("group_inactive") is True, f"response must flag group_inactive, got {dict(d)}"


@th.django_unit_test("endpoint: NOT geofenced itself — blocked users still get response")
def test_endpoint_not_geofenced(opts):
    """Even when system rules would block the calling IP, /api/geo/check returns 200
    with allowed=False in the body so the UI can tell the user why they're blocked."""
    from mojo.decorators.limits import clear_rate_limits
    clear_rate_limits(ip="127.0.0.1")
    with th.server_settings(GEOFENCE_SYSTEM_RULES={"country": {"in": ["US"]}},
                            GEOFENCE_TEST_OVERRIDE=GEO_RU,
                            GEOFENCE_CACHE_TTL=0):
        resp = opts.client.get("/api/geo/check")
    assert resp.status_code == 200, \
        f"/api/geo/check must return 200 even when caller is blocked (so UI can show why), got {resp.status_code}"
    assert resp.response.data.allowed is False, "body must carry the block decision"
