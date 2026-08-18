"""
GoDaddy adapter — BYO domains managed through a linked DnsCredential.

Transport, auth headers and the `raise_on_error` policy all belong to
`mojo.helpers.dns.godaddy.DNSManager`; this module only translates between the
uniform record surface and GoDaddy's idea of one.

GoDaddy semantics this adapter has to encode explicitly:

  - **Relative names.** Record names are labels relative to the zone, and the
    apex is "@". Callers speak FQDN; the conversion happens here.
  - **Raw TXT.** GoDaddy stores TXT text as-is, so a Route53-shaped (quoted,
    255-chunked) value must be unquoted before it is written, and read back as
    raw text.
  - **One entry PER VALUE on read.** `get_records` returns a flat list of
    entries, not record sets, so `list_records` regroups them.
  - **PUT replaces the whole (type, name) set.** An upsert therefore has to send
    the COMPLETE desired value list, never just the new value.
  - **There is no true delete.** Deleting means PUTting the values that remain;
    when none remain GoDaddy rejects the empty replace, so `delete_record`
    raises a clear "cannot delete the last record of this type" error rather
    than silently no-opping. `clear_record` is the escape hatch for callers that
    need the data gone and cannot act on that refusal — see its docstring.
  - **There is no ChangeInfo API**, so propagation starts with an authoritative
    DNS probe. ACME TXT replacements then wait one enforced minimum TTL so a
    CA's secondary validator cannot retain the prior value.
"""

import time

from mojo import errors as me
from mojo.helpers import logit
from mojo.helpers.dns import godaddy, probe
from mojo.helpers.settings import settings

from .base import (
    DEFAULT_PROPAGATION_TIMEOUT, TXT_TYPES, DnsProvider, as_value_list,
    make_record, to_fqdn, to_relative, unquote_txt)


logger = logit.get_logger("dnsman", "dnsman.log")

# GoDaddy's minimum accepted TTL — it 422s below this.
MIN_TTL = 600

# The single value a RETIRED character-string record is left holding, because
# GoDaddy cannot delete the last record of a set. It is deliberately nothing:
# not a digest, not a policy token, not anything a resolver could act on — and
# it names no product, since a leftover value in a customer's zone should not
# advertise what wrote it.
RETIRED_VALUE = "retired"


def build_manager_from_pair(api_key, api_secret):
    """
    A strict DNSManager for a raw key/secret pair (link + rotation probes).

    `raise_on_error=True` is the point: a revoked or mistyped key must surface
    as an error, not as a silently-wrong answer.
    """
    if not api_key or not api_secret:
        raise me.ValueException("An API key and secret are required")
    return godaddy.DNSManager(api_key, api_secret, raise_on_error=True)


def build_manager(credential):
    """A strict DNSManager for a stored credential."""
    if credential is None:
        raise me.ValueException("A GoDaddy credential is required")
    return build_manager_from_pair(credential.api_key, credential.api_secret)


def account_domain_count(manager, status="ACTIVE"):
    """
    Prove a credential works and report ONLY how many domains the account holds.

    The count is a health signal. The domain list itself is deliberately never
    returned, stored or exposed: a provider API key is account-wide, and a
    "list everything in this account" surface is exactly the blast radius BYO
    onboarding is designed to avoid.
    """
    data = manager.get_domains(status=status)
    if isinstance(data, dict):
        # An error body (raise_on_error is on, so this is belt and braces) or a
        # wrapped list — either way, only the count leaves this function.
        data = data.get("domains") or []
    return len(data or [])


def domain_info(manager, name):
    """Per-name ownership probe. Returns the provider's domain object."""
    return manager.get_domain_info(name)


