"""
Thin transport over the GoDaddy v1 domains API.

Two GoDaddy facts shape this module and are easy to get wrong:

  * **Some endpoints answer with a JSON array, some with a JSON object.** The
    record endpoints and the domain list are arrays (records: one entry PER
    VALUE, not one per record set); a single domain is an object. `objict`
    subclasses `dict`, so wrapping an array in it goes through
    `dict(sequence)` and raises `ValueError` — which is why every body goes
    through `parse_body` and an array comes back as a LIST of `objict`.
  * **A record PUT replaces the entire (type, name) set.** There is no
    "append" and no true delete; writing a partial value list silently erases
    the values left out.

`raise_on_error` defaults to **False** on purpose — see `DNSManager`.
"""

import requests
from urllib.parse import quote

from objict import objict

BASE_URL = "https://api.godaddy.com/v1"


def build_url(*parts):
    """
    Join path segments onto BASE_URL, percent-encoding each one.

    Every segment is encoded, including "/". Record names reach here from
    callers that may not have validated their charset, and an unencoded segment
    containing dot-segments is normalized by the HTTP client into a request
    against a DIFFERENT domain in the same provider account — turning one
    managed domain into write access to every domain the credential can reach.
    This is the layer that actually makes that traversal impossible.

    "@" is left unencoded: it is GoDaddy's apex marker, is not special in a URL
    path, and cannot be used to escape a segment. "." is unreserved and so
    passes through untouched, which keeps domain segments readable.
    """
    encoded = [quote(str(part), safe="@") for part in parts]
    return "/".join([BASE_URL] + encoded)


def parse_body(resp):
    """
    Decode a GoDaddy response body.

    Returns a LIST of `objict` for a JSON array, a single `objict` for a JSON
    object, and None for an empty body. The array case is the whole reason this
    exists: `objict([...])` raises `ValueError`, so wrapping a record or domain
    list directly would fail for every account that actually holds anything —
    and succeed (as `{}`) only for an empty one, which is exactly how that bug
    stayed hidden.
    """
    if not resp.content:
        return None
    data = resp.json()
    if isinstance(data, (dict, list)):
        return objict.from_dict(data)
    return data


class DNSManager:
    def __init__(self, api_key, api_secret, raise_on_error=False):
        self.api_key = api_key
        self.api_secret = api_secret
        # Opt-in strictness. Default stays False: existing callers rely on the
        # historical swallow-everything behaviour (a bad key surfaces as an odd
        # body, not an exception). Pass raise_on_error=True to get an
        # HTTPError instead of a silently wrong answer.
        self.raise_on_error = raise_on_error

    def _check(self, resp):
        if self.raise_on_error:
            resp.raise_for_status()
        return resp

    def _headers(self):
        return {
            "Authorization": f"sso-key {self.api_key}:{self.api_secret}",
            "Content-Type": "application/json",
            "Accept": "application/json"
        }

    def get_domains(self, status="ACTIVE"):
        """
        The domains the account holds.

        GoDaddy answers with a JSON ARRAY, so this returns a LIST of `objict` —
        one per domain. (A mapping body, which is what an error looks like when
        `raise_on_error` is False, comes back as a single `objict`.)
        """
        params = {"statuses": status} if status else {}
        resp = requests.get(build_url("domains"), headers=self._headers(), params=params)
        self._check(resp)
        return parse_body(resp)

    def get_domain_info(self, domain):
        """One domain. This endpoint answers with a JSON object -> one `objict`."""
        resp = requests.get(build_url("domains", domain), headers=self._headers())
        self._check(resp)
        return parse_body(resp)

    def is_domain_active(self, domain):
        info = self.get_domain_info(domain)
        return info.status == "ACTIVE"

    def get_record(self, domain, record_type, name):
        """
        Every record of one (type, name).

        GoDaddy answers with a JSON ARRAY carrying one entry PER VALUE, so this
        returns a LIST of `objict` — a multi-value TXT set is several entries,
        not one.
        """
        resp = requests.get(
            build_url("domains", domain, "records", record_type, name),
            headers=self._headers())
        self._check(resp)
        return parse_body(resp)

    def get_records(self, domain):
        """
        Every record in the zone.

        GoDaddy answers with a JSON ARRAY (again, one entry per value), so this
        returns a LIST of `objict`.
        """
        resp = requests.get(
            build_url("domains", domain, "records"), headers=self._headers())
        self._check(resp)
        return parse_body(resp)

    def put_records(self, domain, record_type, name, entries):
        """
        REPLACE the whole (record_type, name) set with the raw payload `entries`.

        `entries` is GoDaddy's own shape — a list of {"data", "ttl"} mappings —
        for callers that need a DIFFERENT ttl per value. Most callers want
        `edit_record`.

        The PUT replaces EVERY record of this (type, name), so `entries` must be
        the COMPLETE desired set: a partial list silently erases the rest.
        """
        resp = requests.put(
            build_url("domains", domain, "records", record_type, name),
            headers=self._headers(), json=list(entries))
        self._check(resp)
        return parse_body(resp) if resp.content else {"status": "success"}

    def edit_record(self, domain, record_type, name, data, ttl):
        """
        REPLACE the whole (record_type, name) set with `data`, all at `ttl`.

        `data` is either a single value or a LIST of values, and one payload
        entry is built per value. Multi-value matters because the PUT replaces
        the entire set: a wildcard and its apex share one `_acme-challenge` TXT
        name and both digests have to go out in the SAME call, or the second
        write erases the first.

        Returns the parsed body, or {"status": "success"} for the empty body
        GoDaddy normally sends.
        """
        values = list(data) if isinstance(data, (list, tuple, set)) else [data]
        return self.put_records(
            domain, record_type, name,
            [{"data": value, "ttl": ttl} for value in values])

    def add_record(self, domain, record_type, name, data, ttl):
        return self.edit_record(domain, record_type, name, data, ttl)

    def bulk_add_records(self, domain, records):
        resp = requests.patch(
            build_url("domains", domain, "records"),
            headers=self._headers(), json=records)
        self._check(resp)
        return parse_body(resp) if resp.content else {"status": "success"}
