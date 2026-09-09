"""Exact local-host firewall reconciliation regressions."""

import datetime
import json
from unittest import mock

from testit import helpers as th


TEST_PERM = "198.51.100.50"
TEST_TTL = "198.51.100.52"
TEST_ABSENT = "198.51.100.53"
LOCAL_INCARNATION = {"host": "test-host", "started": "test-start"}
LOCAL_TARGET = {**LOCAL_INCARNATION, "runner_id": "test-runner"}


class _Redis:
    def __init__(self):
        self.store = {}
        self.evals = []
        self.mget_calls = []

    def get(self, key):
        return self.store.get(key)

    def mget(self, keys):
        self.mget_calls.append(list(keys))
        return [self.store.get(key) for key in keys]

    def set(self, key, value, nx=False, ex=None):
        if nx and key in self.store:
            return False
        self.store[key] = value
        return True

    def eval(self, script, count, *args):
        keys, argv = args[:count], args[count:]
        self.evals.append((script, count, keys, argv))
        if "redis.call('incr'" in script:
            if self.store.get(keys[0]) != argv[0]:
                return False
            self.store[keys[1]] = int(self.store.get(keys[1], 0)) + 1
            values = []
            for key in keys[2:]:
                self.store[key] = int(self.store.get(key, 0)) + 1
                values.append(self.store[key])
            return values
        key = keys[0]
        token = argv[0]
        if self.store.get(key) != token:
            return 0
        if "expire" in script:
            return 1
        self.store.pop(key, None)
        return 1


def _job(target=None):
    from objict import objict
    value = objict(logs=[], payload={"target": target or LOCAL_TARGET},
                   attempt=1, max_retries=8)
    value.add_log = value.logs.append
    return value


def _set_result(name, cidrs, present):
    from mojo.apps.incident.services.firewall_truth import network_digest
    return {"ok": True, "observed": {
        "name": name, "present": present, "exists": present,
        "type": "hash:net" if present else None,
        "family": "inet" if present else None,
        "count": len(cidrs) if present else 0,
        "digest": network_digest(cidrs if present else []),
        "input_count": 1 if present else 0,
        "forward_count": 0, "forwarding_required": False,
    }}


def _ip_result(ip, present):
    return {"ok": True, "observed": {
        "ip": ip, "present": present, "input_count": 1 if present else 0,
        "forward_count": 0, "forwarding_required": False,
    }}


def _permanent_result(cidrs):
    from mojo.apps.incident.services.firewall_truth import permanent_set_name
    return _set_result(permanent_set_name(), cidrs, True)


def _observation(kind, identity, fence, fingerprint, host, desired,
                 started=None):
    from mojo.apps.incident.services import firewall_truth
    return json.dumps({
        "schema": firewall_truth.FIREWALL_SEMANTIC_SCHEMA,
        "version": firewall_truth.FIREWALL_SEMANTIC_VERSION,
        "kind": kind, "identity": identity, "fence": fence,
        "fingerprint": fingerprint, "host": host,
        "started": started or f"{host}-start", "desired": desired,
    }, sort_keys=True, separators=(",", ":"))


def _run_sync(job):
    from mojo.apps.incident.asyncjobs import _firewall_host, sync_firewall
    # These tests exercise post-admission kernel/fence behavior on an enrolled
    # host. Readiness rejection itself is covered by firewall_readiness tests.
    with mock.patch(
            "mojo.apps.incident.services.firewall_readiness.probe",
            return_value={"ready": True, "code": "ready"}), mock.patch(
            "mojo.apps.incident.services.firewall_truth.expected_hosts",
            return_value=[_firewall_host()]), mock.patch(
            "mojo.apps.incident.services.firewall_truth.current_host_runner_target",
            return_value=job.payload["target"]):
        return sync_firewall(job)


@th.django_unit_setup()
def setup_sync_firewall(opts):
    from django.db.models import Q
    from mojo.apps.account.models import GeoLocatedIP
    from mojo.apps.incident.models import IPSet
    from mojo.helpers import dates

    # sync_firewall consumes the complete desired snapshot, so this serial
    # fixture must own that snapshot rather than inherit blocks from earlier
    # packages in the long-lived test database.
    GeoLocatedIP.objects.filter(
        Q(is_blocked=True) | Q(is_whitelisted=True) |
        Q(firewall_pending=True)
    ).update(
        is_blocked=False, blocked_until=None, is_whitelisted=False,
        whitelisted_until=None, firewall_pending=False,
        firewall_sync_error="")
    GeoLocatedIP.objects.filter(ip_address__in=(
        TEST_PERM, TEST_TTL, TEST_ABSENT)).delete()
    IPSet.objects.filter(name__in=("test_sync_fw", "test_disabled_fw")).delete()
    GeoLocatedIP.objects.create(
        ip_address=TEST_PERM, is_blocked=True, blocked_until=None,
        firewall_pending=True)
    GeoLocatedIP.objects.create(
        ip_address=TEST_TTL, is_blocked=True,
        blocked_until=dates.utcnow() + datetime.timedelta(hours=1),
        firewall_pending=True)
    GeoLocatedIP.objects.create(
        ip_address=TEST_ABSENT, is_blocked=False, firewall_pending=True)
    enabled = IPSet.objects.create(
        name="test_sync_fw", kind="custom", source="manual",
        data="172.16.1.1/12\n10.0.0.0/8\n10.1.0.0/8")
    enabled.set_enabled_desired(True)
    IPSet.objects.create(
        name="test_disabled_fw", kind="custom", source="manual",
        data="192.0.2.0/24")


