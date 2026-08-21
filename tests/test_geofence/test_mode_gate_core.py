"""Default-tier core of the test-mode header gate (maestro item #1839).

The gate (mojo.helpers.test_mode.is_test_request) is the ONLY thing standing
between an accidental production leak and arbitrary-callable RCE via the
X-Mojo-Test-* handler headers. The spoofing-refusal cases below must stay in
the default tier: a regression here is a remote geofence/handler bypass.

Parallel-safety: the test project arms MOJO_TEST_MODE=True in its settings,
so these tests READ the armed state (asserted explicitly) instead of mutating
django.conf like the exhaustive matrix in
tests/test_geofence_extended_serial/test_mode_gate.py, which also covers the
disarmed state and the end-to-end loopback cases.
"""
from testit import helpers as th


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


def _require_armed():
    from django.conf import settings as dj_settings
    assert getattr(dj_settings, "MOJO_TEST_MODE", False) is True, (
        "the test project must arm MOJO_TEST_MODE=True — these gate "
        "regressions read the armed state instead of mutating settings"
    )


@th.django_unit_test("gate core: loopback + no proxy passes while armed")
def test_gate_core_pass(opts):
    from mojo.helpers import test_mode
    _require_armed()
    assert test_mode.is_test_request(_FakeRequest()) is True, \
        "loopback + no proxy + MOJO_TEST_MODE=True must pass"
    assert test_mode.is_test_request(_FakeRequest(remote_addr="::1")) is True, \
        "IPv6 loopback must pass"


@th.django_unit_test("gate core: X-Forwarded-For closes the gate")
def test_gate_core_xff_blocks(opts):
    from mojo.helpers import test_mode
    _require_armed()
    req = _FakeRequest(remote_addr="127.0.0.1", x_forwarded_for="1.2.3.4")
    assert test_mode.is_test_request(req) is False, \
        "X-Forwarded-For present must close the gate (proxy chain)"


@th.django_unit_test("gate core: Forwarded header closes the gate")
def test_gate_core_forwarded_blocks(opts):
    from mojo.helpers import test_mode
    _require_armed()
    req = _FakeRequest(remote_addr="127.0.0.1", forwarded='for="1.2.3.4"')
    assert test_mode.is_test_request(req) is False, \
        "Forwarded header must close the gate"


@th.django_unit_test("gate core: Via header closes the gate")
def test_gate_core_via_blocks(opts):
    from mojo.helpers import test_mode
    _require_armed()
    req = _FakeRequest(remote_addr="127.0.0.1", via="1.1 proxy.example.com")
    assert test_mode.is_test_request(req) is False, \
        "Via header must close the gate"


@th.django_unit_test("gate core: non-loopback callers are refused")
def test_gate_core_non_loopback_blocks(opts):
    from mojo.helpers import test_mode
    _require_armed()
    for ip in ("1.2.3.4", "10.0.0.1", "8.8.8.8", "192.168.1.1", ""):
        req = _FakeRequest(remote_addr=ip)
        assert test_mode.is_test_request(req) is False, \
            f"non-loopback {ip!r} must close the gate"


@th.django_unit_test("gate core: a None request is refused")
def test_gate_core_none_request(opts):
    from mojo.helpers import test_mode
    _require_armed()
    assert test_mode.is_test_request(None) is False, \
        "a None request must close the gate (defensive)"
