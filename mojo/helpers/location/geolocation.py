import requests
import ipaddress
import random
from mojo.helpers.settings import settings
from .countries import get_country_name

# Lazy-load model to avoid circular imports
_GeoLocatedIP = None
GEOLOCATION_PROVIDERS = settings.get('GEOLOCATION_PROVIDERS', ['ipinfo'])

# Detection settings
ENABLE_TOR_DETECTION = settings.get('GEOLOCATION_ENABLE_TOR_DETECTION', True)
ENABLE_VPN_DETECTION = settings.get('GEOLOCATION_ENABLE_VPN_DETECTION', True)
ENABLE_CLOUD_DETECTION = settings.get('GEOLOCATION_ENABLE_CLOUD_DETECTION', True)
TOR_EXIT_NODE_LIST_URL = settings.get('TOR_EXIT_NODE_LIST_URL', 'https://check.torproject.org/exit-addresses')

# Cloud provider IP ranges (ASN-based detection is more reliable, but these help)
CLOUD_PROVIDERS = {
    'AWS': ['amazon', 'aws'],
    'GCP': ['google cloud', 'gcp'],
    'Azure': ['microsoft', 'azure'],
    'DigitalOcean': ['digitalocean'],
    'Linode': ['linode'],
    'OVH': ['ovh'],
    'Hetzner': ['hetzner'],
}


def get_geo_located_ip_model():
    global _GeoLocatedIP
    if _GeoLocatedIP is None:
        from mojo.apps.account.models.geolocated_ip import GeoLocatedIP
        _GeoLocatedIP = GeoLocatedIP
    return _GeoLocatedIP


def detect_tor(ip_address):
    """
    Check if the IP is a known Tor exit node.
    Uses the Tor Project's exit node list.
    """
    if not ENABLE_TOR_DETECTION:
        return False

    try:
        # Check against Tor Project's exit list
        response = requests.get(TOR_EXIT_NODE_LIST_URL, timeout=3)
        if response.status_code == 200:
            exit_nodes = []
            for line in response.text.split('\n'):
                if line.startswith('ExitAddress '):
                    parts = line.split()
                    if len(parts) >= 2:
                        exit_nodes.append(parts[1])
            return ip_address in exit_nodes
    except Exception as e:
        print(f"[Tor Detection Error] Failed to check Tor status for {ip_address}: {e}")

    return False


def detect_vpn_proxy_cloud(asn_org, isp, connection_type):
    """
    Detect VPN, proxy, and cloud services based on ASN organization, ISP, and connection type.
    Returns a dict with is_vpn, is_proxy, is_cloud, is_datacenter flags.
    """
    result = {
        'is_vpn': False,
        'is_proxy': False,
        'is_cloud': False,
        'is_datacenter': False,
    }

    if not asn_org:
        asn_org = ''
    if not isp:
        isp = ''

    combined_text = f"{asn_org} {isp}".lower()

    # VPN detection keywords
    vpn_keywords = [
        'vpn', 'virtual private', 'nordvpn', 'expressvpn', 'surfshark',
        'private internet access', 'pia', 'cyberghost', 'protonvpn',
        'tunnelbear', 'ipvanish', 'purevpn', 'windscribe', 'mullvad',
        'hide.me', 'hotspot shield'
    ]

    # Proxy detection keywords
    proxy_keywords = [
        'proxy', 'anonymizer', 'anonymous', 'squid', 'privoxy'
    ]

    # Cloud provider detection
    if ENABLE_CLOUD_DETECTION:
        for provider, keywords in CLOUD_PROVIDERS.items():
            if any(keyword in combined_text for keyword in keywords):
                result['is_cloud'] = True
                break

    # Datacenter/hosting detection
    datacenter_keywords = [
        'hosting', 'datacenter', 'data center', 'server', 'colocation',
        'colo', 'dedicated', 'vps', 'virtual server', 'cloud'
    ]

    if ENABLE_VPN_DETECTION:
        result['is_vpn'] = any(keyword in combined_text for keyword in vpn_keywords)

    result['is_proxy'] = any(keyword in combined_text for keyword in proxy_keywords)

    # Connection type hints
    if connection_type:
        conn_lower = connection_type.lower()
        if 'hosting' in conn_lower or 'datacenter' in conn_lower:
            result['is_datacenter'] = True
        if 'business' in conn_lower and any(keyword in combined_text for keyword in datacenter_keywords):
            result['is_datacenter'] = True

    # If not already marked as cloud but shows datacenter characteristics
    if not result['is_cloud'] and any(keyword in combined_text for keyword in datacenter_keywords):
        result['is_datacenter'] = True

    return result


