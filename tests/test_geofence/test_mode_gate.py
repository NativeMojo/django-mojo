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


@th.unit_test("gate: env var enabled + loopback + no proxy → True")
def test_gate_pass(opts):
    from mojo.helpers import test_mode
    # Sanity: env var is set in the test server, but for the gate function
    # we control _TEST_MODE_ENABLED directly to make the test independent of
    # env state.
    orig = test_mode._TEST_MODE_ENABLED
    test_mode._TEST_MODE_ENABLED = True
    try:
        assert test_mode.is_test_request(_FakeRequest()) is True, \
            "loopback + no proxy + env-enabled must pass"
        assert test_mode.is_test_request(_FakeRequest(remote_addr="::1")) is True, \
            "IPv6 loopback must pass"
    finally:
        test_mode._TEST_MODE_ENABLED = orig


@th.unit_test("gate: env var disabled → False (even with loopback)")
def test_gate_env_disabled(opts):
    from mojo.helpers import test_mode
    orig = test_mode._TEST_MODE_ENABLED
    test_mode._TEST_MODE_ENABLED = False
    try:
        assert test_mode.is_test_request(_FakeRequest()) is False, \
            "env var disabled must always fail the gate"
    finally:
        test_mode._TEST_MODE_ENABLED = orig


@th.unit_test("gate: X-Forwarded-For present → False (proxy chain detected)")
def test_gate_xff_blocks(opts):
    from mojo.helpers import test_mode
    orig = test_mode._TEST_MODE_ENABLED
    test_mode._TEST_MODE_ENABLED = True
    try:
        req = _FakeRequest(remote_addr="127.0.0.1", x_forwarded_for="1.2.3.4")
        assert test_mode.is_test_request(req) is False, \
            "X-Forwarded-For present must close the gate (proxy chain)"
    finally:
        test_mode._TEST_MODE_ENABLED = orig


@th.unit_test("gate: Forwarded header present → False")
def test_gate_forwarded_blocks(opts):
    from mojo.helpers import test_mode
    orig = test_mode._TEST_MODE_ENABLED
    test_mode._TEST_MODE_ENABLED = True
    try:
        req = _FakeRequest(remote_addr="127.0.0.1", forwarded='for="1.2.3.4"')
        assert test_mode.is_test_request(req) is False, \
            "Forwarded header must close the gate"
    finally:
        test_mode._TEST_MODE_ENABLED = orig


@th.unit_test("gate: Via header present → False")
def test_gate_via_blocks(opts):
    from mojo.helpers import test_mode
    orig = test_mode._TEST_MODE_ENABLED
    test_mode._TEST_MODE_ENABLED = True
    try:
        req = _FakeRequest(remote_addr="127.0.0.1", via="1.1 proxy.example.com")
        assert test_mode.is_test_request(req) is False, \
            "Via header must close the gate"
    finally:
        test_mode._TEST_MODE_ENABLED = orig


@th.unit_test("gate: non-loopback REMOTE_ADDR → False (external traffic)")
def test_gate_non_loopback_blocks(opts):
    from mojo.helpers import test_mode
    orig = test_mode._TEST_MODE_ENABLED
    test_mode._TEST_MODE_ENABLED = True
    try:
        for ip in ("1.2.3.4", "10.0.0.1", "8.8.8.8", "192.168.1.1", ""):
            req = _FakeRequest(remote_addr=ip)
            assert test_mode.is_test_request(req) is False, \
                f"non-loopback {ip!r} must close the gate"
    finally:
        test_mode._TEST_MODE_ENABLED = orig


@th.unit_test("gate: None request → False (defensive)")
def test_gate_none_request(opts):
    from mojo.helpers import test_mode
    orig = test_mode._TEST_MODE_ENABLED
    test_mode._TEST_MODE_ENABLED = True
    try:
        assert test_mode.is_test_request(None) is False, \
            "None request must fail safely, not crash"
    finally:
        test_mode._TEST_MODE_ENABLED = orig


# ---------------------------------------------------------------------------
# End-to-end: verify the gate actually closes when X-Forwarded-For is set.
# This is the critical regression guard for a production-LB scenario.
# ---------------------------------------------------------------------------

@th.django_unit_test("e2e: X-Forwarded-For closes the gate — geofence header ignored")
def test_e2e_xff_disables_test_geo(opts):
    """A request with X-Forwarded-For must NOT honor X-Mojo-Test-Geo,
    even though MOJO_TEST_MODE=1 is set in the test server."""
    import json
    # Hit /api/geo/check with both: a geofence-blocking rule AND an X-Mojo-Test-Geo
    # override that would normally allow. With X-Forwarded-For set, the gate
    # closes and the override is ignored — falls through to GEOFENCE_TEST_OVERRIDE
    # setting (None in default) → real geoip lookup of 127.0.0.1 → private_ip
    # branch → allowed per GEOFENCE_ALLOW_PRIVATE_IPS default True. Result is
    # `allowed=True, reason="private_ip"` — proving the X-Mojo-Test-Geo override
    # was NOT honored (it would have returned "passed" or "country_not_allowed"
    # depending on the rule). The key signal is that the response shape differs
    # from what the override would have produced.
    headers = {
        "X-Forwarded-For": "1.2.3.4",  # closes the gate
        "X-Mojo-Test-Geo": json.dumps({"country_code": "US", "region_code": "US-CA",
                                       "is_tor": False, "is_vpn": False,
                                       "is_proxy": False, "is_datacenter": False}),
        "X-Mojo-Test-Geofence-System": json.dumps({"country": {"in": ["US"]}}),
        "X-Mojo-Test-Geofence-Cache-Ttl": "0",
    }
    resp = opts.client.get("/api/geo/check", headers=headers)
    assert resp.status_code == 200, f"got {resp.status_code}: {opts.client.last_response.body}"
    d = resp.response.data
    # With gate CLOSED, no test headers applied. System rules default to {} →
    # no_rules fast path. The X-Mojo-Test-Geofence-System header MUST be ignored.
    assert d.reason == "no_rules", \
        f"gate closure must ignore X-Mojo-Test-Geofence-System; expected no_rules, got reason={d.reason!r}"
    assert d.allowed is True, \
        f"no rules apply when gate is closed; expected allowed=True, got {d.allowed}"


@th.django_unit_test("e2e: control — without X-Forwarded-For, gate is open and headers apply")
def test_e2e_gate_open_control(opts):
    """Control case: identical request WITHOUT X-Forwarded-For applies the
    test override. Proves the previous test's pass wasn't accidental."""
    import json
    headers = {
        "X-Mojo-Test-Geo": json.dumps({"country_code": "RU", "region_code": "RU-MOW",
                                       "is_tor": False, "is_vpn": False,
                                       "is_proxy": False, "is_datacenter": False}),
        "X-Mojo-Test-Geofence-System": json.dumps({"country": {"in": ["US"]}}),
        "X-Mojo-Test-Geofence-Cache-Ttl": "0",
    }
    resp = opts.client.get("/api/geo/check", headers=headers)
    d = resp.response.data
    assert d.allowed is False, \
        f"gate-open control must apply override; expected blocked, got allowed={d.allowed}"
    assert d.reason == "country_not_allowed", \
        f"override should produce country_not_allowed, got {d.reason!r}"
