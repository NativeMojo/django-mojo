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

REDIS_KEY = "mojo:sync_firewall:last_sync"


def _make_job():
    from objict import objict
    job = objict(logs=[])
    job.add_log = lambda msg: job.logs.append(msg)
    return job


def _mock_redis(last_sync_value=None):
    """Return a mock redis client with get/set."""
    store = {}
    if last_sync_value:
        store[REDIS_KEY] = last_sync_value

    r = mock.MagicMock()
    r.get = lambda key: store.get(key)
    r.set = lambda key, val, **kwargs: store.__setitem__(key, val)
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

    stored = mock_redis.get(REDIS_KEY)
    assert stored is not None, "Should store last sync timestamp in Redis"


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
