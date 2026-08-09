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
@md.requires_params("webapp")
@md.requires_perms("manage_webapp")
def on_webapp_link_key(request):
    """Mint and link this site's CI credential. Returns the token once.

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

    web_app, api_key, token, rotated = webapp_keys.link(
        web_app, rotate=True)
    return dict(webapp=web_app.pk, api_key=api_key.pk, token=token,
                revoked_previous=rotated)
