"""
Tests for the `_mode` aggregation surface against /api/system/geoip.

Verifies the firewall KPI tile path (count by is_blocked=true).
"""
from testit import helpers as th


TEST_USER = "geoip_agg_admin"
TEST_PWORD = "geoip##mojo99"


def _reset_admin(username, password):
    from mojo.apps.account.models import User
    user = User.objects.filter(username=username).last()
    if user is None:
        user = User(username=username, email=f"{username}@example.com")
        user.save()
    user.is_email_verified = True
    user.save_password(password)
    user.remove_all_permissions()
    user.is_staff = True
    user.is_superuser = True
    user.save()
    return user


@th.django_unit_setup()
def setup_geolocated_ip_aggregation(opts):
    from mojo.apps.account.models.geolocated_ip import GeoLocatedIP
    GeoLocatedIP.objects.filter(ip_address__startswith="203.0.113.").delete()

    _reset_admin(TEST_USER, TEST_PWORD)
    opts.user_name = TEST_USER
    opts.pword = TEST_PWORD


def _seed_geoip():
    from mojo.apps.account.models.geolocated_ip import GeoLocatedIP
    GeoLocatedIP.objects.filter(ip_address__startswith="203.0.113.").delete()
    # Three blocked, two not blocked.
    GeoLocatedIP.objects.create(ip_address="203.0.113.1", is_blocked=True)
    GeoLocatedIP.objects.create(ip_address="203.0.113.2", is_blocked=True)
    GeoLocatedIP.objects.create(ip_address="203.0.113.3", is_blocked=True)
    GeoLocatedIP.objects.create(ip_address="203.0.113.10", is_blocked=False)
    GeoLocatedIP.objects.create(ip_address="203.0.113.11", is_blocked=False)


@th.django_unit_test()
def test_mode_count_is_blocked(opts):
    _seed_geoip()
    assert opts.client.login(opts.user_name, opts.pword), "admin login failed"
    resp = opts.client.get(
        "/api/system/geoip",
        params={"_mode": "count", "is_blocked": "true"},
    )
    assert resp.status_code == 200, f"expected 200, got {resp.status_code}: {resp.body}"
    body = resp.response
    assert body.status is True, f"status=true expected: {body}"
    assert body["count"] >= 3, (
        f"expected at least 3 blocked seed rows, got {body['count']}"
    )
