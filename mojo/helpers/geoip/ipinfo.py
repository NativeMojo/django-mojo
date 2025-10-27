"""
IPInfo.io provider for GeoIP lookups.
https://ipinfo.io/
"""
import requests
from mojo.helpers.location.countries import get_country_name
from .config import IPINFO_API_KEY


def fetch(ip_address, api_key=None):
    """
    Fetches geolocation data from the ipinfo.io API and normalizes it.

    Args:
        ip_address: The IP address to look up
        api_key: Optional API key (uses config default if not provided)

    Returns:
        dict: Normalized geolocation data, or None on failure
    """
    if api_key is None:
        api_key = IPINFO_API_KEY

    try:
        url = f"https://ipinfo.io/{ip_address}"
        if api_key:
            url += f"?token={api_key}"

        response = requests.get(url, timeout=5)
        response.raise_for_status()
        data = response.json()

        # Normalize the data to our model's schema
        loc_parts = data.get('loc', '').split(',')
        latitude = float(loc_parts[0]) if len(loc_parts) == 2 else None
        longitude = float(loc_parts[1]) if len(loc_parts) == 2 else None
        country_code = data.get('country')

        # Extract ASN info from org field (format: "AS15169 Google LLC")
        org = data.get('org', '')
        asn = None
        asn_org = org
        if org.startswith('AS'):
            parts = org.split(' ', 1)
            if len(parts) == 2:
                asn = parts[0]
                asn_org = parts[1]

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
            'asn': asn,
            'asn_org': asn_org,
            'isp': asn_org,  # ipinfo doesn't separate ISP, use org
            'connection_type': None,  # ipinfo doesn't provide this
            'data': data  # Store the raw response
        }

    except Exception as e:
        print(f"[GeoIP Error] Failed to fetch from ipinfo.io for IP {ip_address}: {e}")
        return None
