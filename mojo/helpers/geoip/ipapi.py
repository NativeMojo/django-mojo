"""
IP-API.com provider for GeoIP lookups.
http://ip-api.com/
"""
import requests
from mojo.helpers.location.countries import get_country_name


def fetch(ip_address, api_key=None):
    """
    Fetches geolocation data from the ip-api.com API and normalizes it.
    Note: The free tier does not require an API key.

    Args:
        ip_address: The IP address to look up
        api_key: Optional API key (not used by free tier)

    Returns:
        dict: Normalized geolocation data, or None on failure
    """
    try:
        url = f"http://ip-api.com/json/{ip_address}"
        response = requests.get(url, timeout=5)
        response.raise_for_status()
        data = response.json()

        if data.get('status') == 'fail':
            error_info = data.get('message', 'Unknown error')
            print(f"[GeoIP Error] ip-api.com API error: {error_info}")
            return None

        country_code = data.get('countryCode')

        # Extract ASN info from 'as' field (format: "AS15169 Google LLC")
        as_field = data.get('as', '')
        asn = None
        asn_org = as_field
        if as_field.startswith('AS'):
            parts = as_field.split(' ', 1)
            if len(parts) == 2:
                asn = parts[0]
                asn_org = parts[1]

        # ip-api's "region" field is the ISO 3166-2 subdivision code (e.g. "FL");
        # "regionName" is the full name. Combine with country to form "US-FL".
        sub_code = data.get('region')
        region_code = f"{country_code}-{sub_code}" if (country_code and sub_code) else None

        return {
            'provider': 'ip-api',
            'country_code': country_code,
            'country_name': data.get('country') or get_country_name(country_code),
            'region': data.get('regionName'),
            'region_code': region_code,
            'city': data.get('city'),
            'postal_code': data.get('zip'),
            'latitude': data.get('lat'),
            'longitude': data.get('lon'),
            'timezone': data.get('timezone'),
            'asn': asn,
            'asn_org': asn_org,
            'isp': data.get('isp'),
            'connection_type': None,  # ip-api doesn't provide this in free tier
            'data': data
        }
    except Exception as e:
        print(f"[GeoIP Error] Failed to fetch from ip-api.com for IP {ip_address}: {e}")
        return None
