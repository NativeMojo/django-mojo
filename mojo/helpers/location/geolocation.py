import requests
import ipaddress
import random
from mojo.helpers.settings import settings
from .countries import get_country_name

# Lazy-load model to avoid circular imports
_GeoLocatedIP = None
GEOLOCATION_PROVIDERS = settings.get('GEOLOCATION_PROVIDERS', ['ipinfo'])


def get_geo_located_ip_model():
    global _GeoLocatedIP
    if _GeoLocatedIP is None:
        from mojo.apps.account.models.device import GeoLocatedIP
        _GeoLocatedIP = GeoLocatedIP
    return _GeoLocatedIP


def geolocate_ip(ip_address):
    """
    Fetches geolocation data for a given IP address. It handles both
    public IPs (by calling an external provider) and private IPs.
    Returns a normalized dictionary of geolocation data.
    """
    # 1. Handle private/reserved IPs
    try:
        ip_obj = ipaddress.ip_address(ip_address)
        if ip_obj.is_private or ip_obj.is_reserved:
            return {
                'provider': 'internal',
                'country_name': 'Private Network',
                'region': 'Private' if ip_obj.is_private else 'Reserved',
            }
    except ValueError:
        return None  # Invalid IP

    # 2. Handle public IPs by dispatching to a randomly selected provider
    providers = GEOLOCATION_PROVIDERS
    provider = random.choice(providers)
    api_key_setting_name = f'GEOLOCATION_API_KEY_{provider.upper()}'
    api_key = getattr(settings, api_key_setting_name, None)

    provider_map = {
        'ipinfo': fetch_from_ipinfo,
        'ipstack': fetch_from_ipstack,
        'ip-api': fetch_from_ipapi,
        'maxmind': fetch_from_maxmind,
    }

    fetch_function = provider_map.get(provider)

    if fetch_function:
        return fetch_function(ip_address, api_key)
    else:
        # In a real app, you might want to log this or handle it differently
        print(f"[Geolocation Error] Provider '{provider}' is not supported.")
        return None


def refresh_geolocation_for_ip(ip_address):
    """
    This function is the entry point for the background task.
    It gets or creates a GeoLocatedIP record and refreshes it if necessary.
    """
    GeoLocatedIP = get_geo_located_ip_model()

    # Get or create the record, then call its internal refresh logic.
    geo_record, created = GeoLocatedIP.objects.get_or_create(ip_address=ip_address)

    if created or geo_record.is_expired:
        geo_record.refresh()

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


def fetch_from_ipstack(ip_address, api_key):
    """
    Fetches geolocation data from the ipstack.com API and normalizes it.
    """
    if not api_key:
        print("[Geolocation Error] ipstack provider requires an API key (GEOLOCATION_API_KEY_IPSTACK).")
        return None
    try:
        url = f"http://api.ipstack.com/{ip_address}?access_key={api_key}"
        response = requests.get(url, timeout=5)
        response.raise_for_status()
        data = response.json()

        if data.get('success') is False:
            error_info = data.get('error', {}).get('info', 'Unknown error')
            print(f"[Geolocation Error] ipstack API error: {error_info}")
            return None

        country_code = data.get('country_code')
        return {
            'provider': 'ipstack',
            'country_code': country_code,
            'country_name': data.get('country_name') or get_country_name(country_code),
            'region': data.get('region_name'),
            'city': data.get('city'),
            'postal_code': data.get('zip'),
            'latitude': data.get('latitude'),
            'longitude': data.get('longitude'),
            'data': data
        }
    except Exception as e:
        print(f"[Geolocation Error] Failed to fetch from ipstack.com for IP {ip_address}: {e}")
        return None


def fetch_from_ipapi(ip_address, api_key=None):
    """
    Fetches geolocation data from the ip-api.com API and normalizes it.
    Note: The free tier does not require an API key.
    """
    try:
        url = f"http://ip-api.com/json/{ip_address}"
        response = requests.get(url, timeout=5)
        response.raise_for_status()
        data = response.json()

        if data.get('status') == 'fail':
            error_info = data.get('message', 'Unknown error')
            print(f"[Geolocation Error] ip-api.com API error: {error_info}")
            return None

        country_code = data.get('countryCode')
        return {
            'provider': 'ip-api',
            'country_code': country_code,
            'country_name': data.get('country') or get_country_name(country_code),
            'region': data.get('regionName'),
            'city': data.get('city'),
            'postal_code': data.get('zip'),
            'latitude': data.get('lat'),
            'longitude': data.get('lon'),
            'timezone': data.get('timezone'),
            'data': data
        }
    except Exception as e:
        print(f"[Geolocation Error] Failed to fetch from ip-api.com for IP {ip_address}: {e}")
        return None


def fetch_from_maxmind(ip_address, api_key):
    """
    Placeholder for MaxMind GeoIP2 web service integration.
    """
    # MaxMind's GeoIP2 web services are best accessed via their official client library.
    # See: https://github.com/maxmind/geoip2-python
    # This is a placeholder for where you would integrate the geoip2.webservice.Client.
    # You would typically fetch account_id and license_key from settings here instead of a single api_key.
    raise NotImplementedError(
        "MaxMind provider requires the 'geoip2' client library. "
        "Set GEOLOCATION_API_KEY_MAXMIND_ACCOUNT_ID and GEOLOCATION_API_KEY_MAXMIND_LICENSE_KEY in your settings."
    )
