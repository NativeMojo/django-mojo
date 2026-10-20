"""
Every blocking path refuses unsafe targets (maestro #8885).

One predicate, `mojo.apps.incident.services.block_safety`, runs at the
desired-state write, the desired-state builders and every present-direction
firewall dispatch. Unsafe: non-global addresses, MOJOSEC_PROTECTED_CIDRS,
MOJOSEC_TRUSTED_PROXY_CIDRS and active whitelists. Every refusal must leave
the GeoLocatedIP row unchanged and never reach the broker.

Documentation ranges (203.0.113.0/24 and friends) are non-global, so the
public fixtures here use 203.0.114.0/24.
"""
import contextlib
from datetime import timedelta
from unittest import mock

from testit import helpers as th

TESTIT_TIER = "extended"

PUBLIC = "203.0.114.250"
PRIVATE = "10.20.30.40"
PROXY_MEMBER = "203.0.114.20"
PROTECTED_MEMBER = "203.0.114.5"
CONFIG = {
    "MOJOSEC_PROTECTED_CIDRS": ["203.0.114.0/28"],
    "MOJOSEC_TRUSTED_PROXY_CIDRS": ["203.0.114.16/28"],
}
RECONCILE = "mojo.apps.incident.services.firewall_truth.reconcile_geolocated_ip"
BROKER = "mojo.apps.incident.firewall._broker_request"
VERIFIED = {"status": "verified", "ok": True}

NON_GLOBAL = (
    "10.1.2.3", "127.0.0.2", "169.254.1.1", "100.64.1.1", "172.16.5.5",
    "192.168.1.1", "0.0.0.1", "192.0.0.9", "192.0.2.1", "198.18.0.1",
    "198.51.100.1", "203.0.113.100", "224.0.0.5", "239.1.1.1", "240.0.0.1",
    "255.255.255.255",
)
SAFE = ("8.8.8.8", "203.0.114.250", "100.63.255.255", "100.128.0.1", "172.32.0.1")


@contextlib.contextmanager
def _settings(overrides):
    from mojo.helpers.settings import settings
    original = settings.get_static

    def fake(name, default=None, *args, **kwargs):
        if name in overrides:
            return overrides[name]
        return original(name, default, *args, **kwargs)
    with mock.patch.object(settings, "get_static", side_effect=fake):
        yield


def _fresh(ip, **fields):
    from mojo.apps.account.models import GeoLocatedIP
    GeoLocatedIP.objects.filter(ip_address=ip).delete()
    return GeoLocatedIP.objects.create(ip_address=ip, provider="test", **fields)


def _cleanup(*ips):
    """Drop rows a test whitelisted, so no other test's set is carved."""
    from mojo.apps.account.models import GeoLocatedIP
    GeoLocatedIP.objects.filter(ip_address__in=ips).delete()


def _state(geo):
    geo.refresh_from_db()
    return (geo.is_blocked, geo.blocked_until, geo.firewall_generation,
            geo.threat_level, geo.block_count)


def _broker_ok(operation, timeout=20, **values):
    observed = {"present": values.get("present", True), "input_count": 0,
                "forward_count": 0, "count": len(values.get("cidrs") or []),
                "exists": bool(values.get("present", True))}
    return {"ok": True, "observed": observed}


def _assert_refused(result, code, label):
    th.assert_eq(result.get("outcome"), "refused", f"{label}: outcome, got {result}")
    th.assert_eq((result.get("error") or {}).get("code"), code, f"{label}: code, got {result}")


# =============================================================================
# P1, P2: the predicate and its configuration
# =============================================================================

