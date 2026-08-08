import mojo.decorators as md

from mojo.apps.edge.models import BlocklistEntry


@md.URL('blocklist')
@md.URL('blocklist/<int:pk>')
@md.uses_model_security(BlocklistEntry)
def on_blocklist(request, pk=None):
    """CRUD for the fleet blocklist.

    Plain model security, no house guard: the model is group-less on purpose
    (fleet-scoped), so the permission check resolves on the caller's GLOBAL
    security grants only — a member-scoped grant plus `?group=` opens
    nothing, which the tests pin.
    """
    return BlocklistEntry.on_rest_request(request, pk)
