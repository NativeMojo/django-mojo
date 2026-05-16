"""Integration tests for POST /api/system/geoip/sync.

Exercises the federation receiver endpoint end-to-end via opts.client:
auth gating via the `geoip_sync` ApiKey permission, MAX semantics for
threat_level, OR semantics for is_known_attacker/is_known_abuser,
rejection of per-fleet enforcement fields, partial payloads, and
loop-prevention (the endpoint applies via raw save, never re-pushes).
"""
from testit import helpers as th


@th.django_unit_setup()
def setup_sync_endpoint(opts):
    """Create a group + ApiKey with geoip_sync permission, store the raw token."""
    from mojo.apps.account.models import Group, ApiKey

    ApiKey.objects.filter(name__startswith="geoip_sync_test_").delete()
    Group.objects.filter(name="geoip_sync_test_group").delete()

    group = Group.objects.create(name="geoip_sync_test_group", kind="organization")

    # Key WITH the geoip_sync permission
    api_key_authz, token_authz = ApiKey.create_for_group(
        group=group,
        name="geoip_sync_test_authorized",
        permissions={"geoip_sync": True},
    )

    # Key WITHOUT the geoip_sync permission (perm gate test)
    api_key_unauthz, token_unauthz = ApiKey.create_for_group(
        group=group,
        name="geoip_sync_test_unauthorized",
        permissions={"view_data": True},
    )

    opts.sync_group_id = group.id
    opts.sync_token_authz = token_authz
    opts.sync_token_unauthz = token_unauthz


def _use_apikey(opts, token):
    """Switch the test client to send `Authorization: apikey <token>`."""
    opts.client.logout()
    opts.client.bearer = "apikey"
    opts.client.access_token = token
    opts.client.is_authenticated = True


@th.django_unit_test()
def test_sync_requires_geoip_sync_perm(opts):
    """An ApiKey without geoip_sync must be denied."""
    _use_apikey(opts, opts.sync_token_unauthz)
    resp = opts.client.post(
        "/api/system/geoip/sync",
        {"ip": "203.0.113.30", "threat_level": "high"},
    )
    assert resp.status_code in (401, 403), (
        f"unauthorized key must be denied, got {resp.status_code} {resp.response}"
    )
    opts.client.logout()


@th.django_unit_test()
def test_sync_applies_threat_level_max(opts):
    """First sync sets threat_level; second with lower value is ignored."""
    from mojo.apps.account.models.geolocated_ip import GeoLocatedIP

    GeoLocatedIP.objects.filter(ip_address="203.0.113.31").delete()
    _use_apikey(opts, opts.sync_token_authz)

    # Raise to 'high'
    resp = opts.client.post(
        "/api/system/geoip/sync",
        {"ip": "203.0.113.31", "threat_level": "high"},
    )
    assert resp.status_code == 200, (
        f"first sync failed: {resp.status_code} {resp.response}"
    )
    data = resp.response.data
    assert data.threat_level == "high", f"threat_level not applied: {data!r}"
    assert data.applied["threat_level"] == "high", (
        f"applied dict must record the change: {data.applied!r}"
    )

    # Lower value must NOT downgrade
    resp = opts.client.post(
        "/api/system/geoip/sync",
        {"ip": "203.0.113.31", "threat_level": "low"},
    )
    assert resp.status_code == 200, "second sync should succeed"
    data = resp.response.data
    assert data.threat_level == "high", (
        f"MAX semantics violated — sync downgraded high to low: {data!r}"
    )
    assert "threat_level" not in data.applied, (
        f"applied dict must not record a non-change: {data.applied!r}"
    )

    # DB state matches
    geo = GeoLocatedIP.objects.get(ip_address="203.0.113.31")
    assert geo.threat_level == "high", f"DB not in sync: {geo.threat_level!r}"
    opts.client.logout()


