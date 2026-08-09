"""
The CI-facing release endpoints.

`release_webapp` is the only permission CI holds. It is linked to exactly one
WebApp; verified completion follows that WebApp's automatic-deploy policy and
the deployment status endpoint exposes only that same WebApp.
"""

import mojo.decorators as md
from mojo import errors as me
from mojo.models.rest import _resolve_stamp_actor

from mojo.apps.edge.models import WebApp, WebAppDeployment, WebAppRelease
from mojo.apps.edge.services import releases, webapp_deploy


def _authorized_webapp(request, pk):
    """Resolve a WebApp this caller may release, or refuse.

    Two checks, and the null case is the one that bites:

    1. The caller's ApiKey must BE the site's linked key — a direct FK identity
       comparison, not a permissions lookup. One key per site is enforced by
       the schema (`WebApp.api_key` is a OneToOne), so this cannot drift.
    2. **Fail closed when the site has no key.** `api_key_id is None` refuses.
       A falsy-tolerant comparison would turn an unlinked site into one that
       ANY key can release, which is the opposite of the intent.

    An interactive user holding manage_dns also passes, so a human can register
    a release by hand; they are already trusted with the site's configuration.
    """
    web_app = WebApp.objects.filter(pk=pk).select_related("group").first()
    not_found = lambda: me.ValueException(
        "Web app not found", code=404, status=404)
    if web_app is None:
        raise not_found()

    api_key = getattr(request, "api_key", None)
    if api_key is not None:
        if web_app.api_key_id is None or api_key.pk != web_app.api_key_id:
            # Sequential pks must not classify "another tenant's site" against
            # "a site with no key yet".
            raise not_found()
        if not api_key.has_permission("release_webapp"):
            raise me.PermissionDeniedException(
                "This key may not register releases",
                branch="edge.release.key_missing_perm")
        return web_app

    try:
        WebApp.rest_check_permission_or_raise(
            request, ["SAVE_PERMS", "VIEW_PERMS"], web_app)
    except me.PermissionDeniedException:
        raise not_found()
    return web_app


@md.POST('release')
@md.requires_params("webapp", "version", "manifest")
@md.custom_security("site ApiKey identity, or manage_dns on the WebApp, in body")
def on_release_register(request):
    """Declare a release and receive one upload URL per file.

    The manifest is declared BEFORE the upload precisely so every object key is
    known here — that is what makes a per-object presigned URL possible instead
    of a prefix-wide credential.
    """
    web_app = _authorized_webapp(request, request.DATA.get("webapp"))

    release, uploads = releases.register(
        web_app,
        request.DATA.get("version"),
        request.DATA.get("manifest"),
        # _resolve_stamp_actor, not a hand-rolled check: for a reference-mode
        # ApiKey `request.user` IS the ApiKey, and it has a pk — so the obvious
        # `user.pk` test passes and hands a non-User to a ForeignKey, which is
        # a 500 on the CI happy path. This helper returns None for every
        # non-model identity (ApiKey, ANONYMOUS_USER, the system pseudo-user).
        user=_resolve_stamp_actor(request),
    )
    return dict(
        release=release.pk,
        version=release.version,
        status=release.status,
        bucket=web_app.bucket,
        prefix=release.storage_prefix(),
        uploads=uploads,
    )


@md.POST('release/complete')
@md.requires_params("release")
@md.custom_security("site ApiKey identity, or manage_dns on the WebApp, in body")
def on_release_complete(request):
    """Verify the upload and start fleet deployment when automatic is on."""
    release = WebAppRelease.objects.filter(
        pk=request.DATA.get("release")).select_related("webapp").first()
    if release is None:
        raise me.ValueException("Release not found", code=404, status=404)

    # Re-authorize against the OWNING site rather than trusting the release id.
    web_app = _authorized_webapp(request, release.webapp_id)

    releases.complete(release)
    deployment = releases.maybe_auto_promote(release)
    release.refresh_from_db()
    return dict(
        release=release.pk,
        version=release.version,
        status=release.status,
        promoted=bool(deployment),
        deployment=deployment.pk if deployment else None,
        deployment_status=deployment.status if deployment else None,
        webapp=web_app.pk,
    )


@md.GET('release/deployment/<int:pk>')
@md.custom_security("owning site's linked ApiKey identity, or WebApp viewer")
def on_release_deployment(request, pk):
    """Return CI-safe fleet status for one WebApp deployment."""
    deployment = WebAppDeployment.objects.filter(pk=pk).select_related(
        "webapp", "release").first()
    if deployment is None:
        raise me.ValueException("Deployment not found", code=404, status=404)
    _authorized_webapp(request, deployment.webapp_id)
    return webapp_deploy.payload(deployment)
