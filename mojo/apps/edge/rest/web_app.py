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


def _flag(value):
    """Strict truthiness for a request flag.

    `bool("false")` is True, and the browser sends form values as strings — so
    a plain bool() here would turn "false" into a certificate re-request. Only
    a real True or an explicit affirmative word counts; everything else, junk
    included, is False.
    """
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in ("1", "true", "yes", "on")


@md.POST('webapp/attach_domain')
@md.denies_key_backed_session()
@md.requires_fresh_auth(600)
@md.requires_params("webapp", "hostname")
@md.requires_perms("manage_webapp")
def on_webapp_attach_domain(request):
    """Point one more address at this site. Safe to call again at any point.

    The service's result is returned as-is: a status, a plain-language reason,
    and — when the caller has records to publish — the exact records. The UI's
    Check button is this same call, which is why it must stay side-effect-free
    for an address that is already attached.

    Same guard stack as `link_key`: no CI key (`denies_key_backed_session`),
    recent interactive auth, and an explicit object check, because
    `uses_model_security` does not gate a custom action and `requires_perms`
    gates the verb but not the row.
    """
    from mojo.apps.edge.services import webapp_alias

    web_app = WebApp.get_instance_or_404(request.DATA.get("webapp"))
    WebApp.rest_check_permission_or_raise(
        request, ["SAVE_PERMS", "VIEW_PERMS"], web_app)
    result = webapp_alias.attach(
        web_app, request.DATA.get("hostname"), request.user,
        retry_certificate=_flag(request.DATA.get("retry_certificate")))
    return dict(webapp=web_app.pk, **result)


@md.POST('webapp/detach_domain')
@md.denies_key_backed_session()
@md.requires_fresh_auth(600)
@md.requires_params("webapp", "vhost")
@md.requires_perms("manage_webapp")
def on_webapp_detach_domain(request):
    """Remove one extra address from this site. The site itself stays up.

    The address is scoped to this site — and to its ALIASES specifically — so a
    foreign or primary address 404s instead of leaking that it exists. Taking
    the site's own address down is `detach_address`, deliberately a different,
    louder action.
    """
    from mojo.apps.edge.models import Vhost
    from mojo.apps.edge.services import webapp_alias

    web_app = WebApp.get_instance_or_404(request.DATA.get("webapp"))
    WebApp.rest_check_permission_or_raise(
        request, ["SAVE_PERMS", "VIEW_PERMS"], web_app)
    vhost = Vhost.objects.filter(
        pk=request.DATA.get("vhost"), alias_of=web_app).first()
    if vhost is None:
        raise me.RestErrorException("address not found", code=404, status=404)
    return dict(webapp=web_app.pk, **webapp_alias.detach(web_app, vhost))


@md.GET('webapp/aliases')
@md.denies_key_backed_session()
@md.requires_params("webapp")
@md.requires_perms("view_dns", "manage_dns", "security")
def on_webapp_aliases(request):
    """Every address this site answers on: its own first, then the extra ones.

    A read, so no step-up: seeing which addresses a site serves is exactly what
    a viewer of the site is entitled to. The rows carry no provider round trip.
    """
    from mojo.apps.edge.services import webapp_alias

    web_app = WebApp.get_instance_or_404(request.DATA.get("webapp"))
    WebApp.rest_check_permission_or_raise(
        request, ["VIEW_PERMS", "SAVE_PERMS"], web_app)
    return dict(webapp=web_app.pk,
                addresses=webapp_alias.status_rows(web_app))


@md.GET('webapp/serving')
@md.denies_key_backed_session()
@md.requires_params("webapp")
@md.requires_perms("view_dns", "manage_dns", "security")
def on_webapp_serving(request):
    """How this site is served: address, certificate, shape, and paths.

    A read, so no step-up — the same entitlement as `webapp/aliases`. The
    caller's WRITE authority is evaluated separately and non-raisingly: only a
    caller who could actually save is told which fleet pools and which
    destinations exist, because that inventory is the deployment's topology
    rather than anything about this one site.
    """
    from mojo.apps.edge.services import webapp_serving

    web_app = WebApp.get_instance_or_404(request.DATA.get("webapp"))
    WebApp.rest_check_permission_or_raise(
        request, ["VIEW_PERMS", "SAVE_PERMS"], web_app)
    include_editables = WebApp.rest_check_permission(
        request, ["SAVE_PERMS"], web_app)
    return webapp_serving.serving_for(
        web_app, include_editables=include_editables)


