"""Geofence REST — public pre-flight check, the member policy read, and the
perm-gated config plane.

Public (rate-limited):
    GET  /api/geo/check           — full GeoDecision for the calling IP

Member plane (group-scoped read; global OR member `view_security`/`security`):
    GET  /api/geo/policy          — narrow effective policy for ONE group

Config plane (new perms `view_geofence` / `manage_geofence`; `security` is the
domain category — legal/business staff manage jurisdiction rules WITHOUT
manage_settings):
    GET    /api/geo/rules          — effective rules + posture + enforced endpoints
    POST   /api/geo/rules          — replace system rules (validated, attributed)
    DELETE /api/geo/rules          — drop the DB override (back to django.conf)
    POST   /api/geo/simulate       — uncached what-if for an arbitrary ip/geo
    GET    /api/geo/allowlist      — active IP exemptions (auditor artifact)
    POST   /api/geo/allowlist      — replace the CIDR allowlist (validated)
    GET    /api/geo/bypass_holders — users exempt via bypass_geofence/superuser

/api/geo/check is itself NOT geofenced — otherwise a blocked user could never
see *why* they're blocked. Config writes are recorded as geofence_config
incident events (the change history) and invalidate the decision cache via
the Setting model hooks.

SECURITY: config-plane permissions are checked against the user's GLOBAL
grants only (@md.requires_global_perms) — a GroupMember-scoped permission,
which any tenant/group admin can hand out, must never authorize reading or
writing platform-wide enforcement config. The ONE deliberate member-scoped
read is GET /api/geo/policy: @requires_perms accepts a member grant checked
against request.group itself, and the response is confined to that group
with a deliberately narrowed payload (no enforced_endpoints / allowlist /
posture internals / config provenance).
"""
from mojo import decorators as md
from mojo import errors as merrors
from mojo.helpers import dates
from mojo.helpers.response import JsonResponse
from mojo.helpers.settings import settings
from mojo.apps.account.models.setting import Setting
from mojo.apps.account.services.geofence import GeoFenceEngine, evidence
from mojo.apps.account.services.geofence.dsl import validate_rule
from mojo.apps.account.services.geofence.engine import entry_active, validate_allowlist