class GoDaddyProvider(DnsProvider):

    name = "godaddy"

    # GoDaddy rejects an empty record-set replacement, so the LAST value of a
    # (type, name) set can never be removed. `clear_record` below is what a
    # caller uses when the data must stop resolving regardless.
    can_delete_last_record = False

    def __init__(self, domain):
        super().__init__(domain)
        credential = domain.credential
        # services/dns.get_adapter is the real fail-closed gate; this is the
        # belt-and-braces copy for anyone constructing the adapter directly.
        if credential is None or not credential.is_usable:
            raise me.ValueException(
                f"'{domain.name}' has no usable GoDaddy credential — "
                "link and verify one before managing its DNS")
        self.credential = credential
        self.manager = build_manager(credential)

    # -- reads ---------------------------------------------------------------

    def _entries(self, rtype=None, relative=None):
        """Raw GoDaddy record entries, optionally scoped to one (type, name)."""
        if rtype and relative:
            data = self.manager.get_record(self.zone_name, rtype, relative)
        else:
            data = self.manager.get_records(self.zone_name)
        if isinstance(data, dict):
            data = [data]
        return list(data or [])

    def list_records(self):
        """
        GoDaddy returns ONE entry per value; Route53 returns record SETS.

        Entries are grouped back into sets here so both providers hand callers
        the same shape.
        """
        grouped = {}
        order = []
        for entry in self._entries():
            rtype = (entry.get("type") or "").strip().upper()
            fqdn = to_fqdn(entry.get("name"), self.zone_name)
            value = entry.get("data")
            if rtype in TXT_TYPES:
                value = unquote_txt(value)
            key = (rtype, fqdn)
            if key not in grouped:
                grouped[key] = make_record(rtype, fqdn, [], entry.get("ttl"))
                order.append(key)
            grouped[key].record_values.append(value)
        return [grouped[key] for key in order]

    # -- writes --------------------------------------------------------------

    def _prepare(self, rtype, record_values):
        rtype = (rtype or "").strip().upper()
        values = as_value_list(record_values)
        if rtype in TXT_TYPES:
            # GoDaddy stores TXT raw — a Route53-shaped quoted/chunked value
            # written verbatim would leave literal quotes in the zone and break
            # both SES verification and ACME validation.
            values = [unquote_txt(value) for value in values]
        return rtype, values

    def _surviving(self, rtype, entries, requested):
        """The entries that would remain once `requested` is taken out."""
        if not requested:
            return []
        remaining = []
        for entry in entries:
            value = entry.get("data")
            if rtype in TXT_TYPES:
                value = unquote_txt(value)
            if value not in requested:
                remaining.append(entry)
        return remaining

    def _payload(self, entries):
        """GoDaddy's PUT body for a list of existing entries, TTL floor applied."""
        return [
            {"data": entry.get("data"),
             "ttl": max(int(entry.get("ttl") or 0), MIN_TTL)}
            for entry in entries
        ]

    def upsert_record(self, rtype, name, record_values, ttl=300):
        rtype, values = self._prepare(rtype, record_values)
        if not values:
            raise me.ValueException("At least one record value is required")
        relative = to_relative(name, self.zone_name)
        ttl = max(int(ttl or 0), MIN_TTL)
        # PUT replaces every record of this (type, name), so the value list is
        # handed over COMPLETE — the manager builds one payload entry per value.
        self.manager.edit_record(self.zone_name, rtype, relative, values, ttl)
        # GoDaddy issues no change id.
        return None

    def delete_record(self, rtype, name, record_values=None):
        rtype, requested = self._prepare(rtype, record_values)
        relative = to_relative(name, self.zone_name)
        entries = self._entries(rtype, relative)
        if not entries:
            raise me.ValueException(
                f"No {rtype} record named '{name}' exists for '{self.zone_name}'")

        remaining = self._surviving(rtype, entries, requested)
        if not remaining:
            raise me.ValueException(
                f"GoDaddy cannot delete the last record of this type: removing the final "
                f"{rtype} value(s) for '{name}' would leave an empty record set, which the "
                f"GoDaddy API rejects. Replace the record with a new value instead.")

        self.manager.put_records(
            self.zone_name, rtype, relative, self._payload(remaining))
        return None

    def clear_record(self, rtype, name, record_values=None):
        """
        Retire values so none of them resolve any more, placeholder if need be.

        `delete_record` REFUSES to remove the last value of a set — GoDaddy
        rejects an empty replacement and a silent no-op would be worse. That
        refusal is right for a caller that asked to delete a record. It is wrong
        for certificate cleanup, which only needs the `_acme-challenge` digests
        to stop resolving and has nowhere to put the error: left to raise, the
        zone keeps its challenge TXT and gains another stale digest on EVERY
        renewal.

        So when the requested values are all the set holds, the set is
        overwritten with ONE inert placeholder: no digest is answered any more,
        and GoDaddy gets the non-empty replacement it insists on. Only
        character-string types get a placeholder — inventing an address for an A
        record or a target for a CNAME would point real traffic somewhere, which
        is worse than leaving the record alone.
        """
        rtype, requested = self._prepare(rtype, record_values)
        relative = to_relative(name, self.zone_name)
        entries = self._entries(rtype, relative)
        if not entries:
            # Already gone. Nothing to retire and nothing to complain about.
            return None

        remaining = self._surviving(rtype, entries, requested)
        if remaining:
            self.manager.put_records(
                self.zone_name, rtype, relative, self._payload(remaining))
            return None

        if rtype not in TXT_TYPES:
            raise me.ValueException(
                f"GoDaddy cannot remove the last {rtype} record for '{name}', and a "
                f"placeholder {rtype} value would point real traffic somewhere — "
                f"replace the record with a real value instead.")

        self.manager.edit_record(
            self.zone_name, rtype, relative, [RETIRED_VALUE], MIN_TTL)
        logger.info(
            f"dnsman: GoDaddy has no delete for the last {rtype} value, so "
            f"'{name}' on {self.zone_name} was retired to a placeholder instead")
        return None

    # -- propagation ---------------------------------------------------------

    def wait_for_propagation(self, rtype, name, record_values, timeout=None, change_id=None):
        rtype = (rtype or "").strip().upper()
        values = as_value_list(record_values)
        if timeout is None:
            timeout = settings.get(
                "DNSMAN_DNS_PROPAGATION_TIMEOUT", DEFAULT_PROPAGATION_TIMEOUT)

        if rtype in TXT_TYPES:
            # No ChangeInfo equivalent exists, so first prove the replacement
            # at the authoritative servers.
            ok, seen = probe.wait_for_txt(
                name, [unquote_txt(value) for value in values], timeout=timeout)
            if ok and name.lower().split(".", 1)[0] == "_acme-challenge":
                # GoDaddy refuses TXT TTLs below MIN_TTL.  A CA's secondary
                # validator can therefore still hold the prior challenge value
                # after the authoritative servers show this replacement.  Wait
                # one complete provider TTL before telling ACME to validate.
                logger.info(
                    f"dnsman: GoDaddy ACME TXT '{name}' is authoritative; "
                    f"waiting {MIN_TTL}s for recursive caches to expire")
                time.sleep(MIN_TTL)
            return ok, seen

        logger.warning(
            f"dnsman: GoDaddy has no change API — propagation of the {rtype} record "
            f"'{name}' could not be verified")
        return True, values
