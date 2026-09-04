"""Exact local-host firewall reconciliation regressions."""

import datetime
import json
from unittest import mock

from testit import helpers as th


TEST_PERM = "198.51.100.50"
TEST_TTL = "198.51.100.52"
TEST_ABSENT = "198.51.100.53"


class _Redis:
    def __init__(self):
        self.store = {}
        self.evals = []

    def get(self, key):
        return self.store.get(key)

    def set(self, key, value, nx=False, ex=None):
        if nx and key in self.store:
            return False
        self.store[key] = value
        return True

    def eval(self, script, count, *args):
        keys, argv = args[:count], args[count:]
        self.evals.append((script, count, keys, argv))
        if "redis.call('incr'" in script:
            values = []
            for key in keys:
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


def _job():
    from objict import objict
    value = objict(logs=[], payload={})
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


def _observation(kind, identity, fence, fingerprint, host, desired):
    from mojo.apps.incident.services import firewall_truth
    return json.dumps({
        "schema": firewall_truth.FIREWALL_SEMANTIC_SCHEMA,
        "version": firewall_truth.FIREWALL_SEMANTIC_VERSION,
        "kind": kind, "identity": identity, "fence": fence,
        "fingerprint": fingerprint, "host": host, "desired": desired,
    }, sort_keys=True, separators=(",", ":"))


@th.django_unit_setup()
def setup_sync_firewall(opts):
    from mojo.apps.account.models import GeoLocatedIP
    from mojo.apps.incident.models import IPSet
    from mojo.helpers import dates

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
            mock.patch("mojo.apps.incident.firewall.normalize_ip",
                       side_effect=normalize_ip):
        result = sync_firewall(_job())

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
            mock.patch("mojo.apps.incident.firewall.normalize_ip",
                       side_effect=_ip_result):
        sync_firewall(_job())
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
            mock.patch("mojo.apps.incident.firewall.normalize_ip",
                       side_effect=_ip_result):
        sync_firewall(_job())
    assert any("expire" in call[0] for call in redis.evals), \
        "long reconciliation never renewed its owned lease"
    assert any("del" in call[0] for call in redis.evals), \
        "lock release did not use compare-and-delete Lua"


@th.django_unit_test("busy host lock refuses before firewall writes")
def test_busy_host_lock_is_not_success(opts):
    from mojo.apps.incident.asyncjobs import _sync_firewall_keys, sync_firewall

    redis = _Redis()
    redis.store[_sync_firewall_keys()[2]] = "another-generation"
    with mock.patch("mojo.apps.jobs.adapters.get_adapter", return_value=redis), \
            mock.patch("mojo.apps.jobs.publish"), \
            mock.patch("mojo.apps.incident.firewall.normalize_ipset") as sets, \
            mock.patch("mojo.apps.incident.firewall.normalize_ip") as ips:
        result = sync_firewall(_job())
    assert result is False, "host lock collision looked like reconcile success"
    sets.assert_not_called()
    ips.assert_not_called()


@th.django_unit_test("stale generation cannot clear a newer pending decision")
def test_object_generation_fences_finalize(opts):
    from mojo.apps.account.models import GeoLocatedIP
    from mojo.apps.incident.asyncjobs import _sync_firewall_keys, sync_firewall

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
            mock.patch("mojo.apps.incident.firewall.normalize_ip",
                       side_effect=normalize_ip):
        result = sync_firewall(_job())
    row = GeoLocatedIP.objects.get(ip_address=TEST_ABSENT)
    assert row.firewall_generation == 99 and row.firewall_pending is True, \
        "stale observation overwrote a newer desired generation"
    assert result is False and redis.get(_sync_firewall_keys()[0]) is None, \
        "superseded desired generation advanced host success"


