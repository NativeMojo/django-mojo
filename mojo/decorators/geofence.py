"""@requires_geofence — drop-in decorator that gates an endpoint against the
geofence engine. Default no-op when no rules are configured.

On block: returns JSON 403 with {error, code, reason, detail} only —
country/region/abuse signals are intentionally omitted to avoid leaking
detection capabilities to attackers. The pre-flight endpoint at
GET /api/geo/check returns the full decision (per-user, by design).

Usage:
    @md.requires_geofence                    # bare
    @md.requires_geofence(scope="auth")      # scope: posture + evidence context
    @md.requires_geofence(scope="auth", after_auth=True)  # deferred (DM-043)

`scope` is passed to the engine — a scope listed in GEOFENCE_FAIL_CLOSED_SCOPES
fails CLOSED on geo-lookup failure (money endpoints) while others keep the
fail-open default. Every block (and fail-open allow / exercised allowlist
exemption) is recorded by the evidence plane (services.geofence.evidence).

`after_auth=True` (DM-043) registers the endpoint in the security registry but
does NOT enforce pre-view. Identity-bearing auth endpoints use it: enforcement
happens after credential verification instead — inside `jwt_login()` (and the
MFA branch of on_user_login) via services.geofence.enforcement.enforce(),
with the verified user, so `bypass_geofence` works at login and block evidence
carries the user. Identity-less endpoints (register, forgot, sends, begins)
keep the default blocking mode.
"""
from functools import wraps


# Mirrors the global SECURITY_REGISTRY in mojo.decorators.auth
from .auth import SECURITY_REGISTRY


def requires_geofence(scope=None, after_auth=False):
    """Apply geofence enforcement to a view.

    `scope` is recorded in SECURITY_REGISTRY for audit AND passed to the
    engine: scopes listed in GEOFENCE_FAIL_CLOSED_SCOPES fail closed on
    geo-lookup failure; everything else keeps the fail-open default.

    `after_auth=True` defers enforcement to the post-credential check in
    jwt_login (see module docstring) — the endpoint is registered for audit
    but the wrapper passes straight through.
    """
    # Allow bare @requires_geofence (without parens) by detecting first arg as a callable
    if callable(scope) and not isinstance(scope, str):
        return _apply_geofence(scope, scope=None)

    def decorator(func):
        return _apply_geofence(func, scope=scope, after_auth=after_auth)
    return decorator


def _apply_geofence(func, scope=None, after_auth=False):
    func._mojo_requires_geofence = True
    func._mojo_geofence_scope = scope

    key = f"{func.__module__}.{func.__name__}"
    entry = SECURITY_REGISTRY.get(key, {})
    entry.setdefault("geofence", {})
    gf_entry = {"scope": scope}
    if after_auth:
        gf_entry["after_auth"] = True
    entry["geofence"] = gf_entry
    SECURITY_REGISTRY[key] = entry

    if after_auth:
        # Deferred mode: registered for audit (enforced_endpoints), enforced
        # post-credential in jwt_login / the MFA branch — not here.
        return func

    @wraps(func)
    def wrapper(request, *args, **kwargs):
        # Import lazily so importing the decorator doesn't drag in Django models.
        from mojo.apps.account.services.geofence import enforcement

        blocked = enforcement.enforce(request, scope=scope)
        if blocked is not None:
            return blocked
        return func(request, *args, **kwargs)

    return wrapper
