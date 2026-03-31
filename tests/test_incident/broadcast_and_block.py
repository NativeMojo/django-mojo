"""
Tests for broadcast function fixes and block idempotency.

Covers:
1. Broadcast functions accept plain dict (not Job)
2. geo.block() idempotency — skip re-block if already blocked
3. BlockHandler includes incident/event in reason
4. BlockHandler resolves incident after successful block
"""
from testit import helpers as th
from unittest import mock


TEST_USER = "testit"
TEST_PWORD = "testit##mojo"


# =============================================================================
# Broadcast functions accept dict
# =============================================================================

@th.django_unit_test()
def test_broadcast_block_ip_accepts_dict(opts):
    """broadcast_block_ip receives a plain dict from pub/sub, not a Job."""
    from mojo.apps.incident.asyncjobs import broadcast_block_ip

    data = {"ips": ["192.168.99.1"], "ttl": 600}

    # Mock firewall.block since we don't have iptables in test
    with mock.patch("mojo.apps.incident.firewall.block", return_value=True) as mock_block:
        broadcast_block_ip(data)
        mock_block.assert_called_once_with("192.168.99.1"), \
            "broadcast_block_ip should call firewall.block with the IP"


@th.django_unit_test()
def test_broadcast_unblock_ip_accepts_dict(opts):
    """broadcast_unblock_ip receives a plain dict from pub/sub, not a Job."""
    from mojo.apps.incident.asyncjobs import broadcast_unblock_ip

    data = {"ips": ["192.168.99.2"]}

    with mock.patch("mojo.apps.incident.firewall.unblock", return_value=True) as mock_unblock:
        broadcast_unblock_ip(data)
        mock_unblock.assert_called_once_with("192.168.99.2"), \
            "broadcast_unblock_ip should call firewall.unblock with the IP"


@th.django_unit_test()
def test_broadcast_sync_ipset_accepts_dict(opts):
    """broadcast_sync_ipset receives a plain dict from pub/sub, not a Job."""
    from mojo.apps.incident.asyncjobs import broadcast_sync_ipset

    data = {"name": "test_set", "cidrs": ["10.0.0.0/8"]}

    with mock.patch("mojo.apps.incident.firewall.ipset_load", return_value=(True, 1)) as mock_load:
        broadcast_sync_ipset(data)
        mock_load.assert_called_once_with("test_set", ["10.0.0.0/8"]), \
            "broadcast_sync_ipset should call firewall.ipset_load"


@th.django_unit_test()
def test_broadcast_remove_ipset_accepts_dict(opts):
    """broadcast_remove_ipset receives a plain dict from pub/sub, not a Job."""
    from mojo.apps.incident.asyncjobs import broadcast_remove_ipset

    data = {"name": "test_set"}

    with mock.patch("mojo.apps.incident.firewall.ipset_remove") as mock_remove:
        broadcast_remove_ipset(data)
        mock_remove.assert_called_once_with("test_set"), \
            "broadcast_remove_ipset should call firewall.ipset_remove"


@th.django_unit_test()
def test_broadcast_block_ip_no_ips(opts):
    """broadcast_block_ip should handle empty IP list gracefully."""
    from mojo.apps.incident.asyncjobs import broadcast_block_ip

    # Should not raise
    with mock.patch("mojo.apps.incident.firewall.block") as mock_block:
        broadcast_block_ip({})
        assert not mock_block.called, "firewall.block should not be called with no IPs"
        broadcast_block_ip({"ips": []})
        assert not mock_block.called, "firewall.block should not be called with empty IP list"


# =============================================================================
# Block idempotency
# =============================================================================

