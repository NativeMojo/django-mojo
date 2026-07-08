"""GeoFenceEngine — decision engine for system + group geofence rules.

Decision flow:
  1. GEOFENCE_ENABLED=False    → allowed=True, reason="disabled"
  2. user has bypass_geofence  → allowed=True, reason="bypass", NO cache write
  3. No rules at any level     → allowed=True, reason="no_rules", NO geoip lookup
  4. Cache hit                 → return cached decision
  4b. IP allowlisted           → allowed=True, reason="ip_allowlisted" — full
      exemption (jurisdiction + abuse flags). The rules still run in shadow so
      evidence can record would_block / would_block_reason.
  5. Resolve geo (GEOFENCE_TEST_OVERRIDE wins if set, else GeoLocatedIP.geolocate)
  6. Lookup failure            → allowed=(not fail-closed), reason="lookup_failed".
      Fail-closed when GEOFENCE_FAIL_CLOSED or scope ∈ GEOFENCE_FAIL_CLOSED_SCOPES.
      NEVER cached — scope isn't in the cache key, so a fail-open allow must not
      be replayed to a fail-closed scope.
  7. Private/reserved IP       → allowed=GEOFENCE_ALLOW_PRIVATE_IPS, reason="private_ip"
  8. System rule blocks        → rule_level="system"
  9. Group rule blocks         → rule_level="group"
 10. Else                      → allowed=True, reason="passed"

Steps 4b and 7-10 cache the decision under (ip, group_id) for GEOFENCE_CACHE_TTL.

TEST MODE — when the test-mode gate passes (see mojo.helpers.test_mode for the
defense-in-depth checks: env var + loopback-only + no proxy chain), the engine
honors per-request headers so test suites can run in parallel without server
reloads:
    X-Mojo-Test-Geo               : JSON dict, replaces geoip lookup result;
                                    the literal string "fail" forces a lookup failure
    X-Mojo-Test-Geofence-System   : JSON dict, replaces GEOFENCE_SYSTEM_RULES
    X-Mojo-Test-Geofence-Allowlist : JSON list, replaces GEOFENCE_ALLOWLIST
    X-Mojo-Test-Geofence-Enabled  : "0" or "1", overrides GEOFENCE_ENABLED
    X-Mojo-Test-Geofence-Fail-Closed : "0" or "1", overrides GEOFENCE_FAIL_CLOSED
    X-Mojo-Test-Geofence-Fail-Closed-Scopes : comma list, overrides GEOFENCE_FAIL_CLOSED_SCOPES
    X-Mojo-Test-Geofence-Allow-Private : "0" or "1", overrides GEOFENCE_ALLOW_PRIVATE_IPS
    X-Mojo-Test-Geofence-Cache-Ttl : int seconds; <=0 disables cache for this request
The gate is closed by default; production never satisfies all four conditions.
"""
import ipaddress
import json

from objict import objict

from mojo.helpers import dates, logit, test_mode as _tm
from mojo.helpers.settings import settings

from . import cache as gf_cache
from .dsl import evaluate_rule, validate_rule


# ---------------------------------------------------------------------------
# Test-mode header overrides — gated by mojo.helpers.test_mode.is_test_request
# which enforces env var + loopback-only + no proxy chain. Production traffic
# fails the gate so the header read paths short-circuit.
# ---------------------------------------------------------------------------

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
    if _tm.is_test_request(request):
        h = _header(request, header_name)
        if h is not None:
            return h not in ("0", "false", "False", "")
    return settings.get(setting_name, default, kind="bool")


def _int_setting_with_header(request, header_name, setting_name, default):
    if _tm.is_test_request(request):
        h = _header(request, header_name)
        if h is not None:
            try:
                return int(h)
            except (TypeError, ValueError):
                pass
    return settings.get(setting_name, default, kind="int")


def _list_setting_with_header(request, header_name, setting_name, default):
    if _tm.is_test_request(request):
        h = _header(request, header_name)
        if h is not None:
            return [x.strip() for x in h.split(",") if x.strip()]
    return settings.get(setting_name, default, kind="list")


def _system_rules(request=None):
    if _tm.is_test_request(request):
        override = _json_header(request, "X-Mojo-Test-Geofence-System")
        if override is not None:
            return override or {}
    # kind="dict" — a DB-backed Setting stores the rule as a JSON string
    return settings.get("GEOFENCE_SYSTEM_RULES", {}, kind="dict") or {}


def _group_rules(group):
    if group is None:
        return {}
    md = getattr(group, "metadata", None) or {}
    return md.get("geofence") or {}


def _both_empty(system, group_rules):
    return not system and not group_rules


# ---------------------------------------------------------------------------
# IP allowlist — full geofence exemption (jurisdiction + abuse flags) for
# developer / office egress IPs. Two sources: the GEOFENCE_ALLOWLIST setting
# (CIDR entries, optionally {cidr, reason, until}) and per-IP
# GeoLocatedIP.is_whitelisted rows. There is no shared ip-in-cidr helper in
# mojo/ (kernel ipset does that matching for the firewall) — it lives here.
# ---------------------------------------------------------------------------

