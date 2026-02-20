# GeoIP and Threat Intelligence System

The Mojo framework includes a comprehensive IP geolocation and threat intelligence system designed to track device locations, detect security threats, and provide actionable intelligence about incoming connections.

## Table of Contents

- [Overview](#overview)
- [Quick Start](#quick-start)
- [Models](#models)
- [Geolocation Features](#geolocation-features)
- [Threat Intelligence](#threat-intelligence)
- [Configuration](#configuration)
- [Usage Examples](#usage-examples)
- [API Endpoints](#api-endpoints)
- [Best Practices](#best-practices)

## Overview

The GeoIP system provides:

- **IP Geolocation**: Location data for IP addresses with caching
- **Device Tracking**: Track user devices and their locations over time
- **Threat Detection**: Identify Tor, VPN, proxies, cloud platforms, and malicious actors
- **Internal Analytics**: Analyze your incident data to identify attackers and abusers
- **External Intelligence**: Integration with blocklists like AbuseIPDB and Blocklist.de
- **Risk Scoring**: Automatic calculation of threat levels and risk scores

## Quick Start

### Basic Geolocation

```python
from mojo.apps.account.models import GeoLocatedIP

# Geolocate an IP address
geo_ip = GeoLocatedIP.geolocate("8.8.8.8")

print(f"Location: {geo_ip.city}, {geo_ip.country_name}")
print(f"Coordinates: {geo_ip.latitude}, {geo_ip.longitude}")
print(f"Timezone: {geo_ip.timezone}")
```

### Device Tracking

```python
from mojo.apps.account.models import UserDevice

# Automatically track devices from a request
device = UserDevice.track(request)

# View device locations
for location in device.locations.all():
    print(f"Seen at: {location.geolocation.city} on {location.last_seen}")
```

### Threat Checking

```python
from mojo.apps.account.models import GeoLocatedIP

geo_ip = GeoLocatedIP.geolocate("1.2.3.4")

# Check for threats
threat_results = geo_ip.check_threats()

if geo_ip.is_known_attacker:
    print(f"⚠️ Known attacker detected! Risk score: {geo_ip.risk_score}")
    
if geo_ip.is_tor:
    print("🧅 Tor exit node detected")
```

## Models

### GeoLocatedIP

The core model that caches geolocation data and threat intelligence for IP addresses.

**Location Fields:**
- `ip_address` - The IP address (unique, indexed)
- `subnet` - Network subnet for the IP
- `country_code` - ISO country code
- `country_name` - Full country name
- `region` - State/province/region
- `city` - City name
- `postal_code` - Postal/ZIP code
- `latitude` / `longitude` - Geographic coordinates
- `timezone` - IANA timezone identifier

**Security Detection Fields:**
- `is_tor` - Tor exit node
- `is_vpn` - VPN service
- `is_proxy` - Proxy server
- `is_cloud` - Cloud platform (AWS, GCP, Azure, etc.)
- `is_datacenter` - Datacenter/hosting provider
- `is_known_attacker` - High-severity threat actor
- `is_known_abuser` - Abuse pattern detected

**Network Information:**
- `asn` - Autonomous System Number
- `asn_org` - Organization owning the ASN
- `isp` - Internet Service Provider
- `connection_type` - Connection classification (residential, business, hosting, etc.)

**Metadata:**
- `threat_level` - Risk classification: `low`, `medium`, `high`, `critical`
- `provider` - Geolocation data provider (ipinfo, ipstack, etc.)
- `data` - Raw JSON data from provider and threat intelligence
- `created` / `modified` / `last_seen` - Timestamp tracking
- `expires_at` - When cached data expires

**Methods:**

```python
# Geolocate an IP (class method)
geo_ip = GeoLocatedIP.geolocate(ip_address, auto_refresh=False, subdomain_only=False)

# Refresh geolocation data
geo_ip.refresh(check_threats=False)

# Perform threat intelligence check
threat_data = geo_ip.check_threats()

# Properties
is_suspicious = geo_ip.is_suspicious  # True if any threat flags
risk = geo_ip.risk_score  # 0-100 risk score
expired = geo_ip.is_expired  # True if cache expired
```

### UserDevice

Represents a unique device used by a user.

**Fields:**
- `user` - Foreign key to User
- `duid` - Device unique identifier
- `device_info` - Parsed user agent data (browser, OS, device type)
- `user_agent_hash` - SHA256 hash of user agent string
- `last_ip` - Most recent IP address
- `first_seen` / `last_seen` - Timestamp tracking

**Methods:**

```python
# Track a device from a request (class method)
device = UserDevice.track(request, user=None)

# Access device locations
locations = device.locations.all()
```

### UserDeviceLocation

Links devices to IP addresses and tracks when/where devices are used.

**Fields:**
- `user` - Foreign key to User
- `user_device` - Foreign key to UserDevice
- `ip_address` - IP address used
- `geolocation` - Foreign key to GeoLocatedIP
- `first_seen` / `last_seen` - Timestamp tracking

**Methods:**

```python
# Track a location (class method)
location = UserDeviceLocation.track(device, ip_address)
```

## Geolocation Features

### Supported Providers

The system supports multiple geolocation providers with automatic failover:

- **IPInfo.io** - Recommended, includes ASN data
- **IPStack** - Requires API key
- **IP-API.com** - Free tier available
- **MaxMind GeoIP2** - Placeholder for future integration

### Private IP Handling

Private and reserved IP addresses (RFC 1918, loopback, etc.) are automatically detected and marked with provider `internal`:

```python
geo_ip = GeoLocatedIP.geolocate("192.168.1.1")
# Returns: country_name="Private Network", region="Private"
```

### Subnet Lookback

When enabled, the system can use subnet-level data for IPs without exact matches:

```python
# settings.py
GEOLOCATION_ALLOW_SUBNET_LOOKUP = True

# If 192.168.1.100 isn't cached but 192.168.1.50 is,
# the system will use the subnet data
geo_ip = GeoLocatedIP.geolocate("192.168.1.100")
```

### Caching and Expiration

Geolocation data is cached to reduce API calls:

- Cache duration: 30 days (configurable)
- Automatic refresh when expired
- Background task processing to avoid blocking requests
- Internal/private IPs never expire

```python
# Check if data is expired
if geo_ip.is_expired:
    # Trigger background refresh
    from mojo.apps.account.models.geolocated_ip import trigger_refresh_task
    trigger_refresh_task(geo_ip.ip_address)
```

## Threat Intelligence

### Internal Threat Detection

The system analyzes your `Event` model to identify patterns of malicious activity:

**Known Attackers:**
- IPs with 5+ high-severity events (level 8+) in the lookback period
- Automatically flagged as `is_known_attacker=True`

**Known Abusers:**
- IPs with 10+ medium-severity events (level 4-7)
- Pattern of abuse without critical attacks
- Flagged as `is_known_abuser=True`

**Internal Statistics Tracked:**
```python
geo_ip.check_threats()

# View internal threat data
stats = geo_ip.data['threat_data']['internal']
print(f"Total events: {stats['total_events']}")
print(f"High severity: {stats['high_severity_events']}")
print(f"Average level: {stats['avg_level']}")
print(f"Top categories: {stats['top_categories']}")
```

### External Blocklists

#### AbuseIPDB Integration

[AbuseIPDB](https://www.abuseipdb.com/) is a community-driven IP reputation database:

```python
# settings.py
THREAT_INTEL_ABUSEIPDB_ENABLED = True
THREAT_INTEL_ABUSEIPDB_API_KEY = "your-api-key-here"
```

AbuseIPDB provides:
- Abuse confidence score (0-100)
- Total reports count
- Usage type and domain info
- Free tier: 1,000 checks/day

#### Blocklist.de Integration

[Blocklist.de](https://www.blocklist.de/) is a free community blocklist:

```python
# settings.py
THREAT_INTEL_BLOCKLIST_DE_ENABLED = True
```

Note: In production, you should cache this list and refresh periodically rather than checking on every request.

### Anonymity Detection

The system detects various anonymization technologies:

**Tor Detection:**
- Checks against Tor Project's official exit node list
- Real-time validation
- Automatic updates from `https://check.torproject.org/exit-addresses`

**VPN Detection:**
- Keyword matching against ASN/ISP data
- Detects major VPN providers: NordVPN, ExpressVPN, ProtonVPN, etc.
- Configurable keyword list

**Proxy Detection:**
- Identifies proxy servers and anonymizers
- Based on ASN organization names

**Cloud Platform Detection:**
- Identifies major cloud providers: AWS, GCP, Azure, DigitalOcean, Linode, OVH, Hetzner
- Useful for detecting bot traffic and automated attacks

### Threat Levels

Automatically calculated based on all available threat data:

| Level | Score | Description |
|-------|-------|-------------|
| `low` | 0-24 | Normal traffic, minimal risk |
| `medium` | 25-49 | Elevated risk, VPN/proxy usage |
| `high` | 50-74 | Significant risk, Tor or known abuser |
| `critical` | 75-100 | Severe risk, known attacker or blocklisted |

**Risk Score Calculation:**

```python
risk_score = geo_ip.risk_score  # 0-100

# Factors:
# +50 points: Known attacker
# +40 points: Tor exit node
# +30 points: Known abuser or blocklisted
# +25 points: Proxy server
# +20 points: VPN service
# +10-20 points: High-severity event history
```

### Background Processing

Threat checks can be expensive, so they're designed to run in the background:

```python
from mojo.apps.account.models.geolocated_ip import (
    trigger_refresh_task,
    trigger_threat_check_task
)

# Refresh geolocation with threat check
trigger_refresh_task(ip_address, check_threats=True)

# Standalone threat check
trigger_threat_check_task(ip_address)
```

## Configuration

### Required Settings

```python
# settings.py or settings.yaml

# Geolocation Providers (at least one required)
GEOLOCATION_PROVIDERS = ['ipinfo', 'ipstack', 'ip-api']

# Provider API Keys (if required)
GEOLOCATION_API_KEY_IPINFO = "your-token"
GEOLOCATION_API_KEY_IPSTACK = "your-access-key"
```

### Optional Settings

```python
# Geolocation Cache
GEOLOCATION_CACHE_DURATION_DAYS = 30  # How long to cache geolocation data
GEOLOCATION_ALLOW_SUBNET_LOOKUP = False  # Use subnet data for cache misses
GEOLOCATION_DEVICE_LOCATION_AGE = 300  # Seconds before updating device location

# Detection Features
GEOLOCATION_ENABLE_TOR_DETECTION = True
GEOLOCATION_ENABLE_VPN_DETECTION = True
GEOLOCATION_ENABLE_CLOUD_DETECTION = True
TOR_EXIT_NODE_LIST_URL = 'https://check.torproject.org/exit-addresses'

# Threat Intelligence
GEOLOCATION_ENABLE_BLOCKLIST_CHECK = True
GEOLOCATION_ENABLE_INTERNAL_THREAT_CHECK = True
GEOLOCATION_INTERNAL_THREAT_LOOKBACK_DAYS = 90
GEOLOCATION_INTERNAL_THREAT_EVENT_THRESHOLD = 5
GEOLOCATION_INTERNAL_ATTACKER_LEVEL_THRESHOLD = 8

# External Blocklists
THREAT_INTEL_ABUSEIPDB_ENABLED = False
THREAT_INTEL_ABUSEIPDB_API_KEY = None
THREAT_INTEL_BLOCKLIST_DE_ENABLED = True
```

## Usage Examples

### Example 1: Rate Limiting Based on Threat Level

```python
from mojo.apps.account.models import GeoLocatedIP

def check_rate_limit(request):
    geo_ip = GeoLocatedIP.geolocate(request.ip)
    
    # Adjust rate limits based on threat level
    limits = {
        'low': 1000,      # requests per hour
        'medium': 500,
        'high': 100,
        'critical': 10
    }
    
    limit = limits.get(geo_ip.threat_level, 100)
    return apply_rate_limit(request.ip, limit)
```

### Example 2: Additional Authentication for Suspicious IPs

```python
from mojo.apps.account.models import GeoLocatedIP

def login_view(request):
    geo_ip = GeoLocatedIP.geolocate(request.ip)
    
    # Require 2FA for suspicious locations
    if geo_ip.is_suspicious or geo_ip.is_tor:
        request.session['require_2fa'] = True
        request.session['threat_reason'] = (
            "Login from suspicious location detected"
        )
    
    return authenticate_user(request)
```

### Example 3: Security Dashboard

```python
from mojo.apps.account.models import GeoLocatedIP
from datetime import timedelta
from mojo.helpers import dates

def get_security_metrics():
    now = dates.utcnow()
    last_24h = now - timedelta(hours=24)
    
    return {
        'active_threats': GeoLocatedIP.objects.filter(
            threat_level__in=['high', 'critical'],
            last_seen__gte=last_24h
        ).count(),
        
        'tor_connections': GeoLocatedIP.objects.filter(
            is_tor=True,
            last_seen__gte=last_24h
        ).count(),
        
        'known_attackers': GeoLocatedIP.objects.filter(
            is_known_attacker=True,
            last_seen__gte=last_24h
        ).count(),
        
        'top_threat_countries': GeoLocatedIP.objects.filter(
            threat_level='critical',
            last_seen__gte=last_24h
        ).values('country_code', 'country_name').annotate(
            count=models.Count('id')
        ).order_by('-count')[:10]
    }
```

### Example 4: Automatic IP Blocking

```python
from mojo.apps.account.models import GeoLocatedIP

def should_block_ip(ip_address):
    geo_ip = GeoLocatedIP.geolocate(ip_address)
    
    # Check threats in background
    if not geo_ip.data.get('threat_data'):
        from mojo.apps.account.models.geolocated_ip import trigger_threat_check_task
        trigger_threat_check_task(ip_address)
    
    # Block known attackers immediately
    if geo_ip.is_known_attacker:
        return True, "Known attacker"
    
    # Block high-risk IPs
    if geo_ip.risk_score >= 75:
        return True, f"High risk score: {geo_ip.risk_score}"
    
    # Block if on multiple blocklists
    threat_data = geo_ip.data.get('threat_data', {})
    blocklist_hits = len(threat_data.get('blocklists', []))
    if blocklist_hits >= 2:
        return True, f"Multiple blocklist hits: {blocklist_hits}"
    
    return False, None
```

### Example 5: Alerting on New Threats

```python
from mojo.apps.account.models import GeoLocatedIP
from mojo.apps.incident.models import Event

def on_high_severity_event(event):
    """Called when a high-severity security event occurs"""
    
    if not event.source_ip:
        return
    
    geo_ip = GeoLocatedIP.geolocate(event.source_ip)
    
    # Trigger threat check
    threat_data = geo_ip.check_threats()
    
    # Alert security team if this is a new attacker
    if geo_ip.is_known_attacker:
        stats = threat_data['threat_data']['internal']
        send_security_alert(
            title=f"Known Attacker Active: {event.source_ip}",
            details=f"""
            Location: {geo_ip.city}, {geo_ip.country_name}
            Total Events: {stats['total_events']}
            High Severity: {stats['high_severity_events']}
            Risk Score: {geo_ip.risk_score}/100
            Threat Level: {geo_ip.threat_level}
            
            Flags:
            - Tor: {geo_ip.is_tor}
            - VPN: {geo_ip.is_vpn}
            - Cloud: {geo_ip.is_cloud}
            """,
            priority='high'
        )
```

### Example 6: Bulk Threat Analysis

```python
from mojo.apps.account.models import GeoLocatedIP
from datetime import timedelta
from mojo.helpers import dates

def analyze_recent_ips():
    """Analyze all IPs seen in the last 7 days"""
    
    cutoff = dates.utcnow() - timedelta(days=7)
    recent_ips = GeoLocatedIP.objects.filter(last_seen__gte=cutoff)
    
    # Trigger threat checks for IPs without recent checks
    for geo_ip in recent_ips:
        threat_data = geo_ip.data.get('threat_data', {})
        last_check = threat_data.get('threat_checked_at')
        
        if not last_check or is_stale(last_check):
            from mojo.apps.account.models.geolocated_ip import trigger_threat_check_task
            trigger_threat_check_task(geo_ip.ip_address)
    
    # Generate report
    return {
        'total_ips': recent_ips.count(),
        'critical': recent_ips.filter(threat_level='critical').count(),
        'high': recent_ips.filter(threat_level='high').count(),
        'tor_nodes': recent_ips.filter(is_tor=True).count(),
        'known_attackers': recent_ips.filter(is_known_attacker=True).count(),
        'cloud_platforms': recent_ips.filter(is_cloud=True).count(),
    }
```

## API Endpoints

The system provides REST endpoints for accessing geolocation and device data:

### GeoLocatedIP Endpoints

```bash
# List/search geolocated IPs
GET /api/system/geoip

# Get specific IP record
GET /api/system/geoip/<id>

# Lookup IP address
GET /api/system/geoip/lookup?ip=8.8.8.8&auto_refresh=true

# Update IP record
PUT /api/system/geoip/<id>
```

**Permissions:** Requires `manage_users` permission

### UserDevice Endpoints

```bash
# List user devices
GET /api/user/device

# Get specific device
GET /api/user/device/<id>

# Lookup device by DUID
GET /api/user/device/lookup?duid=device-unique-id

# List device locations
GET /api/user/device/location
```

**Permissions:** 
- Viewing: `manage_users` or device owner
- Lookup: `manage_users` or `manage_devices`

### REST API Example

```python
import requests

# Lookup an IP address via API
response = requests.get(
    'https://your-app.com/api/system/geoip/lookup',
    params={'ip': '1.2.3.4', 'auto_refresh': True},
    headers={'Authorization': 'Bearer your-token'}
)

data = response.json()
print(f"Location: {data['city']}, {data['country_name']}")
print(f"Threat Level: {data['threat_level']}")
print(f"Is Tor: {data['is_tor']}")
```

## Best Practices

### 1. Background Processing

Always perform threat checks in the background to avoid blocking user requests:

```python
# ✅ Good - non-blocking
geo_ip = GeoLocatedIP.geolocate(ip_address)
if geo_ip.is_expired or not geo_ip.data.get('threat_data'):
    trigger_threat_check_task(ip_address)

# ❌ Bad - blocks the request
geo_ip = GeoLocatedIP.geolocate(ip_address)
geo_ip.check_threats()  # This makes API calls!
```

### 2. Cache Threat Data

Don't check threats on every request - use the cached data:

```python
# Check if threat data is recent
threat_data = geo_ip.data.get('threat_data', {})
last_check = threat_data.get('threat_checked_at')

# Only refresh if older than 24 hours
if not last_check or (dates.utcnow() - parse_iso(last_check)) > timedelta(hours=24):
    trigger_threat_check_task(ip_address)
```

### 3. Graduated Response

Don't block immediately - use graduated responses based on threat level:

```python
if geo_ip.threat_level == 'critical':
    # Block completely
    return HttpResponseForbidden("Access denied")
elif geo_ip.threat_level == 'high':
    # Require CAPTCHA or 2FA
    request.session['require_verification'] = True
elif geo_ip.threat_level == 'medium':
    # Apply stricter rate limits
    apply_rate_limit(ip_address, limit=100)
```

### 4. Monitor False Positives

Legitimate users may use VPNs or cloud platforms:

```python
# Allow users to report false positives
def report_false_positive(user, ip_address):
    geo_ip = GeoLocatedIP.objects.get(ip_address=ip_address)
    
    # Store user feedback
    if not geo_ip.data:
        geo_ip.data = {}
    geo_ip.data.setdefault('user_reports', []).append({
        'user_id': user.id,
        'type': 'false_positive',
        'timestamp': dates.utcnow().isoformat()
    })
    geo_ip.save()
```

### 5. Regular Cleanup

Clean up old geolocation data periodically:

```python
from datetime import timedelta
from mojo.helpers import dates

# Delete IPs not seen in 90+ days
cutoff = dates.utcnow() - timedelta(days=90)
GeoLocatedIP.objects.filter(
    last_seen__lt=cutoff,
    is_known_attacker=False,  # Keep attacker records
    is_known_abuser=False
).delete()
```

### 6. Privacy Considerations

Geolocation data can be sensitive. Follow privacy best practices:

- Only store geolocation data as long as necessary
- Respect user privacy settings and requests
- Comply with GDPR and other privacy regulations
- Provide users access to their location history
- Allow users to clear their location data

```python
# Example: User privacy controls
def clear_user_location_data(user):
    """Clear location data for a user"""
    UserDeviceLocation.objects.filter(user=user).delete()
    UserDevice.objects.filter(user=user).delete()
```

### 7. API Rate Limits

Most geolocation providers have rate limits. Implement caching and fallbacks:

```python
# Use multiple providers with fallback
GEOLOCATION_PROVIDERS = ['ipinfo', 'ip-api', 'ipstack']

# Respect API limits
GEOLOCATION_CACHE_DURATION_DAYS = 30  # Cache for 30 days

# Monitor usage
if settings.DEBUG:
    # Log API calls in development
    print(f"[GeoIP] Fetching data for {ip_address} from {provider}")
```

### 8. Testing

Test with known IPs to verify functionality:

```python
# Test cases
test_ips = {
    '8.8.8.8': {'expected_provider': 'Google', 'is_cloud': True},
    '185.220.101.1': {'expected_tor': True},  # Known Tor exit node
    '127.0.0.1': {'expected_private': True},
}

for ip, expected in test_ips.items():
    geo_ip = GeoLocatedIP.geolocate(ip)
    # Assert expectations...
```

---

## Support and Contributing

For issues, questions, or contributions related to the GeoIP system, please refer to the main Mojo framework documentation.

### Related Documentation

- [Device Tracking](./device-tracking.md)
- [Incident Management](./incidents.md)
- [Security Best Practices](./security.md)
