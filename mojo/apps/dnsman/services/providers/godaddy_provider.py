"""
GoDaddy adapter — BYO domains managed through a linked DnsCredential.

GoDaddy semantics this adapter has to encode explicitly:

  - **Relative names.** Record names are labels relative to the zone, and the
    apex is "@". Callers speak FQDN; the conversion happens here.
  - **Raw TXT.** GoDaddy stores TXT text as-is, so a Route53-shaped (quoted,
    255-chunked) value must be unquoted before it is written, and read back as
    raw text.
  - **PUT replaces the whole (type, name) set.** An upsert therefore has to send
    the COMPLETE desired value list, never just the new value.
  - **There is no true delete.** Deleting means PUTting the values that remain;
    when none remain GoDaddy rejects the empty replace, so this adapter raises a
    clear "cannot delete the last record of this type" error rather than
    silently no-opping.
  - **There is no ChangeInfo API**, so propagation is the authoritative DNS
    probe only.

WHY THIS MODULE TALKS HTTP DIRECTLY
-----------------------------------
Transport, auth headers and the raise_on_error policy all come from
`DNSManager`, but two of its methods cannot express what this adapter needs:

  * `get_records()` / `get_record()` wrap the response in `objict(...)`, and the
    GoDaddy record endpoints return a JSON **array** — objict subclasses dict,
    so wrapping an array raises `ValueError` for any zone that actually has
    records; and
  * `edit_record()` hardcodes a single-element payload
    (`[{"data": ..., "ttl": ...}]`), so it cannot write a multi-value record set
    — which is exactly what a wildcard + apex `_acme-challenge` needs.

Until those are fixed in `mojo/helpers/dns/godaddy.py`, the record reads and
writes below issue their own request using the manager's own `_headers()` and
`_check()` — so the credential and the strictness policy are still the
manager's, and only the response parsing is ours.
"""

import requests

from mojo import errors as me
from mojo.helpers import logit
from mojo.helpers.dns import godaddy, probe
from mojo.helpers.settings import settings

from .base import (
    DEFAULT_PROPAGATION_TIMEOUT, TXT_TYPES, DnsProvider, as_value_list,
    make_record, to_fqdn, to_relative, unquote_txt)


logger = logit.get_logger("dnsman", "dnsman.log")

# GoDaddy's minimum accepted TTL.
MIN_TTL = 600


def _url(*parts):
    return "/".join([godaddy.BASE_URL] + [str(part) for part in parts])


def _get_json(manager, url, params=None):
    resp = requests.get(url, headers=manager._headers(), params=params)
    manager._check(resp)
    if not resp.content:
        return None
    return resp.json()


def _put_json(manager, url, payload):
    resp = requests.put(url, headers=manager._headers(), json=payload)
    manager._check(resp)
    return True


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
    params = {"statuses": status} if status else None
    data = _get_json(manager, _url("domains"), params=params)
    if isinstance(data, dict):
        data = data.get("domains") or []
    return len(data or [])


def domain_info(manager, name):
    """
    Per-name ownership probe. Returns the provider's domain object.

    Goes through `DNSManager.get_domain_info` — that endpoint returns a JSON
    object, so objict wrapping is safe there.
    """
    return manager.get_domain_info(name)


class GoDaddyProvider(DnsProvider):

    name = "godaddy"

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
            url = _url("domains", self.zone_name, "records", rtype, relative)
        else:
            url = _url("domains", self.zone_name, "records")
        data = _get_json(self.manager, url)
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

    def upsert_record(self, rtype, name, record_values, ttl=300):
        rtype, values = self._prepare(rtype, record_values)
        if not values:
            raise me.ValueException("At least one record value is required")
        relative = to_relative(name, self.zone_name)
        ttl = max(int(ttl or 0), MIN_TTL)
        # PUT replaces every record of this (type, name), so the payload is the
        # COMPLETE desired value list.
        payload = [{"data": value, "ttl": ttl} for value in values]
        _put_json(
            self.manager,
            _url("domains", self.zone_name, "records", rtype, relative),
            payload)
        # GoDaddy issues no change id.
        return None

    def delete_record(self, rtype, name, record_values=None):
        rtype, requested = self._prepare(rtype, record_values)
        relative = to_relative(name, self.zone_name)
        entries = self._entries(rtype, relative)
        if not entries:
            raise me.ValueException(
                f"No {rtype} record named '{name}' exists for '{self.zone_name}'")

        if requested:
            remaining = []
            for entry in entries:
                value = entry.get("data")
                if rtype in TXT_TYPES:
                    value = unquote_txt(value)
                if value not in requested:
                    remaining.append(entry)
        else:
            remaining = []

        if not remaining:
            raise me.ValueException(
                f"GoDaddy cannot delete the last record of this type: removing the final "
                f"{rtype} value(s) for '{name}' would leave an empty record set, which the "
                f"GoDaddy API rejects. Replace the record with a new value instead.")

        payload = [
            {"data": entry.get("data"), "ttl": max(int(entry.get("ttl") or 0), MIN_TTL)}
            for entry in remaining
        ]
        _put_json(
            self.manager,
            _url("domains", self.zone_name, "records", rtype, relative),
            payload)
        return None

    # -- propagation ---------------------------------------------------------

    def wait_for_propagation(self, rtype, name, record_values, timeout=None, change_id=None):
        rtype = (rtype or "").strip().upper()
        values = as_value_list(record_values)
        if timeout is None:
            timeout = settings.get(
                "DNSMAN_DNS_PROPAGATION_TIMEOUT", DEFAULT_PROPAGATION_TIMEOUT)

        if rtype in TXT_TYPES:
            # No ChangeInfo equivalent exists, so the authoritative probe is the
            # ONLY gate GoDaddy offers.
            return probe.wait_for_txt(
                name, [unquote_txt(value) for value in values], timeout=timeout)

        logger.warning(
            f"dnsman: GoDaddy has no change API — propagation of the {rtype} record "
            f"'{name}' could not be verified")
        return True, values