@th.django_unit_test("reconcile covers permanent aggregate, tombstones and TTL truth")
def test_sync_exact_desired_snapshot(opts):
    from mojo.apps.incident.asyncjobs import _sync_firewall_keys, sync_firewall
    from mojo.apps.incident.services.firewall_truth import permanent_set_name

    redis = _Redis()
    sets = []
    ips = []

    def normalize_set(name, cidrs, present=True):
        sets.append((name, list(cidrs), present))
        return _set_result(name, cidrs, present)

    def normalize_ip(ip, present):
        ips.append((ip, present))
        return _ip_result(ip, present)

    with mock.patch("mojo.apps.jobs.adapters.get_adapter", return_value=redis), \
            mock.patch("mojo.apps.jobs.publish"), \
            mock.patch("mojo.apps.incident.firewall.normalize_ipset",
                       side_effect=normalize_set), \
            mock.patch("mojo.apps.incident.firewall.normalize_permanent_ipset",
                       side_effect=lambda cidrs: normalize_set(
                           permanent_set_name(), cidrs, True)), \
            mock.patch("mojo.apps.incident.firewall.normalize_ip",
                       side_effect=normalize_ip):
        result = _run_sync(_job())

    assert result is True, "exact mocked reconciliation did not verify"
    aggregate_name = permanent_set_name()
    permanent = next(row for row in sets if row[0] == aggregate_name)
    assert permanent == (aggregate_name, [TEST_PERM + "/32"], True), \
        f"permanent aggregate was not exact: {permanent!r}"
    assert ("test_disabled_fw", [], False) in sets, \
        f"disabled tombstone was not reconciled: {sets!r}"
    assert (TEST_TTL, True) in ips and (TEST_ABSENT, False) in ips, \
        f"TTL presence/absence truth was incomplete: {ips!r}"
    assert redis.get(_sync_firewall_keys()[0]) is not None, \
        "verified exact generation did not advance its host marker"
    from mojo.apps.account.models import GeoLocatedIP
    from mojo.apps.incident.models import IPSet
    assert GeoLocatedIP.objects.get(ip_address=TEST_TTL).firewall_pending, \
        "one host directly cleared shared Geo truth"
    assert IPSet.objects.get(name="test_sync_fw").sync_error, \
        "one host directly cleared shared IPSet truth"


@th.django_unit_test("empty permanent desired state removes stale membership")
def test_empty_permanent_state_is_normalized(opts):
    from mojo.apps.account.models import GeoLocatedIP
    from mojo.apps.incident.asyncjobs import sync_firewall
    from mojo.apps.incident.services.firewall_truth import permanent_set_name

    GeoLocatedIP.objects.filter(ip_address=TEST_PERM).update(
        is_blocked=False, firewall_pending=True)
    redis = _Redis()
    calls = []

    def normalize_set(name, cidrs, present=True):
        calls.append((name, list(cidrs), present))
        return _set_result(name, cidrs, present)

    with mock.patch("mojo.apps.jobs.adapters.get_adapter", return_value=redis), \
            mock.patch("mojo.apps.jobs.publish"), \
            mock.patch("mojo.apps.incident.firewall.normalize_ipset",
                       side_effect=normalize_set), \
            mock.patch("mojo.apps.incident.firewall.normalize_permanent_ipset",
                       side_effect=lambda cidrs: normalize_set(
                           permanent_set_name(), cidrs, True)), \
            mock.patch("mojo.apps.incident.firewall.normalize_ip",
                       side_effect=_ip_result):
        _run_sync(_job())
    assert (permanent_set_name(), [], True) in calls, \
        "empty permanent aggregate was skipped instead of normalized"


@th.django_unit_test("host lock release and renewal are token-checked Lua operations")
def test_lock_is_atomic_and_renewed(opts):
    from mojo.apps.incident.asyncjobs import _sync_firewall_keys, sync_firewall

    redis = _Redis()
    with mock.patch("mojo.apps.jobs.adapters.get_adapter", return_value=redis), \
            mock.patch("mojo.apps.jobs.publish"), \
            mock.patch("mojo.apps.incident.firewall.normalize_ipset",
                       side_effect=_set_result), \
            mock.patch("mojo.apps.incident.firewall.normalize_permanent_ipset",
                       side_effect=_permanent_result), \
            mock.patch("mojo.apps.incident.firewall.normalize_ip",
                       side_effect=_ip_result):
        _run_sync(_job())
    assert any("expire" in call[0] for call in redis.evals), \
        "long reconciliation never renewed its owned lease"
    assert any("del" in call[0] for call in redis.evals), \
        "lock release did not use compare-and-delete Lua"


