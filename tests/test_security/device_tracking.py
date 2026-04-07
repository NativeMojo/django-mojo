"""
Tests for django-mojo device tracking.

These are HTTP integration tests — they go through the real login flow via
opts.client so the full chain is tested: middleware sets request.muid/msid/ip →
login handler calls UserDevice.track() → DB records created with real values.

No MagicMock for requests. The test client emulates a real browser (persistent
cookies, realistic headers) so device tracking sees exactly what production sees.

NOTE: Tests that depend on _muid cookie persistence (second_login, fresh_browser)
require DEBUG=True on the server. When DEBUG=False, _muid is set with Secure flag
and won't be sent over HTTP (test server is HTTP on localhost). These tests skip
automatically when DEBUG is off.

Contracts enforced:
  - Login creates a UserDevice with muid from server cookie
  - Login creates a UserDeviceLocation linking device to IP
  - GeoLocatedIP record is created for the login IP
  - Private IP geolocation is marked as 'internal'
  - GeoLocatedIP.refresh() fetches real geo data from providers
"""
from testit import helpers as th
from testit.helpers import assert_true, assert_eq

TEST_USER = "devtrack_user"
TEST_PWORD = "devtrack##mojo99"


def _is_debug():
    from mojo.helpers.settings import settings
    return bool(settings.DEBUG)


@th.django_unit_setup()
def setup_device_testing(opts):
    from mojo.apps.account.models import User
    from mojo.apps.account.models.device import UserDevice, UserDeviceLocation
    from mojo.apps.account.models.geolocated_ip import GeoLocatedIP
    from mojo.decorators.limits import clear_rate_limits
    clear_rate_limits(ip='127.0.0.1')

    # Ensure clean test user
    User.objects.filter(username=TEST_USER).delete()
    user = User(username=TEST_USER, display_name=TEST_USER, email=f"{TEST_USER}@example.com")
    user.save()
    user.is_active = True
    user.is_email_verified = True
    user.save_password(TEST_PWORD)
    user.save()

    UserDevice.objects.filter(user=user).delete()
    UserDeviceLocation.objects.filter(user=user).delete()
    GeoLocatedIP.objects.filter(ip_address__in=['127.0.0.1', '192.168.1.100', '80.96.70.170']).delete()


@th.django_unit_test()
def test_login_creates_device_with_muid(opts):
    """Login creates a UserDevice with muid from the server _muid cookie."""
    from mojo.apps.account.models.device import UserDevice

    opts.client.login(TEST_USER, TEST_PWORD)

    device = UserDevice.objects.filter(user__username=TEST_USER).order_by('-last_seen').first()
    assert_true(device is not None, "expected UserDevice created on login")
    assert_true(device.duid, "expected duid to be set")
    assert_true(device.muid, "expected muid from server cookie to be stored")
    assert_true(device.last_ip, "expected last_ip to be set")

    # Store for later tests
    opts.device_id = device.pk
    opts.device_muid = device.muid


@th.django_unit_test()
def test_login_creates_device_location(opts):
    """Login creates a UserDeviceLocation linking the device to the login IP."""
    from mojo.apps.account.models.device import UserDeviceLocation

    location = UserDeviceLocation.objects.filter(
        user_device_id=opts.device_id
    ).first()
    assert_true(location is not None, "expected UserDeviceLocation created on login")
    assert_true(location.ip_address, "expected ip_address on location")
    assert_true(location.user_id, "expected user FK on location")


@th.django_unit_test()
def test_login_creates_geolocated_ip(opts):
    """Login creates a GeoLocatedIP record for the login IP."""
    from mojo.apps.account.models.geolocated_ip import GeoLocatedIP

    # The test server runs on 127.0.0.1 which is a private IP
    geo = GeoLocatedIP.objects.filter(ip_address='127.0.0.1').first()
    assert_true(geo is not None, "expected GeoLocatedIP for login IP")
    assert_eq(geo.provider, 'internal', "expected private IP to be marked as internal")


@th.django_unit_test()
def test_second_login_updates_device(opts):
    """Logging in again from the same browser updates the existing device, not creates a new one.

    Requires DEBUG=True — _muid cookie is Secure when DEBUG=False and won't
    persist over HTTP on the test server.
    """
    from testit.helpers import TestitSkip
    if not _is_debug():
        raise TestitSkip("requires DEBUG=True for _muid cookie persistence over HTTP")

    from mojo.apps.account.models.device import UserDevice

    opts.client.logout()
    opts.client.login(TEST_USER, TEST_PWORD)

    # Same browser session = same _muid cookie = same device
    count = UserDevice.objects.filter(user__username=TEST_USER).count()
    assert_eq(count, 1, f"expected 1 device (updated), got {count}")

    device = UserDevice.objects.get(pk=opts.device_id)
    assert_eq(device.muid, opts.device_muid, "expected same muid on second login")


