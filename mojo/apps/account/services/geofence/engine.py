"""GeoFenceEngine — decision engine for system + group geofence rules.

Decision flow:
  1. GEOFENCE_ENABLED=False    → allowed=True, reason="disabled"
  2. user has bypass_geofence  → allowed=True, reason="bypass", NO cache write
  3. No rules at any level     → allowed=True, reason="no_rules", NO geoip lookup
  4. Cache hit                 → return cached decision
  5. Resolve geo (GEOFENCE_TEST_OVERRIDE wins if set, else GeoLocatedIP.geolocate)
  6. Lookup failure            → allowed=(not GEOFENCE_FAIL_CLOSED), reason="lookup_failed"
  7. Private/reserved IP       → allowed=GEOFENCE_ALLOW_PRIVATE_IPS, reason="private_ip"
  8. System rule blocks        → rule_level="system"
  9. Group rule blocks         → rule_level="group"
 10. Else                      → allowed=True, reason="passed"

Steps 6-10 all cache the decision under (ip, group_id) for GEOFENCE_CACHE_TTL.
"""
from objict import objict

from mojo.helpers import dates, logit
from mojo.helpers.settings import settings

from . import cache as gf_cache
from .dsl import evaluate_rule, validate_rule


def _system_rules():
    return settings.get("GEOFENCE_SYSTEM_RULES", {}) or {}


def _group_rules(group):
    if group is None:
        return {}
    md = getattr(group, "metadata", None) or {}
    return md.get("geofence") or {}


def _both_empty(system, group_rules):
    return not system and not group_rules


def _resolve_geo(ip):
    """Return a geo dict (provider-shaped) for `ip`, or None on lookup failure.

    GEOFENCE_TEST_OVERRIDE — if set, returns the override dict verbatim. Lets
    tests + local dev exercise blocked-region paths without real IPs.
    """
    override = settings.get("GEOFENCE_TEST_OVERRIDE", None)
    if override:
        return dict(override)
    try:
        from mojo.apps.account.models.geolocated_ip import GeoLocatedIP
        geo_ip = GeoLocatedIP.geolocate(ip)
        if geo_ip is None:
            return None
        # Build a geo dict from the model so the DSL sees the same shape as
        # the override path.
        return {
            "country_code": geo_ip.country_code,
            "region_code": geo_ip.region_code,
            "is_tor": geo_ip.is_tor,
            "is_vpn": geo_ip.is_vpn,
            "is_proxy": geo_ip.is_proxy,
            "is_datacenter": geo_ip.is_datacenter,
        }
    except Exception as exc:
        logit.error("geofence", f"geo lookup failed for {ip}: {exc}")
        return None


def _build_decision(allowed, reason, *, ip, geo=None, rule_level=None, detail=None):
    abuse = {
        "tor": bool((geo or {}).get("is_tor", False)),
        "vpn": bool((geo or {}).get("is_vpn", False)),
        "datacenter": bool((geo or {}).get("is_datacenter", False)),
        "proxy": bool((geo or {}).get("is_proxy", False)),
    }
    return objict(
        allowed=allowed,
        reason=reason,
        detail=detail or _default_detail(reason),
        ip=ip,
        country=(geo or {}).get("country_code"),
        country_code=(geo or {}).get("country_code"),
        region=(geo or {}).get("region_code"),
        region_code=(geo or {}).get("region_code"),
        abuse=abuse,
        checked_at=dates.utcnow().isoformat(),
        rule_level=rule_level,
    )


_DETAIL_MAP = {
    "no_rules":            "No geofence rules configured.",
    "disabled":            "Geofencing is disabled.",
    "bypass":              "Bypass permission granted.",
    "passed":              "Allowed.",
    "lookup_failed":       "Geolocation lookup failed.",
    "private_ip":          "Private or reserved IP.",
    "country_not_allowed": "Service is not available in your country.",
    "region_not_allowed":  "Service is not available in your region.",
    "tor_detected":        "Tor traffic is not permitted.",
    "vpn_detected":        "VPN traffic is not permitted.",
    "proxy_detected":      "Proxy traffic is not permitted.",
    "datacenter_detected": "Datacenter traffic is not permitted.",
    "rule_invalid":        "Geofence configuration is invalid; access denied.",
    "group_inactive":      "The requested group is inactive; evaluating system rules only.",
}


def _default_detail(reason):
    return _DETAIL_MAP.get(reason or "", "")


# GeoDecision is just an objict alias for typing/intent — kept as a re-exportable name.
GeoDecision = objict


class GeoFenceEngine:
    """Stateless engine. Public API is `check()`."""

    @classmethod
    def check(cls, request, group=None, user=None):
        ip = getattr(request, "ip", None) or request.META.get("REMOTE_ADDR", "")
        if user is None:
            user = getattr(request, "user", None)

        # 1. Master kill
        if not settings.get("GEOFENCE_ENABLED", True, kind="bool"):
            return _build_decision(True, "disabled", ip=ip)

        # 2. Bypass permission
        if user is not None and getattr(user, "is_authenticated", False):
            try:
                if user.has_permission("bypass_geofence"):
                    return _build_decision(True, "bypass", ip=ip)
            except Exception:
                # has_permission errors should not crash the request
                pass

        system = _system_rules()
        group_r = _group_rules(group)
        group_id = getattr(group, "pk", None)

        # 3. Zero-cost fast path
        if _both_empty(system, group_r):
            return _build_decision(True, "no_rules", ip=ip)

        # 4. Cache lookup
        cached = gf_cache.get(ip, group_id)
        if cached:
            return objict(cached)

        # 5. Resolve geo
        geo = _resolve_geo(ip)

        # 6. Lookup failure
        if geo is None:
            fail_closed = settings.get("GEOFENCE_FAIL_CLOSED", False, kind="bool")
            dec = _build_decision(not fail_closed, "lookup_failed", ip=ip)
            _maybe_cache(ip, group_id, dec)
            return dec

        # 7. Private/reserved IP — no country code
        if not geo.get("country_code"):
            allow_priv = settings.get("GEOFENCE_ALLOW_PRIVATE_IPS", True, kind="bool")
            dec = _build_decision(allow_priv, "private_ip", ip=ip, geo=geo)
            _maybe_cache(ip, group_id, dec)
            return dec

        # 8 + 9. Evaluate rules. System first (hard floor).
        for level, rule in (("system", system), ("group", group_r)):
            if not rule:
                continue
            try:
                validate_rule(rule)
            except ValueError as exc:
                logit.error("geofence", f"{level}-level rule invalid: {exc}")
                dec = _build_decision(False, "rule_invalid", ip=ip, geo=geo,
                                      rule_level=level, detail=str(exc))
                _maybe_cache(ip, group_id, dec)
                return dec
            ok, reason = evaluate_rule(rule, geo)
            if not ok:
                dec = _build_decision(False, reason, ip=ip, geo=geo, rule_level=level)
                _maybe_cache(ip, group_id, dec)
                return dec

        # 10. Passed
        dec = _build_decision(True, "passed", ip=ip, geo=geo)
        _maybe_cache(ip, group_id, dec)
        return dec


def _maybe_cache(ip, group_id, decision):
    ttl = settings.get("GEOFENCE_CACHE_TTL", 300, kind="int")
    if ttl <= 0:
        return
    # Serialize objict → plain dict for JSON
    gf_cache.set(ip, group_id, dict(decision), ttl)
