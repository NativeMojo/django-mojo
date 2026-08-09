"""
Site management: CRUD, key linkage, and the promote/rollback action.

The linked release credential can complete the site's normal automatic deploy;
`manage_webapp` remains the authority for manual promote/rollback and key
rotation.
"""

import mojo.decorators as md
from mojo import errors as me

from mojo.apps.edge.models import WebApp, WebAppRelease
from mojo.apps.edge.services import releases


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
    from mojo.apps.account.models import ApiKey

    web_app = WebApp.get_instance_or_404(request.DATA.get("webapp"))
    WebApp.rest_check_permission_or_raise(
        request, ["SAVE_PERMS", "VIEW_PERMS"], web_app)

    previous = web_app.api_key
    api_key, token = ApiKey.create_for_group(
        web_app.group,
        f"webapp:{web_app.slug}",
        permissions={"release_webapp": True})

    web_app.api_key = api_key
    web_app.save()

    if previous is not None:
        # Hard cutover, no grace window: two live credentials for one site is
        # exactly the state that makes revocation unprovable. Updating CI is
        # cheap; an un-revocable second key is not.
        previous.is_active = False
        previous.save()

    web_app.log(f"Release key minted for '{web_app.slug}'", "edge:webapp_key")
    return dict(webapp=web_app.pk, api_key=api_key.pk, token=token,
                revoked_previous=bool(previous))


@md.POST('webapp/promote')
@md.requires_params("webapp", "release")
@md.requires_perms("manage_webapp")
def on_webapp_promote(request):
    """Make a release live. **Rollback is this same call** with an older id.

    Explicit permission check for the same reason as `link_key` above.
    """
    web_app = WebApp.get_instance_or_404(request.DATA.get("webapp"))
    WebApp.rest_check_permission_or_raise(
        request, ["SAVE_PERMS", "VIEW_PERMS"], web_app)

    release = WebAppRelease.objects.filter(
        pk=request.DATA.get("release"), webapp=web_app).first()
    if release is None:
        raise me.ValueException("Release not found", code=404, status=404)

    deployment = releases.promote(
        web_app, release, user=getattr(request, "user", None))
    web_app.refresh_from_db()
    return dict(
        webapp=web_app.pk,
        current_release=web_app.current_release_id,
        version=release.version,
        status=release.status,
        deployment=deployment.pk,
        deployment_status=deployment.status,
    )
