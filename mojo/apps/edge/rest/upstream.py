import mojo.decorators as md
from mojo import errors as me

from mojo.apps.dnsman.rest.gates import require_platform_admin
from mojo.apps.edge.models import Upstream
from mojo.apps.edge.models.upstream import KIND_HTTP, KIND_UNIX


@md.URL('upstream')
@md.URL('upstream/<int:pk>')
@md.uses_model_security(Upstream)
def on_upstream(request, pk=None):
    """Read and toggle declared upstreams.

    `RestMeta.NO_SAVE_FIELDS` pins every field that decides WHERE traffic goes,
    so the only thing a `manage_dns` holder can write here is `is_enabled`.
    Creating one is `upstream/declare` below.
    """
    return Upstream.on_rest_request(request, pk)


@md.POST('upstream/declare')
@md.requires_params("name", "kind")
@md.custom_security("require_platform_admin gate in body")
def on_upstream_declare(request):
    """Declare a new upstream. **Platform administrators only.**

    This is the allowlist that makes `Vhost.upstream` meaningful. If a tenant
    admin could add a row here, the foreign key would stop being a constraint
    and become a text field with extra steps — they would simply declare an
    upstream pointing at the metadata service and select it.

    The gate runs FIRST here, unlike the per-instance routes below and unlike
    dnsman's certificate routes. There is no instance to classify and no
    sequential pk in play, so there is no oracle to protect against — and a
    caller who may not declare should learn that before their payload is
    parsed.
    """
    require_platform_admin(request, "Declaring an edge upstream")

    kind = request.DATA.get("kind")
    if kind not in (KIND_HTTP, KIND_UNIX):
        raise me.ValueException("kind must be 'http' or 'unix'")

    group = None
    group_pk = request.DATA.get("group", None)
    if group_pk:
        from mojo.apps.account.models import Group
        group = Group.get_instance_or_404(group_pk)

    port = request.DATA.get("port", None)
    if port is not None:
        try:
            port = int(port)
        except (TypeError, ValueError):
            raise me.ValueException("port must be an integer")

    # Field validation lives in Upstream.save() -> validators.validate_upstream,
    # so a bad host or an escaping socket path raises the same way here as it
    # would from a shell.
    upstream = Upstream(
        group=group,
        name=request.DATA.get("name"),
        kind=kind,
        host=request.DATA.get("host", None),
        port=port,
        socket_path=request.DATA.get("socket_path", None),
    )
    upstream.save()
    upstream.log(f"Upstream '{upstream.name}' declared", "edge:upstream_declared")
    return upstream.on_rest_get(request)


@md.POST('upstream/retire')
@md.requires_params("upstream")
@md.custom_security("require_platform_admin gate in body")
def on_upstream_retire(request):
    """Retire an upstream. Platform administrators only, same reasoning.

    Deliberately not a delete: `Vhost.upstream` and `VhostRoute.upstream` are
    `on_delete=PROTECT`, and a retired row keeps the historical answer to
    "what was this vhost pointing at?" Retiring only clears `is_enabled`; the
    INSTALLER then takes the same fork as unreadable certificate material —
    a tenant vhost reaching this upstream (directly or via a route) is
    excluded from the generation with an incident, a house vhost aborts the
    install. The vhost stops being served rather than being silently
    repointed, and the fleet converges onto that exclusion because
    `is_enabled` is part of the hashed desired-state payload.
    """
    require_platform_admin(request, "Retiring an edge upstream")
    upstream = Upstream.get_instance_or_404(request.DATA.get("upstream"))
    upstream.is_enabled = False
    upstream.save()
    upstream.log(f"Upstream '{upstream.name}' retired", "edge:upstream_retired")
    return upstream.on_rest_get(request)