@th.django_unit_test("busy host lock refuses before firewall writes")
def test_busy_host_lock_is_not_success(opts):
    from mojo.apps.incident.asyncjobs import (
        FirewallSyncRetry, _sync_firewall_keys, sync_firewall)

    redis = _Redis()
    redis.store[_sync_firewall_keys()[2]] = "another-generation"
    with mock.patch("mojo.apps.jobs.adapters.get_adapter", return_value=redis), \
            mock.patch("mojo.apps.jobs.publish"), \
            mock.patch("mojo.apps.incident.firewall.normalize_ipset") as sets, \
            mock.patch("mojo.apps.incident.firewall.normalize_permanent_ipset") \
                    as permanent, \
            mock.patch("mojo.apps.incident.firewall.normalize_ip") as ips:
        with th.assert_raises(FirewallSyncRetry) as raised:
            _run_sync(_job())
    assert raised.exception.code == "host_busy", \
        "host lock collision completed instead of requesting durable retry"
    sets.assert_not_called()
    permanent.assert_not_called()
    ips.assert_not_called()


@th.django_unit_test("permanent broker failure starts backoff before later work")
def test_permanent_broker_failure_stops_immediately(opts):
    from mojo.apps.incident.asyncjobs import FirewallSyncRetry
    from mojo.apps.incident.services import firewall_truth

    redis = _Redis()
    failed = {"ok": False, "error": {"code": "broker_timeout"}}
    with mock.patch("mojo.apps.jobs.adapters.get_adapter", return_value=redis), \
            mock.patch("mojo.apps.jobs.publish") as publish, \
            mock.patch("mojo.apps.incident.firewall.normalize_permanent_ipset",
                       return_value=failed), \
            mock.patch("mojo.apps.incident.firewall.normalize_ipset") as sets, \
            mock.patch("mojo.apps.incident.firewall.normalize_ip") as ips, \
            mock.patch.object(
                firewall_truth, "permanent_snapshot",
                wraps=firewall_truth.permanent_snapshot) as snapshots, \
            th.assert_raises(FirewallSyncRetry) as raised:
        _run_sync(_job())
    assert raised.exception.code == "broker_timeout", "broker timeout lost its retry code"
    assert snapshots.call_count == 1, "broker failure triggered a post-call snapshot"
    sets.assert_not_called()
    ips.assert_not_called()
    publish.assert_not_called()


@th.django_unit_test("set broker failure starts backoff before later work")
def test_set_broker_failure_stops_immediately(opts):
    from mojo.apps.incident.asyncjobs import FirewallSyncRetry
    from mojo.apps.incident.services import firewall_truth

    redis = _Redis()
    failed = {"ok": False, "error": {"code": "broker_start_failed"}}
    with mock.patch("mojo.apps.jobs.adapters.get_adapter", return_value=redis), \
            mock.patch("mojo.apps.jobs.publish") as publish, \
            mock.patch("mojo.apps.incident.firewall.normalize_permanent_ipset",
                       side_effect=_permanent_result), \
            mock.patch("mojo.apps.incident.firewall.normalize_ipset",
                       return_value=failed) as sets, \
            mock.patch("mojo.apps.incident.firewall.normalize_ip") as ips, \
            mock.patch.object(
                firewall_truth, "ipset_snapshot",
                wraps=firewall_truth.ipset_snapshot) as snapshots, \
            th.assert_raises(FirewallSyncRetry) as raised:
        _run_sync(_job())
    assert raised.exception.code == "broker_start_failed", "broker startup failure lost its code"
    assert sets.call_count == 1, "set broker failure did not stop sibling calls"
    snapshots.assert_not_called()
    ips.assert_not_called()
    publish.assert_not_called()


@th.django_unit_test("IP broker failure starts backoff before later work")
def test_ip_broker_failure_stops_immediately(opts):
    from mojo.apps.incident.asyncjobs import FirewallSyncRetry
    from mojo.apps.incident.services import firewall_truth

    redis = _Redis()
    failed = {"ok": False, "error": {"code": "broker_invalid_response"}}
    with mock.patch("mojo.apps.jobs.adapters.get_adapter", return_value=redis), \
            mock.patch("mojo.apps.jobs.publish") as publish, \
            mock.patch("mojo.apps.incident.firewall.normalize_permanent_ipset",
                       side_effect=_permanent_result), \
            mock.patch("mojo.apps.incident.firewall.normalize_ipset",
                       side_effect=_set_result), \
            mock.patch("mojo.apps.incident.firewall.normalize_ip",
                       return_value=failed) as ips, \
            mock.patch.object(
                firewall_truth, "geolocated_snapshot",
                wraps=firewall_truth.geolocated_snapshot) as snapshots, \
            th.assert_raises(FirewallSyncRetry) as raised:
        _run_sync(_job())
    assert raised.exception.code == "broker_invalid_response", "invalid broker response lost its code"
    assert ips.call_count == 1, "IP broker failure did not stop sibling calls"
    snapshots.assert_not_called()
    publish.assert_not_called()


