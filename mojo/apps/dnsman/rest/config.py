import mojo.decorators as md
from mojo.apps.dnsman.models import Domain
from mojo.apps.dnsman.services import config as config_service


@md.GET('config')
def on_config(request):
    """
    Capability discovery: what's turned on, without probing a gated action.

    Gated on view_dns (not public) -- this is operator configuration, not
    tenant data, and not group-scoped: nothing here varies per group.
    """
    Domain.rest_check_permission_or_raise(request, "VIEW_PERMS")
    return config_service.get_config()
