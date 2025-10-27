"""
Detection logic for Tor, VPN, Proxy, Cloud services, and Mobile carriers.
"""
import requests
from .config import (
    ENABLE_TOR_DETECTION,
    ENABLE_VPN_DETECTION,
    ENABLE_CLOUD_DETECTION,
    TOR_EXIT_NODE_LIST_URL,
    CLOUD_PROVIDERS,
    MOBILE_CARRIERS,
    VPN_KEYWORDS,
    PROXY_KEYWORDS,
    DATACENTER_KEYWORDS,
)


def detect_tor(ip_address):
    """
    Check if the IP is a known Tor exit node.
    Uses the Tor Project's exit node list.

    Args:
        ip_address: The IP address to check

    Returns:
        bool: True if the IP is a known Tor exit node
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
    Detect VPN, proxy, cloud services, and mobile carriers based on ASN organization,
    ISP, and connection type.

    Args:
        asn_org: ASN organization name
        isp: ISP name
        connection_type: Connection type hint from provider

    Returns:
        dict: Dictionary with detection flags:
            - is_vpn: bool
            - is_proxy: bool
            - is_cloud: bool
            - is_datacenter: bool
            - is_mobile: bool
            - mobile_carrier: str or None
    """
    result = {
        'is_vpn': False,
        'is_proxy': False,
        'is_cloud': False,
        'is_datacenter': False,
        'is_mobile': False,
        'mobile_carrier': None,
    }

    if not asn_org:
        asn_org = ''
    if not isp:
        isp = ''

    combined_text = f"{asn_org} {isp}".lower()

    # Mobile/Cellular carrier detection
    for carrier, keywords in MOBILE_CARRIERS.items():
        if any(keyword in combined_text for keyword in keywords):
            result['is_mobile'] = True
            result['mobile_carrier'] = carrier
            break

    # Cloud provider detection
    if ENABLE_CLOUD_DETECTION:
        for provider, keywords in CLOUD_PROVIDERS.items():
            if any(keyword in combined_text for keyword in keywords):
                result['is_cloud'] = True
                break

    # VPN detection
    if ENABLE_VPN_DETECTION:
        result['is_vpn'] = any(keyword in combined_text for keyword in VPN_KEYWORDS)

    # Proxy detection
    result['is_proxy'] = any(keyword in combined_text for keyword in PROXY_KEYWORDS)

    # Connection type hints
    if connection_type:
        conn_lower = connection_type.lower()
        if 'hosting' in conn_lower or 'datacenter' in conn_lower:
            result['is_datacenter'] = True
        if 'business' in conn_lower and any(keyword in combined_text for keyword in DATACENTER_KEYWORDS):
            result['is_datacenter'] = True
        if 'cellular' in conn_lower or 'mobile' in conn_lower:
            result['is_mobile'] = True

    # If not already marked as cloud but shows datacenter characteristics
    if not result['is_cloud'] and any(keyword in combined_text for keyword in DATACENTER_KEYWORDS):
        result['is_datacenter'] = True

    return result


def calculate_threat_level(is_tor, is_vpn, is_proxy, threat_data=None):
    """
    Calculate a threat level based on detected characteristics.

    Args:
        is_tor: Whether the IP is a Tor exit node
        is_vpn: Whether the IP appears to be a VPN
        is_proxy: Whether the IP appears to be a proxy
        threat_data: Optional threat data from provider

    Returns:
        str: Threat level - 'low', 'medium', 'high', or 'critical'
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