@th.django_unit_test()
def test_block_idempotency_skips_reblock(opts):
    """geo.block() should skip re-blocking if IP is already actively blocked."""
    from mojo.apps.account.models import GeoLocatedIP

    # Clean up
    GeoLocatedIP.objects.filter(ip_address="10.99.99.1").delete()

    geo = GeoLocatedIP.objects.create(ip_address="10.99.99.1")

    # First block should succeed and set block_count=1
    with mock.patch("mojo.apps.jobs.broadcast_execute"):
        result = geo.block(reason="test:first", ttl=600)
    assert result is True, "First block should succeed"
    geo.refresh_from_db()
    assert geo.block_count == 1, f"block_count should be 1 after first block, got {geo.block_count}"
    assert geo.is_blocked is True, "IP should be blocked"

    # Second block should be idempotent — no side effects
    with mock.patch("mojo.apps.jobs.broadcast_execute") as mock_broadcast:
        result = geo.block(reason="test:second", ttl=600)
    assert result is True, "Idempotent block should return True (already blocked)"
    geo.refresh_from_db()
    assert geo.block_count == 1, f"block_count should still be 1 after idempotent block, got {geo.block_count}"
    assert geo.blocked_reason == "test:first", \
        f"Reason should remain from first block, got {geo.blocked_reason}"
    assert not mock_broadcast.called, "Should not re-broadcast for already-blocked IP"


@th.django_unit_test()
def test_block_reblocks_after_expiry(opts):
    """geo.block() should re-block if the previous block has expired."""
    from mojo.apps.account.models import GeoLocatedIP
    from mojo.helpers import dates
    from datetime import timedelta

    GeoLocatedIP.objects.filter(ip_address="10.99.99.2").delete()
    geo = GeoLocatedIP.objects.create(ip_address="10.99.99.2")

    # First block with short TTL
    with mock.patch("mojo.apps.jobs.broadcast_execute"):
        geo.block(reason="test:first", ttl=60)
    geo.refresh_from_db()
    assert geo.block_count == 1, f"block_count should be 1, got {geo.block_count}"

    # Simulate expiry by backdating blocked_until
    GeoLocatedIP.objects.filter(pk=geo.pk).update(
        blocked_until=dates.utcnow() - timedelta(seconds=10)
    )
    geo.refresh_from_db()

    # block_active should be False now
    assert not geo.block_active, "block_active should be False after expiry"

    # Re-block should go through
    with mock.patch("mojo.apps.jobs.broadcast_execute"):
        result = geo.block(reason="test:reblock", ttl=600)
    assert result is True, "Re-block after expiry should succeed"
    geo.refresh_from_db()
    assert geo.block_count == 2, f"block_count should be 2 after re-block, got {geo.block_count}"
    assert geo.blocked_reason == "test:reblock", \
        f"Reason should be updated after re-block, got {geo.blocked_reason}"


@th.django_unit_test()
def test_block_whitelisted_ip_returns_false(opts):
    """geo.block() should refuse to block whitelisted IPs."""
    from mojo.apps.account.models import GeoLocatedIP

    GeoLocatedIP.objects.filter(ip_address="10.99.99.3").delete()
    geo = GeoLocatedIP.objects.create(ip_address="10.99.99.3", is_whitelisted=True)

    result = geo.block(reason="test:whitelist")
    assert result is False, "Blocking a whitelisted IP should return False"
    geo.refresh_from_db()
    assert geo.is_blocked is False, "Whitelisted IP should not be blocked"


# =============================================================================
# BlockHandler includes incident/event in reason + resolves incident
# =============================================================================