@th.django_unit_test("stale generation cannot clear a newer pending decision")
def test_object_generation_fences_finalize(opts):
    from mojo.apps.account.models import GeoLocatedIP
    from mojo.apps.incident.asyncjobs import (
        FirewallSyncRetry, _sync_firewall_keys, sync_firewall)

    redis = _Redis()

    def normalize_ip(ip, present):
        if ip == TEST_ABSENT:
            GeoLocatedIP.objects.filter(ip_address=ip).update(
                firewall_generation=99, firewall_pending=True)
        return _ip_result(ip, present)

    with mock.patch("mojo.apps.jobs.adapters.get_adapter", return_value=redis), \
            mock.patch("mojo.apps.jobs.publish"), \
            mock.patch("mojo.apps.incident.firewall.normalize_ipset",
                       side_effect=_set_result), \
            mock.patch("mojo.apps.incident.firewall.normalize_permanent_ipset",
                       side_effect=_permanent_result), \
            mock.patch("mojo.apps.incident.firewall.normalize_ip",
                       side_effect=normalize_ip):
        with th.assert_raises(FirewallSyncRetry):
            _run_sync(_job())
    row = GeoLocatedIP.objects.get(ip_address=TEST_ABSENT)
    assert row.firewall_generation == 99 and row.firewall_pending is True, \
        "stale observation overwrote a newer desired generation"
    assert redis.get(_sync_firewall_keys()[0]) is None, \
        "superseded desired generation advanced host success"


@th.django_unit_test("semantic mismatch does not advance host marker")
def test_mismatch_does_not_advance_marker(opts):
    from mojo.apps.incident.asyncjobs import (
        FirewallSyncRetry, _sync_firewall_keys, sync_firewall)

    redis = _Redis()
    unused_marker, force_key, unused_lock = _sync_firewall_keys()
    redis.store[force_key] = "pending-force-generation"
    bad = {"ok": False, "observed": None,
           "error": {"code": "state_mismatch"}}
    with mock.patch("mojo.apps.jobs.adapters.get_adapter", return_value=redis), \
            mock.patch("mojo.apps.jobs.publish"), \
            mock.patch("mojo.apps.incident.firewall.normalize_ipset",
                       return_value=bad), \
            mock.patch("mojo.apps.incident.firewall.normalize_permanent_ipset",
                       return_value=bad), \
            mock.patch("mojo.apps.incident.firewall.normalize_ip",
                       side_effect=_ip_result):
        with th.assert_raises(FirewallSyncRetry):
            _run_sync(_job())
    assert redis.get(_sync_firewall_keys()[0]) is None, \
        "host marker advanced without semantic verification"
    assert redis.get(force_key) == "pending-force-generation", \
        "failed reconciliation erased its pending force generation"


@th.django_unit_test("invalid legacy rows are quarantined without poisoning valid repair")
def test_per_object_quarantine_continues_valid_rows(opts):
    from mojo.apps.account.models import GeoLocatedIP
    from mojo.apps.incident.asyncjobs import _sync_firewall_keys, sync_firewall
    from mojo.apps.incident.models import IPSet

    invalid_ip = "2001:db8::252"
    invalid_set = "legacy_ipv6_sync"
    reserved_set = "mojo_legacy_operator"
    overlong_set = "x" * 28
    GeoLocatedIP.objects.filter(ip_address=invalid_ip).delete()
    IPSet.objects.filter(name__in=(
        invalid_set, reserved_set, overlong_set)).delete()
    GeoLocatedIP.objects.create(
        ip_address=invalid_ip, firewall_pending=True)
    IPSet.objects.bulk_create([
        IPSet(name=invalid_set, kind="custom", is_enabled=True,
              data="2001:db8::/32", cidr_count=1),
        IPSet(name=reserved_set, kind="custom", is_enabled=False,
              data="192.0.2.0/24", cidr_count=1),
        IPSet(name=overlong_set, kind="custom", is_enabled=False,
              data="192.0.2.0/24", cidr_count=1),
    ])
    redis = _Redis()
    job = _job()
    operator_calls = []

    def normalize_set(name, cidrs, present=True):
        operator_calls.append(name)
        return _set_result(name, cidrs, present)

    with mock.patch("mojo.apps.jobs.adapters.get_adapter", return_value=redis), \
            mock.patch("mojo.apps.jobs.publish") as publish, \
            mock.patch("mojo.apps.incident.firewall.normalize_ipset",
                       side_effect=normalize_set), \
            mock.patch("mojo.apps.incident.firewall.normalize_permanent_ipset",
                       side_effect=_permanent_result), \
            mock.patch("mojo.apps.incident.firewall.normalize_ip",
                       side_effect=_ip_result):
        result = _run_sync(job)
    assert result is True and redis.get(_sync_firewall_keys()[0]) is not None
    publish.assert_called_once()
    assert any("quarantined" in line for line in job.logs), job.logs
    assert any(key.startswith("mojo:firewall:observation:set:test_sync_fw")
               for key in redis.store), \
        "valid sibling was not observed after a legacy row was quarantined"
    assert not {invalid_set, reserved_set, overlong_set} & set(operator_calls), \
        "quarantined operator namespace/data reached the privileged broker"


