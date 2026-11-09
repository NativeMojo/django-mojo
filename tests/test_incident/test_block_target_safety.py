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
PUSH = "mojo.apps.account.models.GeoLocatedIP._maybe_push_abuse_signals"
MALFORMED = {"MOJOSEC_PROTECTED_CIDRS": {"bad": 1}}
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


class _Redis:
    """The desired-state lease, fences and sync locks, in memory."""

    def __init__(self):
        self.store = {}

    def get(self, key):
        return self.store.get(key)

    def mget(self, keys):
        return [self.store.get(key) for key in keys]

    def set(self, key, value, nx=False, ex=None):
        if nx and key in self.store:
            return False
        self.store[key] = value
        return True

    def eval(self, script, count, *args):
        keys, argv = args[:count], args[count:]
        if "redis.call('incr'" in script:
            if self.store.get(keys[0]) != argv[0]:
                return False
            self.store[keys[1]] = int(self.store.get(keys[1], 0)) + 1
            values = []
            for key in keys[2:]:
                self.store[key] = int(self.store.get(key, 0)) + 1
                values.append(self.store[key])
            return values
        if self.store.get(keys[0]) != argv[0]:
            return 0
        if "expire" not in script:
            self.store.pop(keys[0], None)
        return 1


def _job():
    from objict import objict
    job = objict(logs=[], payload={"target": {"host": "t8885-host", "started": "t8885-start",
                                              "runner_id": "t8885-runner"}},
                 attempt=1, max_retries=8)
    job.add_log = job.logs.append
    return job


@contextlib.contextmanager
def _isolated_firewall_state():
    """Own the whole desired snapshot inside a transaction that rolls back."""
    from django.db import transaction
    from mojo.apps.account.models import GeoLocatedIP
    from mojo.apps.incident.models import IPSet

    with transaction.atomic():
        GeoLocatedIP.objects.all().update(
            is_blocked=False, is_whitelisted=False, firewall_pending=False)
        IPSet.objects.all().update(is_enabled=False)
        yield
        transaction.set_rollback(True)


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
    """A refused block leaves block state alone; the threat level the incident
    raised is a separate observation and persists with its push (#8885 R3)."""
    with _settings(CONFIG):
        for ip in (PRIVATE, PROXY_MEMBER):
            geo = _fresh(ip, threat_level="low")
            geo.refresh_from_db()
            before = (geo.is_blocked, geo.blocked_until, geo.firewall_generation,
                      geo.block_count, geo.firewall_pending, geo.firewall_sync_error)
            with mock.patch(RECONCILE, return_value=VERIFIED) as reconcile, \
                    mock.patch(BROKER, side_effect=_broker_ok) as broker, \
                    mock.patch(PUSH) as push:
                geo.update_threat_from_incident(13, block=True)
            geo.refresh_from_db()
            th.assert_eq(geo.threat_level, "critical",
                         f"the incident's threat level persists for {ip}")
            th.assert_eq((geo.is_blocked, geo.blocked_until, geo.firewall_generation,
                          geo.block_count, geo.firewall_pending, geo.firewall_sync_error),
                         before, f"threat escalation must write no block state for {ip}")
            th.assert_true(not reconcile.called, f"threat escalation must not reconcile {ip}")
            th.assert_true(not broker.called, f"threat escalation must not reach the broker for {ip}")
            th.assert_eq(push.call_count, 1,
                         f"only the threat-level rise is pushed for {ip}, got {push.call_count}")


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


# =============================================================================
# Review 131555 / ruling 131611
# =============================================================================