@th.django_unit_test("semantic mismatch does not advance host marker")
def test_mismatch_does_not_advance_marker(opts):
    from mojo.apps.incident.asyncjobs import _sync_firewall_keys, sync_firewall

    redis = _Redis()
    unused_marker, force_key, unused_lock = _sync_firewall_keys()
    redis.store[force_key] = "pending-force-generation"
    bad = {"ok": False, "observed": None,
           "error": {"code": "state_mismatch"}}
    with mock.patch("mojo.apps.jobs.adapters.get_adapter", return_value=redis), \
            mock.patch("mojo.apps.jobs.publish"), \
            mock.patch("mojo.apps.incident.firewall.normalize_ipset",
                       return_value=bad), \
            mock.patch("mojo.apps.incident.firewall.normalize_ip",
                       side_effect=_ip_result):
        result = sync_firewall(_job())
    assert result is False, "mismatch was reported as successful reconcile"
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
    GeoLocatedIP.objects.filter(ip_address=invalid_ip).delete()
    IPSet.objects.filter(name=invalid_set).delete()
    GeoLocatedIP.objects.create(
        ip_address=invalid_ip, firewall_pending=True)
    IPSet.objects.bulk_create([
        IPSet(name=invalid_set, kind="custom", is_enabled=True,
              data="2001:db8::/32", cidr_count=1)])
    redis = _Redis()
    job = _job()
    with mock.patch("mojo.apps.jobs.adapters.get_adapter", return_value=redis), \
            mock.patch("mojo.apps.jobs.publish") as publish, \
            mock.patch("mojo.apps.incident.firewall.normalize_ipset",
                       side_effect=_set_result), \
            mock.patch("mojo.apps.incident.firewall.normalize_ip",
                       side_effect=_ip_result):
        result = sync_firewall(job)
    assert result is True and redis.get(_sync_firewall_keys()[0]) is not None
    publish.assert_called_once()
    assert any("quarantined" in line for line in job.logs), job.logs
    assert any(key.startswith("mojo:firewall:observation:set:test_sync_fw")
               for key in redis.store), \
        "valid sibling was not observed after a legacy row was quarantined"


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
                firewall_truth, "exact_compatible_hosts",
                return_value=["node-a", "node-b"]):
        exact = firewall_truth.aggregate_observations(
            "ip", TEST_TTL, 7, fingerprint, desired)
    assert exact["ok"] is True, exact
    with mock.patch.object(firewall_truth, "_redis_client", return_value=redis), \
            mock.patch.object(
                firewall_truth, "exact_compatible_hosts",
                return_value=["node-a", "node-b", "node-c"]):
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
            hosts=["node-a", "node-b"])
        redis.store.pop(key_b)  # model TTL expiry
        expired = firewall_truth.aggregate_observations(
            "ip", TEST_TTL, 8, fingerprint, desired,
            hosts=["node-a", "node-b"])
    assert failed["ok"] is False and expired["ok"] is False, \
        "partial current-host evidence was accepted as fleet truth"


@th.django_unit_test("fleet aggregator alone clears shared IPSet truth")
def test_fleet_aggregator_requires_every_host(opts):
    from mojo.apps.incident.asyncjobs import aggregate_firewall_truth
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
                firewall_truth, "exact_compatible_hosts",
                return_value=["node-a", "node-b"]):
        aggregate_firewall_truth(_job())
    row.refresh_from_db()
    assert row.last_synced is None and row.sync_error, \
        "one successful host cleared shared IPSet truth"

    redis.store[key_b] = _observation(
        "set", row.name, fence, snapshot["fingerprint"], "node-b",
        snapshot["desired"])
    with mock.patch("mojo.apps.jobs.adapters.get_adapter", return_value=redis), \
            mock.patch.object(
                firewall_truth, "exact_compatible_hosts",
                return_value=["node-a", "node-b"]):
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
            mock.patch("mojo.apps.incident.firewall.normalize_ip",
                       side_effect=_ip_result):
        assert sync_firewall(_job()) is True
    assert redis.get(force_key) is None, \
        "verified reconciliation left a consumed force generation behind"


@th.django_unit_test("firewall cron schedule and host fanout stay registered")
def test_cron_schedule_and_broadcast(opts):
    from mojo.apps.incident import cronjobs
    from mojo.decorators.cron import schedule

    specs = {spec["func"].__name__: spec
             for spec in schedule.scheduled_functions}
    assert specs["sweep_expired_blocks"]["minutes"] == "*/5"
    assert specs["sync_firewall"]["minutes"] == "0"
    with th.capture_publishes(
            lambda call: call.get("func") == cronjobs.FIREWALL_SYNC_JOB) as calls:
        cronjobs.sync_firewall()
    assert len(calls) == 1 and calls[0].get("broadcast") is True, calls
    assert calls[0].get("channel") == "default", calls


@th.django_unit_test("firewall cron preserves hostname-channel fallback")
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
        cronjobs.sync_firewall()
    assert len(calls) == 1 and not calls[0].get("broadcast"), calls
