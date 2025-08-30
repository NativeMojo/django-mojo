from testit import helpers as th
from unittest.mock import patch, MagicMock

# Use the same test user as other account tests
TEST_USER = "testit"

@th.django_unit_test()
def setup_device_testing(opts):
    """
    Nuke any test entries from the database so we can do repeat testing.
    """
    from mojo.apps.account.models.device import UserDevice, UserDeviceLocation, GeoLocatedIP
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
    from mojo.apps.account.models.device import UserDevice, UserDeviceLocation, GeoLocatedIP
    from mojo.helpers.location.geolocation import refresh_geolocation_for_ip

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
    from mojo.apps.account.models.device import UserDevice, UserDeviceLocation, GeoLocatedIP
    from mojo.helpers.location.geolocation import refresh_geolocation_for_ip

    user = User.objects.get(username=TEST_USER)
    mock_request = MagicMock()
    mock_request.user = user
    mock_request.ip = '192.168.1.100'
    mock_request.user_agent = 'TestAgent/1.0'
    mock_request.duid = 'test-duid-private-ip'

    UserDevice.track(mock_request)

    # Check that the GeoLocatedIP record was created and marked as internal
    # by the background task. We call the task directly to simulate the thread.
    refresh_geolocation_for_ip('192.168.1.100')

    try:
        geo_ip = GeoLocatedIP.objects.get(ip_address='192.168.1.100')
    except GeoLocatedIP.DoesNotExist:
        available_geo_ips = GeoLocatedIP.objects.all()
        assert False, f"GeoLocatedIP not found for IP 192.168.1.100 after refresh. Available geo IPs: {[ip.ip_address for ip in available_geo_ips]}"
    except Exception as e:
        assert False, f"Error querying GeoLocatedIP after refresh: {e}"

    assert geo_ip.provider == 'internal', f"Expected provider 'internal', got: {geo_ip.provider}"
    assert geo_ip.country_name == 'Private Network', f"Expected country_name 'Private Network', got: {geo_ip.country_name}"


@th.django_unit_test()
@patch('mojo.helpers.location.geolocation.fetch_from_ipinfo')
def test_geolocation_refresh_logic(opts, mock_fetch_from_ipinfo):
    """
    Tests the GeoLocatedIP.refresh() method to ensure it calls the external
    API and updates the model instance correctly.
    """
    from mojo.apps.account.models import User
    from mojo.apps.account.models.device import UserDevice, UserDeviceLocation, GeoLocatedIP
    from mojo.helpers.location.geolocation import refresh_geolocation_for_ip

    # 1. Mock the return value of the external API call
    mock_fetch_from_ipinfo.return_value = {
        'provider': 'ipinfo',
        'country_code': 'US',
        'country_name': 'United States of America',
        'region': 'California',
        'city': 'Mountain View',
        'postal_code': '94043',
        'latitude': 37.422,
        'longitude': -122.084,
        'timezone': 'America/Los_Angeles',
        'data': {'some': 'raw_data'}
    }

    # 2. Create a GeoLocatedIP object to test with
    geo_ip, created = GeoLocatedIP.objects.get_or_create(ip_address='1.1.1.1')

    # 3. Call the refresh method
    geo_ip.refresh()

    # 4. Assertions
    mock_fetch_from_ipinfo.assert_called_once()

    # Reload the object from the database to confirm it was saved
    try:
        refreshed_geo_ip = GeoLocatedIP.objects.get(ip_address='1.1.1.1')
    except GeoLocatedIP.DoesNotExist:
        available_geo_ips = GeoLocatedIP.objects.all()
        assert False, f"GeoLocatedIP not found for IP 1.1.1.1 after refresh. Available geo IPs: {[ip.ip_address for ip in available_geo_ips]}"
    except Exception as e:
        assert False, f"Error querying refreshed GeoLocatedIP: {e}"

    assert refreshed_geo_ip.provider == 'ipinfo', f"Expected provider 'ipinfo', got: {refreshed_geo_ip.provider}"
    assert refreshed_geo_ip.city == 'Mountain View', f"Expected city 'Mountain View', got: {refreshed_geo_ip.city}"
    assert refreshed_geo_ip.country_code == 'US', f"Expected country_code 'US', got: {refreshed_geo_ip.country_code}"
    assert refreshed_geo_ip.country_name == 'United States of America', f"Expected country_name 'United States of America', got: {refreshed_geo_ip.country_name}"
    assert refreshed_geo_ip.is_expired is False, f"Expected is_expired False, got: {refreshed_geo_ip.is_expired}"
