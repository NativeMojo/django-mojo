"""Site management and one-time WebApp deployment-key linkage."""

import mojo.decorators as md

from mojo.apps.edge.models import WebApp, WebAppRelease


@md.URL('webapp')
@md.URL('webapp/<int:pk>')
@md.uses_model_security(WebApp)
def on_webapp(request, pk=None):
    """CRUD for sites.

    `bucket`, `prefix`, `api_key` and `current_release` are all in
    NO_SAVE_FIELDS — see the model for why `bucket` in particular must never be
    caller-chosen.
    """
    return WebApp.on_rest_request(request, pk)


@md.URL('release')
@md.URL('release/<int:pk>')
@md.uses_model_security(WebAppRelease)
def on_release(request, pk=None):
    """Read-only release history. Rows are made by `edge/release` only."""
    return WebAppRelease.on_rest_request(request, pk)


@md.POST('webapp/link_key')
@md.denies_key_backed_session()
@md.requires_fresh_auth(300)
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