@th.django_unit_test()
def test_predicate_table(opts):
    from mojo.apps.incident.services import block_safety

    for ip in NON_GLOBAL:
        _canonical, code = block_safety.check_block_target(ip)
        th.assert_eq(code, "non_global_address", f"{ip} must be non_global_address")
    th.assert_eq(block_safety.check_block_target("2001:db8::1")[1], "unsupported_family",
                 "IPv6 stays unsupported_family")
    th.assert_eq(block_safety.check_block_target("not-an-ip")[1], "invalid_network",
                 "garbage is invalid_network")
    with _settings(CONFIG):
        th.assert_eq(block_safety.check_block_target(PROTECTED_MEMBER)[1],
                     "mojosec_protected_cidrs", "a protected-CIDR member is refused")
        th.assert_eq(block_safety.check_block_target(PROXY_MEMBER)[1],
                     "mojosec_trusted_proxy_cidrs", "a trusted-proxy member is refused")
    _fresh("203.0.114.77", is_whitelisted=True)
    th.assert_eq(block_safety.check_block_target("203.0.114.77")[1], "whitelisted",
                 "an active whitelist is refused")
    for ip in SAFE:
        th.assert_eq(block_safety.check_block_target(ip), (ip, ""), f"{ip} must be safe")
    from mojo.helpers import dates
    _fresh("203.0.114.78", is_whitelisted=True,
           whitelisted_until=dates.utcnow() - timedelta(hours=1))
    th.assert_eq(block_safety.check_block_target("203.0.114.78")[1], "",
                 "an expired whitelist does not protect")
    _cleanup("203.0.114.77", "203.0.114.78")


@th.django_unit_test()
def test_protection_config_forms(opts):
    from mojo.apps.incident.services import block_safety

    for value in (None, ""):
        with _settings({"MOJOSEC_PROTECTED_CIDRS": value, "MOJOSEC_TRUSTED_PROXY_CIDRS": value}):
            th.assert_eq(block_safety.check_block_target(PUBLIC)[1], "",
                         f"{value!r} protection config allows a public target")
    with _settings({"MOJOSEC_TRUSTED_PROXY_CIDRS": "203.0.114.0/28, 203.0.114.16/28"}):
        th.assert_eq(block_safety.check_block_target(PROXY_MEMBER)[1],
                     "mojosec_trusted_proxy_cidrs", "the CSV form is parsed")
        th.assert_eq(block_safety.check_block_target(PUBLIC)[1], "",
                     "the CSV form allows a target outside it")
    with _settings({"MOJOSEC_PROTECTED_CIDRS": ["203.0.114.0/28"]}):
        th.assert_eq(block_safety.check_block_target(PROTECTED_MEMBER)[1],
                     "mojosec_protected_cidrs", "the list form is parsed")
    too_many = [f"8.8.{n}.0/24" for n in range(65)]
    for value in ({"x": 1}, ["bad"], too_many, [1]):
        with _settings({"MOJOSEC_PROTECTED_CIDRS": value}):
            th.assert_eq(block_safety.check_block_target(PUBLIC)[1],
                         "protected_config_invalid",
                         f"malformed protection config {str(value)[:40]} refuses a public target")


# =============================================================================
# P3: every write path
# =============================================================================

def _write_paths():
    """(label, call(geo, ip) -> refusal code or None) for every write path."""
    def direct(geo, ip):
        result = geo.block_checked(reason="t8885", ttl=600)
        return (result.get("error") or {}).get("code") if result.get("outcome") == "refused" else None

    def block(geo, ip):
        return "refused" if geo.block(reason="t8885", ttl=600) is False else None

    def rest(geo, ip):
        result = geo.on_action_block({"reason": "t8885", "ttl": 600})
        return (result.get("error") or {}).get("code") if result.get("outcome") == "refused" else None

    def assistant(geo, ip):
        from mojo.apps.assistant.services.tools.security.ips import _tool_block_ip
        result = _tool_block_ip({"ip": ip, "reason": "t8885", "ttl": 600}, None)
        return result.get("error_code") if result.get("blocked") is False else None

    def llm(geo, ip):
        from mojo.apps.incident.handlers.llm_agent import _tool_block_ip
        result = _tool_block_ip({"ip": ip, "reason": "t8885", "ttl": 600})
        reason = result.get("reason") or ""
        return reason.split("refused: ", 1)[1] if reason.startswith("refused: ") else None

    def mojosec(geo, ip):
        from mojo.apps.incident.services import mojosec_actions
        result = mojosec_actions._apply_block(ip, "mojosec:t8885", 600)
        return (result.get("error") or {}).get("code") if result.get("outcome") != "applied" else None

    return (("block_checked", direct), ("block()", block), ("REST on_action_block", rest),
            ("assistant block_ip", assistant), ("LLM block_ip", llm),
            ("MojoSec _apply_block", mojosec))


