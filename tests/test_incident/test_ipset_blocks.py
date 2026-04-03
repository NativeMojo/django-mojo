"""
Tests for ipset-based permanent block routing.

Verifies that block(ttl=None) routes through ipset broadcast and
block(ttl>0) routes through individual iptables broadcast.
Also tests unblock routing based on permanent vs TTL status.
"""
from testit import helpers as th
from unittest import mock

TEST_IP = "198.51.100.99"


@th.django_unit_setup()
def setup_ipset_blocks(opts):
    from mojo.apps.account.models import GeoLocatedIP

    # Clean up from previous runs
    GeoLocatedIP.objects.filter(ip_address=TEST_IP).delete()

    opts.geo = GeoLocatedIP.objects.create(
        ip_address=TEST_IP,
        country_code="US",
    )


@th.django_unit_test()
def test_permanent_block_broadcasts_ipset_add(opts):
    """block(ttl=None) should broadcast ipset_add, not iptables."""
    from mojo.apps.account.models import GeoLocatedIP

    geo = GeoLocatedIP.objects.get(ip_address=TEST_IP)
    # Reset state
    geo.is_blocked = False
    geo.blocked_until = None
    geo.save(update_fields=["is_blocked", "blocked_until"])

    with mock.patch("mojo.apps.account.models.geolocated_ip.jobs") as mock_jobs:
        geo.block(reason="test_permanent", ttl=None)

    calls = mock_jobs.broadcast_execute.call_args_list
    assert len(calls) >= 1, f"Expected at least 1 broadcast call, got {len(calls)}"

    func_path = calls[0][0][0]
    payload = calls[0][0][1]
    assert "broadcast_ipset_add_blocked" in func_path, \
        f"Permanent block should broadcast ipset_add, got: {func_path}"
    assert payload.get("ip") == TEST_IP, \
        f"Payload should contain ip={TEST_IP}, got: {payload}"


@th.django_unit_test()
def test_ttl_block_broadcasts_iptables(opts):
    """block(ttl=600) should broadcast iptables block, not ipset."""
    from mojo.apps.account.models import GeoLocatedIP

    geo = GeoLocatedIP.objects.get(ip_address=TEST_IP)
    geo.is_blocked = False
    geo.blocked_until = None
    geo.save(update_fields=["is_blocked", "blocked_until"])

    with mock.patch("mojo.apps.account.models.geolocated_ip.jobs") as mock_jobs:
        geo.block(reason="test_ttl", ttl=600)

    calls = mock_jobs.broadcast_execute.call_args_list
    assert len(calls) >= 1, f"Expected at least 1 broadcast call, got {len(calls)}"

    func_path = calls[0][0][0]
    payload = calls[0][0][1]
    assert "broadcast_block_ip" in func_path, \
        f"TTL block should broadcast iptables block, got: {func_path}"
    assert payload.get("ips") == [TEST_IP], \
        f"Payload should contain ips=[{TEST_IP}], got: {payload}"
    assert payload.get("ttl") == 600, \
        f"Payload should contain ttl=600, got: {payload}"


@th.django_unit_test()
def test_unblock_permanent_broadcasts_ipset_del(opts):
    """unblock() of a permanent block should broadcast ipset_del."""
    from mojo.apps.account.models import GeoLocatedIP

    geo = GeoLocatedIP.objects.get(ip_address=TEST_IP)
    # Set up as permanently blocked
    geo.is_blocked = True
    geo.blocked_until = None
    geo.blocked_reason = "test"
    geo.save(update_fields=["is_blocked", "blocked_until", "blocked_reason"])

    with mock.patch("mojo.apps.account.models.geolocated_ip.jobs") as mock_jobs:
        geo.unblock(reason="test_unblock")

    calls = mock_jobs.broadcast_execute.call_args_list
    assert len(calls) >= 1, f"Expected at least 1 broadcast call, got {len(calls)}"

    func_path = calls[0][0][0]
    payload = calls[0][0][1]
    assert "broadcast_ipset_del_blocked" in func_path, \
        f"Unblock of permanent should broadcast ipset_del, got: {func_path}"
    assert payload.get("ip") == TEST_IP, \
        f"Payload should contain ip={TEST_IP}, got: {payload}"


@th.django_unit_test()
def test_unblock_ttl_broadcasts_iptables(opts):
    """unblock() of a TTL block should broadcast iptables unblock."""
    from mojo.apps.account.models import GeoLocatedIP
    from django.utils import timezone
    from datetime import timedelta

    geo = GeoLocatedIP.objects.get(ip_address=TEST_IP)
    # Set up as TTL blocked (has blocked_until)
    geo.is_blocked = True
    geo.blocked_until = timezone.now() + timedelta(hours=1)
    geo.blocked_reason = "test"
    geo.save(update_fields=["is_blocked", "blocked_until", "blocked_reason"])

    with mock.patch("mojo.apps.account.models.geolocated_ip.jobs") as mock_jobs:
        geo.unblock(reason="test_unblock_ttl")

    calls = mock_jobs.broadcast_execute.call_args_list
    assert len(calls) >= 1, f"Expected at least 1 broadcast call, got {len(calls)}"

    func_path = calls[0][0][0]
    assert "broadcast_unblock_ip" in func_path, \
        f"Unblock of TTL block should broadcast iptables unblock, got: {func_path}"


@th.django_unit_test()
def test_block_no_broadcast_when_disabled(opts):
    """block(broadcast=False) should not broadcast anything."""
    from mojo.apps.account.models import GeoLocatedIP

    geo = GeoLocatedIP.objects.get(ip_address=TEST_IP)
    geo.is_blocked = False
    geo.blocked_until = None
    geo.save(update_fields=["is_blocked", "blocked_until"])

    with mock.patch("mojo.apps.account.models.geolocated_ip.jobs") as mock_jobs:
        geo.block(reason="test_no_broadcast", ttl=None, broadcast=False)

    mock_jobs.broadcast_execute.assert_not_called()