@th.django_unit_test()
def test_removals_dispatch_under_malformed_config(opts):
    """R1: removals are never refused; the permanent list is narrowed to
    NON_GLOBAL_V4 and the whitelist; every other present request refuses."""
    from mojo.apps.incident import firewall
    from mojo.apps.incident.services.firewall_truth import permanent_set_name

    name = permanent_set_name()
    with _settings(MALFORMED):
        dispatched = (
            ("normalize_geolocated_ip removal", lambda: firewall.normalize_geolocated_ip(
                "10.0.0.1", name, [], False)),
            ("normalize_geolocated_ip public removal", lambda: firewall.normalize_geolocated_ip(
                PUBLIC, name, [f"{PUBLIC}/32"], False)),
            ("normalize_permanent_ipset empty", lambda: firewall.normalize_permanent_ipset([])),
            ("normalize_permanent_ipset public", lambda: firewall.normalize_permanent_ipset(
                [f"{PUBLIC}/32"])),
            ("normalize_ipset empty", lambda: firewall.normalize_ipset("t8885set", [])),
            ("normalize_ip absent", lambda: firewall.normalize_ip("10.0.0.1", False)),
        )
        for label, call in dispatched:
            with mock.patch(BROKER, side_effect=_broker_ok) as broker:
                result = call()
            th.assert_true(broker.called, f"{label} must be dispatched, got {result}")

        refused = (
            ("normalize_permanent_ipset non-global", "non_global_address",
             lambda: firewall.normalize_permanent_ipset(["10.0.0.1/32"])),
            ("normalize_geolocated_ip non-global permanent", "non_global_address",
             lambda: firewall.normalize_geolocated_ip(PUBLIC, name, ["10.0.0.1/32"], False)),
            ("normalize_ip present", "protected_config_invalid",
             lambda: firewall.normalize_ip(PUBLIC, True)),
            ("normalize_geolocated_ip present", "protected_config_invalid",
             lambda: firewall.normalize_geolocated_ip(PUBLIC, name, [], True)),
            ("normalize_ipset present", "protected_config_invalid",
             lambda: firewall.normalize_ipset("t8885set", [f"{PUBLIC}/32"])),
        )
        for label, code, call in refused:
            with mock.patch(BROKER, side_effect=_broker_ok) as broker:
                result = call()
            th.assert_eq((result.get("error") or {}).get("code"), code,
                         f"{label} must refuse with {code}, got {result}")
            th.assert_true(not broker.called, f"{label} must not reach the broker")
        for label, call in (
                ("block", lambda: firewall.block(PUBLIC) is False),
                ("ipset_add", lambda: firewall.ipset_add(name, PUBLIC) is False),
                ("ipset_load", lambda: firewall.ipset_load("t8885set", [f"{PUBLIC}/32"])[0] is False)):
            with mock.patch(BROKER, side_effect=_broker_ok) as broker:
                th.assert_true(call(), f"{label} must refuse under malformed config")
            th.assert_true(not broker.called, f"{label} must not reach the broker")


@th.django_unit_test()
def test_unblock_dispatches_under_malformed_config(opts):
    """R1 end to end: GeoLocatedIP.unblock reaches the broker's removal."""
    from mojo.apps.incident import firewall
    from mojo.apps.incident.services import firewall_truth

    def dispatch(func, payload, **kwargs):
        # The host runner's half, in process: the job would call this.
        return firewall.normalize_geolocated_ip(
            payload["ip"], payload["permanent_set_name"],
            payload["permanent_ips"], payload["temporary_present"])

    with _isolated_firewall_state():
        geo = _fresh("10.20.30.43", is_blocked=True)
        with _settings(MALFORMED), \
                mock.patch("mojo.apps.jobs.adapters.get_adapter", return_value=_Redis()), \
                mock.patch("mojo.apps.jobs.publish"), \
                mock.patch.object(firewall_truth, "_dispatch_firewall", side_effect=dispatch), \
                mock.patch(BROKER, side_effect=_broker_ok) as broker:
            geo.unblock(reason="t8885")
        operations = [call.args[0] for call in broker.call_args_list]
        th.assert_true("geolocated.normalize" in operations,
                       f"unblock must dispatch the removal under malformed config, got {operations}")
        values = broker.call_args_list[operations.index("geolocated.normalize")].kwargs
        th.assert_true(values.get("temporary_present") is False,
                       f"the dispatched request is a removal, got {values}")