@th.django_unit_test()
def test_write_paths_refuse_unsafe_targets(opts):
    with _settings(CONFIG):
        for ip, code in ((PRIVATE, "non_global_address"),
                         (PROXY_MEMBER, "mojosec_trusted_proxy_cidrs")):
            for label, call in _write_paths():
                geo = _fresh(ip)
                before = _state(geo)
                with mock.patch(RECONCILE, return_value=VERIFIED) as reconcile:
                    got = call(geo, ip)
                expected = "refused" if label == "block()" else code
                th.assert_eq(got, expected, f"{label} on {ip} must refuse with {expected}, got {got}")
                th.assert_eq(_state(geo), before, f"{label} on {ip} must leave the row unchanged")
                th.assert_true(not reconcile.called, f"{label} on {ip} must not reconcile the firewall")


@th.django_unit_test()
def test_threat_escalation_does_not_block_unsafe_targets(opts):
    with _settings(CONFIG):
        for ip in (PRIVATE, PROXY_MEMBER):
            geo = _fresh(ip)
            with mock.patch(RECONCILE, return_value=VERIFIED) as reconcile:
                geo.update_threat_from_incident(13, block=True)
            geo.refresh_from_db()
            th.assert_true(geo.is_blocked is False, f"threat escalation must not block {ip}")
            th.assert_eq((geo.firewall_generation, geo.block_count), (0, 0),
                         f"threat escalation must write no block state for {ip}")
            th.assert_true(not reconcile.called, f"threat escalation must not reconcile {ip}")


@th.django_unit_test()
def test_block_handler_refuses_and_keeps_incident_open(opts):
    from mojo.apps.incident.handlers.event_handlers import BlockHandler
    from mojo.apps.incident.models import Event, Incident, IncidentHistory

    Incident.objects.filter(category="test:block_8885").delete()
    geo = _fresh(PRIVATE)
    before = _state(geo)
    incident = Incident.objects.create(
        title="t8885", category="test:block_8885", priority=10, status="open")
    event = Event.objects.create(
        title="t8885", category="test:block_8885", level=10,
        source_ip=PRIVATE, incident=incident)
    with mock.patch(RECONCILE, return_value=VERIFIED) as reconcile:
        result = BlockHandler(ttl="600").run(event)
    incident.refresh_from_db()
    th.assert_true(result is False, "BlockHandler must report the refusal")
    th.assert_eq(incident.status, "open", "a refused block must not resolve the incident")
    notes = [h.note or "" for h in IncidentHistory.objects.filter(
        parent=incident, kind="handler:block")]
    th.assert_true(any("non_global_address" in note for note in notes),
                   f"the incident history must name the refusal code, got {notes}")
    th.assert_eq(_state(geo), before, "the row must be unchanged")
    th.assert_true(not reconcile.called, "BlockHandler must not reconcile the firewall")


@th.django_unit_test()
def test_mojosec_rechecks_after_config_change(opts):
    """A target that validated at proposal time is refused at execution once
    the protection config covers it."""
    from mojo.apps.incident.services import mojosec_actions

    geo = _fresh(PROXY_MEMBER)
    _canonical, state, _why = mojosec_actions.validate_target(PROXY_MEMBER)
    th.assert_eq(state, "validated", "control: the target is safe before the config change")
    before = _state(geo)
    with _settings(CONFIG), mock.patch(RECONCILE, return_value=VERIFIED) as reconcile:
        result = mojosec_actions._apply_block(PROXY_MEMBER, "mojosec:t8885", 600)
    th.assert_eq((result.get("error") or {}).get("code"), "mojosec_trusted_proxy_cidrs",
                 f"execution must re-check the target, got {result}")
    th.assert_eq(_state(geo), before, "the row must be unchanged")
    th.assert_true(not reconcile.called, "execution must not reconcile the firewall")


