"""
DEPRECATED: This module has been refactored into mojo.helpers.geoip

This file provides backward compatibility. Please update your imports to:
    from mojo.helpers.geoip import geolocate_ip

The old monolithic geolocation.py has been split into a cleaner modular structure:
    - mojo.helpers.geoip.config - Configuration and constants
    - mojo.helpers.geoip.detection - Tor/VPN/Proxy/Cloud detection
    - mojo.helpers.geoip.threat_intel - Threat intelligence checks
    - mojo.helpers.geoip.ipinfo - IPInfo provider
    - mojo.helpers.geoip.ipstack - IPStack provider
    - mojo.helpers.geoip.ipapi - IP-API provider
    - mojo.helpers.geoip.maxmind - MaxMind provider
    - mojo.helpers.geoip - Main orchestration with primary/fallback logic
"""

# Import from the new location for backward compatibility
from mojo.helpers.geoip import geolocate_ip

__all__ = ['geolocate_ip']