@th.django_unit_test()
def test_unsafe_marker_survives_reconcile(opts):
    """R2: a verified reconcile keeps `unsafe_target: <code>` on an unsafe
    blocked row, through aggregation and through the checked path."""
    from mojo.apps.incident.asyncjobs import FirewallSyncRetry, _aggregate_firewall_truth
    from mojo.apps.incident.services import firewall_truth

    with _isolated_firewall_state():
        unsafe = _fresh("10.222.33.44", is_blocked=True, firewall_pending=True)
        public = _fresh("203.0.114.244", is_blocked=True, firewall_pending=True)
        firewall_truth.permanent_snapshot()
        unsafe.refresh_from_db()
        th.assert_eq(unsafe.firewall_sync_error, "unsafe_target: non_global_address",
                     "control: the snapshot flags the unsafe row")
        with mock.patch("mojo.apps.jobs.adapters.get_adapter", return_value=_Redis()), \
                mock.patch("mojo.apps.jobs.publish"), \
                mock.patch.object(firewall_truth, "exact_compatible_roster",
                                  return_value=[{"host": "t8885-host", "started": "t8885-start"}]), \
                mock.patch.object(firewall_truth, "aggregate_observations",
                                  return_value={"ok": True}):
            try:
                _aggregate_firewall_truth(_job())
            except FirewallSyncRetry:
                pass
        unsafe.refresh_from_db()
        public.refresh_from_db()
        th.assert_eq(unsafe.firewall_sync_error, "unsafe_target: non_global_address",
                     "aggregation must keep the unsafe-target marker")
        th.assert_true(unsafe.is_blocked and not unsafe.firewall_pending,
                       "the unsafe row stays blocked and is no longer pending")
        th.assert_eq(public.firewall_sync_error, "",
                     "a safe verified row is cleared as before")
        th.assert_true(not public.firewall_pending, "the safe row is no longer pending")

        for geo, expected in ((unsafe, "unsafe_target: non_global_address"), (public, "")):
            GeoLocatedIP = type(geo)
            GeoLocatedIP.objects.filter(pk=geo.pk).update(
                firewall_pending=True, firewall_sync_error="pending")
            geo.refresh_from_db()
            geo._finish_firewall_reconcile(geo.firewall_generation, dict(VERIFIED))
            geo.refresh_from_db()
            th.assert_eq(geo.firewall_sync_error, expected,
                         f"_finish_firewall_reconcile on {geo.ip_address} must write {expected!r}")
            th.assert_true(not geo.firewall_pending, f"{geo.ip_address} is no longer pending")