SYSTEM_RULES_KEY = "GEOFENCE_SYSTEM_RULES"
ALLOWLIST_KEY = "GEOFENCE_ALLOWLIST"
BYPASS_HOLDERS_MAX = 200
GEOIP_LIST_MAX = 500


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
      scope      — optional. Endpoint scope to preview fail posture for
                   (scopes in GEOFENCE_FAIL_CLOSED_SCOPES fail closed on
                   geo-lookup failure).
    """
    scope = (request.DATA.get("scope") or "").strip() or None
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
            decision = GeoFenceEngine.check(
                request, group=None, user=getattr(request, "user", None), scope=scope)
            data = dict(decision)
            data["detail"] = "Group is inactive; evaluated against system rules only."
            data["group_inactive"] = True
            return JsonResponse({"status": True, "data": data})

    decision = GeoFenceEngine.check(
        request, group=group, user=getattr(request, "user", None), scope=scope)
    return JsonResponse({"status": True, "data": dict(decision)})


@md.GET("geo/policy")
@md.requires_perms("view_security", "security")
def on_geo_policy(request):
    """Member-readable effective geofence policy for ONE group (the caller's).

    The group-scoped counterpart to GET geo/rules for a brand's own admin:
    baseline + the group's own rule + effective strict posture — the policy
    that actually applies to their traffic. Deliberately narrow: never
    includes platform operational detail (enforced_endpoints, allowlist
    internals, fail-closed scopes, cache TTL, config provenance).

    Auth: global view_security/security OR a member grant on request.group.
    @requires_perms verifies the member grant against request.group itself
    (resolved by the dispatcher from group/group_uuid — active groups only
    for group_uuid), and the response is built solely from request.group, so
    a member can never read another group's policy. Requires a group param:
    without one, members 403 at the decorator and global holders get a 400.
    """
    group = request.group
    if group is None:
        raise merrors.ValueException("group required")
    strict_global = settings.get("GEOFENCE_STRICT_POSTURE", False, kind="bool")
    gf_strict = (group.metadata or {}).get("geofence_strict")
    data = {
        "group": {"id": group.pk, "uuid": group.get_uuid(),
                  "name": group.name, "is_active": group.is_active},
        "enabled": settings.get("GEOFENCE_ENABLED", True, kind="bool"),
        "evaluation_order": ["system", "group"],
        "system_rule": settings.get(SYSTEM_RULES_KEY, {}, kind="dict") or {},
        "group_rule": (group.metadata or {}).get("geofence") or {},
        "strict_posture": gf_strict,
        "strict_posture_effective": (bool(gf_strict) if gf_strict is not None
                                     else strict_global),
    }
    return {"status": True, "data": data}


# ---------------------------------------------------------------------------
# Config plane — perm-gated admin surface (legal/business staff, not
# engineers, maintain jurisdiction rules; see docs/django_developer/account/
# geofence.md). Writes are validated here AND at the Setting model layer, so
# the generic /api/settings path has no unvalidated back door.
# ---------------------------------------------------------------------------

def _resolve_group_param(request):
    """Explicit group_uuid lookup (unknown → 400). Unlike the dispatcher's
    group_uuid fallback this also returns inactive groups — admins may inspect
    or simulate an inactive group's rules."""
    group_uuid = (request.DATA.get("group_uuid") or "").strip()
    if not group_uuid:
        return None
    from mojo.apps.account.models.group import Group
    group = Group.objects.filter(uuid=group_uuid).first()
    if group is None:
        raise merrors.ValueException("Unknown group")
    return group


def _enforced_endpoints():
    """Every @requires_geofence endpoint + its scope, from SECURITY_REGISTRY —
    part of the compliance artifact ("rules in an active state" includes WHERE
    they are enforced)."""
    from mojo.decorators.auth import SECURITY_REGISTRY
    out = []
    for key, entry in sorted(SECURITY_REGISTRY.items()):
        gf = entry.get("geofence") if isinstance(entry, dict) else None
        if gf is not None:
            out.append({"endpoint": key, "scope": gf.get("scope")})
    return out


def _allowlist_summary():
    from django.db.models import Q
    from mojo.apps.account.models.geolocated_ip import GeoLocatedIP
    entries = settings.get(ALLOWLIST_KEY, [], kind="list") or []
    geoip_active = GeoLocatedIP.objects.filter(is_whitelisted=True).filter(
        Q(whitelisted_until__isnull=True) | Q(whitelisted_until__gt=dates.utcnow())
    ).count()
    return {"setting_entries": len(entries), "geoip_active": geoip_active}


@md.GET("geo/rules")
@md.requires_global_perms("view_geofence", "manage_geofence", "security")
def on_geo_rules_get(request):
    """Effective geofence configuration — the machine-readable "rules in an
    active state" artifact for the admin UI and compliance reviews."""
    system_rule = settings.get(SYSTEM_RULES_KEY, {}, kind="dict") or {}
    row = Setting.objects.filter(key=SYSTEM_RULES_KEY, group=None).first()
    if row is not None:
        source, modified = "setting", row.modified.isoformat()
    elif settings.get_static(SYSTEM_RULES_KEY, None) is not None:
        source, modified = "conf", None
    else:
        source, modified = "none", None
    data = {
        "system": {"rule": system_rule, "source": source, "modified": modified},
        "posture": {
            "enabled": settings.get("GEOFENCE_ENABLED", True, kind="bool"),
            "fail_closed": settings.get("GEOFENCE_FAIL_CLOSED", False, kind="bool"),
            "fail_closed_scopes": settings.get("GEOFENCE_FAIL_CLOSED_SCOPES", [], kind="list"),
            "allow_private_ips": settings.get("GEOFENCE_ALLOW_PRIVATE_IPS", True, kind="bool"),
            "strict_posture": settings.get("GEOFENCE_STRICT_POSTURE", False, kind="bool"),
            "cache_ttl": settings.get("GEOFENCE_CACHE_TTL", 300, kind="int"),
        },
        "allowlist_summary": _allowlist_summary(),
        "evaluation_order": ["system", "group"],
        "enforced_endpoints": _enforced_endpoints(),
    }
    group = _resolve_group_param(request)
    if group is not None:
        gf_strict = (group.metadata or {}).get("geofence_strict")
        data["group"] = {
            "id": group.pk,
            "uuid": group.get_uuid(),
            "is_active": group.is_active,
            "rule": (group.metadata or {}).get("geofence") or {},
            # raw tri-state override (null = inherit) + the resolved outcome
            "strict_posture": gf_strict,
            "strict_posture_effective": (
                bool(gf_strict) if gf_strict is not None
                else data["posture"]["strict_posture"]),
        }
    return {"status": True, "data": data}


@md.POST("geo/rules")
@md.requires_global_perms("manage_geofence", "security")
def on_geo_rules_post(request):
    """Replace the system geofence rule. Full replace, never merge —
    legal-reviewed rulesets are replace-by-review. Validated with the DSL
    validator; persisted as the GEOFENCE_SYSTEM_RULES Setting row, whose save
    hook invalidates every cached decision."""
    rule = request.DATA.get("rule")
    if rule is None:
        raise merrors.ValueException("'rule' is required")
    if not isinstance(rule, dict):
        raise merrors.ValueException("'rule' must be a dict")
    rule = dict(rule)
    try:
        validate_rule(rule)
    except ValueError as exc:
        raise merrors.ValueException(str(exc))
    old = settings.get(SYSTEM_RULES_KEY, {}, kind="dict") or {}
    row = Setting.set(SYSTEM_RULES_KEY, rule)
    evidence.report_config_change("system", old=old, new=rule, request=request)
    return {"status": True, "data": {
        "rule": rule, "source": "setting", "modified": row.modified.isoformat()}}


@md.DELETE("geo/rules")
@md.requires_global_perms("manage_geofence", "security")
def on_geo_rules_delete(request):
    """Remove the DB override — the engine falls back to the django.conf value
    (or to no rules). Setting.delete() invalidates cached decisions."""
    old = settings.get(SYSTEM_RULES_KEY, {}, kind="dict") or {}
    removed = Setting.remove(SYSTEM_RULES_KEY)
    evidence.report_config_change("system", old=old, new=None, request=request)
    return {"status": True, "data": {"removed": removed}}


@md.POST("geo/simulate")
@md.requires_global_perms("view_geofence", "manage_geofence", "security")
def on_geo_simulate(request):
    """Uncached what-if decision for an arbitrary IP or geo dict — lets a
    non-engineer demonstrate "a WA IP is blocked" without owning a WA IP.
    Distinct from the public /api/geo/check (which evaluates the CALLER's
    IP). Never emits evidence events, never touches the decision cache."""
    ip = (request.DATA.get("ip") or "").strip()
    geo = request.DATA.get("geo")
    if not ip and geo is None:
        raise merrors.ValueException("Provide 'ip' or 'geo'")
    if geo is not None and not isinstance(geo, dict):
        raise merrors.ValueException("'geo' must be a dict")
    scope = (request.DATA.get("scope") or "").strip() or None
    group = _resolve_group_param(request)
    decision = GeoFenceEngine.simulate(
        request, ip=ip or None, geo=dict(geo) if geo is not None else None,
        group=group, scope=scope)
    return {"status": True, "data": dict(decision)}


@md.GET("geo/allowlist")
@md.requires_global_perms("view_geofence", "manage_geofence", "security")
def on_geo_allowlist_get(request):
    """Active IP exemptions with reason/expiry — the auditor's "who is
    exempt" artifact (IP/CIDR side; user grants are geo/bypass_holders).
    Expired entries are listed with active=false, not hidden."""
    from mojo.apps.account.models.geolocated_ip import GeoLocatedIP
    setting_entries = []
    for entry in settings.get(ALLOWLIST_KEY, [], kind="list") or []:
        norm = {"cidr": entry} if isinstance(entry, str) else dict(entry)
        setting_entries.append({
            "cidr": norm.get("cidr") or norm.get("ip"),
            "reason": norm.get("reason"),
            "until": norm.get("until"),
            "active": entry_active(norm),
        })
    geoip_entries = [{
        "ip": row.ip_address,
        "reason": row.whitelisted_reason,
        "until": row.whitelisted_until.isoformat() if row.whitelisted_until else None,
        "active": row.whitelist_active,
    } for row in GeoLocatedIP.objects.filter(
        is_whitelisted=True).order_by("-modified")[:GEOIP_LIST_MAX]]
    return {"status": True, "data": {"setting": setting_entries, "geoip": geoip_entries}}


@md.POST("geo/allowlist")
@md.requires_global_perms("manage_geofence", "security")
def on_geo_allowlist_post(request):
    """Replace the GEOFENCE_ALLOWLIST setting (full replace; an empty list
    clears it). Entries are "CIDR-or-IP" strings or {cidr, reason, until}.
    Per-IP entries are managed on /api/system/geoip via the whitelist /
    unwhitelist actions, not here."""
    entries = request.DATA.get("entries")
    if entries is None:
        raise merrors.ValueException("'entries' is required (a list; may be empty)")
    if isinstance(entries, tuple):
        entries = list(entries)
    if not isinstance(entries, list):
        raise merrors.ValueException("'entries' must be a list")
    try:
        validate_allowlist(entries)
    except ValueError as exc:
        raise merrors.ValueException(str(exc))
    old = settings.get(ALLOWLIST_KEY, [], kind="list") or []
    Setting.set(ALLOWLIST_KEY, list(entries))
    evidence.report_config_change("allowlist", old=old, new=list(entries), request=request)
    return {"status": True, "data": {"entries": list(entries)}}


@md.GET("geo/bypass_holders")
@md.requires_global_perms("view_geofence", "manage_geofence", "security")
def on_geo_bypass_holders(request):
    """Users exempt from geofencing: explicit bypass_geofence grants PLUS
    superusers (User.has_permission returns True for a superuser on every
    perm, so they bypass implicitly — an auditor list that omitted them
    would be misleading). High-privilege audit surface.

    Deliberately returns id/username only — email/display_name are gated by
    the "users" permission category on the User model and must not leak
    through a geofence-only grant."""
    from django.db.models import Q
    from mojo.apps.account.models.user import User
    qs = User.objects.filter(
        Q(permissions__bypass_geofence__isnull=False) | Q(is_superuser=True)
    ).order_by("id")
    rows = list(qs[:BYPASS_HOLDERS_MAX + 1])
    capped = len(rows) > BYPASS_HOLDERS_MAX
    holders = []
    for user in rows[:BYPASS_HOLDERS_MAX]:
        value = (user.permissions or {}).get("bypass_geofence", False)
        if not value and not user.is_superuser:
            continue  # falsy explicit grant — has_permission would deny
        holders.append({
            "id": user.pk,
            "username": user.username,
            "is_active": user.is_active,
            "source": "permission" if value else "superuser",
            "value": value,
        })
    return {"status": True, "data": {
        "holders": holders, "count": len(holders), "capped": capped}}
