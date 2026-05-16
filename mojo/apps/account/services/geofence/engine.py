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

TEST MODE — when settings.MOJO_TEST_MODE is True, the engine honors per-request
headers so test suites can run in parallel without server reloads:
    X-Mojo-Test-Geo               : JSON dict, replaces geoip lookup result
    X-Mojo-Test-Geofence-System   : JSON dict, replaces GEOFENCE_SYSTEM_RULES
    X-Mojo-Test-Geofence-Enabled  : "0" or "1", overrides GEOFENCE_ENABLED
    X-Mojo-Test-Geofence-Fail-Closed : "0" or "1", overrides GEOFENCE_FAIL_CLOSED
    X-Mojo-Test-Geofence-Allow-Private : "0" or "1", overrides GEOFENCE_ALLOW_PRIVATE_IPS
    X-Mojo-Test-Geofence-Cache-Ttl : int seconds; <=0 disables cache for this request
MOJO_TEST_MODE defaults to False, so production never honors these headers.
"""
import json

from objict import objict

from mojo.helpers import dates, logit
from mojo.helpers.settings import settings

from . import cache as gf_cache
from .dsl import evaluate_rule, validate_rule


# ---------------------------------------------------------------------------
# Test-mode header overrides — read once per request, no production cost when
# MOJO_TEST_MODE is False (default).
# ---------------------------------------------------------------------------

def _test_mode():
    return settings.get("MOJO_TEST_MODE", False, kind="bool")


def _header(request, name):
    if request is None:
        return None
    key = "HTTP_" + name.upper().replace("-", "_")
    return request.META.get(key)


def _json_header(request, name):
    raw = _header(request, name)
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None


def _bool_setting_with_header(request, header_name, setting_name, default):
    if request is not None and _test_mode():
        h = _header(request, header_name)
        if h is not None:
            return h not in ("0", "false", "False", "")
    return settings.get(setting_name, default, kind="bool")


def _int_setting_with_header(request, header_name, setting_name, default):
    if request is not None and _test_mode():
        h = _header(request, header_name)
        if h is not None:
            try:
                return int(h)
            except (TypeError, ValueError):
                pass
    return settings.get(setting_name, default, kind="int")


def _system_rules(request=None):
    if request is not None and _test_mode():
        override = _json_header(request, "X-Mojo-Test-Geofence-System")
        if override is not None:
            return override or {}
    return settings.get("GEOFENCE_SYSTEM_RULES", {}) or {}


def _group_rules(group):
    if group is None:
        return {}
    md = getattr(group, "metadata", None) or {}
    return md.get("geofence") or {}


def _both_empty(system, group_rules):
    return not system and not group_rules


def _resolve_geo(ip, request=None):
    """Return a geo dict (provider-shaped) for `ip`, or None on lookup failure.

    Test-mode header X-Mojo-Test-Geo (JSON dict) wins over everything when
    MOJO_TEST_MODE=True. Otherwise GEOFENCE_TEST_OVERRIDE setting wins over
    real lookups.
    """
    if request is not None and _test_mode():
        header_override = _json_header(request, "X-Mojo-Test-Geo")
        if header_override is not None:
            return header_override
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
        enabled = _bool_setting_with_header(
            request, "X-Mojo-Test-Geofence-Enabled", "GEOFENCE_ENABLED", True)
        if not enabled:
            return _build_decision(True, "disabled", ip=ip)

        # 2. Bypass permission
        if user is not None and getattr(user, "is_authenticated", False):
            try:
                if user.has_permission("bypass_geofence"):
                    return _build_decision(True, "bypass", ip=ip)
            except Exception:
                # has_permission errors should not crash the request
                pass

        system = _system_rules(request)
        group_r = _group_rules(group)
        group_id = getattr(group, "pk", None)

        # 3. Zero-cost fast path
        if _both_empty(system, group_r):
            return _build_decision(True, "no_rules", ip=ip)

        ttl = _int_setting_with_header(
            request, "X-Mojo-Test-Geofence-Cache-Ttl", "GEOFENCE_CACHE_TTL", 300)
        cache_enabled = ttl > 0

        # 4. Cache lookup (skipped when caller disabled it)
        if cache_enabled:
            cached = gf_cache.get(ip, group_id)
            if cached:
                return objict(cached)

        # 5. Resolve geo
        geo = _resolve_geo(ip, request)

        # 6. Lookup failure
        if geo is None:
            fail_closed = _bool_setting_with_header(
                request, "X-Mojo-Test-Geofence-Fail-Closed", "GEOFENCE_FAIL_CLOSED", False)
            dec = _build_decision(not fail_closed, "lookup_failed", ip=ip)
            _maybe_cache(ip, group_id, dec, ttl)
            return dec

        # 7. Private/reserved IP — no country code
        if not geo.get("country_code"):
            allow_priv = _bool_setting_with_header(
                request, "X-Mojo-Test-Geofence-Allow-Private", "GEOFENCE_ALLOW_PRIVATE_IPS", True)
            dec = _build_decision(allow_priv, "private_ip", ip=ip, geo=geo)
            _maybe_cache(ip, group_id, dec, ttl)
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
                _maybe_cache(ip, group_id, dec, ttl)
                return dec
            ok, reason = evaluate_rule(rule, geo)
            if not ok:
                dec = _build_decision(False, reason, ip=ip, geo=geo, rule_level=level)
                _maybe_cache(ip, group_id, dec, ttl)
                return dec

        # 10. Passed
        dec = _build_decision(True, "passed", ip=ip, geo=geo)
        _maybe_cache(ip, group_id, dec, ttl)
        return dec


def _maybe_cache(ip, group_id, decision, ttl):
    if ttl <= 0:
        return
    # Serialize objict → plain dict for JSON
    gf_cache.set(ip, group_id, dict(decision), ttl)
