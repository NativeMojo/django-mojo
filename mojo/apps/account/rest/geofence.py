"""Pre-flight geofence check — for UI use only.

GET /api/geo/check?group_uuid=<uuid>

Public, rate-limited. Returns the full GeoDecision so the calling UI can
render appropriate "not available in your region" messaging instead of
letting the user attempt a login they can't complete.

This endpoint is itself NOT geofenced — otherwise a blocked user could
never see *why* they're blocked.
"""
from mojo import decorators as md
from mojo import errors as merrors
from mojo.helpers.response import JsonResponse
from mojo.apps.account.services.geofence import GeoFenceEngine


@md.GET("geo/check")
@md.public_endpoint("Geofence pre-flight for UI")
@md.rate_limit("geo_check", ip_limit=30)
def on_geo_check(request):
    """Return a GeoDecision for the calling IP, optionally evaluated against a group.

    Query params:
      group_uuid — optional. UUID of the Group to evaluate group-level rules
                   against. If absent, only system rules are evaluated.
                   If the UUID is unknown, returns 400.
                   If the group exists but is inactive, evaluates as
                   system-only and includes a "group_inactive" hint in detail.
    """
    group = None
    group_uuid = (request.DATA.get("group_uuid") or "").strip()
    if group_uuid:
        from mojo.apps.account.models.group import Group
        group = Group.objects.filter(uuid=group_uuid).first()
        if group is None:
            raise merrors.ValueException("Unknown group")
        if not group.is_active:
            # Don't 400 — return system-only evaluation with an explanatory detail.
            # This matches the OAuth-state behavior: inactive group is a degraded
            # state, not a hard error for a pre-flight check.
            decision = GeoFenceEngine.check(request, group=None, user=getattr(request, "user", None))
            data = dict(decision)
            data["detail"] = "Group is inactive; evaluated against system rules only."
            data["group_inactive"] = True
            return JsonResponse({"status": True, "data": data})

    decision = GeoFenceEngine.check(request, group=group, user=getattr(request, "user", None))
    return JsonResponse({"status": True, "data": dict(decision)})
