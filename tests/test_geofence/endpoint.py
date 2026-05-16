"""Pre-flight endpoint tests for GET /api/geo/check.

Uses per-request X-Mojo-Test-* headers (no server reloads).
"""
import uuid
from testit import helpers as th
from tests.test_geofence._helpers import headers, GEO_RU, GEO_US


@th.django_unit_setup()
def setup_endpoint(opts):
    from mojo.apps.account.models.group import Group
    # Per-setup unique group names so this setup is parallel-safe with other
    # test packages that may also create groups.
    suffix = uuid.uuid4().hex[:8]
    grp = Group.objects.create(name=f"Geofence Endpoint {suffix}", is_active=True)
    grp.metadata = {"geofence": {"country": {"in": ["US"]}}}
    grp.save()
    grp.get_uuid()  # lazy-populate the uuid
    opts.test_group_uuid = grp.uuid
    opts.test_group_id = grp.pk

    inactive = Group.objects.create(name=f"Geofence Endpoint Inactive {suffix}", is_active=False)
    inactive.get_uuid()
    opts.inactive_group_uuid = inactive.uuid


@th.django_unit_test("endpoint: GET /api/geo/check returns full GeoDecision shape")
def test_returns_full_decision(opts):
    resp = opts.client.get("/api/geo/check",
                           headers=headers(geo=GEO_RU,
                                           system_rules={"country": {"in": ["US"]}}))
    assert resp.status_code == 200, f"got {resp.status_code}"
    d = resp.response.data
    assert d.allowed is False, "RU blocked by country.in=[US]"
    assert "country_code" in d, "full decision must include country_code"
    assert "region_code" in d, "full decision must include region_code"
    assert "abuse" in d, "full decision must include abuse"
    assert d.abuse.tor is False, f"abuse.tor must be False, got {d.abuse.tor}"


@th.django_unit_test("endpoint: group_uuid evaluates group rules")
def test_group_uuid_evaluation(opts):
    # Group's metadata.geofence has country.in=[US]. RU IP should fail.
    resp = opts.client.get(
        f"/api/geo/check?group_uuid={opts.test_group_uuid}",
        headers=headers(geo=GEO_RU, system_rules={}))
    d = resp.response.data
    assert d.allowed is False, "RU IP must be blocked by group country.in=[US]"
    assert d.rule_level == "group", f"rule_level must be 'group', got {d.rule_level!r}"


@th.django_unit_test("endpoint: unknown group_uuid → 400")
def test_unknown_group_uuid(opts):
    resp = opts.client.get(
        f"/api/geo/check?group_uuid=00000000{uuid.uuid4().hex[:24]}",
        headers=headers(geo=GEO_US))
    assert resp.status_code in [400, 422], \
        f"unknown group_uuid must return 4xx, got {resp.status_code}"


@th.django_unit_test("endpoint: inactive group → system-only eval with detail")
def test_inactive_group_falls_back(opts):
    resp = opts.client.get(
        f"/api/geo/check?group_uuid={opts.inactive_group_uuid}",
        headers=headers(geo=GEO_US, system_rules={}))
    assert resp.status_code == 200, f"inactive group must not 400, got {resp.status_code}"
    d = resp.response.data
    assert d.get("group_inactive") is True, f"response must flag group_inactive, got {dict(d)}"


@th.django_unit_test("endpoint: NOT geofenced itself — blocked users still get response")
def test_endpoint_not_geofenced(opts):
    """Even when system rules would block the calling IP, /api/geo/check returns 200
    with allowed=False in the body so the UI can tell the user why."""
    resp = opts.client.get("/api/geo/check",
                           headers=headers(geo=GEO_RU,
                                           system_rules={"country": {"in": ["US"]}}))
    assert resp.status_code == 200, f"got {resp.status_code}"
    assert resp.response.data.allowed is False, "body must carry the block decision"