@th.django_unit_test("firewall reconcile keys are host scoped")
def test_keys_are_host_scoped(opts):
    from mojo.apps.incident.asyncjobs import _sync_firewall_keys

    assert _sync_firewall_keys("node-a") != _sync_firewall_keys("node-b"), \
        "two hosts share reconciliation lock or marker keys"


@th.django_unit_test("fleet aggregate is fenced to exact roster membership")
def test_observation_roster_membership_change(opts):
    from mojo.apps.incident.services import firewall_truth

    redis = _Redis()
    desired = {"ip": TEST_TTL, "present": True}
    fingerprint = firewall_truth.state_fingerprint(desired)
    for host in ("node-a", "node-b"):
        redis.store[firewall_truth.observation_key(
            "ip", TEST_TTL, 7, host)] = _observation(
                "ip", TEST_TTL, 7, fingerprint, host, desired)
    with mock.patch.object(firewall_truth, "_redis_client", return_value=redis), \
            mock.patch.object(
                firewall_truth, "exact_compatible_roster",
                return_value=[
                    {"host": "node-a", "started": "node-a-start"},
                    {"host": "node-b", "started": "node-b-start"}]):
        exact = firewall_truth.aggregate_observations(
            "ip", TEST_TTL, 7, fingerprint, desired)
    assert exact["ok"] is True, exact
    with mock.patch.object(firewall_truth, "_redis_client", return_value=redis), \
            mock.patch.object(
                firewall_truth, "exact_compatible_roster",
                return_value=[
                    {"host": "node-a", "started": "node-a-start"},
                    {"host": "node-b", "started": "node-b-start"},
                    {"host": "node-c", "started": "node-c-start"}]):
        changed = firewall_truth.aggregate_observations(
            "ip", TEST_TTL, 7, fingerprint, desired)
    assert changed["ok"] is False, \
        "old observations verified after compatible-host membership changed"


@th.django_unit_test("expired or failing host observation poisons fleet success")
def test_observation_expiry_and_concurrent_failure(opts):
    from mojo.apps.incident.services import firewall_truth

    redis = _Redis()
    desired = {"ip": TEST_TTL, "present": True}
    fingerprint = firewall_truth.state_fingerprint(desired)
    key_a = firewall_truth.observation_key("ip", TEST_TTL, 8, "node-a")
    key_b = firewall_truth.observation_key("ip", TEST_TTL, 8, "node-b")
    redis.store[key_a] = _observation(
        "ip", TEST_TTL, 8, fingerprint, "node-a", desired)
    # A concurrent failure cannot be represented as success: an invalid value
    # at the expected key poisons the aggregate just like a missing key.
    redis.store[key_b] = _observation(
        "ip", TEST_TTL, 8, fingerprint, "node-b",
        {"ip": TEST_TTL, "present": False})
    with mock.patch.object(firewall_truth, "_redis_client", return_value=redis):
        failed = firewall_truth.aggregate_observations(
            "ip", TEST_TTL, 8, fingerprint, desired,
            roster=[
                {"host": "node-a", "started": "node-a-start"},
                {"host": "node-b", "started": "node-b-start"}])
        redis.store.pop(key_b)  # model TTL expiry
        expired = firewall_truth.aggregate_observations(
            "ip", TEST_TTL, 8, fingerprint, desired,
            roster=[
                {"host": "node-a", "started": "node-a-start"},
                {"host": "node-b", "started": "node-b-start"}])
    assert failed["ok"] is False and expired["ok"] is False, \
        "partial current-host evidence was accepted as fleet truth"


@th.django_unit_test("a restarted hostname cannot reuse its prior observation")
def test_observation_rejects_prior_host_incarnation(opts):
    from mojo.apps.incident.services import firewall_truth

    redis = _Redis()
    desired = {"ip": TEST_TTL, "present": True}
    fingerprint = firewall_truth.state_fingerprint(desired)
    redis.store[firewall_truth.observation_key(
        "ip", TEST_TTL, 9, "node-a")] = _observation(
            "ip", TEST_TTL, 9, fingerprint, "node-a", desired,
            started="before-restart")
    with mock.patch.object(firewall_truth, "_redis_client", return_value=redis):
        result = firewall_truth.aggregate_observations(
            "ip", TEST_TTL, 9, fingerprint, desired,
            roster=[{"host": "node-a", "started": "after-restart"}])
    assert result["ok"] is False, \
        "pre-restart host evidence survived the startup repair gap"
    assert result["error"]["code"] == "host_observation_mismatch", result


