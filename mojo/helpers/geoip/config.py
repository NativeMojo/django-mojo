"""
Configuration and constants for GeoIP services.
"""
from mojo.helpers.settings import settings

# Provider configuration
PRIMARY_PROVIDER = settings.get_static('GEOIP_PRIMARY_PROVIDER', 'maxmind')
FALLBACK_PROVIDER = settings.get_static('GEOIP_FALLBACK_PROVIDER', 'ipinfo')

# Additional fallback providers (tried in order if primary and fallback fail)
ADDITIONAL_PROVIDERS = settings.get_static('GEOIP_ADDITIONAL_PROVIDERS', ['ipstack', 'ip-api'])

# Detection settings
ENABLE_TOR_DETECTION = settings.get_static('GEOIP_ENABLE_TOR_DETECTION', True)
ENABLE_VPN_DETECTION = settings.get_static('GEOIP_ENABLE_VPN_DETECTION', True)
ENABLE_CLOUD_DETECTION = settings.get_static('GEOIP_ENABLE_CLOUD_DETECTION', True)
TOR_EXIT_NODE_LIST_URL = settings.get_static('TOR_EXIT_NODE_LIST_URL', 'https://check.torproject.org/exit-addresses')


def get_api_key(provider):
    """Get API key/credentials for a provider at call time (may be DB-backed)."""
    key_map = {
        'ipinfo': 'GEOIP_API_KEY_IPINFO',
        'ipstack': 'GEOIP_API_KEY_IPSTACK',
        'ip-api': 'GEOIP_API_KEY_IP-API',
        'maxmind_account_id': 'MAXMIND_ACCOUNT_ID',
        'maxmind_license_key': 'MAXMIND_LICENSE_KEY',
    }
    setting_name = key_map.get(provider)
    if not setting_name:
        return None
    return settings.get(setting_name)

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

# Mobile/Cellular carrier detection keywords
MOBILE_CARRIERS = {
    # US Carriers
    'Verizon': ['verizon', 'cellco'],
    'AT&T': ['at&t', 'att mobility', 'att wireless'],
    'T-Mobile': ['t-mobile', 'tmobile', 'sprint'],  # Sprint merged with T-Mobile
    'Sprint': ['sprint'],
    'US Cellular': ['us cellular', 'uscellular'],
    'Cricket': ['cricket'],
    'Boost': ['boost mobile'],
    'Metro': ['metro pcs', 'metropcs'],

    # International Carriers
    'Vodafone': ['vodafone'],
    'Orange': ['orange'],
    'Telefonica': ['telefonica', 'movistar'],
    'Deutsche Telekom': ['deutsche telekom', 't-systems'],
    'Telstra': ['telstra'],
    'Rogers': ['rogers'],
    'Bell': ['bell canada', 'bell mobility'],
    'Telus': ['telus'],
    'O2': ['o2-uk', 'o2 uk'],
    'EE': ['ee limited'],
    'Three': ['three uk', 'hutchison'],
    'Sky': ['sky mobile'],

    # Generic mobile indicators
    'Mobile': ['mobile', 'cellular', 'wireless', 'telecom', '4g', '5g', 'lte']
}

# VPN detection keywords
VPN_KEYWORDS = [
    'vpn', 'virtual private', 'nordvpn', 'expressvpn', 'surfshark',
    'private internet access', 'pia', 'cyberghost', 'protonvpn',
    'tunnelbear', 'ipvanish', 'purevpn', 'windscribe', 'mullvad',
    'hide.me', 'hotspot shield'
]

# Proxy detection keywords
PROXY_KEYWORDS = [
    'proxy', 'anonymizer', 'anonymous', 'squid', 'privoxy'
]

# Datacenter/hosting detection keywords
DATACENTER_KEYWORDS = [
    'hosting', 'datacenter', 'data center', 'server', 'colocation',
    'colo', 'dedicated', 'vps', 'virtual server', 'cloud'
]
