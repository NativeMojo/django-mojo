"""@requires_geofence — drop-in decorator that gates an endpoint against the
geofence engine. Default no-op when no rules are configured.

On block: returns JSON 403 with {error, code, reason, detail} only —
country/region/abuse signals are intentionally omitted to avoid leaking
detection capabilities to attackers. The pre-flight endpoint at
GET /api/geo/check returns the full decision (per-user, by design).

Usage:
    @md.requires_geofence              # bare
    @md.requires_geofence(scope="auth") # scope: posture + evidence context

`scope` is passed to the engine — a scope listed in GEOFENCE_FAIL_CLOSED_SCOPES
fails CLOSED on geo-lookup failure (money endpoints) while others keep the
fail-open default. Every block (and fail-open allow / exercised allowlist
exemption) is recorded by the evidence plane (services.geofence.evidence).
"""
from functools import wraps
from mojo.helpers.response import JsonResponse


# Mirrors the global SECURITY_REGISTRY in mojo.decorators.auth
from .auth import SECURITY_REGISTRY


def requires_geofence(scope=None):
    """Apply geofence enforcement to a view.

    `scope` is recorded in SECURITY_REGISTRY for audit AND passed to the
    engine: scopes listed in GEOFENCE_FAIL_CLOSED_SCOPES fail closed on
    geo-lookup failure; everything else keeps the fail-open default.
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
        from mojo.apps.account.services.geofence import GeoFenceEngine, evidence

        decision = GeoFenceEngine.check(
            request,
            group=getattr(request, "group", None),
            user=getattr(request, "user", None),
            scope=scope,
        )
        if decision.allowed:
            request.geofence_decision = decision
            # Evidence for allowed-but-notable outcomes. Emission lives here at
            # the enforcement point (not in the engine) so cache hits still
            # emit; report_* never raises into the request path.
            if decision.reason == "lookup_failed":
                evidence.report_block(request, decision, scope)
            elif decision.reason == "ip_allowlisted" and decision.get("would_block"):
                evidence.report_exempt(request, decision, scope)
            return func(request, *args, **kwargs)

        evidence.report_block(request, decision, scope)

        # Blocked: emit only reason+detail. Full decision stays in server logs.
        return JsonResponse({
            "error": "geofence_blocked",
            "code": 403,
            "reason": decision.reason,
            "detail": decision.detail,
        }, status=403)

    return wrapper
