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
@md.denies_key_backed_session()
@md.requires_global_perms("view_security", "manage_security", "security")
def on_admin_security(request):
    return _translate(lambda: admin_security.overview(request.DATA))


@md.POST("admin/security/action")
@md.denies_key_backed_session()
@md.requires_fresh_auth(seconds=600)
@md.requires_global_perms("manage_security", "security")
def on_admin_security_action(request):
    return _translate(
        lambda: admin_security.apply_action(request.DATA, request.user))
