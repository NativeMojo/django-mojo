"""
The single provider-dispatched DNS surface.

UNGATED MECHANISM — this module performs **no permission checks**. Gating lives
in the REST layer, which resolves the Domain and calls
`rest_check_permission_or_raise` before delegating here. That split is
deliberate: the certificate service has to plant an `_acme-challenge` TXT record
from a cron job with no request and no user, and it must not have to fabricate
one to get past a permission check.

What this module DOES enforce, before any provider call:

  - the Domain must be `active`,
  - the record type must be in `DNSMAN_ALLOWED_RECORD_TYPES`,
  - the apex NS / SOA record sets are refused outright,
  - a record name outside the Domain's zone is refused,
  - a provider that needs a credential must have a usable one (fail closed).

Names arriving here are FQDN; converting to a provider's own form (relative
labels, "@", quoted TXT) belongs to the adapter and nowhere else.

The value argument is `record_values` and it is ALWAYS a list — even for a
single value. objict subclasses dict, so a field named `values` would resolve to
the bound `dict.values` method and hand callers a method instead of their data.
"""

from objict import objict

from mojo import errors as me
from mojo.helpers.settings import settings

from mojo.apps.dnsman.models.domain import (
    PROVIDER_GODADDY, PROVIDER_ROUTE53, STATUS_ACTIVE)
from mojo.apps.dnsman.services import naming
from mojo.apps.dnsman.services.providers import base
from mojo.apps.dnsman.services.providers.godaddy_provider import GoDaddyProvider
from mojo.apps.dnsman.services.providers.route53_provider import Route53Provider


# Apex NS/SOA are refused regardless of this list — see `_validate`.
DEFAULT_ALLOWED_RECORD_TYPES = ["A", "AAAA", "CNAME", "TXT", "MX", "SRV", "CAA", "NS"]


# ---------------------------------------------------------------------------
# adapter resolution — the fail-closed credential gate
# ---------------------------------------------------------------------------

def _credential_problem(domain):
    """An actionable message naming exactly why the credential is unusable."""
    credential = domain.credential
    if credential is None:
        return (f"'{domain.name}' has no linked {domain.provider} credential. "
                f"Link and verify one before managing its DNS.")
    if not credential.is_active:
        return (f"The {domain.provider} credential '{credential.name}' for "
                f"'{domain.name}' is inactive. Re-activate or relink it.")
    if not credential.verified:
        return (f"The {domain.provider} credential '{credential.name}' for "
                f"'{domain.name}' has not been verified. Relink it to verify.")
    return (f"The {domain.provider} credential '{credential.name}' for "
            f"'{domain.name}' has no stored API key and secret. Relink it.")


def get_adapter(domain):
    """
    Resolve a Domain to its provider adapter, or raise.

    This is where credentials are enforced — NOT at the call sites. A
    `provider="godaddy"` Domain whose credential is missing, inactive,
    unverified or empty raises here, before any network call is attempted.
    """
    if domain is None:
        raise me.ValueException("A domain is required")

    provider = (domain.provider or "").strip().lower()
    if provider == PROVIDER_ROUTE53:
        return Route53Provider(domain)
    if provider == PROVIDER_GODADDY:
        if not domain.has_usable_credential:
            raise me.ValueException(_credential_problem(domain))
        return GoDaddyProvider(domain)
    raise me.ValueException(
        f"'{domain.name}' uses an unknown DNS provider '{domain.provider}'")


# ---------------------------------------------------------------------------
# validation
# ---------------------------------------------------------------------------

def _require_active(domain):
    if domain is None:
        raise me.ValueException("A domain is required")
    if domain.status != STATUS_ACTIVE:
        raise me.ValueException(
            f"'{domain.name}' is not active (status '{domain.status}') — "
            f"DNS operations are refused until it is")


def allowed_record_types():
    types = settings.get("DNSMAN_ALLOWED_RECORD_TYPES", DEFAULT_ALLOWED_RECORD_TYPES)
    return {str(entry).strip().upper() for entry in (types or []) if entry}


