"""
Tests for sync_firewall reconciliation job.

Verifies that sync_firewall queries the correct IPs and IPSets
and calls ipset_load for each.
"""
from testit import helpers as th
from unittest import mock

TEST_IP_1 = "198.51.100.50"
TEST_IP_2 = "198.51.100.51"
TEST_IP_TTL = "198.51.100.52"


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
    """sync_firewall should load permanently blocked IPs into mojo_blocked ipset."""
    from mojo.apps.incident.asyncjobs import sync_firewall, FIREWALL_BLOCKED_IPSET_NAME
    from objict import objict

    job = objict(logs=[])
    job.add_log = lambda msg: job.logs.append(msg)

    with mock.patch("mojo.apps.incident.firewall.ipset_load", return_value=(True, 2)) as mock_load:
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
    """sync_firewall should load all enabled IPSet records."""
    from mojo.apps.incident.asyncjobs import sync_firewall
    from objict import objict

    job = objict(logs=[])
    job.add_log = lambda msg: job.logs.append(msg)

    with mock.patch("mojo.apps.incident.firewall.ipset_load", return_value=(True, 2)) as mock_load:
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
def test_sync_logs_results(opts):
    """sync_firewall should log what it loaded."""
    from mojo.apps.incident.asyncjobs import sync_firewall
    from objict import objict

    job = objict(logs=[])
    job.add_log = lambda msg: job.logs.append(msg)

    with mock.patch("mojo.apps.incident.firewall.ipset_load", return_value=(True, 2)):
        sync_firewall(job)

    assert len(job.logs) >= 2, f"Expected at least 2 log entries, got {len(job.logs)}"
    assert "permanent blocks" in job.logs[0].lower() or "mojo_blocked" in job.logs[0], \
        f"First log should mention permanent blocks, got: {job.logs[0]}"


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
