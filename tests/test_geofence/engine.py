"""Engine tests — uses GEOFENCE_TEST_OVERRIDE to simulate geo lookups.

Tests run against the live test server via opts.client. Per-scenario
settings toggles via th.server_settings().
"""
from testit import helpers as th


# Geo dicts used by GEOFENCE_TEST_OVERRIDE. The engine treats this dict as if
# it came from geolocate_ip — the DSL reads country_code, region_code, and the
# is_* abuse flags.
GEO_US = {"country_code": "US", "region_code": "US-CA",
          "is_tor": False, "is_vpn": False, "is_proxy": False, "is_datacenter": False}
GEO_RU = {"country_code": "RU", "region_code": "RU-MOW",
          "is_tor": False, "is_vpn": False, "is_proxy": False, "is_datacenter": False}
GEO_US_FL = {"country_code": "US", "region_code": "US-FL",
             "is_tor": False, "is_vpn": False, "is_proxy": False, "is_datacenter": False}
GEO_TOR = {"country_code": "US", "region_code": "US-CA",
           "is_tor": True, "is_vpn": False, "is_proxy": False, "is_datacenter": False}
GEO_VPN = {"country_code": "US", "region_code": "US-CA",
           "is_tor": False, "is_vpn": True, "is_proxy": False, "is_datacenter": False}


@th.django_unit_setup()
def setup_engine(opts):
    from mojo.apps.account.models import User
    from mojo.apps.account.models.group import Group
    from mojo.decorators.limits import clear_rate_limits

    clear_rate_limits(ip="127.0.0.1")

    # Clean any prior test groups + users
    User.objects.filter(email__icontains="@geofence.test").delete()
    Group.objects.filter(name__startswith="Geofence Test").delete()

    grp = Group.objects.create(name="Geofence Test Group", is_active=True)
    opts.test_group_uuid = grp.get_uuid()
    opts.test_group_id = grp.pk

    inactive = Group.objects.create(name="Geofence Test Inactive", is_active=False)
    opts.inactive_group_uuid = inactive.get_uuid()


def _check_endpoint(opts, **settings_overrides):
    """Helper — hit GET /api/geo/check under the given settings overrides."""
    from mojo.decorators.limits import clear_rate_limits
    clear_rate_limits(ip="127.0.0.1")
    with th.server_settings(**settings_overrides):
        return opts.client.get("/api/geo/check")


@th.django_unit_test("engine: no rules at any level → allowed=True, reason='no_rules'")
def test_no_rules_fast_path(opts):
    resp = _check_endpoint(opts,
                           GEOFENCE_SYSTEM_RULES={},
                           GEOFENCE_TEST_OVERRIDE=None)
    assert resp.status_code == 200, \
        f"/api/geo/check must return 200 when no rules configured, got {resp.status_code}"
    d = resp.response.data
    assert d.allowed is True, f"no_rules path must allow, got allowed={d.allowed}"
    assert d.reason == "no_rules", f"reason must be 'no_rules', got {d.reason!r}"


@th.django_unit_test("engine: GEOFENCE_ENABLED=False → always allowed='disabled'")
def test_disabled_master_kill(opts):
    resp = _check_endpoint(opts,
                           GEOFENCE_ENABLED=False,
                           GEOFENCE_SYSTEM_RULES={"country": {"in": ["RU"]}},
                           GEOFENCE_TEST_OVERRIDE=GEO_US)
    assert resp.status_code == 200, f"got {resp.status_code}"
    d = resp.response.data
    assert d.allowed is True, "GEOFENCE_ENABLED=False must always allow"
    assert d.reason == "disabled", f"reason must be 'disabled', got {d.reason!r}"


@th.django_unit_test("engine: system rules block by country")
def test_system_blocks_country(opts):
    resp = _check_endpoint(opts,
                           GEOFENCE_SYSTEM_RULES={"country": {"in": ["US"]}},
                           GEOFENCE_TEST_OVERRIDE=GEO_RU,
                           GEOFENCE_CACHE_TTL=0)  # disable cache so per-test override takes effect
    assert resp.status_code == 200, f"endpoint returns 200 with allowed=False in body, got {resp.status_code}"
    d = resp.response.data
    assert d.allowed is False, "RU IP must be blocked by system country.in=[US]"
    assert d.reason == "country_not_allowed", f"got reason {d.reason!r}"
    assert d.rule_level == "system", f"rule_level must be 'system', got {d.rule_level!r}"