def _resolve_name(domain, name):
    """
    Normalize a record name to an in-zone FQDN.

    A name that already carries a dot is treated as fully qualified (which is
    what this layer speaks) and must be inside the zone — otherwise
    `normalize_record_name` would helpfully turn "www.attacker.com" into
    "www.attacker.com.example.com" and quietly accept a request that was meant
    to write somewhere else entirely.
    """
    raw = str("" if name is None else name).strip().lower().rstrip(".")
    if raw in ("", "@"):
        return domain.name
    if "." in raw and not naming.is_in_zone(raw, domain.name):
        raise me.ValueException(
            f"'{raw}' is not inside the '{domain.name}' zone")
    return naming.normalize_record_name(raw, domain.name)


def _validate(domain, rtype, name):
    _require_active(domain)
    rtype = (rtype or "").strip().upper()
    if not rtype:
        raise me.ValueException("A record type is required")

    fqdn = _resolve_name(domain, name)

    if fqdn == domain.name and rtype in ("NS", "SOA"):
        raise me.ValueException(
            f"Refusing to modify the apex {rtype} record set for '{domain.name}' — "
            f"it would take the domain off the internet")

    allowed = allowed_record_types()
    if rtype not in allowed:
        raise me.ValueException(
            f"'{rtype}' is not an allowed record type "
            f"({', '.join(sorted(allowed))})")
    return rtype, fqdn


# ---------------------------------------------------------------------------
# the surface
# ---------------------------------------------------------------------------

def list_records(domain):
    """Every record set in the Domain's zone: [objict(type, name, record_values, ttl)]."""
    _require_active(domain)
    return get_adapter(domain).list_records()


def upsert_record(domain, rtype, name, record_values, ttl=300):
    """
    Create or REPLACE a record set. Returns objict(change_id, provider, type, name).

    `record_values` is the COMPLETE desired value list — both providers replace
    the whole set on write, which is what lets a wildcard and its apex share one
    `_acme-challenge` name carrying two values.
    """
    rtype, fqdn = _validate(domain, rtype, name)
    values = base.as_value_list(record_values)
    if not values:
        raise me.ValueException("At least one record value is required")
    adapter = get_adapter(domain)
    change_id = adapter.upsert_record(rtype, fqdn, values, ttl=ttl)
    return objict(change_id=change_id, provider=adapter.name, type=rtype, name=fqdn)


def delete_record(domain, rtype, name, record_values=None):
    """
    Delete a record set (or just the given values from it).

    Returns objict(change_id, provider, type, name). A provider that cannot
    express the requested delete raises — it never silently no-ops.
    """
    rtype, fqdn = _validate(domain, rtype, name)
    adapter = get_adapter(domain)
    values = base.as_value_list(record_values) or None
    change_id = adapter.delete_record(rtype, fqdn, record_values=values)
    return objict(change_id=change_id, provider=adapter.name, type=rtype, name=fqdn)


def clear_record(domain, rtype, name, record_values=None):
    """
    Retire a record's values — the strongest removal the provider can express.

    Returns objict(change_id, provider, type, name). Same intent as
    `delete_record`, different failure contract: a provider that cannot express
    a true delete of the last value in a set retires the record to an inert
    placeholder here instead of raising. Reach for this only when the caller
    genuinely cannot act on a refusal (certificate challenge cleanup, which runs
    in a `finally`); `delete_record` stays the honest answer everywhere else.
    """
    rtype, fqdn = _validate(domain, rtype, name)
    adapter = get_adapter(domain)
    values = base.as_value_list(record_values) or None
    change_id = adapter.clear_record(rtype, fqdn, record_values=values)
    return objict(change_id=change_id, provider=adapter.name, type=rtype, name=fqdn)


def wait_for_propagation(domain, rtype, name, record_values, timeout=None, change_id=None):
    """
    Block until the record is authoritatively visible. Returns (ok, seen_values).

    Route53 gates on its ChangeInfo reaching INSYNC and then queries the zone's
    authoritative nameservers; GoDaddy has no change API, so it is the
    authoritative probe only. `change_id` is optional — pass the one returned by
    `upsert_record` to get the INSYNC gate when the adapter was not reused.
    """
    rtype, fqdn = _validate(domain, rtype, name)
    adapter = get_adapter(domain)
    return adapter.wait_for_propagation(
        rtype, fqdn, base.as_value_list(record_values),
        timeout=timeout, change_id=change_id)
