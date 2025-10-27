"""
IPStack.com provider for GeoIP lookups.
https://ipstack.com/
"""
import requests
from mojo.helpers.location.countries import get_country_name
from .config import IPSTACK_API_KEY


def fetch(ip_address, api_key=None):
    """
    Fetches geolocation data from the ipstack.com API and normalizes it.

    Args:
        ip_address: The IP address to look up
        api_key: Optional API key (uses config default if not provided)

    Returns:
        dict: Normalized geolocation data, or None on failure
    """
    if api_key is None:
        api_key = IPSTACK_API_KEY

    if not api_key:
        print("[GeoIP Error] ipstack provider requires an API key (GEOIP_API_KEY_IPSTACK).")
        return None

    try:
        url = f"http://api.ipstack.com/{ip_address}?access_key={api_key}"
        response = requests.get(url, timeout=5)
        response.raise_for_status()
        data = response.json()

        if data.get('success') is False:
            error_info = data.get('error', {}).get('info', 'Unknown error')
            print(f"[GeoIP Error] ipstack API error: {error_info}")
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
            'timezone': data.get('time_zone', {}).get('id') if data.get('time_zone') else None,
            'asn': None,  # ipstack doesn't provide ASN in basic plan
            'asn_org': None,
            'isp': None,
            'connection_type': data.get('connection_type'),
            'data': data
        }
    except Exception as e:
        print(f"[GeoIP Error] Failed to fetch from ipstack.com for IP {ip_address}: {e}")
        return None
