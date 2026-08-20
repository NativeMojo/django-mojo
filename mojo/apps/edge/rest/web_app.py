"""Site management and one-time WebApp deployment-key linkage."""

import mojo.decorators as md

from mojo import errors as me
from mojo.apps.edge.models import WebApp, WebAppDeployment, WebAppRelease


@md.URL('webapp')
@md.URL('webapp/<int:pk>')
@md.uses_model_security(WebApp)
def on_webapp(request, pk=None):
    """CRUD for sites.

    `bucket`, `prefix`, `api_key` and `current_release` are all in
    NO_SAVE_FIELDS — see the model for why `bucket` in particular must never be
    caller-chosen. DELETE routes through `WebApp.on_rest_delete`, which tears
    the deploy key and serving vhost down atomically.
    """
    return WebApp.on_rest_request(request, pk)


@md.URL('release')
@md.URL('release/<int:pk>')
@md.uses_model_security(WebAppRelease)
def on_release(request, pk=None):
    """Read-only release history. Rows are made by `edge/release` only."""
    return WebAppRelease.on_rest_request(request, pk)


@md.URL('deployment')
@md.URL('deployment/<int:pk>')
@md.uses_model_security(WebAppDeployment)
def on_deployment(request, pk=None):
    """Read-only fleet-convergence history. Filter a list with `?webapp=<id>`.

    Group-scoped by `webapp__group` in the model's RestMeta, so a tenant only
    ever sees their own deployments. Rows are made by promote/rollback only.
    """
    return WebAppDeployment.on_rest_request(request, pk)


@md.POST('webapp/link_key')
@md.denies_key_backed_session()
@md.requires_fresh_auth(600)
@md.requires_params("webapp", "operation_id", "action")
@md.requires_perms("manage_webapp")
def on_webapp_link_key(request):
    """Create or rotate this site's ``MOJO_DEPLOY_KEY``. Returns it once.

    Without this the `api_key` FK stays null forever and the identity check in
    `rest/release.py` is inert.

    The explicit `rest_check_permission_or_raise` is required: this endpoint
    fetches by pk and `uses_model_security` does not gate custom actions, so
    without it `manage_webapp` in any group would reach any site.
    """
    from mojo.apps.edge.services import webapp_keys

    web_app = WebApp.get_instance_or_404(request.DATA.get("webapp"))
    WebApp.rest_check_permission_or_raise(
        request, ["SAVE_PERMS", "VIEW_PERMS"], web_app)

    action = str(request.DATA.get("action", "")).strip().lower()
    result = webapp_keys.link_once(
        web_app, action, request.user, request.DATA.get("operation_id"))
    return dict(webapp=web_app.pk, secret_name="MOJO_DEPLOY_KEY", **result)


@md.GET('webapp/key_status')
@md.denies_key_backed_session()
@md.requires_params("webapp")
@md.requires_perms("view_dns", "manage_dns", "security")
def on_webapp_key_status(request):
    """Safe credential metadata; never exports the deployment token."""
    from mojo.apps.edge.services import webapp_keys

    web_app = WebApp.get_instance_or_404(request.DATA.get("webapp"))
    WebApp.rest_check_permission_or_raise(
        request, ["VIEW_PERMS", "SAVE_PERMS"], web_app)
    return dict(
        webapp=web_app.pk,
        secret_name="MOJO_DEPLOY_KEY",
        status=webapp_keys.status(web_app),
    )


@md.POST('webapp/revoke_key')
@md.denies_key_backed_session()
@md.requires_fresh_auth(300)
@md.requires_params("webapp", "operation_id")
@md.requires_perms("manage_webapp")
def on_webapp_revoke_key(request):
    """Deactivate and unlink this site's deployment credential."""
    from mojo.apps.edge.services import webapp_keys

    web_app = WebApp.get_instance_or_404(request.DATA.get("webapp"))
    WebApp.rest_check_permission_or_raise(
        request, ["SAVE_PERMS", "VIEW_PERMS"], web_app)
    result = webapp_keys.revoke_once(
        web_app, request.user, request.DATA.get("operation_id"))
    return dict(webapp=web_app.pk, secret_name="MOJO_DEPLOY_KEY", **result)


