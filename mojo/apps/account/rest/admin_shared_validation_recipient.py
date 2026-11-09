"""Exact named hub Group lookup and protected-purpose credential issuance."""
import mojo.decorators as md
from mojo import errors as me
from .admin_fleet import _actor
from .admin_shared_validation import _private, _installed


@md.GET("account/admin/fleet/shared-validation/recipient")
@_private
@md.denies_key_backed_session(deny_user_api_keys=True)
@md.requires_global_perms("admin")
def on_shared_validation_recipient(request):
    _actor(request)
    if not _installed():
        raise me.ValueException("dnsman_unavailable", code=503, status=503)
    from mojo.apps.dnsman.services import recipient_keys
    return recipient_keys.lookup(request, dict(request.DATA))


@md.POST("account/admin/fleet/shared-validation/recipient")
@_private
@md.denies_key_backed_session(deny_user_api_keys=True)
@md.requires_fresh_auth(seconds=600)
@md.requires_global_perms("admin")
def on_shared_validation_recipient_issue(request):
    _actor(request, write=True)
    if not _installed():
        raise me.ValueException("dnsman_unavailable", code=503, status=503)
    from mojo.apps.dnsman.services import recipient_keys
    return recipient_keys.issue(request, dict(request.DATA))
