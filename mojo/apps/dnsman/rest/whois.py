import mojo.decorators as md
import mojo.errors as me
from mojo.apps.dnsman.models import Domain
from mojo.apps.dnsman.services import registrar


def _get_domain(request, perms):
    pk = request.DATA.get("domain", None)
    if not pk:
        raise me.ValueException("domain is required")
    domain = Domain.get_instance_or_404(pk)
    Domain.rest_check_permission_or_raise(request, perms, domain)
    return domain


@md.GET('whois')
@md.requires_params("domain")
def on_whois_get(request):
    """
    Registrar-held WHOIS/ICANN contacts and privacy state.

    Gated on SAVE_PERMS despite being a read: the registrar returns the real
    registrant name, street address, phone and email to the account owner
    regardless of WHOIS privacy. That is PII, and a read-only view_dns holder
    has no reason to see it.
    """
    domain = _get_domain(request, ["SAVE_PERMS", "VIEW_PERMS"])
    return registrar.get_contacts(domain)


@md.POST('whois')
@md.requires_params("domain", "contact")
def on_whois_update(request):
    domain = _get_domain(request, ["SAVE_PERMS", "VIEW_PERMS"])
    contact = request.DATA.get("contact")
    if not isinstance(contact, dict):
        raise me.ValueException("contact must be an object")
    return registrar.update_contacts(domain, contact)


@md.POST('whois/privacy')
@md.requires_params("domain", "enabled")
def on_whois_privacy(request):
    """Toggle WHOIS privacy. Refused up front for TLDs that do not offer it,
    with an explicit reason rather than a silent no-op."""
    domain = _get_domain(request, ["SAVE_PERMS", "VIEW_PERMS"])
    enabled = request.DATA.get("enabled")
    if isinstance(enabled, str):
        enabled = enabled.lower() in ("1", "true", "yes")
    return registrar.set_privacy(domain, bool(enabled))