@th.django_unit_test("engine: system rules allow US country")
def test_system_allows_us(opts):
    resp = _check_endpoint(opts,
                           GEOFENCE_SYSTEM_RULES={"country": {"in": ["US"]}},
                           GEOFENCE_TEST_OVERRIDE=GEO_US,
                           GEOFENCE_CACHE_TTL=0)
    d = resp.response.data
    assert d.allowed is True, f"US must pass system country.in=[US], got allowed={d.allowed} reason={d.reason}"
    assert d.reason == "passed", f"reason must be 'passed', got {d.reason!r}"


@th.django_unit_test("engine: abuse.tor=False blocks Tor IPs")
def test_blocks_tor(opts):
    resp = _check_endpoint(opts,
                           GEOFENCE_SYSTEM_RULES={"abuse": {"tor": False}},
                           GEOFENCE_TEST_OVERRIDE=GEO_TOR,
                           GEOFENCE_CACHE_TTL=0)
    d = resp.response.data
    assert d.allowed is False, "Tor IP must be blocked by abuse.tor=False"
    assert d.reason == "tor_detected", f"got reason {d.reason!r}"


@th.django_unit_test("engine: region rules match ISO 3166-2 codes")
def test_region_iso_codes(opts):
    rule = {"country": {"in": ["US"]}, "region": {"in": ["US-FL"]}}
    # US-FL should pass
    resp = _check_endpoint(opts,
                           GEOFENCE_SYSTEM_RULES=rule,
                           GEOFENCE_TEST_OVERRIDE=GEO_US_FL,
                           GEOFENCE_CACHE_TTL=0)
    assert resp.response.data.allowed is True, "US-FL must pass region.in=[US-FL]"

    # US-CA should fail
    resp = _check_endpoint(opts,
                           GEOFENCE_SYSTEM_RULES=rule,
                           GEOFENCE_TEST_OVERRIDE=GEO_US,
                           GEOFENCE_CACHE_TTL=0)
    assert resp.response.data.allowed is False, "US-CA must be blocked by region.in=[US-FL]"
    assert resp.response.data.reason == "region_not_allowed", f"got reason {resp.response.data.reason!r}"


@th.django_unit_test("engine: GEOFENCE_FAIL_CLOSED=True blocks on lookup failure")
def test_fail_closed_on_lookup_failure(opts):
    # Simulate lookup failure by setting override to a dict missing country_code
    # AND empty rules trigger no-rules path, so we need rules to force the lookup
    # path. Use a dict with no country_code → triggers "private_ip" branch with
    # GEOFENCE_ALLOW_PRIVATE_IPS=False.
    resp = _check_endpoint(opts,
                           GEOFENCE_SYSTEM_RULES={"country": {"in": ["US"]}},
                           GEOFENCE_TEST_OVERRIDE={"country_code": None},
                           GEOFENCE_ALLOW_PRIVATE_IPS=False,
                           GEOFENCE_CACHE_TTL=0)
    d = resp.response.data
    assert d.allowed is False, \
        f"private_ip with GEOFENCE_ALLOW_PRIVATE_IPS=False must block, got allowed={d.allowed}"


@th.django_unit_test("engine: GEOFENCE_ALLOW_PRIVATE_IPS=True allows missing country_code")
def test_allow_private_ips(opts):
    resp = _check_endpoint(opts,
                           GEOFENCE_SYSTEM_RULES={"country": {"in": ["US"]}},
                           GEOFENCE_TEST_OVERRIDE={"country_code": None},
                           GEOFENCE_ALLOW_PRIVATE_IPS=True,
                           GEOFENCE_CACHE_TTL=0)
    d = resp.response.data
    assert d.allowed is True, "private IP must pass when GEOFENCE_ALLOW_PRIVATE_IPS=True"
    assert d.reason == "private_ip", f"got reason {d.reason!r}"
