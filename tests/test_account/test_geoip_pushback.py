"""Tests for the abuse-signal push-back hook on GeoLocatedIP.

Verifies that _maybe_push_abuse_signals enqueues a jobs.publish call when:
  - provider == 'mojo'
  - GEOIP_MOJO_PROVIDER_URL is set
  - GEOIP_MOJO_SYNC_ENABLED is True
  - A signal actually rose (strict-rise / False->True flip)
  - from_sync is False
And that the call is mandatory async (never inline HTTP).
"""
from unittest import mock
from testit import helpers as th


def _push_path():
    return "mojo.apps.account.models.geolocated_ip.jobs.publish"


def _enable_sync():
    """Context-manager bundle that enables the federation config."""
    return [
        mock.patch("mojo.helpers.geoip.config.MOJO_PROVIDER_URL", "https://hub.example.com"),
        mock.patch("mojo.helpers.geoip.config.MOJO_SYNC_ENABLED", True),
    ]


@th.django_unit_test()
def test_block_on_mojo_provider_enqueues_pushback(opts):
    from mojo.apps.account.models.geolocated_ip import GeoLocatedIP

    GeoLocatedIP.objects.filter(ip_address="203.0.113.200").delete()
    geo = GeoLocatedIP.objects.create(
        ip_address="203.0.113.200", provider="mojo", threat_level=None,
    )

    patches = _enable_sync() + [mock.patch(_push_path())]
    with patches[0], patches[1], patches[2] as m_publish:
        geo.block(reason="test_pushback_mojo", ttl=600, broadcast=False)

    assert m_publish.called, (
        "block() on a mojo-provider record must enqueue push-back via jobs.publish"
    )
    args, kwargs = m_publish.call_args
    assert args[0] == "mojo.apps.account.asyncjobs.push_abuse_signals", (
        f"wrong job path: {args[0]!r}"
    )
    payload = args[1]
    assert payload["ip"] == "203.0.113.200", f"ip missing/wrong: {payload!r}"
    assert payload.get("threat_level") == "high", (
        f"expected threat_level=high in payload, got {payload!r}"
    )
    assert "idempotency_key" in kwargs and kwargs["idempotency_key"].startswith(
        "geoip_sync:203.0.113.200:"
    ), f"idempotency key missing or wrong: {kwargs.get('idempotency_key')!r}"


@th.django_unit_test()
def test_block_on_non_mojo_provider_does_not_enqueue(opts):
    from mojo.apps.account.models.geolocated_ip import GeoLocatedIP

    GeoLocatedIP.objects.filter(ip_address="203.0.113.201").delete()
    geo = GeoLocatedIP.objects.create(
        ip_address="203.0.113.201", provider="maxmind", threat_level=None,
    )

    patches = _enable_sync() + [mock.patch(_push_path())]
    with patches[0], patches[1], patches[2] as m_publish:
        geo.block(reason="test_no_pushback_maxmind", ttl=600, broadcast=False)

    assert not m_publish.called, (
        "block() on a non-mojo-provider record must NOT enqueue push-back; "
        f"got call_args={m_publish.call_args!r}"
    )


@th.django_unit_test()
def test_block_when_from_sync_does_not_enqueue(opts):
    from mojo.apps.account.models.geolocated_ip import GeoLocatedIP

    GeoLocatedIP.objects.filter(ip_address="203.0.113.202").delete()
    geo = GeoLocatedIP.objects.create(
        ip_address="203.0.113.202", provider="mojo", threat_level=None,
    )

    patches = _enable_sync() + [mock.patch(_push_path())]
    with patches[0], patches[1], patches[2] as m_publish:
        geo.block(reason="test_from_sync", ttl=600, broadcast=False, from_sync=True)

    assert not m_publish.called, (
        "from_sync=True must suppress push-back; "
        f"got call_args={m_publish.call_args!r}"
    )


@th.django_unit_test()
def test_block_when_sync_disabled_does_not_enqueue(opts):
    from mojo.apps.account.models.geolocated_ip import GeoLocatedIP

    GeoLocatedIP.objects.filter(ip_address="203.0.113.203").delete()
    geo = GeoLocatedIP.objects.create(
        ip_address="203.0.113.203", provider="mojo", threat_level=None,
    )

    with mock.patch("mojo.helpers.geoip.config.MOJO_PROVIDER_URL", "https://hub.example.com"), \
         mock.patch("mojo.helpers.geoip.config.MOJO_SYNC_ENABLED", False), \
         mock.patch(_push_path()) as m_publish:
        geo.block(reason="test_sync_disabled", ttl=600, broadcast=False)

    assert not m_publish.called, (
        "GEOIP_MOJO_SYNC_ENABLED=False must suppress push-back"
    )


@th.django_unit_test()
def test_block_when_url_unset_does_not_enqueue(opts):
    from mojo.apps.account.models.geolocated_ip import GeoLocatedIP

    GeoLocatedIP.objects.filter(ip_address="203.0.113.204").delete()
    geo = GeoLocatedIP.objects.create(
        ip_address="203.0.113.204", provider="mojo", threat_level=None,
    )

    with mock.patch("mojo.helpers.geoip.config.MOJO_PROVIDER_URL", None), \
         mock.patch("mojo.helpers.geoip.config.MOJO_SYNC_ENABLED", True), \
         mock.patch(_push_path()) as m_publish:
        geo.block(reason="test_no_url", ttl=600, broadcast=False)

    assert not m_publish.called, (
        "GEOIP_MOJO_PROVIDER_URL unset must suppress push-back"
    )


