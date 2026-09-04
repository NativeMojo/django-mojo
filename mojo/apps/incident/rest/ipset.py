from mojo import decorators as md
from mojo import errors as merrors
from mojo.apps.incident.models import IPSet
from mojo.apps.incident.services import admin_security


@md.POST('ipset/action')
@md.denies_key_backed_session()
@md.requires_fresh_auth(seconds=600)
@md.requires_global_perms("manage_security", "security")
def on_ipset_action(request):
    data = dict(request.DATA)
    action = data.get("action")
    if isinstance(action, str) and not action.startswith("ipset."):
        data["action"] = f"ipset.{action}"
    try:
        return admin_security.apply_action(data, request.user)
    except admin_security.SecurityActionError as err:
        raise merrors.ValueException(
            str(err), code=err.status, status=err.status) from err


@md.URL('ipset')
@md.URL('ipset/<int:pk>')
def on_ipset(request, pk=None):
    return IPSet.on_rest_request(request, pk)
