"""Exact local-host firewall reconciliation regressions."""

import datetime
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

    def eval(self, script, count, key, token, *extra):
        self.evals.append((script, count, key, token, extra))
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
    from mojo.apps.incident.asyncjobs import (
        FIREWALL_BLOCKED_IPSET_NAME, _sync_firewall_keys, sync_firewall)

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
            mock.patch("mojo.apps.incident.firewall.normalize_ipset",
                       side_effect=normalize_set), \
            mock.patch("mojo.apps.incident.firewall.normalize_ip",
                       side_effect=normalize_ip):
        result = sync_firewall(_job())

    assert result is True, "exact mocked reconciliation did not verify"
    permanent = next(row for row in sets if row[0] == FIREWALL_BLOCKED_IPSET_NAME)
    assert permanent == (FIREWALL_BLOCKED_IPSET_NAME, [TEST_PERM + "/32"], True), \
        f"permanent aggregate was not exact: {permanent!r}"
    assert ("test_disabled_fw", [], False) in sets, \
        f"disabled tombstone was not reconciled: {sets!r}"
    assert (TEST_TTL, True) in ips and (TEST_ABSENT, False) in ips, \
        f"TTL presence/absence truth was incomplete: {ips!r}"
    assert redis.get(_sync_firewall_keys()[0]) is not None, \
        "verified exact generation did not advance its host marker"


@th.django_unit_test("empty permanent desired state removes stale membership")
def test_empty_permanent_state_is_normalized(opts):
    from mojo.apps.account.models import GeoLocatedIP
    from mojo.apps.incident.asyncjobs import (
        FIREWALL_BLOCKED_IPSET_NAME, sync_firewall)

    GeoLocatedIP.objects.filter(ip_address=TEST_PERM).update(
        is_blocked=False, firewall_pending=True)
    redis = _Redis()
    calls = []

    def normalize_set(name, cidrs, present=True):
        calls.append((name, list(cidrs), present))
        return _set_result(name, cidrs, present)

    with mock.patch("mojo.apps.jobs.adapters.get_adapter", return_value=redis), \
            mock.patch("mojo.apps.incident.firewall.normalize_ipset",
                       side_effect=normalize_set), \
            mock.patch("mojo.apps.incident.firewall.normalize_ip",
                       side_effect=_ip_result):
        sync_firewall(_job())
    assert (FIREWALL_BLOCKED_IPSET_NAME, [], True) in calls, \
        "empty permanent aggregate was skipped instead of normalized"


@th.django_unit_test("host lock release and renewal are token-checked Lua operations")
def test_lock_is_atomic_and_renewed(opts):
    from mojo.apps.incident.asyncjobs import _sync_firewall_keys, sync_firewall

    redis = _Redis()
    with mock.patch("mojo.apps.jobs.adapters.get_adapter", return_value=redis), \
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


@th.django_unit_test("firewall reconcile keys are host scoped")
def test_keys_are_host_scoped(opts):
    from mojo.apps.incident.asyncjobs import _sync_firewall_keys

    assert _sync_firewall_keys("node-a") != _sync_firewall_keys("node-b"), \
        "two hosts share reconciliation lock or marker keys"


@th.django_unit_test("verified reconciliation consumes its force generation")
def test_verified_sync_clears_force_flag(opts):
    from mojo.apps.incident.asyncjobs import _sync_firewall_keys, sync_firewall

    redis = _Redis()
    unused_marker, force_key, unused_lock = _sync_firewall_keys()
    redis.store[force_key] = "pending-force-generation"
    with mock.patch("mojo.apps.jobs.adapters.get_adapter", return_value=redis), \
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