def calculate_threat_level(is_tor, is_vpn, is_proxy, threat_data=None):
    """
    Calculate a threat level based on detected characteristics.
    Returns: 'low', 'medium', 'high', or 'critical'
    """
    if is_tor:
        return 'high'  # Tor is often used for anonymity, which could be suspicious

    if threat_data and isinstance(threat_data, dict):
        # If provider returns threat scores, use those
        if threat_data.get('is_threat') or threat_data.get('threat_score', 0) > 75:
            return 'critical'
        elif threat_data.get('threat_score', 0) > 50:
            return 'high'
        elif threat_data.get('threat_score', 0) > 25:
            return 'medium'

    if is_proxy:
        return 'medium'

    if is_vpn:
        return 'low'  # VPNs are common and not necessarily malicious

    return 'low'


def geolocate_ip(ip_address, check_threats=False):
    """
    Fetches geolocation data for a given IP address. It handles both
    public IPs (by calling an external provider) and private IPs.
    Returns a normalized dictionary of geolocation data including
    security detection (Tor, VPN, cloud, etc.).
    """
    # 1. Handle private/reserved IPs
    try:
        ip_obj = ipaddress.ip_address(ip_address)
        if ip_obj.is_private or ip_obj.is_reserved:
            return {
                'provider': 'internal',
                'country_name': 'Private Network',
                'region': 'Private' if ip_obj.is_private else 'Reserved',
                'is_tor': False,
                'is_vpn': False,
                'is_proxy': False,
                'is_cloud': False,
                'is_datacenter': False,
                'is_known_attacker': False,
                'is_known_abuser': False,
                'threat_level': 'low',
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
        geo_data = fetch_function(ip_address, api_key)

        if geo_data:
            # Perform detection
            is_tor = detect_tor(ip_address)
            detection = detect_vpn_proxy_cloud(
                geo_data.get('asn_org'),
                geo_data.get('isp'),
                geo_data.get('connection_type')
            )

            geo_data['is_tor'] = is_tor
            geo_data.update(detection)

            # Perform threat intelligence checks if requested
            if check_threats:
                from .threat_intel import perform_threat_check
                threat_results = perform_threat_check(ip_address)
                geo_data['is_known_attacker'] = threat_results['is_known_attacker']
                geo_data['is_known_abuser'] = threat_results['is_known_abuser']

                # Store threat data in the data field
                if 'data' not in geo_data:
                    geo_data['data'] = {}
                geo_data['data']['threat_data'] = threat_results['threat_data']
            else:
                geo_data['is_known_attacker'] = False
                geo_data['is_known_abuser'] = False

            geo_data['threat_level'] = calculate_threat_level(
                is_tor,
                detection['is_vpn'],
                detection['is_proxy'],
                geo_data.get('data', {}).get('threat', None)
            )

        return geo_data
    else:
        # In a real app, you might want to log this or handle it differently
        print(f"[Geolocation Error] Provider '{provider}' is not supported.")
        return None


def refresh_geolocation_for_ip(ip_address, check_threats=False):
    """
    This function is the entry point for the background task.
    It gets or creates a GeoLocatedIP record and refreshes it if necessary.

    Args:
        ip_address: IP address to refresh
        check_threats: If True, also perform threat intelligence checks
    """
    GeoLocatedIP = get_geo_located_ip_model()

    # Get or create the record, then call its internal refresh logic.
    geo_record, created = GeoLocatedIP.objects.get_or_create(ip_address=ip_address)

    if created or geo_record.is_expired:
        geo_record.refresh(check_threats=check_threats)

    return geo_record


def check_threats_for_ip(ip_address):
    """
    Background task to perform threat intelligence checks on an IP.
    This is separate from geolocation refresh and can be scheduled independently.
    """
    GeoLocatedIP = get_geo_located_ip_model()

    try:
        geo_record = GeoLocatedIP.objects.get(ip_address=ip_address)
        return geo_record.check_threats()
    except GeoLocatedIP.DoesNotExist:
        # Create the record with threat checking
        geo_record = GeoLocatedIP.objects.create(ip_address=ip_address)
        geo_record.refresh(check_threats=True)
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
