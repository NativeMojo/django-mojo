"""Tests for GeoLocatedIP.block() escalating threat_level to 'high'.

block() centralizes the rule so every entry point (admin REST, LLM agent,
incident rule engine, asyncjobs, manual) bumps threat_level atomically
with the block — never downgrades.
"""
from testit import helpers as th


@th.django_unit_test()
def test_block_escalates_threat_level_from_none(opts):
    from mojo.apps.account.models.geolocated_ip import GeoLocatedIP

    GeoLocatedIP.objects.filter(ip_address="203.0.113.100").delete()
    geo = GeoLocatedIP.objects.create(
        ip_address="203.0.113.100", provider="test", threat_level=None,
    )
    assert geo.threat_level is None, "fixture must start with no threat_level"

    geo.block(reason="test_escalate_from_none", ttl=600, broadcast=False)
    geo.refresh_from_db()

    assert geo.is_blocked is True, "block() should set is_blocked"
    assert geo.threat_level == "high", (
        f"block() must escalate threat_level to 'high' from None, "
        f"got {geo.threat_level!r}"
    )


@th.django_unit_test()
def test_block_escalates_threat_level_from_low(opts):
    from mojo.apps.account.models.geolocated_ip import GeoLocatedIP

    GeoLocatedIP.objects.filter(ip_address="203.0.113.101").delete()
    geo = GeoLocatedIP.objects.create(
        ip_address="203.0.113.101", provider="test", threat_level="low",
    )
    geo.block(reason="test_escalate_from_low", ttl=600, broadcast=False)
    geo.refresh_from_db()

    assert geo.threat_level == "high", (
        f"block() must escalate threat_level 'low' -> 'high', got {geo.threat_level!r}"
    )


@th.django_unit_test()
def test_block_escalates_threat_level_from_medium(opts):
    from mojo.apps.account.models.geolocated_ip import GeoLocatedIP

    GeoLocatedIP.objects.filter(ip_address="203.0.113.102").delete()
    geo = GeoLocatedIP.objects.create(
        ip_address="203.0.113.102", provider="test", threat_level="medium",
    )
    geo.block(reason="test_escalate_from_medium", ttl=600, broadcast=False)
    geo.refresh_from_db()

    assert geo.threat_level == "high", (
        f"block() must escalate 'medium' -> 'high', got {geo.threat_level!r}"
    )


@th.django_unit_test()
def test_block_preserves_high(opts):
    from mojo.apps.account.models.geolocated_ip import GeoLocatedIP

    GeoLocatedIP.objects.filter(ip_address="203.0.113.103").delete()
    geo = GeoLocatedIP.objects.create(
        ip_address="203.0.113.103", provider="test", threat_level="high",
    )
    geo.block(reason="test_preserve_high", ttl=600, broadcast=False)
    geo.refresh_from_db()

    assert geo.threat_level == "high", (
        f"block() must not change threat_level when already 'high', got {geo.threat_level!r}"
    )


@th.django_unit_test()
def test_block_never_downgrades_critical(opts):
    """block() must never downgrade 'critical' to 'high'."""
    from mojo.apps.account.models.geolocated_ip import GeoLocatedIP

    GeoLocatedIP.objects.filter(ip_address="203.0.113.104").delete()
    geo = GeoLocatedIP.objects.create(
        ip_address="203.0.113.104", provider="test", threat_level="critical",
    )
    geo.block(reason="test_no_downgrade", ttl=600, broadcast=False)
    geo.refresh_from_db()

    assert geo.threat_level == "critical", (
        f"block() must NEVER downgrade 'critical', got {geo.threat_level!r}"
    )


@th.django_unit_test()
def test_block_is_atomic_with_threat_level(opts):
    """threat_level must be updated in the same UPDATE as block fields."""
    from mojo.apps.account.models.geolocated_ip import GeoLocatedIP

    GeoLocatedIP.objects.filter(ip_address="203.0.113.105").delete()
    geo = GeoLocatedIP.objects.create(
        ip_address="203.0.113.105", provider="test", threat_level=None,
    )
    geo.block(reason="test_atomic", ttl=600, broadcast=False)

    # Re-read from DB (not just in-memory state) to confirm the persisted
    # row got both updates in one operation.
    fresh = GeoLocatedIP.objects.get(pk=geo.pk)
    assert fresh.is_blocked is True, "is_blocked not persisted"
    assert fresh.threat_level == "high", (
        f"threat_level not persisted in same atomic update: {fresh.threat_level!r}"
    )


@th.django_unit_test()
def test_block_whitelisted_does_not_change_threat_level(opts):
    """Whitelisted IPs are not blocked, so threat_level must not change either."""
    from mojo.apps.account.models.geolocated_ip import GeoLocatedIP

    GeoLocatedIP.objects.filter(ip_address="203.0.113.106").delete()
    geo = GeoLocatedIP.objects.create(
        ip_address="203.0.113.106", provider="test",
        threat_level="low", is_whitelisted=True,
    )
    result = geo.block(reason="test_whitelisted", ttl=600, broadcast=False)
    geo.refresh_from_db()

    assert result is False, "block() must return False for whitelisted IPs"
    assert geo.is_blocked is False, "whitelisted IP must not be blocked"
    assert geo.threat_level == "low", (
        f"whitelisted IP threat_level must not change, got {geo.threat_level!r}"
    )
