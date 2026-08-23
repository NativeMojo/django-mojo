"""
Tests for sync_firewall reconciliation job.

Verifies that sync_firewall queries the correct IPs and IPSets,
calls ipset_load for each, and skips unchanged sets on subsequent runs.
"""
from testit import helpers as th
from unittest import mock

TEST_IP_1 = "198.51.100.50"
TEST_IP_2 = "198.51.100.51"
TEST_IP_TTL = "198.51.100.52"

def _keys(host=None):
    """(last_sync, force, lock) Redis keys, host-scoped exactly as the job."""
    from mojo.apps.incident.asyncjobs import _sync_firewall_keys
    return _sync_firewall_keys(host)


def _last_sync_key(host=None):
    return _keys(host)[0]


def _make_job():
    from objict import objict
    job = objict(logs=[])
    job.add_log = lambda msg: job.logs.append(msg)
    return job


def _mock_redis(last_sync_value=None, store=None):
    """Return a mock redis client with get/set/delete and nx semantics.

    `store` lets two runs in one test share one Redis, which is how the
    per-node marker regression is proven.
    """
    store = {} if store is None else store
    if last_sync_value:
        store[_last_sync_key()] = last_sync_value

    def _set(key, val, **kwargs):
        if kwargs.get("nx") and key in store:
            return False
        store[key] = val
        return True

    def _delete(*keys):
        removed = 0
        for key in keys:
            if store.pop(key, None) is not None:
                removed += 1
        return removed

    r = mock.MagicMock()
    r.store = store
    r.get = lambda key: store.get(key)
    r.set = _set
    r.delete = _delete
    return r


@th.django_unit_setup()
def setup_sync_firewall(opts):
    from mojo.apps.account.models import GeoLocatedIP
    from mojo.apps.incident.models import IPSet
    from django.utils import timezone
    from datetime import timedelta

    # Clean up from previous runs
    GeoLocatedIP.objects.filter(
        ip_address__in=[TEST_IP_1, TEST_IP_2, TEST_IP_TTL]
    ).delete()
    IPSet.objects.filter(name="test_sync_fw").delete()

    # Permanent blocks (should be included in sync)
    GeoLocatedIP.objects.create(
        ip_address=TEST_IP_1, is_blocked=True, blocked_until=None,
        blocked_reason="test_perm_1")
    GeoLocatedIP.objects.create(
        ip_address=TEST_IP_2, is_blocked=True, blocked_until=None,
        blocked_reason="test_perm_2")

    # TTL block (should NOT be included — has blocked_until)
    GeoLocatedIP.objects.create(
        ip_address=TEST_IP_TTL, is_blocked=True,
        blocked_until=timezone.now() + timedelta(hours=1),
        blocked_reason="test_ttl")

    # Enabled IPSet
    opts.test_ipset = IPSet.objects.create(
        name="test_sync_fw",
        kind="custom",
        source="manual",
        data="10.0.0.0/8\n172.16.0.0/12",
        is_enabled=True,
        cidr_count=2,
    )


@th.django_unit_test()
def test_sync_loads_permanent_blocks(opts):
    """sync_firewall should load permanently blocked IPs into mojo_blocked ipset on first run."""
    from mojo.apps.incident.asyncjobs import sync_firewall, FIREWALL_BLOCKED_IPSET_NAME

    job = _make_job()
    mock_redis = _mock_redis()  # No last_sync → first run

    with mock.patch("mojo.apps.incident.firewall.ipset_load", return_value=(True, 2)) as mock_load, \
         mock.patch("mojo.apps.jobs.adapters.get_adapter", return_value=mock_redis):
        sync_firewall(job)

    # Find the call for permanent blocks
    perm_calls = [
        c for c in mock_load.call_args_list
        if c[0][0] == FIREWALL_BLOCKED_IPSET_NAME
    ]
    assert len(perm_calls) == 1, \
        f"Expected 1 ipset_load call for {FIREWALL_BLOCKED_IPSET_NAME}, got {len(perm_calls)}"

    loaded_ips = perm_calls[0][0][1]
    assert TEST_IP_1 in loaded_ips, f"Permanent IP {TEST_IP_1} should be in loaded IPs"
    assert TEST_IP_2 in loaded_ips, f"Permanent IP {TEST_IP_2} should be in loaded IPs"
    assert TEST_IP_TTL not in loaded_ips, f"TTL IP {TEST_IP_TTL} should NOT be in loaded IPs"