@th.django_unit_test()
def test_carve_reported_once_per_sync(opts):
    """R4: one metric, one job-log line and one logit line per carved set per
    sync, though the snapshot is built more than once."""
    from mojo.apps.incident import asyncjobs
    from mojo.apps.incident.models import IPSet
    from mojo.apps.incident.services import firewall_truth

    def set_result(name, cidrs, present=True):
        return {"ok": True, "observed": {
            "name": name, "present": present, "exists": present,
            "type": "hash:net" if present else None, "family": "inet" if present else None,
            "count": len(cidrs) if present else 0,
            "digest": firewall_truth.network_digest(cidrs if present else []),
            "input_count": 1 if present else 0, "forward_count": 0,
            "forwarding_required": False}}

    def permanent_result(cidrs):
        return set_result(firewall_truth.permanent_set_name(), cidrs, True)

    def ip_result(ip, present):
        return {"ok": True, "observed": {"ip": ip, "present": present,
                                         "input_count": 1 if present else 0,
                                         "forward_count": 0, "forwarding_required": False}}

    job = _job()
    with _isolated_firewall_state():
        IPSet.objects.filter(name="t8885carve").delete()
        row = IPSet.objects.create(name="t8885carve", kind="custom", source="manual",
                                   data="10.0.0.0/8\n8.8.8.0/24")
        row.set_enabled_desired(True)
        with mock.patch("mojo.apps.incident.services.firewall_readiness.probe",
                        return_value={"ready": True, "code": "ready"}), \
                mock.patch.object(firewall_truth, "expected_hosts",
                                  return_value=[asyncjobs._firewall_host()]), \
                mock.patch.object(firewall_truth, "current_host_runner_target",
                                  return_value=job.payload["target"]), \
                mock.patch("mojo.apps.jobs.adapters.get_adapter", return_value=_Redis()), \
                mock.patch("mojo.apps.jobs.publish"), \
                mock.patch("mojo.apps.incident.firewall.normalize_ipset", side_effect=set_result), \
                mock.patch("mojo.apps.incident.firewall.normalize_permanent_ipset",
                           side_effect=permanent_result), \
                mock.patch("mojo.apps.incident.firewall.normalize_ip", side_effect=ip_result), \
                mock.patch.object(firewall_truth, "ipset_snapshot_from_row",
                                  wraps=firewall_truth.ipset_snapshot_from_row) as built, \
                mock.patch("mojo.apps.metrics.record") as record, \
                mock.patch.object(asyncjobs.logit, "info") as info:
            try:
                asyncjobs.sync_firewall(job)
            except asyncjobs.FirewallSyncRetry:
                pass
    carved_builds = [c for c in built.call_args_list if c.args[0].name == "t8885carve"]
    th.assert_true(len(carved_builds) >= 2,
                   f"control: the carved set's snapshot is built more than once, got {len(carved_builds)}")
    metrics = [c for c in record.call_args_list if c.args and c.args[0] == "firewall:ipset_carved"]
    th.assert_eq(len(metrics), 1, f"one carve metric per sync, got {len(metrics)}")
    lines = [line for line in job.logs if "t8885carve carves" in str(line)]
    th.assert_eq(len(lines), 1, f"one job-log line per sync, got {job.logs}")
    infos = [c for c in info.call_args_list if "t8885carve carves" in str(c.args[0])]
    th.assert_eq(len(infos), 1, f"one logit line per sync, got {len(infos)}")


@th.django_unit_test()
def test_empty_lists_never_refused_under_malformed_config(opts):
    """Ruling 131872: an empty network list is never refused, even while the
    protection settings are malformed; a non-empty load still refuses."""
    from mojo.apps.incident import firewall
    from mojo.apps.incident.models import IPSet
    from mojo.apps.incident.services import block_safety, firewall_truth

    with _settings(MALFORMED):
        th.assert_eq(block_safety.safe_block_networks([]), ([], []),
                     "an empty list is safe under malformed config")

        with mock.patch(BROKER, side_effect=_broker_ok) as broker:
            got = firewall.ipset_load("t8885set", [])
        th.assert_eq(got, (True, 0), f"an empty ipset_load succeeds with 0, got {got}")
        th.assert_eq(broker.call_count, 1, f"one broker call, got {broker.call_count}")
        th.assert_eq(broker.call_args.kwargs.get("cidrs"), [],
                     f"the broker is asked for an empty set, got {broker.call_args}")

        row = IPSet(name="t8885empty", data="", is_enabled=True)
        plan = firewall_truth.ipset_snapshot_from_row(row)
        th.assert_eq(plan["desired"]["count"], 0, f"an empty enabled set plans 0, got {plan['desired']}")
        th.assert_eq(plan["carved"], 0, "nothing is carved from an empty set")

        with mock.patch(BROKER, side_effect=_broker_ok) as broker:
            got = firewall.ipset_load("t8885set", [f"{PUBLIC}/32"])
        th.assert_eq(got, (False, 0), f"a non-empty load still refuses, got {got}")
        th.assert_true(not broker.called, "a refused load must not reach the broker")
