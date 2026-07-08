"""Redis cache wrapper for geofence decisions.

Keys are namespaced under `geofence:dec:{ip}:{group_id_or_'_'}`. Values are
JSON-serialized GeoDecision dicts.

Cache writes are skipped for bypass-permission users and for the no-rules
fast path — both are short-circuits that don't depend on geo lookups.
"""
import json
from mojo.helpers import logit
from mojo.helpers.redis import get_connection


def _key(ip, group_id):
    return f"geofence:dec:{ip}:{group_id if group_id is not None else '_'}"


def get(ip, group_id):
    """Return cached decision dict, or None on miss / parse error."""
    try:
        raw = get_connection().get(_key(ip, group_id))
    except Exception as exc:
        logit.error("geofence", f"cache get failed: {exc}")
        return None
    if not raw:
        return None
    try:
        return json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return None


def set(ip, group_id, decision_dict, ttl):
    """Write decision dict with TTL (seconds). Errors are swallowed + logged."""
    try:
        get_connection().setex(_key(ip, group_id), int(ttl), json.dumps(decision_dict))
    except Exception as exc:
        logit.error("geofence", f"cache set failed: {exc}")


def invalidate(ip, group_id=None):
    """Remove a cached decision. If group_id is None, removes the system-only entry."""
    try:
        get_connection().delete(_key(ip, group_id))
    except Exception as exc:
        logit.error("geofence", f"cache delete failed: {exc}")


def invalidate_ip(ip):
    """Remove every cached decision for one IP (any group scope) — used when a
    per-IP whitelist entry changes."""
    _invalidate_pattern(f"geofence:dec:{ip}:*")


def invalidate_group(group_id):
    """Remove every cached decision evaluated against a group — used when the
    group's geofence rules change."""
    _invalidate_pattern(f"geofence:dec:*:{group_id}")


def invalidate_all():
    """Remove every cached geofence decision — used when the system rules or
    the allowlist change (an emergency edit must not serve stale allows)."""
    _invalidate_pattern("geofence:dec:*")


def _invalidate_pattern(pattern):
    try:
        r = get_connection()
        for key in r.scan_iter(pattern):
            r.delete(key)
    except Exception as exc:
        logit.error("geofence", f"cache invalidate failed for {pattern}: {exc}")