@th.django_unit_test("checked finalization rejects a post-reply runner restart")
def test_checked_finalization_rejects_changed_incarnation(opts):
    from mojo.apps.incident.services import firewall_truth

    checked = {
        "expected_roster": [
            {"host": "node-a", "started": "before-restart"}],
    }
    with mock.patch.object(
            firewall_truth, "exact_compatible_roster",
            return_value=[{"host": "node-a", "started": "after-restart"}]), \
            th.assert_raises(firewall_truth.FirewallTruthError) as raised:
        firewall_truth._checked_current_roster(checked, "firewall")
    assert raised.exception.code == "runner_roster_changed", \
        "a checked reply remained authoritative after its runner restarted"


@th.django_unit_test("host repair releases the global lease during broker I/O")
def test_sync_broker_io_does_not_starve_other_hosts(opts):
    from mojo.apps.incident.asyncjobs import sync_firewall
    from mojo.apps.incident.services import firewall_truth

    redis = _Redis()
    observed_unlocked = []
    permanent_snapshot = firewall_truth.permanent_snapshot
    ipset_snapshot_from_row = firewall_truth.ipset_snapshot_from_row
    geo_snapshot_from_row = firewall_truth.geolocated_snapshot_from_row
    ipset_snapshot = firewall_truth.ipset_snapshot
    geo_snapshot = firewall_truth.geolocated_snapshot

    def outside_lease(callback, *args, **kwargs):
        observed_unlocked.append(
            redis.get(firewall_truth.DESIRED_STATE_LOCK) is None)
        return callback(*args, **kwargs)

    def normalize_permanent(cidrs):
        observed_unlocked.append(
            redis.get(firewall_truth.DESIRED_STATE_LOCK) is None)
        return _permanent_result(cidrs)

    def normalize_set(name, cidrs, present=True):
        observed_unlocked.append(
            redis.get(firewall_truth.DESIRED_STATE_LOCK) is None)
        return _set_result(name, cidrs, present)

    def normalize_ip(ip, present):
        observed_unlocked.append(
            redis.get(firewall_truth.DESIRED_STATE_LOCK) is None)
        return _ip_result(ip, present)

    with mock.patch("mojo.apps.jobs.adapters.get_adapter", return_value=redis), \
            mock.patch("mojo.apps.jobs.publish"), \
            mock.patch("mojo.apps.incident.firewall.normalize_ipset",
                       side_effect=normalize_set), \
            mock.patch("mojo.apps.incident.firewall.normalize_permanent_ipset",
                       side_effect=normalize_permanent), \
            mock.patch("mojo.apps.incident.firewall.normalize_ip",
                       side_effect=normalize_ip), \
            mock.patch.object(
                firewall_truth, "permanent_snapshot",
                side_effect=lambda: outside_lease(permanent_snapshot)), \
            mock.patch.object(
                firewall_truth, "ipset_snapshot_from_row",
                side_effect=lambda row: outside_lease(
                    ipset_snapshot_from_row, row)), \
            mock.patch.object(
                firewall_truth, "geolocated_snapshot_from_row",
                side_effect=lambda row, permanent=None: outside_lease(
                    geo_snapshot_from_row, row, permanent=permanent)), \
            mock.patch.object(
                firewall_truth, "ipset_snapshot",
                side_effect=lambda name: outside_lease(ipset_snapshot, name)), \
            mock.patch.object(
                firewall_truth, "geolocated_snapshot",
                side_effect=lambda ip, permanent=None: outside_lease(
                    geo_snapshot, ip, permanent=permanent)), \
            mock.patch.object(
                firewall_truth, "current_host_runner_target",
                return_value={"host": "node-a", "runner_id": "runner-a",
                              "started": "node-a-start"}):
        result = _run_sync(_job({
            "host": "node-a", "runner_id": "runner-a",
            "started": "node-a-start"}))
    assert result is True and observed_unlocked and all(observed_unlocked), \
        "snapshot or broker work retained the fleet-wide desired-state lease"
    assert any(key.endswith(":node-a") and "observation" in key
               for key in redis.store), \
        "the unstarved host did not publish its incarnation-bound observation"


@th.django_unit_test("desired-state lease contention retries then stays typed")
def test_desired_lease_contention_is_bounded_and_retryable(opts):
    from mojo.apps.incident.services import firewall_truth

    class ContendedRedis(_Redis):
        def __init__(self):
            super().__init__()
            self.attempts = 0

        def set(self, key, value, nx=False, ex=None):
            if key == firewall_truth.DESIRED_STATE_LOCK:
                self.attempts += 1
                if self.attempts < 3:
                    return False
            return super().set(key, value, nx=nx, ex=ex)

    redis = ContendedRedis()
    with mock.patch.object(firewall_truth, "_redis_client", return_value=redis), \
            mock.patch.object(firewall_truth.time, "sleep"):
        lease = firewall_truth.acquire_desired_state(timeout=0.1)
    assert redis.attempts == 3 and firewall_truth.desired_state_is_current(lease)
    firewall_truth.release_desired_state(lease)

    redis.store[firewall_truth.DESIRED_STATE_LOCK] = "other-owner"
    with mock.patch.object(firewall_truth, "_redis_client", return_value=redis), \
            th.assert_raises(firewall_truth.FirewallTruthError) as raised:
        firewall_truth.acquire_desired_state(timeout=0)
    assert raised.exception.code == "desired_state_busy", \
        "lease contention was not exposed as retryable unknown state"