@md.POST('webapp/rollback')
@md.denies_key_backed_session()
@md.requires_fresh_auth(600)
@md.requires_params("webapp", "release")
@md.requires_perms("manage_webapp")
def on_webapp_rollback(request):
    """Repoint a site at an already-verified earlier release.

    Human-only by construction: `denies_key_backed_session` keeps CI keys out,
    so the "deployment starts only from release completion" invariant still
    holds for automation. This adds no capability an interactive `manage_dns`
    holder lacked — they can already register and auto-deploy a release by hand.
    The explicit object check is required: `requires_perms` gates the verb but
    not the row, so without it `manage_webapp` in any group would reach any site.
    """
    from mojo.apps.edge.services import releases, webapp_deploy

    web_app = WebApp.get_instance_or_404(request.DATA.get("webapp"))
    WebApp.rest_check_permission_or_raise(
        request, ["SAVE_PERMS", "VIEW_PERMS"], web_app)
    # Scope the release to this site so a foreign id 404s instead of leaking
    # that it exists; promote() then re-checks it is promotable (not pending).
    release = WebAppRelease.objects.filter(
        pk=request.DATA.get("release"), webapp=web_app).first()
    if release is None:
        raise me.RestErrorException("release not found", code=404, status=404)
    deployment = releases.promote(web_app, release, request.user)
    return webapp_deploy.payload(deployment)


@md.POST('webapp/detach_address')
@md.denies_key_backed_session()
@md.requires_fresh_auth(600)
@md.requires_params("webapp")
@md.requires_perms("manage_webapp")
def on_webapp_detach_address(request):
    """Take a site offline: unlink and delete its serving vhost, keep the app.

    The vhost's own `delete()` publishes fleet convergence, so nodes drop the
    server block without waiting for the sweep.

    Every ALIAS address goes too, in the same transaction. "Offline" that left
    the customer's own domain serving would be the opposite of what was asked,
    and desired state drops an app's alias rows the moment it has no primary
    anyway (see `releases.desired_webapps`).
    """
    from django.db import transaction

    from mojo.apps.edge.models import Vhost

    web_app = WebApp.get_instance_or_404(request.DATA.get("webapp"))
    WebApp.rest_check_permission_or_raise(
        request, ["SAVE_PERMS", "VIEW_PERMS"], web_app)
    with transaction.atomic():
        locked = WebApp.objects.select_for_update().get(pk=web_app.pk)
        vhost = locked.vhost
        if vhost is not None:
            locked.vhost = None
            locked.save(update_fields=["vhost", "modified"])
            if vhost.kind == "site":
                vhost.delete()
        for alias in Vhost.objects.filter(alias_of=locked):
            alias.delete()
    return dict(webapp=web_app.pk, address=None)


@md.GET('webapp/health')
@md.denies_key_backed_session()
@md.requires_params("webapp")
@md.requires_perms("view_dns", "manage_dns", "security")
def on_webapp_health(request):
    """On-demand public HTTPS reachability for this site's live address.

    A vhost-less site is `not_configured`, not `unhealthy`: nothing is meant to
    be serving yet. Only a safe status/detail is returned — never a raw probe
    exception, which can carry address internals.
    """
    from django.utils import timezone
    from mojo.apps.edge.services.public_probe import (
        UnsafePublicProbe, probe_https_root)

    web_app = WebApp.get_instance_or_404(request.DATA.get("webapp"))
    WebApp.rest_check_permission_or_raise(
        request, ["VIEW_PERMS", "SAVE_PERMS"], web_app)
    if web_app.vhost_id is None:
        return dict(webapp=web_app.pk, status="not_configured",
                    checked=None, detail="No public address is configured yet")
    checked = timezone.now().isoformat()
    try:
        result = probe_https_root(f"https://{web_app.vhost.server_name}")
    except UnsafePublicProbe as err:
        return dict(webapp=web_app.pk, status="unhealthy",
                    checked=checked, detail=str(err))
    if result.get("ok"):
        return dict(webapp=web_app.pk, status="healthy",
                    checked=checked, detail=f"HTTP {result.get('status')}")
    detail = (f"HTTP {result.get('status')}" if result.get("status")
              else result.get("reason") or "unreachable")
    return dict(webapp=web_app.pk, status="unhealthy", checked=checked,
                detail=detail)
