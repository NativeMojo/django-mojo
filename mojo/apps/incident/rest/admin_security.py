"""Dedicated human authority for Admin Security reads and actions."""

from mojo import decorators as md
from mojo import errors as merrors
from mojo.apps.incident.services import admin_security


def _translate(call):
    try:
        return call()
    except admin_security.SecurityActionError as error:
        raise merrors.ValueException(
            str(error), code=error.code, status=error.status) from error


@md.GET("admin/security")
@md.requires_auth()
@md.custom_security("server-derived global or exact-group Admin Security scope")
def on_admin_security(request):
    authority = admin_security.build_authority(request)
    return _translate(
        lambda: admin_security.overview(request.DATA, authority=authority))


@md.POST("admin/security/action")
@md.requires_auth()
@md.requires_fresh_auth()
@md.custom_security("global User or validated UserAPIKey Admin Security mutation")
def on_admin_security_action(request):
    authority = admin_security.build_authority(request, write=True)
    return _translate(
        lambda: admin_security.apply_action(
            request.DATA, request.user, authority=authority,
            actor_context=authority.actor_context))