@th.django_unit_test("sync lease contention is a durable retry, never completion")
def test_sync_lease_contention_raises_for_job_retry(opts):
    from mojo.apps.incident.asyncjobs import FirewallSyncRetry, sync_firewall
    from mojo.apps.incident.services import firewall_truth

    redis = _Redis()
    busy = firewall_truth.FirewallTruthError(
        "desired_state_busy", "another host owns the desired lease")
    with mock.patch("mojo.apps.jobs.adapters.get_adapter", return_value=redis), \
            mock.patch.object(firewall_truth, "current_host_runner_target",
                              return_value=LOCAL_TARGET), \
            mock.patch.object(firewall_truth, "acquire_desired_state",
                              side_effect=busy), \
            mock.patch("mojo.apps.incident.firewall.normalize_ipset") as broker, \
            th.assert_raises(FirewallSyncRetry) as raised:
        _run_sync(_job())
    assert raised.exception.code == "desired_state_busy", "lease contention lost its retry code"
    broker.assert_not_called()


@th.django_unit_test("bounded fence validation uses one Redis round trip")
def test_max_cardinality_fences_are_batch_read(opts):
    from mojo.apps.incident.services import firewall_truth

    redis = _Redis()
    targets = [("ip", f"10.{index // 256}.{index % 256}.1")
               for index in range(10000)]
    assert len(firewall_truth.read_fences(redis, targets)) == 10000
    assert len(redis.mget_calls) == 1 and len(redis.mget_calls[0]) == 10000, \
        "max-cardinality plan performed serialized per-object Redis reads"


@th.django_unit_test("oversize desired plan remains pending through job retry")
def test_desired_plan_bound_raises_for_retry(opts):
    from mojo.apps.incident import asyncjobs

    redis = _Redis()
    with mock.patch("mojo.apps.jobs.adapters.get_adapter", return_value=redis), \
            mock.patch.object(asyncjobs, "SYNC_FIREWALL_MAX_OBJECTS", 0), \
            mock.patch(
                "mojo.apps.incident.services.firewall_truth.current_host_runner_target",
                return_value=LOCAL_TARGET), \
            mock.patch("mojo.apps.incident.firewall.normalize_ipset") as broker, \
            th.assert_raises(asyncjobs.FirewallSyncRetry) as raised:
        _run_sync(_job())
    assert raised.exception.code == "desired_object_bound_exceeded", "oversize plan lost its safety code"
    broker.assert_not_called()


@th.django_unit_test("independent hosts can publish the same desired generation")
def test_two_hosts_publish_without_global_lease_starvation(opts):
    from mojo.apps.incident.asyncjobs import sync_firewall
    from mojo.apps.incident.services import firewall_truth

    redis = _Redis()
    targets = [
        {"host": "node-a", "runner_id": "runner-a",
         "started": "node-a-start"},
        {"host": "node-b", "runner_id": "runner-b",
         "started": "node-b-start"},
    ]
    with mock.patch("mojo.apps.jobs.adapters.get_adapter", return_value=redis), \
            mock.patch("mojo.apps.jobs.publish"), \
            mock.patch("mojo.apps.incident.firewall.normalize_ipset",
                       side_effect=_set_result), \
            mock.patch("mojo.apps.incident.firewall.normalize_permanent_ipset",
                       side_effect=_permanent_result), \
            mock.patch("mojo.apps.incident.firewall.normalize_ip",
                       side_effect=_ip_result), \
            mock.patch.object(
                firewall_truth, "current_host_runner_target",
                side_effect=targets):
        assert _run_sync(_job(targets[0])) is True, "node-a did not publish its observation"
        assert _run_sync(_job(targets[1])) is True, "node-b did not publish its observation"
    permanent = firewall_truth.permanent_snapshot()
    target = ("permanent", permanent["name"])
    fence = firewall_truth.read_fences(redis, [target])[target]
    for incarnation in targets:
        key = firewall_truth.observation_key(
            "permanent", permanent["name"], fence, incarnation["host"])
        assert redis.get(key) is not None, \
            f"host observation was starved: {incarnation!r}"