@th.django_unit_test()
def test_sync_loads_enabled_ipsets(opts):
    """sync_firewall should load all enabled IPSet records on first run."""
    from mojo.apps.incident.asyncjobs import sync_firewall

    job = _make_job()
    mock_redis = _mock_redis()

    with mock.patch("mojo.apps.incident.firewall.ipset_load", return_value=(True, 2)) as mock_load, \
         mock.patch("mojo.apps.jobs.adapters.get_adapter", return_value=mock_redis):
        sync_firewall(job)

    # Find the call for our test ipset
    ipset_calls = [
        c for c in mock_load.call_args_list
        if c[0][0] == "test_sync_fw"
    ]
    assert len(ipset_calls) == 1, \
        f"Expected 1 ipset_load call for test_sync_fw, got {len(ipset_calls)}"

    loaded_cidrs = ipset_calls[0][0][1]
    assert "10.0.0.0/8" in loaded_cidrs, "Should load 10.0.0.0/8 CIDR"
    assert "172.16.0.0/12" in loaded_cidrs, "Should load 172.16.0.0/12 CIDR"


@th.django_unit_test()
def test_sync_skips_unchanged_ipsets(opts):
    """sync_firewall should skip IPSets that haven't changed since last sync."""
    from mojo.apps.incident.asyncjobs import sync_firewall
    from mojo.apps.incident.models import IPSet
    from mojo.helpers import dates
    from datetime import timedelta

    # Mark the test ipset as synced recently (after its modified time)
    ipset = IPSet.objects.get(name="test_sync_fw")
    ipset.last_synced = dates.utcnow()
    ipset.save(update_fields=["last_synced"])

    # Set last_sync to after the ipset was modified
    last_sync = (dates.utcnow() + timedelta(seconds=1)).isoformat()
    mock_redis = _mock_redis(last_sync)

    job = _make_job()

    with mock.patch("mojo.apps.incident.firewall.ipset_load", return_value=(True, 2)) as mock_load, \
         mock.patch("mojo.apps.jobs.adapters.get_adapter", return_value=mock_redis):
        sync_firewall(job)

    # The test ipset should have been skipped
    ipset_calls = [
        c for c in mock_load.call_args_list
        if c[0][0] == "test_sync_fw"
    ]
    assert len(ipset_calls) == 0, \
        f"Expected 0 ipset_load calls for unchanged test_sync_fw, got {len(ipset_calls)}"

    # Should log the skip
    skip_logs = [l for l in job.logs if "skipped" in l.lower()]
    assert len(skip_logs) >= 1, f"Should log skipped IPSets, got logs: {job.logs}"


@th.django_unit_test()
def test_sync_reloads_modified_ipsets(opts):
    """sync_firewall should reload IPSets that changed after last sync."""
    from mojo.apps.incident.asyncjobs import sync_firewall
    from mojo.apps.incident.models import IPSet
    from datetime import timedelta
    from mojo.helpers import dates

    # Set last_sync to well before the ipset was modified
    old_sync = (dates.utcnow() - timedelta(hours=2)).isoformat()
    mock_redis = _mock_redis(old_sync)

    # Ensure last_synced is also old so modified > last_sync
    ipset = IPSet.objects.get(name="test_sync_fw")
    ipset.last_synced = dates.utcnow() - timedelta(hours=3)
    ipset.save(update_fields=["last_synced"])

    job = _make_job()

    with mock.patch("mojo.apps.incident.firewall.ipset_load", return_value=(True, 2)) as mock_load, \
         mock.patch("mojo.apps.jobs.adapters.get_adapter", return_value=mock_redis):
        sync_firewall(job)

    ipset_calls = [
        c for c in mock_load.call_args_list
        if c[0][0] == "test_sync_fw"
    ]
    assert len(ipset_calls) == 1, \
        f"Expected 1 ipset_load call for modified test_sync_fw, got {len(ipset_calls)}"