@th.django_unit_test()
def test_sync_or_semantics_for_booleans(opts):
    """is_known_attacker/abuser flips True via sync, never True->False."""
    from mojo.apps.account.models.geolocated_ip import GeoLocatedIP

    GeoLocatedIP.objects.filter(ip_address="203.0.113.32").delete()
    _use_apikey(opts, opts.sync_token_authz)

    # Flip True
    resp = opts.client.post(
        "/api/system/geoip/sync",
        {"ip": "203.0.113.32", "is_known_attacker": True},
    )
    assert resp.status_code == 200, f"sync failed: {resp.response}"
    data = resp.response.data
    assert data.is_known_attacker is True, f"attacker not set: {data!r}"
    assert data.applied["is_known_attacker"] is True, "applied must record"

    # Try to flip back with False — must be ignored
    resp = opts.client.post(
        "/api/system/geoip/sync",
        {"ip": "203.0.113.32", "is_known_attacker": False},
    )
    assert resp.status_code == 200, "second sync should succeed"
    data = resp.response.data
    assert data.is_known_attacker is True, (
        f"OR semantics violated — sync downgraded True to False: {data!r}"
    )
    assert "is_known_attacker" not in data.applied, (
        f"applied dict must not record OR no-op: {data.applied!r}"
    )
    opts.client.logout()


@th.django_unit_test()
def test_sync_accepts_partial_payload(opts):
    """Payload with just one of the three signal fields is valid."""
    from mojo.apps.account.models.geolocated_ip import GeoLocatedIP

    GeoLocatedIP.objects.filter(ip_address="203.0.113.33").delete()
    _use_apikey(opts, opts.sync_token_authz)

    resp = opts.client.post(
        "/api/system/geoip/sync",
        {"ip": "203.0.113.33", "is_known_abuser": True},
    )
    assert resp.status_code == 200, f"partial sync failed: {resp.response}"
    data = resp.response.data
    assert data.is_known_abuser is True, f"flag not set: {data!r}"
    # threat_level untouched
    geo = GeoLocatedIP.objects.get(ip_address="203.0.113.33")
    assert geo.threat_level in (None, "low"), (
        f"threat_level should be untouched, got {geo.threat_level!r}"
    )
    opts.client.logout()


@th.django_unit_test()
def test_sync_rejects_forbidden_firewall_fields(opts):
    """Per-fleet enforcement fields are never federated — payload is rejected."""
    _use_apikey(opts, opts.sync_token_authz)

    resp = opts.client.post(
        "/api/system/geoip/sync",
        {
            "ip": "203.0.113.34",
            "threat_level": "high",
            "is_blocked": True,  # forbidden
        },
    )
    assert resp.status_code == 200, f"got {resp.status_code} {resp.response}"
    body = resp.response
    assert body.status is False, (
        f"forbidden field must be rejected with status=False, got {body!r}"
    )
    assert body.error and "is_blocked" in body.error, (
        f"error must mention forbidden field, got {body.error!r}"
    )
    opts.client.logout()


@th.django_unit_test()
def test_sync_rejects_invalid_threat_level(opts):
    _use_apikey(opts, opts.sync_token_authz)

    resp = opts.client.post(
        "/api/system/geoip/sync",
        {"ip": "203.0.113.35", "threat_level": "extreme"},
    )
    assert resp.status_code == 200, f"got {resp.status_code} {resp.response}"
    body = resp.response
    assert body.status is False, (
        f"invalid threat_level must be rejected, got {body!r}"
    )
    opts.client.logout()


@th.django_unit_test()
def test_sync_requires_at_least_one_signal(opts):
    """Payload with only `ip` and no signal fields is rejected."""
    _use_apikey(opts, opts.sync_token_authz)

    resp = opts.client.post(
        "/api/system/geoip/sync",
        {"ip": "203.0.113.36"},
    )
    assert resp.status_code == 200, f"got {resp.status_code} {resp.response}"
    body = resp.response
    assert body.status is False, (
        f"empty payload must be rejected, got {body!r}"
    )
    opts.client.logout()


@th.django_unit_test()
def test_sync_cleanup(opts):
    """Remove test ApiKeys and Group."""
    from mojo.apps.account.models import Group, ApiKey
    from mojo.apps.account.models.geolocated_ip import GeoLocatedIP

    ApiKey.objects.filter(name__startswith="geoip_sync_test_").delete()
    Group.objects.filter(name="geoip_sync_test_group").delete()
    GeoLocatedIP.objects.filter(ip_address__startswith="203.0.113.3").delete()