@th.django_unit_test()
def test_block_handler_includes_incident_event_in_reason(opts):
    """BlockHandler should include incident and event IDs in the block reason."""
    from mojo.apps.incident.models import Event, Incident
    from mojo.apps.incident.handlers.event_handlers import BlockHandler
    from mojo.apps.account.models import GeoLocatedIP

    # Clean up
    GeoLocatedIP.objects.filter(ip_address="10.99.99.10").delete()
    Event.objects.filter(category="test:block_reason").delete()
    Incident.objects.filter(category="test:block_reason").delete()

    # Create incident and event
    incident = Incident.objects.create(
        title="Test block reason",
        category="test:block_reason",
        priority=10,
        status="open",
    )
    event = Event.objects.create(
        title="Test event",
        category="test:block_reason",
        level=10,
        source_ip="10.99.99.10",
        incident=incident,
    )

    handler = BlockHandler(target=None, ttl="300")

    with mock.patch("mojo.apps.jobs.broadcast_execute"):
        result = handler.run(event)

    assert result is True, "BlockHandler should succeed"

    geo = GeoLocatedIP.objects.filter(ip_address="10.99.99.10").first()
    assert geo is not None, "GeoLocatedIP should exist after block"
    assert geo.is_blocked is True, "IP should be blocked"
    assert f"incident:{incident.pk}" in geo.blocked_reason, \
        f"Block reason should include incident ID, got: {geo.blocked_reason}"
    assert f"event:{event.pk}" in geo.blocked_reason, \
        f"Block reason should include event ID, got: {geo.blocked_reason}"


@th.django_unit_test()
def test_block_handler_resolves_incident(opts):
    """BlockHandler should resolve the incident after a successful block."""
    from mojo.apps.incident.models import Event, Incident, IncidentHistory
    from mojo.apps.incident.handlers.event_handlers import BlockHandler
    from mojo.apps.account.models import GeoLocatedIP

    # Clean up
    GeoLocatedIP.objects.filter(ip_address="10.99.99.11").delete()
    Event.objects.filter(category="test:block_resolve").delete()
    Incident.objects.filter(category="test:block_resolve").delete()

    incident = Incident.objects.create(
        title="Test block resolve",
        category="test:block_resolve",
        priority=10,
        status="open",
    )
    event = Event.objects.create(
        title="Test event",
        category="test:block_resolve",
        level=10,
        source_ip="10.99.99.11",
        incident=incident,
    )

    handler = BlockHandler(target=None, ttl="600")

    with mock.patch("mojo.apps.jobs.broadcast_execute"):
        result = handler.run(event)

    assert result is True, "BlockHandler should succeed"

    incident.refresh_from_db()
    assert incident.status == "resolved", \
        f"Incident should be resolved after block, got: {incident.status}"

    # Check history entries
    histories = list(IncidentHistory.objects.filter(parent=incident).order_by("created"))
    handler_history = [h for h in histories if h.kind == "handler:block"]
    assert len(handler_history) >= 1, \
        f"Should have at least 1 handler:block history entry, got {len(handler_history)}"
    status_history = [h for h in histories if h.kind == "status_changed" and "Auto-resolved" in (h.note or "")]
    assert len(status_history) >= 1, \
        f"Should have auto-resolved status_changed history entry, got {len(status_history)}"


@th.django_unit_test()
def test_block_handler_skips_resolve_if_already_resolved(opts):
    """BlockHandler should not re-resolve an already resolved incident."""
    from mojo.apps.incident.models import Event, Incident, IncidentHistory
    from mojo.apps.incident.handlers.event_handlers import BlockHandler
    from mojo.apps.account.models import GeoLocatedIP

    # Clean up
    GeoLocatedIP.objects.filter(ip_address="10.99.99.12").delete()
    Event.objects.filter(category="test:block_skip_resolve").delete()
    Incident.objects.filter(category="test:block_skip_resolve").delete()

    incident = Incident.objects.create(
        title="Test already resolved",
        category="test:block_skip_resolve",
        priority=10,
        status="resolved",
    )
    event = Event.objects.create(
        title="Test event",
        category="test:block_skip_resolve",
        level=10,
        source_ip="10.99.99.12",
        incident=incident,
    )

    handler = BlockHandler(target=None, ttl="600")

    with mock.patch("mojo.apps.jobs.broadcast_execute"):
        result = handler.run(event)

    assert result is True, "BlockHandler should succeed"

    # Should NOT have a status_changed history for auto-resolve
    status_history = IncidentHistory.objects.filter(
        parent=incident,
        kind="status_changed",
        note__contains="Auto-resolved",
    )
    assert status_history.count() == 0, \
        "Should not auto-resolve an already resolved incident"
