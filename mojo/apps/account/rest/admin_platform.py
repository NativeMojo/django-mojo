"""Dedicated global authority for Platform and Advanced Admin controls."""

from mojo import decorators as md
from mojo import errors as me
from mojo.apps.account.services import admin_platform as _admin_platform
from mojo.apps.account.services import system_settings


def _deployment(value):
    from mojo.apps.edge.services import platform_deploy
    row = platform_deploy.get(value)
    if row is None:
        raise me.ValueException("Unknown platform deployment", status=404, code=404)
    return row


@md.GET("account/admin/platform")
@md.requires_global_perms(
    "view_platform", "view_platform_security", "manage_platform", "admin")
def on_admin_platform(request):
    return _admin_platform.platform_overview(request)


@md.GET("account/admin/dashboard")
@md.requires_global_perms("view_admin", "manage_users", "manage_settings", "admin")
def on_admin_dashboard(request):
    # The page's refresh control. Bypasses the short provider cache and the
    # PyPI version cache; every collector remains individually bounded, so a
    # refresh cannot cost more than an ordinary read.
    refresh = str(request.DATA.get("refresh") or "").lower() in ("1", "true", "yes")
    return _admin_platform.dashboard_overview(request, refresh=refresh)


@md.GET("account/admin/advanced")
@md.requires_global_perms(
    "view_advanced", "view_advanced_inventory", "view_advanced_security",
    "view_advanced_settings", "manage_advanced", "admin")
def on_admin_advanced(request):
    return _admin_platform.advanced_overview(request)


@md.POST("account/admin/platform/deploy/retry")
@md.denies_key_backed_session()
@md.requires_fresh_auth(seconds=600)
@md.requires_global_perms("manage_platform", "admin")
def on_admin_platform_retry(request):
    from mojo.apps.edge.services import deploy
    source = _deployment(request.DATA.get("deployment"))
    replay_key = request.META.get("HTTP_IDEMPOTENCY_KEY")
    try:
        started, row = deploy.request_deploy(
            source.sha, actor=f"admin:{request.user.pk}", source="admin_retry",
            created_by=request.user, idempotency_key=replay_key, retry_of=source,
            return_deployment=True)
    except deploy.DeploymentCoordinationError:
        raise me.ValueException(
            "Deploy coordination unavailable", code=503, status=503) from None
    _admin_platform.audit_after_commit(request.user, "retry_same_sha", row.pk)
    from mojo.apps.edge.services import platform_deploy
    return {"schema_version": 1, "queued": started,
            "deployment": platform_deploy.serialize(row, include_stderr=True)}


@md.POST("account/admin/platform/deploy/verify")
@md.denies_key_backed_session()
@md.requires_fresh_auth(seconds=600)
@md.requires_global_perms("manage_platform", "admin")
def on_admin_platform_verify(request):
    from mojo.apps.edge.services import platform_deploy
    row = _deployment(request.DATA.get("deployment"))
    result = platform_deploy.verify(row.pk)
    _admin_platform.audit_after_commit(request.user, "verify", row.pk)
    return {"schema_version": 1, "deployment": platform_deploy.serialize(result, include_stderr=True)}


@md.POST("account/admin/platform/deploy/converge")
@md.denies_key_backed_session()
@md.requires_fresh_auth(seconds=600)
@md.requires_global_perms("manage_platform", "admin")
def on_admin_platform_converge(request):
    from mojo.apps.edge.services import platform_deploy
    row = _deployment(request.DATA.get("deployment"))
    result = platform_deploy.converge(row.pk)
    _admin_platform.audit_after_commit(request.user, "converge", row.pk)
    return {"schema_version": 1, "deployment": platform_deploy.serialize(result, include_stderr=True)}


@md.POST("account/admin/advanced/settings")
@md.denies_key_backed_session()
@md.requires_fresh_auth(seconds=600)
@md.requires_global_perms("manage_advanced", "admin")
def on_admin_advanced_settings(request):
    # Both writers re-read an active literal User superuser inside the service.
    target = ""
    if "auth" in request.DATA:
        value = system_settings.set_auth_safe_fields(request.user, request.DATA["auth"])
        action = "auth_safe_fields"
    elif "edge_topology" in request.DATA:
        value = system_settings.set_value(
            request.user, system_settings.EXPECTED_EDGE_TOPOLOGY,
            request.DATA["edge_topology"])
        action = "edge_topology"
    elif "framework_pin" in request.DATA:
        # Which django-mojo version the whole fleet installs. Protected for the
        # same reason the topology is: it decides what runs on every node, so
        # a global manage_settings grant must not reach it.
        from mojo.apps.edge.settings_validators import FRAMEWORK_VERSION_KEY
        value = system_settings.set_value(
            request.user, FRAMEWORK_VERSION_KEY, request.DATA["framework_pin"])
        action = "framework_pin"
        # The pin selects the code every node installs: "who set it to what"
        # must survive in the audit trail, and a distinct target keeps repeat
        # changes within the suppression window from collapsing into one event.
        target = value
    else:
        raise me.ValueException(
            "auth, edge_topology, or framework_pin is required")
    _admin_platform.audit_after_commit(request.user, action, target)
    return {"schema_version": 1, "saved": True, "value": value}
