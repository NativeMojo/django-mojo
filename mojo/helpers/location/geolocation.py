import requests
import ipaddress
from datetime import timedelta
from django.conf import settings
from mojo.helpers import dates
from .countries import get_country_name

# Lazy-load models to avoid circular imports
_GeoLocatedIP = None
_UserDeviceLocation = None


def get_geo_located_ip_model():
    global _GeoLocatedIP
    if _GeoLocatedIP is None:
        from mojo.apps.account.models.device import GeoLocatedIP
        _GeoLocatedIP = GeoLocatedIP
    return _GeoLocatedIP


def get_user_device_location_model():
    global _UserDeviceLocation
    if _UserDeviceLocation is None:
        from mojo.apps.account.models.device import UserDeviceLocation
        _UserDeviceLocation = UserDeviceLocation
    return _UserDeviceLocation


def geolocate(ip_address):
    """
    Retrieves geolocation data for an IP address. For private/reserved IPs, it creates a
    standardized internal record. For public IPs, it uses a cache (`GeoLocatedIP` model)
    to avoid redundant API calls.

    This function is intended to be called from a background task.
    """
    GeoLocatedIP = get_geo_located_ip_model()

    # 1. Validate IP address
    try:
        ip_obj = ipaddress.ip_address(ip_address)
    except ValueError:
        return None  # Not a valid IP address

    # 2. Handle private/reserved IP addresses
    if ip_obj.is_private or ip_obj.is_reserved:
        geo_record, created = GeoLocatedIP.objects.update_or_create(
            ip_address=ip_address,
            defaults={
                'provider': 'internal',
                'country_name': 'Private Network',
                'region': ip_obj.is_private and 'Private' or 'Reserved',
                'expires_at': None  # Internal records never expire
            }
        )
        return geo_record

    # 3. Handle public IPs: Check for a fresh, non-expired entry in the cache
    cached_geo = GeoLocatedIP.objects.filter(ip_address=ip_address).first()
    if cached_geo and not cached_geo.is_expired:
        return cached_geo

    # 4. Fetch from the external provider
    provider = getattr(settings, 'GEOLOCATION_PROVIDER', 'ipinfo').lower()
    api_key_setting_name = f'GEOLOCATION_API_KEY_{provider.upper()}'
    api_key = getattr(settings, api_key_setting_name, None)

    if provider == 'ipinfo':
        geo_data = fetch_from_ipinfo(ip_address, api_key)
    else:
        raise NotImplementedError(f"Geolocation provider '{provider}' is not supported.")

    if not geo_data:
        return None

    # 5. Create or update the cache entry
    cache_duration_days = getattr(settings, 'GEOLOCATION_CACHE_DURATION_DAYS', 30)
    expires_at = dates.utcnow() + timedelta(days=cache_duration_days)

    geo_record, created = GeoLocatedIP.objects.update_or_create(
        ip_address=ip_address,
        defaults={
            **geo_data,
            'expires_at': expires_at
        }
    )

    # 6. Link this new geo record to any device locations waiting for it
    UserDeviceLocation = get_user_device_location_model()
    locations_to_update = UserDeviceLocation.objects.filter(
        ip_address=ip_address,
        geolocation__isnull=True
    )
    locations_to_update.update(geolocation=geo_record)

    return geo_record


def fetch_from_ipinfo(ip_address, api_key):
    """
    Fetches geolocation data from the ipinfo.io API and normalizes it.
    Fails gracefully by returning None if any error occurs.
    """
    try:
        url = f"https://ipinfo.io/{ip_address}"
        if api_key:
            url += f"?token={api_key}"

        response = requests.get(url, timeout=5)
        response.raise_for_status()  # Raise an exception for bad status codes (4xx or 5xx)
        data = response.json()

        # Normalize the data to our model's schema
        loc_parts = data.get('loc', '').split(',')
        latitude = float(loc_parts[0]) if len(loc_parts) == 2 else None
        longitude = float(loc_parts[1]) if len(loc_parts) == 2 else None
        country_code = data.get('country')

        return {
            'provider': 'ipinfo',
            'country_code': country_code,
            'country_name': get_country_name(country_code),
            'region': data.get('region'),
            'city': data.get('city'),
            'postal_code': data.get('postal'),
            'latitude': latitude,
            'longitude': longitude,
            'timezone': data.get('timezone'),
            'data': data  # Store the raw response
        }

    except Exception as e:
        # In a real application, you would want to log this error.
        print(f"[Geolocation Error] Failed to fetch from ipinfo.io for IP {ip_address}: {e}")
        return None
