"""
IPVerify provider for GeoIP lookups.
https://ipverify.com/
"""
import requests
from mojo.helpers.settings import settings
from mojo.helpers import logit
IPVERIFY_API_KEY = settings.get('IPVERIFY_API_KEY')
IPVERIFY_URL = settings.get('IPVERIFY_HOST')


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
        api_key = IPVERIFY_API_KEY

    if not api_key:
        logit.error("[GeoIP Error] ipverify provider requires an API key (IPVERIFY_API_KEY).")
        return None

    headers = {
        'Authorization': f'ApiKey {api_key}'
    }
    path = "/api/comply/platform/geoip/lookup"
    if path[0] == "/":
        path = path[1:]
    url = f"{self.host}{path}"
    response = requests.get(url, timeout=5, headers=headers)
    if not response.ok:
        logit.error(f"[GeoIP Error] ipverify provider failed to fetch data for {ip_address}")
        return None
    data = response.json()
    return data.data