def _allowlist_entries(request=None):
    if _tm.is_test_request(request):
        override = _json_header(request, "X-Mojo-Test-Geofence-Allowlist")
        if override is not None:
            return override or []
    return settings.get("GEOFENCE_ALLOWLIST", [], kind="list") or []


def entry_active(entry):
    """False when a {cidr, reason, until} entry has expired or its expiry is
    unparseable — a malformed `until` never grants a permanent exemption."""
    until = entry.get("until") if isinstance(entry, dict) else None
    if not until:
        return True
    try:
        parsed = dates.parse_datetime(until)
    except Exception:
        parsed = None
    if parsed is None:
        return False
    return dates.utcnow() <= parsed


def _entry_matches(entry, ip):
    """Return the normalized entry dict when `ip` falls inside it, else None."""
    if isinstance(entry, str):
        entry = {"cidr": entry}
    if not isinstance(entry, dict):
        return None
    cidr = entry.get("cidr") or entry.get("ip")
    if not cidr or not entry_active(entry):
        return None
    try:
        if ipaddress.ip_address(ip) in ipaddress.ip_network(str(cidr), strict=False):
            return entry
    except (ValueError, TypeError):
        # family mismatch or malformed entry — a bad entry must never raise;
        # it just doesn't match and evaluation proceeds normally
        return None
    return None


def _ip_allowlisted(request, ip):
    """Return objict(source, reason, until) when `ip` is exempt, else None."""
    if not ip:
        return None
    for entry in _allowlist_entries(request):
        try:
            matched = _entry_matches(entry, ip)
        except Exception as exc:
            logit.error("geofence", f"allowlist entry {entry!r} match failed: {exc}")
            continue
        if matched:
            return objict(source="setting", reason=matched.get("reason"),
                          until=matched.get("until"))
    try:
        from mojo.apps.account.models.geolocated_ip import GeoLocatedIP
        row = GeoLocatedIP.objects.filter(ip_address=ip, is_whitelisted=True).first()
        if row is not None and row.whitelist_active:
            return objict(
                source="geoip", reason=row.whitelisted_reason,
                until=row.whitelisted_until.isoformat() if row.whitelisted_until else None)
    except Exception as exc:
        logit.error("geofence", f"allowlist geoip check failed for {ip}: {exc}")
    return None


def validate_allowlist(entries):
    """Raise ValueError when a GEOFENCE_ALLOWLIST value is malformed.

    Entries are "CIDR-or-IP" strings or {cidr, reason, until} dicts.
    Returns None on success (mirrors dsl.validate_rule).
    """
    if not isinstance(entries, (list, tuple)):
        raise ValueError(
            f"geofence allowlist must be a list, got {type(entries).__name__}")
    for i, entry in enumerate(entries):
        if isinstance(entry, str):
            cidr, reason, until = entry, None, None
        elif isinstance(entry, dict):
            unknown = set(entry) - {"cidr", "ip", "reason", "until"}
            if unknown:
                raise ValueError(
                    f"geofence allowlist entry {i}: unknown keys {sorted(unknown)}")
            cidr = entry.get("cidr") or entry.get("ip")
            reason = entry.get("reason")
            until = entry.get("until")
        else:
            raise ValueError(
                f"geofence allowlist entry {i} must be a string or dict, "
                f"got {type(entry).__name__}")
        if not cidr or not isinstance(cidr, str):
            raise ValueError(f"geofence allowlist entry {i}: missing 'cidr'")
        try:
            ipaddress.ip_network(cidr, strict=False)
        except ValueError as exc:
            raise ValueError(
                f"geofence allowlist entry {i}: invalid CIDR/IP {cidr!r} ({exc})")
        if reason is not None and not isinstance(reason, str):
            raise ValueError(
                f"geofence allowlist entry {i}: 'reason' must be a string")
        if until is not None:
            try:
                parsed = dates.parse_datetime(until)
            except Exception:
                parsed = None
            if parsed is None:
                raise ValueError(
                    f"geofence allowlist entry {i}: invalid 'until' datetime {until!r}")


def _resolve_geo(ip, request=None):
    """Return a geo dict (provider-shaped) for `ip`, or None on lookup failure.

    Test-mode header X-Mojo-Test-Geo (JSON dict) wins over everything when
    the test-mode gate passes. Otherwise GEOFENCE_TEST_OVERRIDE setting wins
    over real lookups.
    """
    if _tm.is_test_request(request):
        if _header(request, "X-Mojo-Test-Geo") == "fail":
            return None  # test vector for the lookup_failed path
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
    "ip_allowlisted":      "IP is allowlisted; geofence exemption applied.",
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

# Sentinel: "resolve the geo yourself" vs. an explicit geo dict (or explicit None).
_UNSET = object()


