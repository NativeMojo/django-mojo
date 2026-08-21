"""Defense-in-depth tests for the test-mode header gate.

The gate (mojo.helpers.test_mode.is_test_request) is the ONLY thing standing
between an accidental production leak and arbitrary-callable RCE via the
X-Mojo-Test-User-Registered-Handler / -Login-Handler / -Pre-Register-Validator
headers. Test every defense layer.
"""
from testit import helpers as th


# ---------------------------------------------------------------------------
# In-process unit tests of the gate function itself
# ---------------------------------------------------------------------------

class _FakeRequest:
    """Minimal request mock for the gate function."""
    def __init__(self, remote_addr="127.0.0.1", x_forwarded_for=None,
                 forwarded=None, via=None):
        self.META = {"REMOTE_ADDR": remote_addr}
        if x_forwarded_for is not None:
            self.META["HTTP_X_FORWARDED_FOR"] = x_forwarded_for
        if forwarded is not None:
            self.META["HTTP_FORWARDED"] = forwarded
        if via is not None:
            self.META["HTTP_VIA"] = via


@th.unit_test("gate: MOJO_TEST_MODE=True + loopback + no proxy → True")
def test_gate_pass(opts):
    from mojo.helpers import test_mode
    # The test project sets MOJO_TEST_MODE=True, but we mutate django.conf
    # directly here to make the test independent of settings state.
    from django.conf import settings as dj_settings
    orig = getattr(dj_settings, "MOJO_TEST_MODE", False)
    dj_settings.MOJO_TEST_MODE = True
    try:
        assert test_mode.is_test_request(_FakeRequest()) is True, \
            "loopback + no proxy + MOJO_TEST_MODE=True must pass"
        assert test_mode.is_test_request(_FakeRequest(remote_addr="::1")) is True, \
            "IPv6 loopback must pass"
    finally:
        dj_settings.MOJO_TEST_MODE = orig


@th.unit_test("gate: MOJO_TEST_MODE=False → gate always closed (even with loopback)")
def test_gate_env_disabled(opts):
    from mojo.helpers import test_mode
    from django.conf import settings as dj_settings
    orig = getattr(dj_settings, "MOJO_TEST_MODE", False)
    dj_settings.MOJO_TEST_MODE = False
    try:
        assert test_mode.is_test_request(_FakeRequest()) is False, \
            "MOJO_TEST_MODE=False must always close the gate"
    finally:
        dj_settings.MOJO_TEST_MODE = orig


@th.unit_test("gate: X-Forwarded-For present → False (proxy chain detected)")
def test_gate_xff_blocks(opts):
    from mojo.helpers import test_mode
    from django.conf import settings as dj_settings
    orig = getattr(dj_settings, "MOJO_TEST_MODE", False)
    dj_settings.MOJO_TEST_MODE = True
    try:
        req = _FakeRequest(remote_addr="127.0.0.1", x_forwarded_for="1.2.3.4")
        assert test_mode.is_test_request(req) is False, \
            "X-Forwarded-For present must close the gate (proxy chain)"
    finally:
        dj_settings.MOJO_TEST_MODE = orig


@th.unit_test("gate: Forwarded header present → False")
def test_gate_forwarded_blocks(opts):
    from mojo.helpers import test_mode
    from django.conf import settings as dj_settings
    orig = getattr(dj_settings, "MOJO_TEST_MODE", False)
    dj_settings.MOJO_TEST_MODE = True
    try:
        req = _FakeRequest(remote_addr="127.0.0.1", forwarded='for="1.2.3.4"')
        assert test_mode.is_test_request(req) is False, \
            "Forwarded header must close the gate"
    finally:
        dj_settings.MOJO_TEST_MODE = orig


@th.unit_test("gate: Via header present → False")
def test_gate_via_blocks(opts):
    from mojo.helpers import test_mode
    from django.conf import settings as dj_settings
    orig = getattr(dj_settings, "MOJO_TEST_MODE", False)
    dj_settings.MOJO_TEST_MODE = True
    try:
        req = _FakeRequest(remote_addr="127.0.0.1", via="1.1 proxy.example.com")
        assert test_mode.is_test_request(req) is False, \
            "Via header must close the gate"
    finally:
        dj_settings.MOJO_TEST_MODE = orig


@th.unit_test("gate: non-loopback REMOTE_ADDR → False (external traffic)")
def test_gate_non_loopback_blocks(opts):
    from mojo.helpers import test_mode
    from django.conf import settings as dj_settings
    orig = getattr(dj_settings, "MOJO_TEST_MODE", False)
    dj_settings.MOJO_TEST_MODE = True
    try:
        for ip in ("1.2.3.4", "10.0.0.1", "8.8.8.8", "192.168.1.1", ""):
            req = _FakeRequest(remote_addr=ip)
            assert test_mode.is_test_request(req) is False, \
                f"non-loopback {ip!r} must close the gate"
    finally:
        dj_settings.MOJO_TEST_MODE = orig


