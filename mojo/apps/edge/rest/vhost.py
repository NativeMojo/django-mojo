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

    The LIST path has its own queryset guard on `Vhost`: non-superusers never
    receive house rows, including callers with global `manage_dns`. This
    function guards the per-instance paths and preserves their no-oracle
    ordering.
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


@md.POST('vhost/claim_reserved')
@md.requires_params("vhost")
@md.custom_security("require_platform_admin gate in body")
def on_vhost_claim_reserved(request):
    """Set (or release) the reserved-name house override on one vhost.

    `claims_reserved` suspends `validate_not_reserved` for exactly one row —
    the mechanism that lets the platform serve its OWN reserved hostnames
    through the edge instead of a hand-managed file. It is in NO_SAVE_FIELDS,
    so this action is the only writer.

    The gate runs FIRST, like `upstream/declare`: the caller supplied the
    vhost id themselves, so there is no classification oracle to protect,
    and a caller who may not claim should learn that before anything is
    loaded. The model save then re-checks the substance: the vhost must sit
    on a HOUSE domain, and the deployment must have DECLARED its reserved
    set — the override never turns fail-closed into fail-open.
    """
    require_platform_admin(request, "Claiming a reserved server name")
    vhost = Vhost.get_instance_or_404(request.DATA.get("vhost"))
    release = request.DATA.get("release", False) in (True, "true", "True", 1, "1")
    vhost.claims_reserved = not release
    # save() -> validate_vhost enforces house-domain-only and the
    # declared-set requirement; a refusal leaves the flag untouched in the DB.
    vhost.save()
    action = "released" if release else "claimed"
    vhost.log(
        f"Reserved-name override {action} for {vhost.server_name}",
        "edge:claim_reserved")
    return vhost.on_rest_get(request)


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
