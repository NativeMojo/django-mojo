import mojo.decorators as md
import mojo.errors as me
from mojo.helpers import logit
from mojo.apps.dnsman.models import Domain, DomainPurchase
from mojo.apps.dnsman.rest.gates import require_platform_admin
from mojo.apps.dnsman.services import registrar, onboarding


@md.URL('purchase')
@md.URL('purchase/<int:pk>')
@md.uses_model_security(DomainPurchase)
def on_purchase(request, pk=None):
    """Read-only ledger. The model declares CAN_CREATE/CAN_DELETE False and
    registers no save route — rows are written only by the registrar service."""
    return DomainPurchase.on_rest_request(request, pk)


@md.POST('registrar/search')
def on_registrar_search(request):
    """
    Availability + live pricing. Creates nothing and spends nothing.

    Three input shapes: {domain} answers with today's flat single-name object
    unchanged; {domain, tlds} and {domains} answer {"results": [...]} with one
    same-shaped row per name. No requires_params — the batch shapes have no
    'domain' key, so the missing-parameter refusal is raised by hand with the
    decorator's exact message.
    """
    Domain.rest_check_permission_or_raise(request, "VIEW_PERMS")
    domain = request.DATA.get("domain")
    domains = request.DATA.get("domains")
    tlds = request.DATA.get("tlds")
    if domains is not None or tlds is not None:
        return registrar.search_batch(domain=domain, domains=domains, tlds=tlds)
    if not domain:
        raise me.ValueException("missing required parameters: domain")
    return registrar.search(domain)


@md.POST('registrar/suggest')
@md.requires_params("domain")
def on_registrar_suggest(request):
    """Alternate-name suggestions with availability and cached pricing.
    Read-only discovery, same gate as search."""
    Domain.rest_check_permission_or_raise(request, "VIEW_PERMS")
    return registrar.suggest(
        request.DATA.get("domain"),
        count=request.DATA.get("count", 10),
        only_available=bool(request.DATA.get("only_available", True)))


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
@md.denies_key_backed_session()
@md.requires_fresh_auth(seconds=600)
@md.requires_params(
    "group", "purchase", "confirm_token", "confirm_domain", "confirm_price")
def on_registrar_purchase(request):
    """Step two of two — the one irreversible, real-money mutation."""
    Domain.rest_check_permission_or_raise(request, ["SAVE_PERMS", "VIEW_PERMS"])
    return registrar.purchase(
        group=request.group,
        user=request.user,
        purchase_id=request.DATA.get("purchase"),
        token=request.DATA.get("confirm_token"),
        confirm_domain=request.DATA.get("confirm_domain"),
        confirm_price=request.DATA.get("confirm_price"),
    )


@md.GET('registrar/discover')
def on_registrar_discover(request):
    """
    What the HOUSE AWS account holds, merged by name, tracked flags included.

    Superuser only. This is the one account-wide listing in dnsman, and the gate
    is the whole reason it is allowed to exist: the response names every domain
    in the house account — including ones already assigned to other tenants — so
    below platform level it is a cross-tenant enumeration surface. The BYO
    credential rule is untouched; see services/onboarding.py.

    Read-only. Creates nothing, changes nothing, spends nothing.
    """
    require_platform_admin(request, "House account discovery")
    Domain.rest_check_permission_or_raise(request, ["SAVE_PERMS", "VIEW_PERMS"])

    untracked = request.DATA.get("untracked", False)
    if isinstance(untracked, str):
        untracked = untracked.lower() in ("1", "true", "yes")
    return onboarding.discover_house_domains(untracked_only=bool(untracked))


@md.POST('registrar/adopt')
@md.requires_params("domain")
def on_registrar_adopt(request):
    """
    Adopt an existing hosted zone in the house AWS account.

    Superuser only, and not because adoption is expensive — because it hands a
    group control over a zone in the HOUSE account. Exposed to any manage_dns
    holder it would be a cross-tenant zone-claim primitive.

    `group` is OPTIONAL. Omitting it adopts the domain platform-scoped (no
    group), which is what the discovery flow does — no tenant can see or reach
    it, and a superuser assigns it later via `registrar/assign-group`.
    """
    require_platform_admin(request, "Adoption")
    Domain.rest_check_permission_or_raise(request, ["SAVE_PERMS", "VIEW_PERMS"])

    # A group that was SUPPLIED but did not resolve is an error, never "no
    # group": Group.get_active returns None for an inactive or nonexistent id,
    # silently and by design, so without this a typo'd or deactivated id would
    # quietly produce a house domain indistinguishable from a deliberate one —
    # and assign-group only ever fires once, so it could not be corrected.
    # Same guard, same reason as rest/credential.py.
    #
    # BOTH keys, not just `group`: the dispatcher also populates request.group
    # from ?group_uuid= (mojo/decorators/http.py), so keying on `group` alone
    # leaves the uuid form taking the exact silent path this guard exists to
    # close.
    if ("group" in request.DATA or "group_uuid" in request.DATA) and request.group is None:
        raise me.ValueException(
            "The requested group does not exist or is not active — "
            "omit 'group' entirely to adopt this domain platform-scoped")

    domain = onboarding.adopt_route53(
        group=request.group,
        user=request.user,
        name=request.DATA.get("domain"),
        create_zone=bool(request.DATA.get("create_zone", False)),
    )
    return domain.on_rest_get(request)


@md.POST('registrar/assign-group')
@md.requires_params("domain", "group")
def on_registrar_assign_group(request):
    """
    Give a platform-scoped (house) domain to a group.

    Superuser only: this is the moment a house asset becomes a tenant's, so it
    is the same class of action as adoption itself.

    ASSIGN ONLY — never re-home. A domain that already belongs to a group is
    refused. Moving one tenant's domain to another tenant is a different
    operation with a different risk profile, and nothing needs it; building it
    "just in case" would hand every future caller a cross-tenant transfer
    primitive.
    """
    require_platform_admin(request, "Assigning a domain to a group")

    domain = Domain.get_instance_or_404(request.DATA.get("domain"))
    Domain.rest_check_permission_or_raise(request, ["SAVE_PERMS", "VIEW_PERMS"], domain)

    if domain.group_id is not None:
        raise me.ValueException(
            f"'{domain.name}' already belongs to a group — "
            f"re-homing a domain between groups is not supported")

    # rest_check_permission_or_raise rebinds request.group to the instance's
    # group (None here), so re-resolve from the body rather than trusting it.
    from mojo.apps.account.models.group import Group

    group = Group.get_active(request.DATA.get_typed("group", None, int))
    if group is None:
        raise me.ValueException("The requested group does not exist or is not active")

    domain.group = group
    domain.save()

    logit.info(
        f"dnsman: domain {domain.name} (id={domain.pk}) assigned to group "
        f"{group.name} (id={group.pk}) by user={getattr(request.user, 'pk', None)} "
        f"ip={request.ip}")
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
