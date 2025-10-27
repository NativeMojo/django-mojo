"""
Threat Intelligence module for checking IPs against various blocklists and
internal incident data.
"""
import requests
from mojo.helpers.settings import settings
from mojo.helpers import dates

# Threat checking settings
ENABLE_BLOCKLIST_CHECK = settings.get('GEOLOCATION_ENABLE_BLOCKLIST_CHECK', True)
ENABLE_INTERNAL_THREAT_CHECK = settings.get('GEOLOCATION_ENABLE_INTERNAL_THREAT_CHECK', True)
INTERNAL_THREAT_LOOKBACK_DAYS = settings.get('GEOLOCATION_INTERNAL_THREAT_LOOKBACK_DAYS', 90)
INTERNAL_THREAT_EVENT_THRESHOLD = settings.get('GEOLOCATION_INTERNAL_THREAT_EVENT_THRESHOLD', 5)
INTERNAL_ATTACKER_LEVEL_THRESHOLD = settings.get('GEOLOCATION_INTERNAL_ATTACKER_LEVEL_THRESHOLD', 8)

# Public blocklists (free services)
BLOCKLISTS = {
    'abuseipdb': {
        'enabled': settings.get('THREAT_INTEL_ABUSEIPDB_ENABLED', False),
        'api_key': settings.get('THREAT_INTEL_ABUSEIPDB_API_KEY', None),
        'url': 'https://api.abuseipdb.com/api/v2/check',
    },
    'blocklist_de': {
        'enabled': settings.get('THREAT_INTEL_BLOCKLIST_DE_ENABLED', True),
        'url': 'https://lists.blocklist.de/lists/all.txt',  # Updated periodically
    },
    'spamhaus': {
        'enabled': settings.get('THREAT_INTEL_SPAMHAUS_ENABLED', False),
        # Spamhaus requires DNS-based lookup or paid API
    }
}


def check_internal_threats(ip_address):
    """
    Check internal incident database for threats from this IP.
    Returns dict with is_known_attacker, is_known_abuser, and stats.
    """
    if not ENABLE_INTERNAL_THREAT_CHECK:
        return {
            'is_known_attacker': False,
            'is_known_abuser': False,
            'internal_stats': {}
        }

    try:
        from mojo.apps.incident.models.event import Event
        from datetime import timedelta

        lookback_date = dates.utcnow() - timedelta(days=INTERNAL_THREAT_LOOKBACK_DAYS)

        # Get all events from this IP in the lookback period
        events = Event.objects.filter(
            source_ip=ip_address,
            created__gte=lookback_date
        )

        total_events = events.count()

        if total_events == 0:
            return {
                'is_known_attacker': False,
                'is_known_abuser': False,
                'internal_stats': {'total_events': 0}
            }

        # Calculate threat metrics
        high_severity_events = events.filter(level__gte=INTERNAL_ATTACKER_LEVEL_THRESHOLD).count()
        avg_level = events.aggregate(avg_level=models.Avg('level'))['avg_level'] or 0

        # Get category breakdown
        from django.db import models
        category_counts = events.values('category').annotate(
            count=models.Count('id')
        ).order_by('-count')[:5]

        # Get most recent event
        recent_event = events.order_by('-created').first()

        # Determine if known attacker (high severity events)
        is_known_attacker = high_severity_events >= INTERNAL_THREAT_EVENT_THRESHOLD

        # Determine if known abuser (lots of low-medium events)
        is_known_abuser = (
            total_events >= INTERNAL_THREAT_EVENT_THRESHOLD * 2 and
            avg_level < INTERNAL_ATTACKER_LEVEL_THRESHOLD and
            avg_level >= 4  # Warning level
        )

        stats = {
            'total_events': total_events,
            'high_severity_events': high_severity_events,
            'avg_level': round(avg_level, 2),
            'top_categories': list(category_counts),
            'last_seen_event': recent_event.created.isoformat() if recent_event else None,
            'lookback_days': INTERNAL_THREAT_LOOKBACK_DAYS
        }

        return {
            'is_known_attacker': is_known_attacker,
            'is_known_abuser': is_known_abuser,
            'internal_stats': stats
        }

    except Exception as e:
        print(f"[Threat Intel] Error checking internal threats for {ip_address}: {e}")
        return {
            'is_known_attacker': False,
            'is_known_abuser': False,
            'internal_stats': {'error': str(e)}
        }


