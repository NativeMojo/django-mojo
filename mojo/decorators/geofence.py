"""@requires_geofence — drop-in decorator that gates an endpoint against the
geofence engine. Default no-op when no rules are configured.

On block: returns JSON 403 with {error, code, reason, detail} only —
country/region/abuse signals are intentionally omitted to avoid leaking
detection capabilities to attackers. The pre-flight endpoint at
GET /api/geo/check returns the full decision (per-user, by design).

Usage:
    @md.requires_geofence              # bare
    @md.requires_geofence(scope="auth") # informational scope (audit/logs only)
"""
from functools import wraps
from mojo.helpers.response import JsonResponse


# Mirrors the global SECURITY_REGISTRY in mojo.decorators.auth
from .auth import SECURITY_REGISTRY


def requires_geofence(scope=None):
    """Apply geofence enforcement to a view.

    `scope` is metadata only — recorded in SECURITY_REGISTRY for audit/logging.
    It does NOT change the check logic.
    """
    # Allow bare @requires_geofence (without parens) by detecting first arg as a callable
    if callable(scope) and not isinstance(scope, str):
        return _apply_geofence(scope, scope=None)

    def decorator(func):
        return _apply_geofence(func, scope=scope)
    return decorator


def _apply_geofence(func, scope=None):
    func._mojo_requires_geofence = True
    func._mojo_geofence_scope = scope

    key = f"{func.__module__}.{func.__name__}"
    entry = SECURITY_REGISTRY.get(key, {})
    entry.setdefault("geofence", {})
    entry["geofence"] = {"scope": scope}
    SECURITY_REGISTRY[key] = entry

    @wraps(func)
    def wrapper(request, *args, **kwargs):
        # Import lazily so importing the decorator doesn't drag in Django models.
        from mojo.apps.account.services.geofence import GeoFenceEngine

        decision = GeoFenceEngine.check(
            request,
            group=getattr(request, "group", None),
            user=getattr(request, "user", None),
        )
        if decision.allowed:
            request.geofence_decision = decision
            return func(request, *args, **kwargs)

        # Blocked: emit only reason+detail. Full decision stays in server logs.
        return JsonResponse({
            "error": "geofence_blocked",
            "code": 403,
            "reason": decision.reason,
            "detail": decision.detail,
        }, status=403)

    return wrapper
