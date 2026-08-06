import mojo.decorators as md

from mojo.apps.dnsman.rest.gates import require_platform_admin
from mojo.apps.edge.models import Vhost


def _guard_house_vhost(request, vhost, what):
    """
    A vhost on a group-less (house) domain is platform property.

    Vhost scopes through RestMeta.GROUP_FIELD = "domain__group". For a house
    domain that resolution yields None, and the permission check falls through
    to the caller's GLOBAL permissions — so any holder of a global manage_dns
    grant passes a check that was never about their group at all. This guard is
    LOAD-BEARING, not redundant with the framework check.

    Same shape as `mojo/apps/dnsman/rest/certificate.py::_guard_house_certificate`,
    and for the same reason: the platform's own serving configuration must not
    be reachable by a tenant-level grant.

    The LIST path is unaffected (`domain__group=<group>` cannot match a null),
    so this guards the per-instance paths.
    """
    if vhost.domain.group_id is None:
        require_platform_admin(request, what)


@md.URL('vhost')
@md.URL('vhost/<int:pk>')
@md.uses_model_security(Vhost)
def on_vhost(request, pk=None):
    """CRUD for vhosts.

    The model permission check runs FIRST, then the house guard — the opposite
    of `upstream/declare` and deliberately so. Vhost pks are sequential, so
    leading with the platform guard would let an unauthenticated caller tell a
    house vhost (403 "platform administrators") from a tenant one (401): a free
    classification oracle over the platform's own serving inventory. Same
    ordering, and the same reason, as dnsman's certificate detail route.
    """
    if pk is not None:
        vhost = Vhost.get_instance_or_404(pk)
        Vhost.rest_check_permission_or_raise(request, ["VIEW_PERMS"], vhost)
        _guard_house_vhost(request, vhost, "House vhosts")
    else:
        _guard_house_domain_create(request)
    return Vhost.on_rest_request(request, pk)


def _guard_house_domain_create(request):
    """CREATING a vhost on a house domain is also platform-only.

    Found by the post-build security review. Guarding only the per-instance
    paths left the create path open: `on_rest_handle_create` checks SAVE_PERMS
    with no instance, which for a group-less domain falls through to the
    caller's GLOBAL permissions — so a global `manage_dns` holder who cannot
    *read* a house vhost could still *mint* one, claiming a new serving name on
    a platform-owned zone with a valid house certificate. `Certificate` avoids
    this by setting `CAN_CREATE = False`; a vhost has to be creatable, so the
    domain is checked here instead.

    Unlike the read path there is no oracle to protect: the caller supplied the
    domain id, so refusing tells them nothing they did not already assert.
    """
    from mojo.apps.dnsman.models import Domain

    domain_pk = request.DATA.get("domain", None)
    if not domain_pk:
        return
    domain = Domain.objects.filter(pk=domain_pk).first()
    if domain is not None and domain.group_id is None:
        require_platform_admin(request, "Creating a vhost on a house domain")
