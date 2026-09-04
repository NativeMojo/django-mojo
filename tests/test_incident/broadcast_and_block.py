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


TEST_USER = "incident_bb_user"
TEST_PWORD = "incident##mojo99"


class _CheckedRedis:
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
        if not argv or self.store.get(keys[0]) != argv[0]:
            return 0
        if "expire" in script:
            return 1
        self.store.pop(keys[0], None)
        return 1


def _checked_state(kind, identity, desired, fence=1):
    from mojo.apps.incident.services import firewall_truth
    redis = _CheckedRedis()
    redis.store[firewall_truth.fence_key(kind, identity)] = fence
    return redis, firewall_truth.state_fingerprint(desired)


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


@th.django_unit_test("checked handler returns only canonical semantic truth")
def test_checked_ip_reconcile_receipt(opts):
    from mojo.apps.incident.asyncjobs import broadcast_reconcile_firewall_ip

    broker = {"ok": True, "observed": {
        "ip": "192.0.2.8", "present": True,
        "input_count": 1, "forward_count": 0,
        "forwarding_required": False,
    }}
    desired = {"ip": "192.0.2.8", "present": True}
    redis, fingerprint = _checked_state("ip", "192.0.2.8", desired)
    with mock.patch("mojo.apps.incident.asyncjobs._raw_redis",
                    return_value=redis), \
            mock.patch("mojo.apps.incident.firewall.normalize_ip",
                       return_value=broker), \
            mock.patch(
                "mojo.apps.incident.services.firewall_truth.record_host_observation"):
        result = broadcast_reconcile_firewall_ip({
            **desired, "fence": 1, "fingerprint": fingerprint})
    assert result["ok"] is True, f"exact observation should verify: {result!r}"
    assert result["observed"] == {"ip": "192.0.2.8", "present": True}, \
        f"raw broker details escaped the checked projection: {result!r}"


@th.django_unit_test("IPv6 checked payload is refused before broker mutation")
def test_checked_ip_reconcile_refuses_ipv6(opts):
    from mojo.apps.incident.asyncjobs import broadcast_reconcile_firewall_ip

    with mock.patch("mojo.apps.incident.firewall.normalize_ip") as normalize:
        result = broadcast_reconcile_firewall_ip(
            {"ip": "2001:db8::8", "present": True, "fence": 1,
             "fingerprint": "0" * 64})
    assert result["ok"] is False, f"IPv6 unexpectedly verified: {result!r}"
    normalize.assert_not_called()


@th.django_unit_test("compound reconciliation uses one root-broker transaction")
def test_checked_geolocated_reconcile_is_compound(opts):
    from mojo.apps.incident.asyncjobs import broadcast_reconcile_geolocated_ip

    broker = {"ok": True, "observed": {
        "ip": {"ip": "192.0.2.8", "present": False},
        "permanent": {
            "name": "mojo_blocked", "present": True,
            "count": 1,
            "digest": "939b8c79f98fcdf61a10cd17f2f6c057955dbd30485e6c06b5be2a61e3adc691",
        },
    }}
    # Use the production digest rather than making the fixture depend on a
    # hand-maintained literal.
    from mojo.apps.incident.services.firewall_truth import network_digest
    broker["observed"]["permanent"]["digest"] = network_digest(
        ["192.0.2.8"])
    desired = {
        "ip": {"ip": "192.0.2.8", "present": False},
        "permanent": broker["observed"]["permanent"],
    }
    from mojo.apps.incident.services import firewall_truth
    redis = _CheckedRedis()
    redis.store[firewall_truth.fence_key("ip", "192.0.2.8")] = 2
    redis.store[firewall_truth.fence_key("permanent", "mojo_blocked")] = 3
    snapshot = {
        "desired": desired, "fingerprint": "c" * 64,
        "permanent": {"fingerprint": "d" * 64},
    }
    with mock.patch(
            "mojo.apps.incident.asyncjobs._raw_redis", return_value=redis), \
            mock.patch(
                "mojo.apps.incident.services.firewall_truth.geolocated_snapshot",
                return_value=snapshot), \
            mock.patch(
                "mojo.apps.incident.services.firewall_truth.record_host_observation"), \
            mock.patch(
            "mojo.apps.incident.firewall.normalize_geolocated_ip",
            return_value=broker) as normalize:
        result = broadcast_reconcile_geolocated_ip({
            "ip": "192.0.2.8", "permanent_set_name": "mojo_blocked",
            "permanent_ips": ["192.0.2.8"],
            "temporary_present": False,
            "ip_fence": 2, "aggregate_fence": 3,
            "fingerprint": "c" * 64,
            "aggregate_fingerprint": "d" * 64,
        })
    assert result["ok"] is True, result
    normalize.assert_called_once_with(
        "192.0.2.8", "mojo_blocked", ["192.0.2.8/32"], False)


@th.django_unit_test("empty checked host evidence cannot verify firewall truth")
def test_firewall_truth_requires_exact_nonempty_evidence(opts):
    from mojo.apps.incident.services import firewall_truth

    malformed = {
        "status": "verified", "expected_hosts": [], "responded_hosts": [],
        "succeeded_hosts": [], "failed_hosts": [], "missing_hosts": [],
        "anomalies": [], "results": [],
    }
    redis = _CheckedRedis()
    with mock.patch(
            "mojo.apps.incident.services.firewall_truth._redis_client",
            return_value=redis), \
            mock.patch(
            "mojo.apps.jobs.broadcast_execute_checked",
            return_value=malformed):
        result = firewall_truth.reconcile_ip("192.0.2.8", True)
    assert result["status"] == "partial" and result["ok"] is False, result
    assert result["error"]["code"] == "checked_evidence_invalid", result


