import mojo.decorators as md
import mojo.errors as me
from mojo.apps.dnsman.models import Domain, DomainPurchase
from mojo.apps.dnsman.services import registrar, onboarding


@md.URL('purchase')
@md.URL('purchase/<int:pk>')
@md.uses_model_security(DomainPurchase)
def on_purchase(request, pk=None):
    """Read-only ledger. The model declares CAN_CREATE/CAN_DELETE False and
    registers no save route — rows are written only by the registrar service."""
    return DomainPurchase.on_rest_request(request, pk)


@md.POST('registrar/search')
@md.requires_params("domain")
def on_registrar_search(request):
    """Availability + live pricing. Creates nothing and spends nothing."""
    Domain.rest_check_permission_or_raise(request, "VIEW_PERMS")
    return registrar.search(request.DATA.get("domain"))


@md.POST('registrar/quote')
@md.requires_params("group", "domain")
def on_registrar_quote(request):
    """
    Step one of two. Returns a price and a single-use confirm token.

    There is deliberately no single-call purchase path anywhere in this app.
    """
    Domain.rest_check_permission_or_raise(request, ["SAVE_PERMS", "VIEW_PERMS"])
    return registrar.quote(
        group=request.group,
        user=request.user,
        name=request.DATA.get("domain"),
        years=int(request.DATA.get("years", 1)),
    )


@md.POST('registrar/purchase')
@md.requires_params("group", "purchase", "confirm_token")
def on_registrar_purchase(request):
    """Step two of two — the one irreversible, real-money mutation."""
    Domain.rest_check_permission_or_raise(request, ["SAVE_PERMS", "VIEW_PERMS"])
    return registrar.purchase(
        group=request.group,
        user=request.user,
        purchase_id=request.DATA.get("purchase"),
        token=request.DATA.get("confirm_token"),
    )


@md.POST('registrar/adopt')
@md.requires_params("group", "domain")
def on_registrar_adopt(request):
    """
    Adopt an existing hosted zone in the house AWS account.

    Superuser only, and not because adoption is expensive — because it hands a
    group control over a zone in the HOUSE account. Exposed to any manage_dns
    holder it would be a cross-tenant zone-claim primitive.
    """
    Domain.rest_check_permission_or_raise(request, ["SAVE_PERMS", "VIEW_PERMS"])
    if not getattr(request.user, "is_superuser", False):
        raise me.PermissionDeniedException("Adoption is restricted to platform administrators")

    domain = onboarding.adopt_route53(
        group=request.group,
        user=request.user,
        name=request.DATA.get("domain"),
        create_zone=bool(request.DATA.get("create_zone", False)),
    )
    return domain.on_rest_get(request)


@md.POST('registrar/register-existing')
@md.requires_params("group", "domain", "credential")
def on_registrar_register_existing(request):
    """
    BYO onboarding: claim a domain the caller already holds at a provider.

    The linked credential is the proof of control — the provider API is asked
    whether that account actually holds this specific domain. The probe is
    per-name, so this can only ever confirm a domain the caller already named;
    there is no surface here that lists an account's domains.
    """
    from mojo.apps.dnsman.models import DnsCredential

    Domain.rest_check_permission_or_raise(request, ["SAVE_PERMS", "VIEW_PERMS"])
    credential = DnsCredential.get_instance_or_404(request.DATA.get("credential"))
    DnsCredential.rest_check_permission_or_raise(request, "SAVE_PERMS", credential)

    domain = onboarding.register_existing(
        group=request.group,
        user=request.user,
        name=request.DATA.get("domain"),
        credential=credential,
    )
    return domain.on_rest_get(request)
