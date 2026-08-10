"""Dedicated global authority for Platform and Advanced Admin controls."""

from mojo import decorators as md
from mojo import errors as me
from mojo.apps.account.services import admin_platform, system_settings


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
    return admin_platform.platform_overview(request)


@md.GET("account/admin/dashboard")
@md.requires_global_perms("view_admin", "manage_users", "admin")
def on_admin_dashboard(request):
    return admin_platform.dashboard_overview(request)


@md.GET("account/admin/advanced")
@md.requires_global_perms(
    "view_advanced", "view_advanced_inventory", "view_advanced_security",
    "view_advanced_settings", "manage_advanced", "admin")
def on_admin_advanced(request):
    return admin_platform.advanced_overview(request)


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
    admin_platform.audit_after_commit(request.user, "retry_same_sha", row.pk)
    from mojo.apps.edge.services import platform_deploy
    return {"schema_version": 1, "queued": started,
            "deployment": platform_deploy.serialize(row)}


@md.POST("account/admin/platform/deploy/verify")
@md.denies_key_backed_session()
@md.requires_fresh_auth(seconds=600)
@md.requires_global_perms("manage_platform", "admin")
def on_admin_platform_verify(request):
    from mojo.apps.edge.services import platform_deploy
    row = _deployment(request.DATA.get("deployment"))
    result = platform_deploy.verify(row.pk)
    admin_platform.audit_after_commit(request.user, "verify", row.pk)
    return {"schema_version": 1, "deployment": platform_deploy.serialize(result)}


@md.POST("account/admin/platform/deploy/converge")
@md.denies_key_backed_session()
@md.requires_fresh_auth(seconds=600)
@md.requires_global_perms("manage_platform", "admin")
def on_admin_platform_converge(request):
    from mojo.apps.edge.services import platform_deploy
    row = _deployment(request.DATA.get("deployment"))
    result = platform_deploy.converge(row.pk)
    admin_platform.audit_after_commit(request.user, "converge", row.pk)
    return {"schema_version": 1, "deployment": platform_deploy.serialize(result)}


@md.POST("account/admin/advanced/settings")
@md.denies_key_backed_session()
@md.requires_fresh_auth(seconds=600)
@md.requires_global_perms("manage_advanced", "admin")
def on_admin_advanced_settings(request):
    # Both writers re-read an active literal User superuser inside the service.
    if "auth" in request.DATA:
        value = system_settings.set_auth_safe_fields(request.user, request.DATA["auth"])
        action = "auth_safe_fields"
    elif "edge_topology" in request.DATA:
        value = system_settings.set_value(
            request.user, system_settings.EXPECTED_EDGE_TOPOLOGY,
            request.DATA["edge_topology"])
        action = "edge_topology"
    else:
        raise me.ValueException("auth or edge_topology is required")
    admin_platform.audit_after_commit(request.user, action)
    return {"schema_version": 1, "saved": True, "value": value}
