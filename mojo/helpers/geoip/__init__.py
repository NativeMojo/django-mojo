"""
GeoIP lookup service with primary/fallback provider support.

This is a pure service module that fetches geolocation data for IP addresses.
It has no knowledge of Django models - it simply returns dictionaries of data.

Public API:
    - geolocate_ip(ip_address, check_threats=False)
"""
import ipaddress
from . import config
from . import detection
from . import ipinfo
from . import ipstack
from . import ipapi
from . import maxmind


# Provider registry
PROVIDERS = {
    'maxmind': maxmind.fetch,
    'ipinfo': ipinfo.fetch,
    'ipstack': ipstack.fetch,
    'ip-api': ipapi.fetch,
}


def geolocate_ip(ip_address, check_threats=False):
    """
    Fetches geolocation data for a given IP address using primary/fallback strategy.

    This is a pure service function that returns a dictionary. It does not interact
    with any Django models.

    Strategy:
        1. Try primary provider (default: maxmind)
        2. If primary fails, try fallback provider (default: ipinfo)
        3. If both fail, try additional providers in configured order

    Args:
        ip_address: The IP address to geolocate
        check_threats: If True, perform threat intelligence checks

    Returns:
        dict: Normalized geolocation data with detection flags, or None on failure

    Example return:
        {
            'provider': 'maxmind',
            'country_code': 'US',
            'country_name': 'United States',
            'region': 'California',
            'city': 'San Francisco',
            'latitude': 37.7749,
            'longitude': -122.4194,
            'timezone': 'America/Los_Angeles',
            'asn': 'AS15169',
            'asn_org': 'Google LLC',
            'isp': 'Google LLC',
            'connection_type': 'Corporate',
            'is_tor': False,
            'is_vpn': False,
            'is_proxy': False,
            'is_cloud': True,
            'is_datacenter': True,
            'is_mobile': False,
            'mobile_carrier': None,
            'is_known_attacker': False,
            'is_known_abuser': False,
            'threat_level': 'low',
            'data': {...}  # Raw provider response
        }
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
                'is_mobile': False,
                'mobile_carrier': None,
                'is_known_attacker': False,
                'is_known_abuser': False,
                'threat_level': 'low',
            }
    except ValueError:
        return None  # Invalid IP

    # 2. Build provider order: primary -> fallback -> additional
    provider_order = []

    primary = config.PRIMARY_PROVIDER
    fallback = config.FALLBACK_PROVIDER
    additional = config.ADDITIONAL_PROVIDERS or []

    if primary:
        provider_order.append(primary)
    if fallback and fallback != primary:
        provider_order.append(fallback)

    # Add additional providers that aren't already in the list
    for provider in additional:
        if provider not in provider_order:
            provider_order.append(provider)

    # 3. Try each provider in order
    geo_data = None
    errors = []

    for provider_name in provider_order:
        fetch_function = PROVIDERS.get(provider_name)

        if not fetch_function:
            errors.append(f"{provider_name}: not supported")
            continue

        try:
            geo_data = fetch_function(ip_address)

            if geo_data:
                # Success! Break out of loop
                break
            else:
                errors.append(f"{provider_name}: returned no data")
        except Exception as e:
            errors.append(f"{provider_name}: {str(e)}")
            continue

    # 4. If we got data from any provider, enhance it with detections
    if geo_data:
        # Perform Tor detection
        is_tor = detection.detect_tor(ip_address)

        # Check if MaxMind already provided Tor detection
        if geo_data.get('data', {}).get('is_tor_exit_node') is not None:
            is_tor = geo_data['data']['is_tor_exit_node']

        # Perform VPN/Proxy/Cloud detection
        vpn_proxy_cloud = detection.detect_vpn_proxy_cloud(
            geo_data.get('asn_org'),
            geo_data.get('isp'),
            geo_data.get('connection_type')
        )

        # Check if MaxMind already provided VPN detection
        if geo_data.get('data', {}).get('is_anonymous_vpn') is not None:
            vpn_proxy_cloud['is_vpn'] = geo_data['data']['is_anonymous_vpn']

        # Check if MaxMind already provided proxy detection
        if geo_data.get('data', {}).get('is_public_proxy') is not None:
            vpn_proxy_cloud['is_proxy'] = geo_data['data']['is_public_proxy']

        # Check if MaxMind already provided hosting/datacenter detection
        if geo_data.get('data', {}).get('is_hosting_provider') is not None:
            vpn_proxy_cloud['is_datacenter'] = geo_data['data']['is_hosting_provider']

        # Add detection results to geo_data
        geo_data['is_tor'] = is_tor
        geo_data.update(vpn_proxy_cloud)

        # Perform threat intelligence checks if requested
        if check_threats:
            try:
                from . import threat_intel
                threat_results = threat_intel.perform_threat_check(ip_address)
                geo_data['is_known_attacker'] = threat_results['is_known_attacker']
                geo_data['is_known_abuser'] = threat_results['is_known_abuser']

                # Store threat data in the data field
                if 'data' not in geo_data:
                    geo_data['data'] = {}
                geo_data['data']['threat_data'] = threat_results['threat_data']
            except ImportError:
                # threat_intel module not available
                geo_data['is_known_attacker'] = False
                geo_data['is_known_abuser'] = False
        else:
            geo_data['is_known_attacker'] = False
            geo_data['is_known_abuser'] = False

        # Calculate threat level
        geo_data['threat_level'] = detection.calculate_threat_level(
            is_tor,
            vpn_proxy_cloud['is_vpn'],
            vpn_proxy_cloud['is_proxy'],
            geo_data.get('data', {}).get('threat', None)
        )

        return geo_data
    else:
        # All providers failed
        print(f"[GeoIP Error] All providers failed for IP {ip_address}: {'; '.join(errors)}")
        return None


# Public API exports
__all__ = [
    'geolocate_ip',
]