@th.django_unit_test()
def test_sync_skips_unchanged_permanent_blocks(opts):
    """sync_firewall should skip mojo_blocked if no GeoLocatedIP changed since last sync."""
    from mojo.apps.incident.asyncjobs import sync_firewall, FIREWALL_BLOCKED_IPSET_NAME
    from datetime import timedelta
    from mojo.helpers import dates

    # Set last_sync to the future so nothing has changed since
    future_sync = (dates.utcnow() + timedelta(hours=1)).isoformat()
    mock_redis = _mock_redis(future_sync)

    job = _make_job()

    with mock.patch("mojo.apps.incident.firewall.ipset_load", return_value=(True, 2)) as mock_load, \
         mock.patch("mojo.apps.jobs.adapters.get_adapter", return_value=mock_redis):
        sync_firewall(job)

    perm_calls = [
        c for c in mock_load.call_args_list
        if c[0][0] == FIREWALL_BLOCKED_IPSET_NAME
    ]
    assert len(perm_calls) == 0, \
        f"Expected 0 ipset_load calls for unchanged permanent blocks, got {len(perm_calls)}"

    skip_logs = [l for l in job.logs if "unchanged" in l.lower() or "skipped" in l.lower()]
    assert len(skip_logs) >= 1, f"Should log that permanent blocks were skipped, got: {job.logs}"


@th.django_unit_test()
def test_sync_logs_results(opts):
    """sync_firewall should log what it loaded on first run."""
    from mojo.apps.incident.asyncjobs import sync_firewall

    job = _make_job()
    mock_redis = _mock_redis()

    with mock.patch("mojo.apps.incident.firewall.ipset_load", return_value=(True, 2)), \
         mock.patch("mojo.apps.jobs.adapters.get_adapter", return_value=mock_redis):
        sync_firewall(job)

    assert len(job.logs) >= 2, f"Expected at least 2 log entries, got {len(job.logs)}"
    assert "permanent blocks" in job.logs[0].lower() or "mojo_blocked" in job.logs[0], \
        f"First log should mention permanent blocks, got: {job.logs[0]}"


@th.django_unit_test()
def test_sync_stores_timestamp_in_redis(opts):
    """sync_firewall should store a timestamp in Redis after running."""
    from mojo.apps.incident.asyncjobs import sync_firewall

    mock_redis = _mock_redis()
    job = _make_job()

    with mock.patch("mojo.apps.incident.firewall.ipset_load", return_value=(True, 2)), \
         mock.patch("mojo.apps.jobs.adapters.get_adapter", return_value=mock_redis):
        sync_firewall(job)

    stored = mock_redis.get(_last_sync_key())
    assert stored is not None, \
        f"Should store this host's last sync timestamp; store={mock_redis.store}"
    assert _last_sync_key().startswith("mojo:sync_firewall:last_sync:"), \
        f"The marker must be host-scoped, got {_last_sync_key()}"


@th.django_unit_test()
def test_cron_schedule_updated(opts):
    """Verify sweep is */5 and sync_firewall is registered."""
    # Import the cronjobs module to ensure decorators fire
    import mojo.apps.incident.cronjobs
    from mojo.decorators.cron import schedule

    func_names = [spec["func"].__name__ for spec in schedule.scheduled_functions]
    assert "sweep_expired_blocks" in func_names, "sweep_expired_blocks should be registered"
    assert "sync_firewall" in func_names, "sync_firewall should be registered as cron"

    # Verify sweep schedule is */5
    sweep_spec = next(s for s in schedule.scheduled_functions if s["func"].__name__ == "sweep_expired_blocks")
    assert sweep_spec["minutes"] == "*/5", \
        f"sweep should run every 5 min, got minutes={sweep_spec['minutes']}"

    # Verify sync_firewall schedule is hourly at minute 0
    sync_spec = next(s for s in schedule.scheduled_functions if s["func"].__name__ == "sync_firewall")
    assert sync_spec["minutes"] == "0", \
        f"sync_firewall should run at minute 0, got minutes={sync_spec['minutes']}"


@th.django_unit_test("one node's sync must not suppress another node's restore")
def test_sync_marker_is_per_node(opts):
    """Regression (item #2716): the last-sync marker was deployment-wide, so
    the first node to reconcile suppressed every other node's reload of
    unchanged rows — a rebooted node could stay with empty ipsets forever.

    Asserts specifically on FIREWALL_BLOCKED_IPSET_NAME: that is the load
    deterministically suppressed by a stale marker, because the GeoLocatedIP
    rows predate run 1. The enabled-IPSet skip additionally requires a truthy
    ipset.last_synced, so it is set here rather than left to whatever a
    sibling test in this module happened to leave behind.
    """
    from mojo.apps.incident.asyncjobs import sync_firewall, FIREWALL_BLOCKED_IPSET_NAME
    from mojo.apps.incident.models import IPSet
    from mojo.helpers import dates

    ipset = IPSet.objects.get(name="test_sync_fw")
    ipset.last_synced = dates.utcnow()
    ipset.save(update_fields=["last_synced"])

    shared = {}

    def _run(host):
        job = _make_job()
        redis_client = _mock_redis(store=shared)
        with mock.patch("mojo.apps.incident.firewall.ipset_load",
                        return_value=(True, 2)) as mock_load, \
             mock.patch("mojo.apps.jobs.adapters.get_adapter",
                        return_value=redis_client), \
             mock.patch("mojo.apps.jobs.job_engine.host_channel",
                        return_value=host):
            sync_firewall(job)
        return mock_load

    _run("node-a")
    second = _run("node-b")

    perm_calls = [
        c for c in second.call_args_list
        if c[0][0] == FIREWALL_BLOCKED_IPSET_NAME
    ]
    assert len(perm_calls) == 1, (
        "node-b did not reload its permanent blocks after node-a synced — "
        "the last-sync marker is still shared across the fleet, so a "
        f"rebooted node never recovers (calls: {second.call_args_list})")