@md.POST('webapp/serving')
@md.denies_key_backed_session()
@md.requires_fresh_auth(600)
@md.requires_params("webapp")
@md.requires_perms("manage_webapp")
def on_webapp_serving_save(request):
    """Change the fleet pool, the single-page fallback, or the certificate.

    Applies to the site's own address AND every extra address it answers on —
    a pool move that landed on one but not the others would leave a custom
    domain on a node fleet that never installs the release behind it.

    Same guard stack as `attach_domain`: no CI key, recent interactive auth,
    and an explicit object check, because `uses_model_security` does not gate
    a custom action and `requires_perms` gates the verb but not the row.
    """
    from mojo.apps.edge.services import webapp_serving

    web_app = WebApp.get_instance_or_404(request.DATA.get("webapp"))
    WebApp.rest_check_permission_or_raise(
        request, ["SAVE_PERMS", "VIEW_PERMS"], web_app)
    return webapp_serving.apply(web_app, request.DATA)


@md.POST('webapp/certificate')
@md.denies_key_backed_session()
@md.requires_fresh_auth(600)
@md.requires_params("webapp")
@md.requires_perms("manage_webapp")
def on_webapp_certificate(request):
    """Request a certificate covering this site's address alone.

    Requesting only. Switching the site onto it is a separate `webapp/serving`
    save once it is active and really covers the name — a site pointed at a
    pending certificate would serve nothing.

    `request.user` is passed as the actor: when the address sits on an
    ANCESTOR's domain, ordering a certificate is a write against that zone and
    needs authority in the group that owns it.
    """
    from mojo.apps.edge.services import webapp_serving

    web_app = WebApp.get_instance_or_404(request.DATA.get("webapp"))
    WebApp.rest_check_permission_or_raise(
        request, ["SAVE_PERMS", "VIEW_PERMS"], web_app)
    webapp_serving.request_dedicated_certificate(web_app, request.user)
    return webapp_serving.serving_for(web_app, include_editables=True)


@md.POST('webapp/add_route')
@md.denies_key_backed_session()
@md.requires_fresh_auth(600)
@md.requires_params("webapp", "path_prefix", "upstream")
@md.requires_perms("manage_webapp")
def on_webapp_add_route(request):
    """Send one path to a declared destination, on every address of this site."""
    from mojo.apps.edge.services import webapp_serving

    web_app = WebApp.get_instance_or_404(request.DATA.get("webapp"))
    WebApp.rest_check_permission_or_raise(
        request, ["SAVE_PERMS", "VIEW_PERMS"], web_app)
    return webapp_serving.add_route(
        web_app, request.DATA.get("path_prefix"), request.DATA.get("upstream"))


@md.POST('webapp/remove_route')
@md.denies_key_backed_session()
@md.requires_fresh_auth(600)
@md.requires_params("webapp", "path_prefix")
@md.requires_perms("manage_webapp")
def on_webapp_remove_route(request):
    """Stop sending one path elsewhere, on every address of this site.

    The platform's own sign-in and account paths are refused here: they are
    derived from the resolved hosted-auth contract, not stored as a flag, so a
    caller cannot delete the routes that make logging in work.
    """
    from mojo.apps.edge.services import webapp_serving

    web_app = WebApp.get_instance_or_404(request.DATA.get("webapp"))
    WebApp.rest_check_permission_or_raise(
        request, ["SAVE_PERMS", "VIEW_PERMS"], web_app)
    return webapp_serving.remove_route(web_app, request.DATA.get("path_prefix"))


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
