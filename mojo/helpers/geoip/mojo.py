"""
Mojo provider for GeoIP lookups — federates with another django-mojo instance.

Calls the upstream's authed GET /api/system/geoip/lookup endpoint with a
group-scoped ApiKey token and returns the full enriched record. Strips
per-fleet firewall fields (is_blocked, is_whitelisted, blocked_*, whitelisted_*)
at the boundary so that local enforcement state from the upstream never leaks
into this downstream's cache.
"""
import requests
from mojo.helpers import logit
from . import config


# Fields that are derived from observed behavior / third-party intel on the
# upstream — safe and desirable to copy across the federation boundary.
_FEDERATED_FIELDS = (
    "country_code", "country_name", "region", "region_code",
    "city", "postal_code", "latitude", "longitude", "timezone",
    "asn", "asn_org", "isp", "connection_type", "mobile_carrier",
    "is_tor", "is_vpn", "is_proxy", "is_cloud", "is_datacenter", "is_mobile",
    "is_known_attacker", "is_known_abuser",
    "threat_level",
)

# Per-fleet enforcement state — explicitly stripped at the boundary.
_FIREWALL_FIELDS = (
    "is_blocked", "is_whitelisted",
    "blocked_at", "blocked_until", "blocked_reason", "block_count",
    "whitelisted_reason",
)


def fetch(ip_address, api_key=None):
    """
    Fetch geolocation data from an upstream django-mojo instance and normalize it.

    Args:
        ip_address: The IP address to look up.
        api_key: Optional API key (defaults to GEOIP_API_KEY_MOJO via config).

    Returns:
        dict with provider='mojo' and the full federated field set on success,
        or None on any failure (missing config, HTTP error, non-2xx).
    """
    base_url = config.MOJO_PROVIDER_URL
    if not base_url:
        logit.warning("[GeoIP] mojo provider requires GEOIP_MOJO_PROVIDER_URL")
        return None

    if api_key is None:
        api_key = config.get_api_key("mojo")
    if not api_key:
        logit.warning("[GeoIP] mojo provider requires GEOIP_API_KEY_MOJO")
        return None

    url = f"{base_url.rstrip('/')}/api/system/geoip/lookup"
    headers = {"Authorization": f"apikey {api_key}"}
    params = {"ip": ip_address, "graph": "detailed"}

    try:
        response = requests.get(url, headers=headers, params=params, timeout=5)
        response.raise_for_status()
        body = response.json()
    except Exception as e:
        logit.warning("[GeoIP] mojo provider fetch failed for %s: %s", ip_address, e)
        return None

    if not isinstance(body, dict) or not body.get("status"):
        logit.warning("[GeoIP] mojo provider returned non-success for %s: %r", ip_address, body)
        return None

    upstream = body.get("data") or {}
    if not isinstance(upstream, dict):
        logit.warning("[GeoIP] mojo provider returned non-dict data for %s", ip_address)
        return None

    result = {"provider": "mojo"}
    for key in _FEDERATED_FIELDS:
        if key in upstream:
            result[key] = upstream[key]

    # Preserve the upstream's raw provider data blob if present, but never
    # copy through per-fleet firewall state.
    raw = upstream.get("data")
    if isinstance(raw, dict):
        # Defensive: scrub firewall fields even if upstream graph leaked them
        # into the nested `data` blob.
        clean = {k: v for k, v in raw.items() if k not in _FIREWALL_FIELDS}
        result["data"] = clean
    else:
        result["data"] = {}

    return result