@th.django_unit_test("a forced run reloads even with a fresh marker")
def test_sync_force_ignores_marker(opts):
    """The marker lives in shared Redis and survives the reboot the startup
    hook exists to recover from, so force must bypass it entirely."""
    from mojo.apps.incident.asyncjobs import sync_firewall, FIREWALL_BLOCKED_IPSET_NAME
    from mojo.helpers import dates
    from datetime import timedelta

    fresh = (dates.utcnow() + timedelta(hours=1)).isoformat()
    mock_redis = _mock_redis(fresh)
    job = _make_job()
    job.payload = {"force": True}

    with mock.patch("mojo.apps.incident.firewall.ipset_load",
                    return_value=(True, 2)) as mock_load, \
         mock.patch("mojo.apps.jobs.adapters.get_adapter", return_value=mock_redis):
        sync_firewall(job)

    names = [c[0][0] for c in mock_load.call_args_list]
    assert FIREWALL_BLOCKED_IPSET_NAME in names, \
        f"a forced run must reload permanent blocks despite a fresh marker, got {names}"
    assert "test_sync_fw" in names, \
        f"a forced run must reload every enabled IPSet, got {names}"


@th.django_unit_test("the Redis force flag forces a reload and is then cleared")
def test_sync_force_flag_in_redis_forces_reload(opts):
    """The flag is how a forced startup reconcile survives a lost lock: it
    outlives the skipped job and the next reconcile on this host honors it."""
    from mojo.apps.incident.asyncjobs import sync_firewall, FIREWALL_BLOCKED_IPSET_NAME
    from mojo.helpers import dates
    from datetime import timedelta

    last_sync_key, force_key, _ = _keys()
    store = {last_sync_key: (dates.utcnow() + timedelta(hours=1)).isoformat(),
             force_key: "1"}
    mock_redis = _mock_redis(store=store)
    job = _make_job()

    with mock.patch("mojo.apps.incident.firewall.ipset_load",
                    return_value=(True, 2)) as mock_load, \
         mock.patch("mojo.apps.jobs.adapters.get_adapter", return_value=mock_redis):
        sync_firewall(job)

    names = [c[0][0] for c in mock_load.call_args_list]
    assert FIREWALL_BLOCKED_IPSET_NAME in names, \
        f"the Redis force flag must force a full reload, got {names}"
    assert mock_redis.get(force_key) is None, \
        "a clean forced run must clear the force flag so it does not force forever"


@th.django_unit_test("a failed load must not advance the marker")
def test_sync_failed_load_does_not_advance_marker(opts):
    """Advancing the marker after a failed load is what would give every
    failing node its own permanent self-suppression."""
    from mojo.apps.incident.asyncjobs import sync_firewall

    last_sync_key, force_key, _ = _keys()
    mock_redis = _mock_redis(store={force_key: "1"})
    job = _make_job()

    with mock.patch("mojo.apps.incident.firewall.ipset_load",
                    return_value=(False, 0)), \
         mock.patch("mojo.apps.jobs.adapters.get_adapter", return_value=mock_redis):
        sync_firewall(job)

    assert mock_redis.get(last_sync_key) is None, \
        "the marker advanced despite every load failing — this node would skip forever"
    assert mock_redis.get(force_key) == "1", \
        "the force flag was cleared on a failed run, losing the pending recovery"
    failure_logs = [l for l in job.logs if "failed" in l.lower()]
    assert failure_logs, f"the failure should be logged on the job, got {job.logs}"