@th.unit_test("gate: None request → False (defensive)")
def test_gate_none_request(opts):
    from mojo.helpers import test_mode
    from django.conf import settings as dj_settings
    orig = getattr(dj_settings, "MOJO_TEST_MODE", False)
    dj_settings.MOJO_TEST_MODE = True
    try:
        assert test_mode.is_test_request(None) is False, \
            "None request must fail safely, not crash"
    finally:
        dj_settings.MOJO_TEST_MODE = orig


# ---------------------------------------------------------------------------
# End-to-end: verify the gate actually closes when X-Forwarded-For is set.
# This is the critical regression guard for a production-LB scenario.
#
# The two tests below are a strict A/B: identical headers, differing ONLY in
# X-Forwarded-For. Both assert on the SENTINEL geo rather than on the decision
# `reason`, because with the gate CLOSED this request controls nothing — the
# engine falls through to shared state that other modules mutate concurrently:
#   - real GEOFENCE_SYSTEM_RULES rows (tests/test_geofence/config_plane.py
#     writes them), which take it off the no_rules fast path;
#   - the (ip, group) decision cache — the cache-ttl header is ignored too when
#     the gate is closed, so a decision cached for 127.0.0.1 by another test is
#     returned verbatim;
#   - whatever GeoLocatedIP has cached for 127.0.0.1.
# Any of those can make `reason` be private_ip / country_not_allowed / a cached
# value, so asserting reason == "no_rules" was a race. The sentinel country code
# cannot come from ANY of them — only from this request's own X-Mojo-Test-Geo —
# which makes its absence an exact, race-free reading of "the gate stayed shut".
# ---------------------------------------------------------------------------

# ISO 3166-1 leaves ZZ user-assigned: no geoip provider returns it, no fixture
# uses it, so it can only reach a response by way of our own header.
SENTINEL_COUNTRY = "ZZ"
SENTINEL_REGION = "ZZ-XFF"


def _gate_probe_headers():
    """Headers whose ONLY visible effect is a sentinel-flavored decision.

    Geo says country ZZ; the system rule allows US only. Gate open → the rule
    and the geo both apply → blocked with country=ZZ. Gate closed → every one
    of these is ignored and ZZ can never appear.
    """
    import json
    return {
        "X-Mojo-Test-Geo": json.dumps({"country_code": SENTINEL_COUNTRY,
                                       "region_code": SENTINEL_REGION,
                                       "is_tor": False, "is_vpn": False,
                                       "is_proxy": False, "is_datacenter": False}),
        "X-Mojo-Test-Geofence-System": json.dumps({"country": {"in": ["US"]}}),
        "X-Mojo-Test-Geofence-Cache-Ttl": "0",
    }


@th.django_unit_test("e2e: X-Forwarded-For closes the gate — geofence headers ignored")
def test_e2e_xff_disables_test_geo(opts):
    """A request with X-Forwarded-For must NOT honor X-Mojo-Test-Geo,
    even though MOJO_TEST_MODE=1 is set in the test server."""
    headers = dict(_gate_probe_headers(), **{"X-Forwarded-For": "1.2.3.4"})
    resp = opts.client.get("/api/geo/check", headers=headers)
    assert resp.status_code == 200, f"got {resp.status_code}: {opts.client.last_response.body}"
    d = resp.response.data
    # The control below proves ZZ is exactly what a gate-open request returns,
    # so its absence here is the gate holding — whatever `reason` says.
    assert d.country != SENTINEL_COUNTRY, \
        (f"gate closure must ignore X-Mojo-Test-Geo, but the decision carries the "
         f"sentinel country {d.country!r} (reason={d.reason!r})")
    assert d.country_code != SENTINEL_COUNTRY, \
        (f"gate closure must ignore X-Mojo-Test-Geo, but country_code is the "
         f"sentinel {d.country_code!r} (reason={d.reason!r})")
    assert d.region != SENTINEL_REGION, \
        (f"gate closure must ignore X-Mojo-Test-Geo, but the decision carries the "
         f"sentinel region {d.region!r} (reason={d.reason!r})")


@th.django_unit_test("e2e: control — without X-Forwarded-For, gate is open and headers apply")
def test_e2e_gate_open_control(opts):
    """Control case: identical request WITHOUT X-Forwarded-For applies the
    test override. Proves the previous test's pass wasn't accidental.

    Concurrency-safe in the other direction: with the gate OPEN these headers
    replace the system rules, the geo and the cache TTL outright, so nothing
    another module writes can reach this decision.
    """
    resp = opts.client.get("/api/geo/check", headers=_gate_probe_headers())
    assert resp.status_code == 200, f"got {resp.status_code}: {opts.client.last_response.body}"
    d = resp.response.data
    assert d.country == SENTINEL_COUNTRY, \
        (f"gate-open control must apply X-Mojo-Test-Geo; expected country "
         f"{SENTINEL_COUNTRY!r}, got {d.country!r} (reason={d.reason!r})")
    assert d.allowed is False, \
        f"gate-open control must apply override; expected blocked, got allowed={d.allowed}"
    assert d.reason == "country_not_allowed", \
        f"override should produce country_not_allowed, got {d.reason!r}"
    assert d.rule_level == "system", \
        f"the blocking rule came from X-Mojo-Test-Geofence-System, got rule_level={d.rule_level!r}"