def _allowlisted_decision(ip, exempt, shadow):
    """Assemble the allowed ip_allowlisted decision, carrying the shadow
    evaluation's geo signals and would-block outcome for evidence/simulate."""
    dec = _build_decision(True, "ip_allowlisted", ip=ip)
    for field in ("country", "country_code", "region", "region_code", "abuse"):
        dec[field] = shadow.get(field)
    dec.allowlist_source = exempt.source
    dec.allowlist_reason = exempt.reason
    dec.allowlist_until = exempt.until
    if shadow.reason == "lookup_failed":
        # Geo never resolved — unknown whether the rules would have blocked.
        dec.would_block = None
        dec.would_block_reason = None
    else:
        dec.would_block = shadow.allowed is False
        dec.would_block_reason = shadow.reason if not shadow.allowed else None
    return dec


class GeoFenceEngine:
    """Stateless engine. Public API is `check()` and `simulate()`."""

    @classmethod
    def check(cls, request, group=None, user=None, scope=None):
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

        # 4b. IP allowlist — wins over rule evaluation (full exemption incl.
        # abuse flags). The rules still run in shadow so evidence can record
        # what would have happened.
        exempt = _ip_allowlisted(request, ip)
        if exempt is not None:
            shadow = cls._evaluate(request, ip, system, group_r, scope=scope)
            dec = _allowlisted_decision(ip, exempt, shadow)
            _maybe_cache(ip, group_id, dec, ttl)
            return dec

        # 5-10. Resolve geo and evaluate rules.
        dec = cls._evaluate(request, ip, system, group_r, scope=scope)
        # lookup_failed is never cached: scope isn't part of the cache key, so
        # a fail-open allow from one scope must not be replayed to a
        # fail-closed scope — and caching transient failures prolongs outages.
        if dec.reason != "lookup_failed":
            _maybe_cache(ip, group_id, dec, ttl)
        return dec

    @classmethod
    def simulate(cls, request, ip=None, geo=None, group=None, scope=None):
        """What-if evaluation for the admin config plane. Uncached.

        Unlike check(): no bypass-permission shortcut, no cache read/write,
        and it still evaluates when GEOFENCE_ENABLED is off (staff stage rules
        before enabling) — the returned decision carries `enabled` so callers
        see posture. An explicit `geo` dict wins over `ip` resolution; the
        allowlist is only consulted when an `ip` is given. Never emits
        evidence events (those happen at the enforcement decorator).
        `request` is passed through solely so the test-mode header overrides
        keep working.
        """
        ip = ip or ""
        enabled = _bool_setting_with_header(
            request, "X-Mojo-Test-Geofence-Enabled", "GEOFENCE_ENABLED", True)
        system = _system_rules(request)
        group_r = _group_rules(group)

        if _both_empty(system, group_r):
            dec = _build_decision(True, "no_rules", ip=ip)
        else:
            exempt = _ip_allowlisted(request, ip) if ip else None
            shadow = cls._evaluate(request, ip, system, group_r, scope=scope,
                                   geo=geo if geo is not None else _UNSET)
            dec = _allowlisted_decision(ip, exempt, shadow) if exempt else shadow
        dec.enabled = enabled
        return dec

    @classmethod
    def _evaluate(cls, request, ip, system, group_r, scope=None, geo=_UNSET):
        """Steps 5-10: resolve geo and evaluate rules. Never caches — the
        caller decides (check() skips lookup_failed; simulate() never caches)."""
        if geo is _UNSET:
            geo = _resolve_geo(ip, request)

        # 6. Lookup failure
        if geo is None:
            fail_closed = _bool_setting_with_header(
                request, "X-Mojo-Test-Geofence-Fail-Closed", "GEOFENCE_FAIL_CLOSED", False)
            if not fail_closed and scope:
                scopes = _list_setting_with_header(
                    request, "X-Mojo-Test-Geofence-Fail-Closed-Scopes",
                    "GEOFENCE_FAIL_CLOSED_SCOPES", [])
                fail_closed = scope in scopes
            return _build_decision(not fail_closed, "lookup_failed", ip=ip)

        # 7. Private/reserved IP — no country code
        if not geo.get("country_code"):
            allow_priv = _bool_setting_with_header(
                request, "X-Mojo-Test-Geofence-Allow-Private", "GEOFENCE_ALLOW_PRIVATE_IPS", True)
            return _build_decision(allow_priv, "private_ip", ip=ip, geo=geo)

        # 8 + 9. Evaluate rules. System first (hard floor).
        for level, rule in (("system", system), ("group", group_r)):
            if not rule:
                continue
            try:
                validate_rule(rule)
            except ValueError as exc:
                logit.error("geofence", f"{level}-level rule invalid: {exc}")
                return _build_decision(False, "rule_invalid", ip=ip, geo=geo,
                                       rule_level=level, detail=str(exc))
            ok, reason = evaluate_rule(rule, geo)
            if not ok:
                return _build_decision(False, reason, ip=ip, geo=geo, rule_level=level)

        # 10. Passed
        return _build_decision(True, "passed", ip=ip, geo=geo)


def _maybe_cache(ip, group_id, decision, ttl):
    if ttl <= 0:
        return
    # Serialize objict → plain dict for JSON
    gf_cache.set(ip, group_id, dict(decision), ttl)