@th.django_unit_test("an empty permanent-block list is not counted as a failure")
def test_sync_empty_permanent_blocks_is_not_a_failure(opts):
    """ipset_load returns (False, 0) for an empty list on purpose — refusing
    to wipe a live set with an empty swap is not an error."""
    from mojo.apps.account.models import GeoLocatedIP
    from mojo.apps.incident.asyncjobs import sync_firewall
    from mojo.apps.incident.models import IPSet

    GeoLocatedIP.objects.filter(
        ip_address__in=[TEST_IP_1, TEST_IP_2]).update(is_blocked=False)
    IPSet.objects.filter(name="test_sync_fw").update(is_enabled=False)
    try:
        last_sync_key, _, _ = _keys()
        mock_redis = _mock_redis()
        job = _make_job()
        with mock.patch("mojo.apps.incident.firewall.ipset_load",
                        return_value=(False, 0)), \
             mock.patch("mojo.apps.jobs.adapters.get_adapter", return_value=mock_redis):
            sync_firewall(job)
        assert mock_redis.get(last_sync_key) is not None, \
            "a run with nothing to load is clean and must advance the marker"
    finally:
        GeoLocatedIP.objects.filter(
            ip_address__in=[TEST_IP_1, TEST_IP_2]).update(is_blocked=True)
        IPSet.objects.filter(name="test_sync_fw").update(is_enabled=True)


@th.django_unit_test("a second reconcile on one host is skipped, not interleaved")
def test_sync_skips_when_lock_held(opts):
    """Two concurrent set.replace calls for one set share a deterministic
    '<name>_tmp' and can swap in a live set missing entries."""
    from mojo.apps.incident.asyncjobs import sync_firewall

    _, _, lock_key = _keys()
    mock_redis = _mock_redis(store={lock_key: "someone-else"})
    job = _make_job()

    with mock.patch("mojo.apps.incident.firewall.ipset_load",
                    return_value=(True, 2)) as mock_load, \
         mock.patch("mojo.apps.jobs.adapters.get_adapter", return_value=mock_redis):
        sync_firewall(job)

    assert mock_load.call_count == 0, \
        f"reconcile ran while another held the host lock: {mock_load.call_args_list}"
    assert mock_redis.get(lock_key) == "someone-else", \
        "the skipped run released a lock it never held"


@th.django_unit_test("a clean reconcile releases its host lock")
def test_sync_releases_lock(opts):
    from mojo.apps.incident.asyncjobs import sync_firewall

    _, _, lock_key = _keys()
    mock_redis = _mock_redis()
    job = _make_job()

    with mock.patch("mojo.apps.incident.firewall.ipset_load",
                    return_value=(True, 2)), \
         mock.patch("mojo.apps.jobs.adapters.get_adapter", return_value=mock_redis):
        sync_firewall(job)

    assert mock_redis.get(lock_key) is None, \
        "the host lock outlived the reconcile and would block the next hour"


@th.django_unit_test("the hourly cron fans the reconcile out to every runner")
def test_cron_publishes_broadcast(opts):
    from mojo.apps.incident import cronjobs

    with th.capture_publishes(
            lambda c: c.get("func") == cronjobs.FIREWALL_SYNC_JOB) as calls:
        cronjobs.sync_firewall()

    assert len(calls) == 1, f"expected one reconcile publish, got {calls}"
    assert calls[0].get("broadcast") is True, \
        f"the reconcile must broadcast or only one node heals: {calls[0]}"
    assert calls[0].get("channel") == "default", \
        f"the reconcile must go to the channel every node consumes: {calls[0]}"


@th.django_unit_test("the cron degrades to a unicast when box-direct channels are off")
def test_cron_falls_back_when_hostname_channel_off(opts):
    """With JOBS_HOSTNAME_CHANNEL off no engine consumes its box-direct
    channel, so a fan-out would strand every job — worse than today."""
    from mojo.apps.incident import cronjobs

    real_get_static = cronjobs.settings.get_static

    def _fake(name, default=None, **kwargs):
        if name == "JOBS_HOSTNAME_CHANNEL":
            return False
        return real_get_static(name, default, **kwargs)

    with mock.patch.object(cronjobs.settings, "get_static", side_effect=_fake), \
         th.capture_publishes(
             lambda c: c.get("func") == cronjobs.FIREWALL_SYNC_JOB) as calls:
        cronjobs.sync_firewall()

    assert len(calls) == 1, f"expected one reconcile publish, got {calls}"
    assert not calls[0].get("broadcast"), \
        f"a fan-out here strands every job; it must be a unicast: {calls[0]}"