# =============================================================================
# P4: dispatch backstop
# =============================================================================

@th.django_unit_test()
def test_dispatch_refuses_unsafe_present_requests(opts):
    from mojo.apps.incident import firewall
    from mojo.apps.incident.asyncjobs import broadcast_block_ip
    from mojo.apps.incident.services.firewall_truth import permanent_set_name

    name = permanent_set_name()
    refused = (
        ("firewall.block", lambda: firewall.block("10.0.0.1") is False),
        ("normalize_ip present", lambda: firewall.normalize_ip("10.0.0.1", True)
         .get("error", {}).get("code") == "non_global_address"),
        ("ipset_add", lambda: firewall.ipset_add(name, "10.0.0.1") is False),
        ("normalize_geolocated_ip source", lambda: firewall.normalize_geolocated_ip(
            "10.0.0.1", name, [], True).get("ok") is False),
        ("normalize_ipset member", lambda: firewall.normalize_ipset(
            "t8885set", ["10.0.0.0/8"]).get("error", {}).get("code") == "non_global_address"),
        ("normalize_permanent_ipset member", lambda: firewall.normalize_permanent_ipset(
            ["10.0.0.1/32"]).get("ok") is False),
        ("normalize_geolocated_ip permanent member", lambda: firewall.normalize_geolocated_ip(
            PUBLIC, name, ["10.0.0.1/32"], False).get("ok") is False),
        ("ipset_load member", lambda: firewall.ipset_load("t8885set", ["10.0.0.0/8"])[0] is False),
    )
    for label, call in refused:
        with mock.patch(BROKER, side_effect=_broker_ok) as broker:
            th.assert_true(call(), f"{label} must refuse an unsafe target")
        th.assert_true(not broker.called, f"{label} must not reach the broker")
    with mock.patch(BROKER, side_effect=_broker_ok) as broker:
        broadcast_block_ip({"ips": ["10.0.0.1"]})
    th.assert_true(not broker.called, "broadcast_block_ip must dispatch nothing for 10.0.0.1")

    allowed = (
        ("normalize_ip absent", lambda: firewall.normalize_ip("10.0.0.1", False)),
        ("unblock", lambda: firewall.unblock("10.0.0.1")),
        ("normalize_ipset absent", lambda: firewall.normalize_ipset("t8885set", [], present=False)),
        ("normalize_geolocated_ip absent", lambda: firewall.normalize_geolocated_ip(
            "10.0.0.1", name, [], False)),
    )
    for label, call in allowed:
        with mock.patch(BROKER, side_effect=_broker_ok) as broker:
            call()
        th.assert_true(broker.called, f"{label} must still be dispatched")


# =============================================================================
# P5: desired state
# =============================================================================

@th.django_unit_test()
def test_desired_state_drops_unsafe_rows(opts):
    from mojo.apps.incident.services import firewall_truth
    from mojo.helpers import dates

    permanent = _fresh("10.20.30.41", is_blocked=True)
    snapshot = firewall_truth.permanent_snapshot()
    th.assert_true("10.20.30.41/32" not in snapshot["cidrs"] and
                   "10.20.30.41" not in snapshot["cidrs"],
                   f"a permanent non-global row must be desired-absent, got {snapshot['cidrs'][:5]}")
    permanent.refresh_from_db()
    th.assert_eq(permanent.firewall_sync_error, "unsafe_target: non_global_address",
                 "the permanent row is flagged for the operator")
    th.assert_true(permanent.is_blocked, "is_blocked is not rewritten")

    temporary = _fresh("10.20.30.42", is_blocked=True,
                       blocked_until=dates.utcnow() + timedelta(hours=1))
    plan = firewall_truth.geolocated_snapshot_from_row(temporary)
    th.assert_true(plan["desired"]["ip"]["present"] is False,
                   "a temporary non-global row is planned absent")
    temporary.refresh_from_db()
    th.assert_eq(temporary.firewall_sync_error, "unsafe_target: non_global_address",
                 "the temporary row is flagged for the operator")


