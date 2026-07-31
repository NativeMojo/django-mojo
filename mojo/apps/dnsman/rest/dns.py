import mojo.decorators as md
import mojo.errors as me
from mojo.apps.dnsman.models import Domain
from mojo.apps.dnsman.services import dns as dns_service


def _get_domain(request, perms):
    """
    Resolve the target Domain and gate it.

    services/dns.py is deliberately ungated mechanism — the certificate service
    plants challenge records with no user in scope — so every REST entry point
    must do its own explicit check. uses_model_security does NOT gate a custom
    pk-fetching endpoint like this one.
    """
    pk = request.DATA.get("domain", None)
    if not pk:
        raise me.ValueException("domain is required")
    domain = Domain.get_instance_or_404(pk)
    Domain.rest_check_permission_or_raise(request, perms, domain)
    return domain


def _record_values(request):
    record_values = request.DATA.get("record_values", None)
    if record_values is None:
        record_values = request.DATA.get("value", None)
    if record_values is None:
        raise me.ValueException("record_values is required")
    if not isinstance(record_values, list):
        record_values = [record_values]
    return record_values


@md.GET('dns')
@md.custom_security("_get_domain gates on the resolved Domain via "
                    "rest_check_permission_or_raise; services/dns.py is "
                    "deliberately ungated mechanism")
@md.requires_params("domain")
def on_dns_list(request):
    """List the live records for a domain. Reads through to the provider —
    we deliberately do not mirror records in the database, because a mirror
    invites drift and doubles every write path."""
    domain = _get_domain(request, "VIEW_PERMS")
    records = dns_service.list_records(domain)
    return dict(domain=domain.name, provider=domain.provider, records=records)


@md.POST('dns')
@md.custom_security("_get_domain gates on the resolved Domain via "
                    "rest_check_permission_or_raise; services/dns.py is "
                    "deliberately ungated mechanism")
@md.requires_params("domain", "type", "name")
def on_dns_upsert(request):
    domain = _get_domain(request, ["SAVE_PERMS", "VIEW_PERMS"])
    result = dns_service.upsert_record(
        domain,
        request.DATA.get("type"),
        request.DATA.get("name"),
        _record_values(request),
        ttl=int(request.DATA.get("ttl", 300)),
    )
    return dict(status=True, change_id=result.change_id, provider=result.provider)


@md.POST('dns/delete')
@md.custom_security("_get_domain gates on the resolved Domain via "
                    "rest_check_permission_or_raise; services/dns.py is "
                    "deliberately ungated mechanism")
@md.requires_params("domain", "type", "name")
def on_dns_delete(request):
    domain = _get_domain(request, ["SAVE_PERMS", "VIEW_PERMS"])
    record_values = request.DATA.get("record_values", None)
    if record_values is not None and not isinstance(record_values, list):
        record_values = [record_values]
    result = dns_service.delete_record(
        domain,
        request.DATA.get("type"),
        request.DATA.get("name"),
        record_values=record_values,
    )
    return dict(status=True, change_id=result.change_id, provider=result.provider)
