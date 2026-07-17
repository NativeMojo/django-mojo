"""Shared geofence enforcement — one routine used by both enforcement points.

`enforce()` is called by:
  1. The blocking `@md.requires_geofence` decorator (pre-view, anonymous) —
     identity-less endpoints: register, forgot, sends, begins.
  2. `jwt_login()` and the MFA branch of `on_user_login` (post-credential,
     verified user) — so `bypass_geofence` works at login and block evidence
     carries the verified user (DM-043).

Behavior is identical at both points: evaluate the engine, emit evidence for
blocks AND the allowed-but-notable outcomes (fail-open lookup failure,
exercised allowlist exemption), and return the leak-scrubbed 403 body on block.
"""
from mojo.helpers.response import JsonResponse


def enforce(request, scope=None, user=None):
    """Evaluate geofence for this request.

    Returns None when the request is allowed (stashing the decision on
    `request.geofence_decision`), or the blocked 403 JsonResponse. `user`
    is the credential-verified user for post-auth enforcement; when None,
    falls back to `request.user` (the decorator path).
    """
    from mojo.apps.account.services.geofence import GeoFenceEngine, evidence
    if user is None:
        user = getattr(request, "user", None)
    decision = GeoFenceEngine.check(
        request,
        group=getattr(request, "group", None),
        user=user,
        scope=scope,
    )
    if decision.allowed:
        request.geofence_decision = decision
        # Allowed-but-notable outcomes still emit evidence — a fail-open
        # lookup failure means enforcement silently isn't happening; an
        # exercised allowlist exemption is a compliance event.
        if decision.reason == "lookup_failed":
            evidence.report_block(request, decision, scope, user=user)
        elif decision.reason == "ip_allowlisted" and decision.get("would_block"):
            evidence.report_exempt(request, decision, scope, user=user)
        return None
    evidence.report_block(request, decision, scope, user=user)
    # Blocked: emit only reason+detail. Country/region/abuse signals stay in
    # server logs — the body must not leak detection capabilities.
    return JsonResponse({
        "error": "geofence_blocked",
        "code": 403,
        "reason": decision.reason,
        "detail": decision.detail,
    }, status=403)