@th.django_unit_test()
def test_operator_set_carve_out(opts):
    from mojo.apps.incident.models import IPSet
    from mojo.apps.incident.services import block_safety, firewall_truth

    _fresh("203.0.114.7", is_whitelisted=True)
    data = "10.0.0.0/8\n203.0.114.0/24"
    row = IPSet(name="t8885set", data=data, is_enabled=True)
    with _settings({"MOJOSEC_TRUSTED_PROXY_CIDRS": ["203.0.114.16/28"]}):
        plan = firewall_truth.ipset_snapshot_from_row(row)
    cidrs = plan["cidrs"]
    th.assert_true(not any(c.startswith("10.") for c in cidrs), f"10/8 is carved out, got {cidrs}")
    th.assert_true("203.0.114.7/32" not in cidrs and "203.0.114.0/24" not in cidrs,
                   f"the whitelisted /32 is carved out, got {cidrs}")
    import ipaddress
    kept = [ipaddress.ip_network(c) for c in cidrs]
    for probe in ("203.0.114.7", "203.0.114.16", "203.0.114.31"):
        th.assert_true(not any(ipaddress.ip_address(probe) in n for n in kept),
                       f"{probe} must not be in the kernel set")
    for probe in ("203.0.114.1", "203.0.114.6", "203.0.114.8", "203.0.114.32", "203.0.114.255"):
        th.assert_true(any(ipaddress.ip_address(probe) in n for n in kept),
                       f"{probe} must still be blocked")
    th.assert_eq(plan["desired"]["digest"], firewall_truth.network_digest(cidrs),
                 "the digest describes the kept networks")
    th.assert_eq(plan["desired"]["count"], len(cidrs), "the count describes the kept networks")
    th.assert_eq(row.data, data, "the operator's stored data is unchanged")
    _cleanup("203.0.114.7")

    with mock.patch.object(block_safety, "MAX_FIREWALL_NETWORKS", 20):
        try:
            block_safety.safe_block_networks(
                ["8.0.0.0/8"], protected=[], whitelist=[f"8.{n}.1.1" for n in range(1, 10)])
        except firewall_truth.FirewallTruthError as err:
            th.assert_eq(err.code, "network_limit", "an oversized carve-out refuses")
        else:
            raise AssertionError("a carve-out over the limit must refuse with network_limit")


# =============================================================================
# P6, P7: whitelist hook and the positive path
# =============================================================================

@th.django_unit_test()
def test_whitelist_changes_publish_a_firewall_sync(opts):
    geo = _fresh("203.0.114.90")
    for action in ("whitelist", "unwhitelist"):
        with mock.patch(RECONCILE, return_value=VERIFIED), \
                mock.patch("mojo.apps.incident.cronjobs.sync_firewall") as sync:
            getattr(geo, action)()
        th.assert_eq(sync.call_count, 1, f"{action}() must publish one firewall sync")


@th.django_unit_test()
def test_public_targets_block_as_before(opts):
    geo = _fresh(PUBLIC)
    with mock.patch(RECONCILE, return_value=dict(VERIFIED)) as reconcile:
        result = geo.block_checked(reason="t8885", ttl=600)
    th.assert_eq(result.get("outcome"), "applied", f"a public target blocks, got {result}")
    th.assert_eq(result.get("status"), "verified", "and is checked verified")
    th.assert_true(reconcile.called, "a public block reconciles the firewall")
    geo.refresh_from_db()
    th.assert_true(geo.is_blocked, "the public target is blocked")

    geo = _fresh("203.0.114.251")
    result = geo.block_checked(reason="t8885", ttl=-1)
    th.assert_eq((result.get("outcome"), (result.get("error") or {}).get("code")),
                 ("invalid", "invalid_ttl"), "TTL bounds are unchanged")
    geo = _fresh("2001:db8::1")
    _assert_refused(geo.block_checked(reason="t8885", ttl=600), "unsupported_family",
                    "IPv6")
