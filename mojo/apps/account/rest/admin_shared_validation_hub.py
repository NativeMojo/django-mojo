"""One-time sharing enablement through the ordinary authenticated Admin."""
import mojo.decorators as md
from mojo import errors as me
from .admin_fleet import _actor
from .admin_shared_validation import _private, _installed


@md.GET("account/admin/fleet/shared-validation/hub")
@_private
@md.denies_key_backed_session(deny_user_api_keys=True)
@md.requires_global_perms("admin")
def on_shared_validation_hub(request):
    actor = _actor(request)
    if request.DATA:
        raise me.ValueException("invalid_request")
    if not _installed():
        raise me.ValueException("dnsman_unavailable", code=503, status=503)
    from mojo.apps.dnsman.services import acme_hub_upgrade
    return acme_hub_upgrade.status(actor)


@md.POST("account/admin/fleet/shared-validation/hub")
@_private
@md.denies_key_backed_session(deny_user_api_keys=True)
@md.requires_fresh_auth(seconds=600)
@md.requires_global_perms("admin")
def on_shared_validation_hub_enable(request):
    actor = _actor(request, write=True)
    if set(request.DATA) != {"deployment_uuid"}:
        raise me.ValueException("invalid_request")
    if not _installed():
        raise me.ValueException("dnsman_unavailable", code=503, status=503)
    from mojo.apps.dnsman.services import acme_hub_upgrade
    return acme_hub_upgrade.enable(actor, request.DATA["deployment_uuid"])