def check_abuseipdb(ip_address):
    """
    Check IP against AbuseIPDB service.
    Free tier: 1,000 checks per day
    """
    config = BLOCKLISTS['abuseipdb']
    if not config['enabled'] or not config['api_key']:
        return None

    try:
        headers = {
            'Accept': 'application/json',
            'Key': config['api_key']
        }
        params = {
            'ipAddress': ip_address,
            'maxAgeInDays': 90,
            'verbose': ''
        }

        response = requests.get(
            config['url'],
            headers=headers,
            params=params,
            timeout=5
        )

        if response.status_code == 200:
            data = response.json().get('data', {})

            abuse_confidence_score = data.get('abuseConfidenceScore', 0)
            total_reports = data.get('totalReports', 0)

            return {
                'source': 'abuseipdb',
                'is_listed': abuse_confidence_score > 25,  # Configurable threshold
                'confidence_score': abuse_confidence_score,
                'total_reports': total_reports,
                'is_public': data.get('isPublic', True),
                'usage_type': data.get('usageType'),
                'domain': data.get('domain'),
            }
    except Exception as e:
        print(f"[Threat Intel] AbuseIPDB check failed for {ip_address}: {e}")

    return None


def check_blocklist_de(ip_address):
    """
    Check IP against blocklist.de
    This is a simple text file check - in production you'd cache this list.
    """
    config = BLOCKLISTS['blocklist_de']
    if not config['enabled']:
        return None

    try:
        # Note: In production, cache this list and refresh periodically
        response = requests.get(config['url'], timeout=5)
        if response.status_code == 200:
            blocklist = response.text.split('\n')
            is_listed = ip_address in blocklist

            return {
                'source': 'blocklist.de',
                'is_listed': is_listed
            }
    except Exception as e:
        print(f"[Threat Intel] Blocklist.de check failed for {ip_address}: {e}")

    return None


def check_all_blocklists(ip_address):
    """
    Check IP against all enabled blocklists.
    Returns aggregated results.
    """
    if not ENABLE_BLOCKLIST_CHECK:
        return {
            'blocklist_hits': [],
            'is_blocklisted': False
        }

    results = []

    # Check AbuseIPDB
    abuseipdb_result = check_abuseipdb(ip_address)
    if abuseipdb_result:
        results.append(abuseipdb_result)

    # Check Blocklist.de
    blocklist_de_result = check_blocklist_de(ip_address)
    if blocklist_de_result and blocklist_de_result['is_listed']:
        results.append(blocklist_de_result)

    # Determine if IP is on any blocklist
    is_blocklisted = any(
        result.get('is_listed', False)
        for result in results
    )

    return {
        'blocklist_hits': results,
        'is_blocklisted': is_blocklisted
    }


def perform_threat_check(ip_address):
    """
    Perform comprehensive threat check on an IP address.
    This is the main entry point for threat intelligence.

    Returns dict with:
    - is_known_attacker: Based on internal high-severity events
    - is_known_abuser: Based on internal abuse patterns
    - is_blocklisted: Listed on external blocklists
    - threat_data: Detailed threat intelligence
    """
    # Check internal incident database
    internal_threats = check_internal_threats(ip_address)

    # Check external blocklists
    blocklist_results = check_all_blocklists(ip_address)

    # Aggregate results
    result = {
        'is_known_attacker': internal_threats['is_known_attacker'],
        'is_known_abuser': internal_threats['is_known_abuser'],
        'is_blocklisted': blocklist_results['is_blocklisted'],
        'threat_data': {
            'internal': internal_threats['internal_stats'],
            'blocklists': blocklist_results['blocklist_hits']
        }
    }

    return result


def recalculate_threat_level(geo_ip):
    """
    Recalculate threat level based on all available data including
    internal threats and blocklists.

    Args:
        geo_ip: GeoLocatedIP instance with threat data populated

    Returns:
        str: 'low', 'medium', 'high', or 'critical'
    """
    score = 0

    # Critical threats
    if geo_ip.is_known_attacker:
        score += 50
    if geo_ip.data and geo_ip.data.get('threat_data', {}).get('is_blocklisted'):
        score += 30

    # High threats
    if geo_ip.is_tor:
        score += 40
    if geo_ip.is_known_abuser:
        score += 30

    # Medium threats
    if geo_ip.is_proxy:
        score += 25
    if geo_ip.is_vpn:
        score += 20

    # External threat intelligence
    threat_data = geo_ip.data.get('threat_data', {})
    internal_stats = threat_data.get('internal', {})

    # Boost score based on internal event history
    high_severity_events = internal_stats.get('high_severity_events', 0)
    if high_severity_events > 10:
        score += 20
    elif high_severity_events > 5:
        score += 10

    # Determine threat level
    if score >= 75:
        return 'critical'
    elif score >= 50:
        return 'high'
    elif score >= 25:
        return 'medium'
    else:
        return 'low'