@th.django_unit_test()
def test_block_no_signal_rise_no_enqueue(opts):
    """When threat_level is already 'critical', block() doesn't push."""
    from mojo.apps.account.models.geolocated_ip import GeoLocatedIP

    GeoLocatedIP.objects.filter(ip_address="203.0.113.205").delete()
    geo = GeoLocatedIP.objects.create(
        ip_address="203.0.113.205", provider="mojo", threat_level="critical",
    )

    patches = _enable_sync() + [mock.patch(_push_path())]
    with patches[0], patches[1], patches[2] as m_publish:
        geo.block(reason="test_no_rise", ttl=600, broadcast=False)

    assert not m_publish.called, (
        "block() must not enqueue when no signal rises; "
        f"call_args={m_publish.call_args!r}"
    )


@th.django_unit_test()
def test_check_threats_flip_attacker_enqueues(opts):
    """check_threats() flipping is_known_attacker False->True triggers push-back."""
    from mojo.apps.account.models.geolocated_ip import GeoLocatedIP
    from mojo.helpers.geoip import threat_intel

    GeoLocatedIP.objects.filter(ip_address="203.0.113.210").delete()
    geo = GeoLocatedIP.objects.create(
        ip_address="203.0.113.210", provider="mojo", threat_level="low",
        is_known_attacker=False, is_known_abuser=False,
    )

    fake_threat = {
        "is_known_attacker": True,
        "is_known_abuser": False,
        "is_blocklisted": False,
        "threat_data": {"internal": {}, "blocklists": []},
    }

    patches = _enable_sync()
    with patches[0], patches[1], \
         mock.patch.object(threat_intel, "perform_threat_check",
                           return_value=fake_threat), \
         mock.patch.object(threat_intel, "recalculate_threat_level",
                           return_value="medium"), \
         mock.patch(_push_path()) as m_publish:
        geo.check_threats()

    assert m_publish.called, "check_threats() must enqueue on attacker flip"
    payload = m_publish.call_args[0][1]
    assert payload.get("is_known_attacker") is True, (
        f"payload must include is_known_attacker=True, got {payload!r}"
    )


@th.django_unit_test()
def test_check_threats_no_flip_does_not_enqueue(opts):
    """If is_known_attacker stays True->True, no push."""
    from mojo.apps.account.models.geolocated_ip import GeoLocatedIP
    from mojo.helpers.geoip import threat_intel

    GeoLocatedIP.objects.filter(ip_address="203.0.113.211").delete()
    geo = GeoLocatedIP.objects.create(
        ip_address="203.0.113.211", provider="mojo", threat_level="high",
        is_known_attacker=True, is_known_abuser=False,
    )

    fake_threat = {
        "is_known_attacker": True,
        "is_known_abuser": False,
        "is_blocklisted": False,
        "threat_data": {"internal": {}, "blocklists": []},
    }

    patches = _enable_sync()
    with patches[0], patches[1], \
         mock.patch.object(threat_intel, "perform_threat_check",
                           return_value=fake_threat), \
         mock.patch.object(threat_intel, "recalculate_threat_level",
                           return_value="high"), \
         mock.patch(_push_path()) as m_publish:
        geo.check_threats()

    assert not m_publish.called, (
        "check_threats() with no signal rise must not enqueue; "
        f"call_args={m_publish.call_args!r}"
    )


@th.django_unit_test()
def test_pushback_is_async_never_inline_http(opts):
    """Confirms the push goes through jobs.publish, never an inline requests.post.

    This is the critical guarantee — block() in production runs from rule
    handlers and admin REST and must NEVER do a 10s upstream HTTP call inline.
    """
    from mojo.apps.account.models.geolocated_ip import GeoLocatedIP

    GeoLocatedIP.objects.filter(ip_address="203.0.113.220").delete()
    geo = GeoLocatedIP.objects.create(
        ip_address="203.0.113.220", provider="mojo", threat_level=None,
    )

    patches = _enable_sync()
    with patches[0], patches[1], \
         mock.patch(_push_path()) as m_publish, \
         mock.patch("mojo.apps.account.asyncjobs.requests.post") as m_post:
        geo.block(reason="test_async_only", ttl=600, broadcast=False)

    assert m_publish.called, "push-back must go through jobs.publish"
    assert not m_post.called, (
        "block() must NEVER make an inline HTTP POST; that's what jobs.publish is for. "
        f"got requests.post call_args={m_post.call_args!r}"
    )


@th.django_unit_test()
def test_publish_failure_does_not_raise(opts):
    """If jobs.publish itself errors, block() must still succeed locally."""
    from mojo.apps.account.models.geolocated_ip import GeoLocatedIP

    GeoLocatedIP.objects.filter(ip_address="203.0.113.221").delete()
    geo = GeoLocatedIP.objects.create(
        ip_address="203.0.113.221", provider="mojo", threat_level=None,
    )

    patches = _enable_sync()
    with patches[0], patches[1], \
         mock.patch(_push_path(), side_effect=RuntimeError("redis down")):
        # Should not raise
        geo.block(reason="test_publish_failure", ttl=600, broadcast=False)

    geo.refresh_from_db()
    assert geo.is_blocked is True, (
        "local block must succeed even if push-back enqueue fails"
    )
    assert geo.threat_level == "high", (
        "threat escalation must succeed even if push-back enqueue fails"
    )
