import mojo.decorators as md

from mojo.apps.dnsman.rest.gates import require_platform_admin
from mojo.apps.edge.models import Vhost, VhostRoute


def _guard_house_route(request, route, what):
    """A route on a house-domain vhost is platform property.

    Same reasoning as the vhost guard it mirrors (rest/vhost.py): the route
    scopes through GROUP_FIELD = "vhost__domain__group", which resolves to
    None for a house domain — and a None group falls the permission check
    through to the caller's GLOBAL permissions, so any global manage_dns
    holder would pass. This guard is LOAD-BEARING, not redundant.
    """
    if route.vhost.domain.group_id is None:
        require_platform_admin(request, what)


@md.URL('route')
@md.URL('route/<int:pk>')
@md.uses_model_security(VhostRoute)
def on_route(request, pk=None):
    """CRUD for site_api proxied prefixes.

    Model permission check first, then the house guard — the same no-oracle
    ordering as the vhost detail route and for the same reason: pks are
    sequential, and leading with the platform guard would let an
    unauthenticated caller classify house serving inventory by status code.
    """
    if pk is not None:
        route = VhostRoute.get_instance_or_404(pk)
        VhostRoute.rest_check_permission_or_raise(request, ["VIEW_PERMS"], route)
        _guard_house_route(request, route, "House vhost routes")
    else:
        _guard_house_vhost_route_create(request)
    return VhostRoute.on_rest_request(request, pk)


def _guard_house_vhost_route_create(request):
    """CREATING a route on a house vhost is also platform-only.

    Same gap the vhost create guard closes (rest/vhost.py): the collection
    POST checks SAVE_PERMS with no instance, which for a house vhost falls
    through to global permissions — so a global manage_dns holder could bolt
    a proxied prefix onto the PLATFORM's own site. The caller supplied the
    vhost id, so refusing tells them nothing they did not already assert.
    """
    vhost_pk = request.DATA.get("vhost", None)
    if not vhost_pk:
        return
    vhost = Vhost.objects.filter(pk=vhost_pk).select_related("domain").first()
    if vhost is not None and vhost.domain.group_id is None:
        require_platform_admin(request, "Creating a route on a house vhost")