@th.django_unit_test()
def test_fresh_browser_keeps_muid(opts):
    """Clearing cookies and logging in from the same UA keeps the original muid.

    muid is set once on the device and never overwritten — a new cookie does not
    replace an established device identity.

    Requires DEBUG=True — _muid cookie is Secure when DEBUG=False.
    """
    from testit.helpers import TestitSkip
    if not _is_debug():
        raise TestitSkip("requires DEBUG=True for _muid cookie persistence over HTTP")

    from mojo.apps.account.models.device import UserDevice

    old_muid = opts.device_muid

    opts.client.clear_cookies()
    opts.client.login(TEST_USER, TEST_PWORD)

    device = UserDevice.objects.get(pk=opts.device_id)
    assert_true(device.muid, "expected muid set after fresh login")
    assert_eq(device.muid, old_muid, "expected muid to be stable after cookie clear")


@th.django_unit_test()
def test_ip_drift_creates_location_not_device(opts):
    """When a user's IP changes between logins, _check_location_drift() creates
    a new UserDeviceLocation without modifying the UserDevice record."""
    from mojo.apps.account.models import User
    from mojo.apps.account.models.device import UserDevice, UserDeviceLocation
    from objict import objict

    user = User.objects.get(username=TEST_USER)
    device = UserDevice.objects.filter(user=user).order_by('-last_seen').first()
    assert_true(device is not None, "expected device from prior login")

    original_last_ip = device.last_ip
    original_muid = device.muid
    drift_ip = '10.0.0.42'

    # Clean up any prior location for the drift IP
    UserDeviceLocation.objects.filter(user=user, ip_address=drift_ip).delete()

    # Simulate an authenticated request from a different IP
    fake_request = objict(ip=drift_ip, user=user, muid=original_muid)
    from mojo.models import rest
    token = rest.ACTIVE_REQUEST.set(fake_request)
    try:
        # Force touch to fire by clearing last_activity
        user.last_activity = None
        user.touch()
    finally:
        rest.ACTIVE_REQUEST.reset(token)

    # Device record should be unchanged
    device.refresh_from_db()
    assert_eq(device.last_ip, original_last_ip, "expected device last_ip unchanged after IP drift")
    assert_eq(device.muid, original_muid, "expected device muid unchanged after IP drift")

    # But a new location record should exist for the drift IP
    loc = UserDeviceLocation.objects.filter(user=user, user_device=device, ip_address=drift_ip).first()
    assert_true(loc is not None, f"expected new UserDeviceLocation for drift IP {drift_ip}")


@th.django_unit_test()
def test_geo_staleness_skips_fresh_update(opts):
    """GeoLocatedIP.geolocate() skips the last_seen UPDATE when the record is fresh."""
    from mojo.apps.account.models.geolocated_ip import GeoLocatedIP
    from mojo.helpers import dates

    test_ip = '10.99.99.1'
    GeoLocatedIP.objects.filter(ip_address=test_ip).delete()

    # First call creates the record
    geo = GeoLocatedIP.geolocate(test_ip, auto_refresh=False)
    assert_true(geo.pk is not None, "expected GeoLocatedIP created")
    first_last_seen = geo.last_seen

    # Second call immediately — last_seen should NOT change (record is fresh)
    geo2 = GeoLocatedIP.geolocate(test_ip, auto_refresh=False)
    geo2.refresh_from_db()
    assert_eq(geo2.last_seen, first_last_seen, "expected last_seen unchanged for fresh geo record")


@th.django_unit_test()
def test_private_ip_geolocation(opts):
    """Private IPs are geolocated as 'internal' with 'Private Network' country."""
    from mojo.apps.account.models.geolocated_ip import GeoLocatedIP
    geo = GeoLocatedIP.geolocate('192.168.1.100', auto_refresh=True)
    assert_eq(geo.provider, 'internal', f"expected provider 'internal', got: {geo.provider}")
    assert_eq(geo.country_name, 'Private Network', f"expected 'Private Network', got: {geo.country_name}")


@th.django_unit_test()
def test_geolocation_refresh_logic(opts):
    """GeoLocatedIP.refresh() fetches real geo data from providers."""
    from mojo.apps.account.models.geolocated_ip import GeoLocatedIP
    geo_ip, created = GeoLocatedIP.objects.get_or_create(ip_address='80.96.70.170')
    result = geo_ip.refresh()
    refreshed = GeoLocatedIP.objects.get(ip_address='80.96.70.170')
    assert_true(refreshed.provider is not None, f"expected provider to be set, got: {refreshed.provider}")
    if result is True:
        assert_true(refreshed.country_name is not None, "expected country_name after successful refresh")