@th.django_unit_test("fleet aggregator alone clears shared IPSet truth")
def test_fleet_aggregator_requires_every_host(opts):
    from mojo.apps.incident.asyncjobs import (
        FirewallSyncRetry, aggregate_firewall_truth)
    from mojo.apps.incident.models import IPSet
    from mojo.apps.incident.services import firewall_truth

    row = IPSet.objects.get(name="test_sync_fw")
    snapshot = firewall_truth.ipset_snapshot(row.name)
    redis = _Redis()
    fence = 11
    redis.store[firewall_truth.fence_key("set", row.name)] = fence
    key_a = firewall_truth.observation_key("set", row.name, fence, "node-a")
    key_b = firewall_truth.observation_key("set", row.name, fence, "node-b")
    redis.store[key_a] = _observation(
        "set", row.name, fence, snapshot["fingerprint"], "node-a",
        snapshot["desired"])
    with mock.patch("mojo.apps.jobs.adapters.get_adapter", return_value=redis), \
            mock.patch.object(
                firewall_truth, "exact_compatible_roster",
                return_value=[
                    {"host": "node-a", "started": "node-a-start"},
                    {"host": "node-b", "started": "node-b-start"}]):
        with th.assert_raises(FirewallSyncRetry):
            aggregate_firewall_truth(_job())
    row.refresh_from_db()
    assert row.last_synced is None and row.sync_error, \
        "one successful host cleared shared IPSet truth"

    redis.store[key_b] = _observation(
        "set", row.name, fence, snapshot["fingerprint"], "node-b",
        snapshot["desired"])
    with mock.patch("mojo.apps.jobs.adapters.get_adapter", return_value=redis), \
            mock.patch.object(
                firewall_truth, "exact_compatible_roster",
                return_value=[
                    {"host": "node-a", "started": "node-a-start"},
                    {"host": "node-b", "started": "node-b-start"}]):
        with th.assert_raises(FirewallSyncRetry):
            aggregate_firewall_truth(_job())
    row.refresh_from_db()
    assert row.last_synced is not None and row.sync_error is None, \
        "complete same-generation fleet evidence did not finalize IPSet truth"


@th.django_unit_test("verified reconciliation consumes its force generation")
def test_verified_sync_clears_force_flag(opts):
    from mojo.apps.incident.asyncjobs import _sync_firewall_keys, sync_firewall

    redis = _Redis()
    unused_marker, force_key, unused_lock = _sync_firewall_keys()
    redis.store[force_key] = "pending-force-generation"
    with mock.patch("mojo.apps.jobs.adapters.get_adapter", return_value=redis), \
            mock.patch("mojo.apps.jobs.publish"), \
            mock.patch("mojo.apps.incident.firewall.normalize_ipset",
                       side_effect=_set_result), \
            mock.patch("mojo.apps.incident.firewall.normalize_permanent_ipset",
                       side_effect=_permanent_result), \
            mock.patch("mojo.apps.incident.firewall.normalize_ip",
                       side_effect=_ip_result):
        assert _run_sync(_job()) is True
    assert redis.get(force_key) is None, \
        "verified reconciliation left a consumed force generation behind"


@th.django_unit_test("firewall cron selects exactly one incarnation per hostname")
def test_cron_schedule_and_host_fanout(opts):
    from mojo.apps.incident import cronjobs
    from mojo.apps.jobs.manager import JobManager
    from mojo.decorators.cron import schedule

    specs = {spec["func"].__name__: spec
             for spec in schedule.scheduled_functions}
    assert specs["sweep_expired_blocks"]["minutes"] == "*/5"
    assert specs["sync_firewall"]["minutes"] == "0"
    rows = [
        {"runner_id": "z-runner", "hostname": "node-a",
         "started": "2026-09-04T12:00:00+00:00", "channels": ["firewall", "z-runner"],
         "capabilities": {"execute_checked": 2, "firewall_reconcile": 1}},
        {"runner_id": "a-runner", "hostname": "node-a",
         "started": "2026-09-04T12:01:00+00:00", "channels": ["firewall", "a-runner"],
         "capabilities": {"execute_checked": 2, "firewall_reconcile": 1}},
        {"runner_id": "b-runner", "hostname": "node-b",
         "started": "2026-09-04T12:02:00+00:00", "channels": ["firewall", "b-runner"],
         "capabilities": {"execute_checked": 2, "firewall_reconcile": 1}},
    ]
    manager = mock.Mock()
    manager.get_runners_bounded.return_value = rows
    manager._checked_host_roster = JobManager._checked_host_roster
    with mock.patch("mojo.apps.jobs.manager.get_manager",
                    return_value=manager), \
            mock.patch("mojo.apps.incident.services.firewall_truth.expected_hosts",
                       return_value=["node-a", "node-b"]), \
            th.capture_publishes(
            lambda call: call.get("func") == cronjobs.FIREWALL_SYNC_JOB) as calls:
        cronjobs.sync_firewall()
    assert [call["channel"] for call in calls] == ["a-runner", "b-runner"], calls
    assert [call["payload"]["target"]["host"] for call in calls] == [
        "node-a", "node-b"], calls
    assert all(call.get("max_retries") == 8 and
               call.get("backoff_max") == 300 for call in calls), calls


@th.django_unit_test("firewall cron fails closed without hostname channels")
def test_cron_fallback_without_hostname_channels(opts):
    from mojo.apps.incident import cronjobs

    original = cronjobs.settings.get_static

    def get_static(name, default=None, **kwargs):
        if name == "JOBS_HOSTNAME_CHANNEL":
            return False
        return original(name, default, **kwargs)

    with mock.patch.object(
            cronjobs.settings, "get_static", side_effect=get_static), \
            th.capture_publishes(
                lambda call: call.get("func") == cronjobs.FIREWALL_SYNC_JOB) as calls:
        result = cronjobs.sync_firewall()
    assert result == "failed" and calls == [], calls