@th.django_unit_test("delayed checked command refuses a stale fence before mutation")
def test_delayed_checked_command_is_fenced(opts):
    from mojo.apps.incident.asyncjobs import broadcast_reconcile_firewall_set
    from mojo.apps.incident.services import firewall_truth

    desired = {
        "name": "stale_checked", "present": True, "count": 1,
        "digest": firewall_truth.network_digest(["192.0.2.0/24"]),
    }
    redis, unused = _checked_state(
        "set", "stale_checked", desired, fence=2)
    with mock.patch("mojo.apps.incident.asyncjobs._raw_redis",
                    return_value=redis), \
            mock.patch("mojo.apps.incident.firewall.normalize_ipset") as mutate, \
            mock.patch(
                "mojo.apps.incident.services.firewall_truth.mark_superseded_pending") \
                    as pending:
        result = broadcast_reconcile_firewall_set({
            "name": "stale_checked", "cidrs": ["192.0.2.0/24"],
            "present": True, "fence": 1, "fingerprint": "e" * 64,
        })
    assert result["ok"] is False and result["error"] == "generation_superseded"
    mutate.assert_not_called()
    pending.assert_called_once_with("set", "stale_checked")


@th.django_unit_test("checked broker I/O never owns the global desired lease")
def test_checked_broker_io_is_between_short_lease_phases(opts):
    from mojo.apps.incident.asyncjobs import broadcast_reconcile_firewall_ip
    from mojo.apps.incident.services import firewall_truth

    desired = {"ip": "192.0.2.18", "present": True}
    redis, fingerprint = _checked_state("ip", "192.0.2.18", desired)

    def normalize(ip, present):
        assert redis.get(firewall_truth.DESIRED_STATE_LOCK) is None, \
            "global desired-state lease remained held across root broker I/O"
        return {"ok": True, "observed": {
            "ip": ip, "present": present, "input_count": 1,
            "forward_count": 0, "forwarding_required": False,
        }}

    with mock.patch("mojo.apps.incident.asyncjobs._raw_redis",
                    return_value=redis), \
            mock.patch("mojo.apps.incident.firewall.normalize_ip",
                       side_effect=normalize), \
            mock.patch.object(firewall_truth, "record_host_observation"):
        result = broadcast_reconcile_firewall_ip({
            **desired, "fence": 1, "fingerprint": fingerprint})
    assert result["ok"] is True, result


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
    with mock.patch(
            "mojo.apps.incident.services.firewall_truth.reconcile_geolocated_ip",
            return_value={"status": "verified", "ok": True}):
        result = geo.block(reason="test:first", ttl=600)
    assert result is True, "First block should succeed"
    geo.refresh_from_db()
    assert geo.block_count == 1, f"block_count should be 1 after first block, got {geo.block_count}"
    assert geo.is_blocked is True, "IP should be blocked"

    # Second block should be idempotent — no side effects
    with mock.patch(
            "mojo.apps.incident.services.firewall_truth.reconcile_geolocated_ip",
            return_value={"status": "verified", "ok": True}) as mock_broadcast:
        result = geo.block(reason="test:second", ttl=600)
    assert result is True, "Idempotent block should return True (already blocked)"
    geo.refresh_from_db()
    assert geo.block_count == 1, f"block_count should still be 1 after idempotent block, got {geo.block_count}"
    assert geo.blocked_reason == "test:first", \
        f"Reason should remain from first block, got {geo.blocked_reason}"
    mock_broadcast.assert_called_once(), \
        "an already-desired block must still re-observe fleet truth"


@th.django_unit_test()
def test_block_reblocks_after_expiry(opts):
    """geo.block() should re-block if the previous block has expired."""
    from mojo.apps.account.models import GeoLocatedIP
    from mojo.helpers import dates
    from datetime import timedelta

    GeoLocatedIP.objects.filter(ip_address="10.99.99.2").delete()
    geo = GeoLocatedIP.objects.create(ip_address="10.99.99.2")

    # First block with short TTL
    with mock.patch(
            "mojo.apps.incident.services.firewall_truth.reconcile_geolocated_ip",
            return_value={"status": "verified", "ok": True}):
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
    with mock.patch(
            "mojo.apps.incident.services.firewall_truth.reconcile_geolocated_ip",
            return_value={"status": "verified", "ok": True}):
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

    with mock.patch(
            "mojo.apps.incident.services.firewall_truth.reconcile_geolocated_ip",
            return_value={"status": "verified", "ok": True}):
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

    with mock.patch(
            "mojo.apps.incident.services.firewall_truth.reconcile_geolocated_ip",
            return_value={"status": "verified", "ok": True}):
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


@th.django_unit_test("partial checked block cannot resolve an incident")
def test_block_handler_partial_is_not_success(opts):
    from mojo.apps.incident.models import Event, Incident
    from mojo.apps.incident.handlers.event_handlers import BlockHandler

    incident = Incident.objects.create(
        title="Partial firewall", category="test:block_partial",
        priority=10, status="open")
    event = Event.objects.create(
        title="Partial firewall event", category="test:block_partial",
        level=10, source_ip="198.51.100.203", incident=incident)
    with mock.patch(
            "mojo.apps.account.models.GeoLocatedIP.block_checked",
            return_value={"status": "partial", "ok": False,
                          "error": {"code": "missing_host"}}):
        result = BlockHandler(ttl="600").run(event)
    incident.refresh_from_db()
    assert result is False, "partial fleet evidence became handler success"
    assert incident.status == "open", "partial fleet evidence resolved incident"


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

    with mock.patch(
            "mojo.apps.incident.services.firewall_truth.reconcile_geolocated_ip",
            return_value={"status": "verified", "ok": True}):
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
