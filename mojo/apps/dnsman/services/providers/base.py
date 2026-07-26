"""
The DNS provider adapter interface.

Every provider difference that could bite silently lives behind this interface
and nowhere else:

  - **Name form.** The service layer always speaks FQDN
    ("_acme-challenge.example.com"). Route53 takes FQDN; GoDaddy takes the
    relative label ("_acme-challenge"), or "@" for the apex. Converting is the
    adapter's job — a caller must never have to know.
  - **TXT quoting.** Route53 stores TXT values as quoted, 255-chunked
    character-strings; GoDaddy stores the raw text. Getting this wrong breaks
    SES verification and ACME DNS-01 validation *silently*, which is why the
    conversion is confined here and tested in both directions.
  - **Write semantics.** Route53 UPSERT and GoDaddy's PUT both REPLACE the whole
    record set, so `upsert_record` always receives the complete desired value
    list — never a single value to append.
  - **Propagation.** Route53 has a ChangeInfo API to gate on; GoDaddy has
    nothing, so it has only the authoritative DNS probe.

The value field is `record_values` EVERYWHERE. objict subclasses dict, so an
attribute named `values` resolves to the bound `dict.values` method and hands
callers a method instead of their data. That has already caused one real bug.
"""

from objict import objict

from mojo import errors as me
from mojo.helpers.dns import probe

from mojo.apps.dnsman.services import naming


# Record types whose values are character-strings and therefore quoted by
# Route53 and stored raw by GoDaddy.
TXT_TYPES = ("TXT", "SPF")

# Fallback when DNSMAN_DNS_PROPAGATION_TIMEOUT is not configured.
DEFAULT_PROPAGATION_TIMEOUT = 300


def as_value_list(record_values):
    """
    Coerce a record value argument into a list of strings.

    A bare string is accepted for convenience and becomes a one-element list —
    the adapters below always work with lists because both providers replace the
    whole record set on every write.
    """
    if record_values is None:
        return []
    if isinstance(record_values, (list, tuple, set)):
        return [str(value) for value in record_values if value is not None]
    return [str(record_values)]


def unquote_txt(value):
    """
    Return the raw text of a TXT value that may be quoted and/or 255-chunked.

    `probe.txt_value` already does exactly this join-the-quoted-chunks work for
    the authoritative probe, so it is reused rather than reimplemented.
    """
    if value is None:
        return ""
    return probe.txt_value(value)


def to_fqdn(name, zone_name):
    """Provider-relative label (or '@') -> fully qualified name, no trailing dot."""
    return naming.normalize_record_name(name, zone_name)


def to_relative(fqdn, zone_name):
    """
    Fully qualified name -> the provider-relative label, '@' for the apex.

    Refuses a name outside the zone rather than producing a label that would
    silently be written into the wrong place.
    """
    name = str(fqdn or "").strip().lower().rstrip(".")
    zone = str(zone_name or "").strip().lower().rstrip(".")
    if name in ("", "@", zone):
        return "@"
    if not naming.is_in_zone(name, zone):
        raise me.ValueException(f"'{name}' is not inside the '{zone}' zone")
    return name[:-(len(zone) + 1)]


def make_record(rtype, name, record_values, ttl):
    """The uniform record-set shape every adapter's `list_records` returns."""
    return objict(
        type=(rtype or "").strip().upper(),
        name=name,
        record_values=list(record_values or []),
        ttl=ttl)


class DnsProvider:
    """
    Uniform record surface over one Domain.

    Subclasses implement the four methods below. Nothing here checks
    permissions — gating belongs to the REST layer, and `services/dns.py` is the
    ungated mechanism that resolves a Domain to one of these adapters.
    """

    # Provider key, matching Domain.provider.
    name = None

    # Whether the provider can remove the LAST value of a record set. True for
    # anything with a real delete API; False for GoDaddy, whose only write is a
    # PUT that replaces a whole set and rejects an empty replacement. A provider
    # that answers False must override `clear_record`.
    can_delete_last_record = True

    def __init__(self, domain):
        self.domain = domain
        self.zone_name = domain.name

    def list_records(self):
        """Return [objict(type, name, record_values, ttl)] — names are FQDN."""
        raise NotImplementedError("list_records")

    def upsert_record(self, rtype, name, record_values, ttl=300):
        """
        Create or REPLACE the (type, name) record set with `record_values`.

        Returns a provider change id, or None for providers that do not issue
        one. `name` is an FQDN; `record_values` is the complete desired list.
        """
        raise NotImplementedError("upsert_record")

    def delete_record(self, rtype, name, record_values=None):
        """
        Remove `record_values` (or the whole set when None) from (type, name).

        Returns a provider change id, or None.
        """
        raise NotImplementedError("delete_record")

    def clear_record(self, rtype, name, record_values=None):
        """
        Retire `record_values` (or the whole set) so none of it resolves any more.

        The difference from `delete_record` is what the caller can be told.
        `delete_record` may refuse — GoDaddy cannot remove the last value of a
        set, and saying so out loud beats a silent no-op. `clear_record` is for
        callers with nowhere to put that refusal: challenge cleanup runs in a
        `finally` and swallows, so a raise there just leaves the record live.

        A provider with a real delete does exactly what `delete_record` does. A
        provider whose `can_delete_last_record` is False overrides this to leave
        the record holding a single inert placeholder value instead.

        Returns a provider change id, or None.
        """
        return self.delete_record(rtype, name, record_values=record_values)

    def wait_for_propagation(self, rtype, name, record_values, timeout=None, change_id=None):
        """Return (ok, seen_values) once the record is authoritatively visible."""
        raise NotImplementedError("wait_for_propagation")
