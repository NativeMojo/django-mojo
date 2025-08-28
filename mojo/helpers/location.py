import requests
from datetime import timedelta
from django.conf import settings
from mojo.helpers import dates

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
    Retrieves geolocation data for an IP address, utilizing a cache (`GeoLocatedIP` model)
    to avoid redundant API calls. If the IP is not in the cache or the entry has expired,
    it fetches data from a configured third-party provider.

    This function is intended to be called from a background task.
    """
    GeoLocatedIP = get_geo_located_ip_model()

    # 1. Check for a fresh, non-expired entry in the cache
    cached_geo = GeoLocatedIP.objects.filter(ip_address=ip_address).first()
    if cached_geo and not cached_geo.is_expired:
        # If a valid entry exists, we don't need to do anything more here.
        # The track() method already handles associating existing entries.
        return cached_geo

    # 2. Fetch from the external provider
    provider = getattr(settings, 'GEOLOCATION_PROVIDER', 'ipinfo').lower()
    api_key = getattr(settings, 'GEOLOCATION_API_KEY', None)

    if provider == 'ipinfo':
        geo_data = fetch_from_ipinfo(ip_address, api_key)
    else:
        # Placeholder for other providers
        # e.g., if provider == 'ipstack':
        #   geo_data = fetch_from_ipstack(ip_address, api_key)
        raise NotImplementedError(f"Geolocation provider '{provider}' is not supported.")

    if not geo_data:
        return None

    # 3. Create or update the cache entry
    cache_duration_days = getattr(settings, 'GEOLOCATION_CACHE_DURATION_DAYS', 30)
    expires_at = dates.utcnow() + timedelta(days=cache_duration_days)

    geo_record, created = GeoLocatedIP.objects.update_or_create(
        ip_address=ip_address,
        defaults={
            **geo_data,
            'expires_at': expires_at
        }
    )

    # 4. Link this new geo record to any device locations waiting for it
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
    """
    try:
        url = f"https://ipinfo.io/{ip_address}"
        if api_key:
            url += f"?token={api_key}"

        response = requests.get(url, timeout=5)
        response.raise_for_status()  # Raise an exception for bad status codes
        data = response.json()

        # Normalize the data to our model's schema
        loc_parts = data.get('loc', '').split(',')
        latitude = float(loc_parts[0]) if len(loc_parts) == 2 else None
        longitude = float(loc_parts[1]) if len(loc_parts) == 2 else None

        return {
            'provider': 'ipinfo',
            'country_code': data.get('country'),
            'country_name': data.get('country'), # ipinfo provides code, can be mapped to name later
            'region': data.get('region'),
            'city': data.get('city'),
            'postal_code': data.get('postal'),
            'latitude': latitude,
            'longitude': longitude,
            'timezone': data.get('timezone'),
            'data': data  # Store the raw response
        }

    except requests.RequestException as e:
        # Log the error in a real application
        print(f"Error fetching from ipinfo.io: {e}")
        return None
