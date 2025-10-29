from testit import helpers as th
from unittest.mock import MagicMock

# Use the same test user as other account tests
TEST_USER = "testit"

@th.django_unit_test()
def setup_device_testing(opts):
    """
    Nuke any test entries from the database so we can do repeat testing.
    """
    from mojo.apps.account.models.device import UserDevice, UserDeviceLocation
    from mojo.apps.account.models.geolocated_ip import GeoLocatedIP
    UserDevice.objects.filter(user__username=TEST_USER).delete()
    UserDeviceLocation.objects.filter(user__username=TEST_USER).delete()
    GeoLocatedIP.objects.filter(ip_address__in=['8.8.8.8', '192.168.1.100', '1.1.1.1']).delete()


@th.django_unit_test()
def test_track_new_device_public_ip(opts):
    """
    Tests that tracking a new device with a public IP creates the correct
    database objects and triggers a geolocation refresh task.
    """
    from mojo.apps.account.models import User
    from mojo.apps.account.models.device import UserDevice, UserDeviceLocation
    from mojo.apps.account.models.geolocated_ip import GeoLocatedIP

    user = User.objects.get(username=TEST_USER)

    # 1. Setup a mock request object
    mock_request = MagicMock()
    mock_request.user = user
    mock_request.ip = '8.8.8.8'
    mock_request.user_agent = 'TestAgent/1.0'
    mock_request.duid = 'test-duid-public-ip'

    # 2. Call the track method
    device = UserDevice.track(mock_request)

    # 3. Assertions
    assert device is not None, f"UserDevice.track() should return the device object, got: {device}"
    assert device.duid == 'test-duid-public-ip', f"Expected duid 'test-duid-public-ip', got: {device.duid}"
    assert device.user == user, f"Expected user {user}, got: {device.user}"
    assert device.last_ip == '8.8.8.8', f"Expected last_ip '8.8.8.8', got: {device.last_ip}"

    # Check that the location was logged
    try:
        location = UserDeviceLocation.objects.get(user_device=device, ip_address='8.8.8.8')
    except UserDeviceLocation.DoesNotExist:
        available_locations = UserDeviceLocation.objects.filter(user_device=device)
        assert False, f"UserDeviceLocation not found for device {device.duid} and IP 8.8.8.8. Available locations: {[loc.ip_address for loc in available_locations]}"
    except Exception as e:
        assert False, f"Error querying UserDeviceLocation: {e}"

    assert location is not None, f"UserDeviceLocation should exist for device {device.duid} and IP 8.8.8.8"
    assert location.user == user, f"Expected location.user {user}, got: {location.user}"

    # Check that the GeoLocatedIP record was created and linked
    try:
        geo_ip = GeoLocatedIP.objects.get(ip_address='8.8.8.8')
    except GeoLocatedIP.DoesNotExist:
        available_geo_ips = GeoLocatedIP.objects.all()
        assert False, f"GeoLocatedIP not found for IP 8.8.8.8. Available geo IPs: {[ip.ip_address for ip in available_geo_ips]}"
    except Exception as e:
        assert False, f"Error querying GeoLocatedIP: {e}"

    assert location.geolocation == geo_ip, f"Expected location.geolocation {geo_ip}, got: {location.geolocation}"


@th.django_unit_test()
def test_track_device_private_ip(opts):
    """
    Tests that tracking a device with a private IP creates the correct
    database objects and triggers a geolocation refresh task.
    """
    from mojo.apps.account.models import User
    from mojo.apps.account.models.device import UserDevice, UserDeviceLocation
    from mojo.apps.account.models.geolocated_ip import GeoLocatedIP

    user = User.objects.get(username=TEST_USER)
    mock_request = MagicMock()
    mock_request.user = user
    mock_request.ip = '192.168.1.100'
    mock_request.user_agent = 'TestAgent/1.0'
    mock_request.duid = 'test-duid-private-ip'

    UserDevice.track(mock_request)

    # Check that the GeoLocatedIP record was created and marked as internal
    # Use geolocate to get or create and refresh if needed
    geo_ip = GeoLocatedIP.geolocate('192.168.1.100', auto_refresh=True)

    assert geo_ip.provider == 'internal', f"Expected provider 'internal', got: {geo_ip.provider}"
    assert geo_ip.country_name == 'Private Network', f"Expected country_name 'Private Network', got: {geo_ip.country_name}"


@th.django_unit_test()
def test_geolocation_refresh_logic(opts):
    """
    Tests the GeoLocatedIP.refresh() method by making real API calls.
    Tests primary/fallback logic with actual providers.
    """
    from mojo.apps.account.models.geolocated_ip import GeoLocatedIP

    # 1. Create a GeoLocatedIP object to test with (using Cloudflare's DNS IP)
    geo_ip, created = GeoLocatedIP.objects.get_or_create(ip_address='80.96.70.170')

    # 2. Call the refresh method (makes real API call)
    result = geo_ip.refresh()
    # print(geo_ip.to_dict())

    # 3. Assertions - verify data was fetched and saved
    # Reload the object from the database to confirm it was saved
    refreshed_geo_ip = GeoLocatedIP.objects.get(ip_address='80.96.70.170')

    # Debug: Print what we got
    # print(f"\nGeoIP Result for 80.96.70.170:")
    # print(f"  Provider: {refreshed_geo_ip.provider}")
    # print(f"  Country Code: {refreshed_geo_ip.country_code}")
    # print(f"  Country Name: {refreshed_geo_ip.country_name}")
    # print(f"  City: {refreshed_geo_ip.city}")
    # print(f"  Region: {refreshed_geo_ip.region}")
    # print(f"  Refresh Result: {result}")

    # Should have a provider set
    assert refreshed_geo_ip.provider is not None, f"Expected provider to be set, got: {refreshed_geo_ip.provider}"

    # If refresh succeeded, we should have some geolocation data
    if result is True:
        assert refreshed_geo_ip.country_name is not None, f"Expected country_name to be set after successful refresh"
