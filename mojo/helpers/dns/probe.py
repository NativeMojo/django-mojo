"""
Authoritative DNS probe — "is this TXT record actually live yet?"

Used as the gate before asking a CA to validate an ACME DNS-01 challenge (and
before trusting any freshly written TXT record). The critical property is that
queries go to the zone's **authoritative nameservers**, never to the local
recursive resolver:

- a recursive answer can be a false NEGATIVE (a negative answer for the name is
  still cached, so a record we just wrote looks absent), and
- worse, a false POSITIVE on a stale value (the old challenge digest is still
  cached, so we tell the CA to validate a record it will never see).

Either mistake costs a failed issuance, so we resolve the zone's NS set
ourselves and ask those servers directly.

All functions return plain values or objict — no Django, no model imports; this
module is importable with zero Django setup.

Example:
    from mojo.helpers.dns import probe

    ok, seen = probe.wait_for_txt(
        "_acme-challenge.example.com",
        ["digest-for-apex", "digest-for-wildcard"],
        timeout=300)
    if not ok:
        # `seen` is what the authoritative servers actually returned
        ...
"""

import re
import time

try:
    import dns.resolver as dns_resolver
    DNS_AVAILABLE = True
except ImportError:
    DNS_AVAILABLE = False

from objict import objict


# Seconds to wait for a single DNS query (per nameserver attempt).
QUERY_TIMEOUT = 5
# Defaults for the polling loop.
DEFAULT_TIMEOUT = 300
DEFAULT_INTERVAL = 5

# Matches each quoted chunk of a TXT rdata rendered as text:
#   "chunk one" "chunk two"
_TXT_CHUNK_RE = re.compile(r'"((?:[^"\\]|\\.)*)"')


def normalize_name(name):
    """Lowercase, strip whitespace and any trailing dot."""
    if name is None:
        return ""
    return str(name).strip().lower().rstrip(".")


def strip_quotes(value):
    """Strip one layer of surrounding double quotes from a TXT string."""
    value = str(value).strip()
    if len(value) >= 2 and value[0] == '"' and value[-1] == '"':
        return value[1:-1]
    return value


def txt_value(rdata):
    """
    Normalize one TXT rdata to a single comparable string.

    dnspython splits a long TXT record into multiple 255-byte strings, so the
    chunks are joined back together, and any surrounding quoting is removed.
    """
    strings = getattr(rdata, "strings", None)
    if strings:
        parts = []
        for chunk in strings:
            if isinstance(chunk, bytes):
                chunk = chunk.decode("utf-8", "replace")
            parts.append(strip_quotes(chunk))
        return "".join(parts)

    raw = str(rdata)
    chunks = _TXT_CHUNK_RE.findall(raw)
    if chunks:
        return "".join(chunks)
    return strip_quotes(raw)


def zone_candidates(fqdn):
    """
    Every name to try when looking for the closest enclosing zone, longest first.

    "_acme-challenge.foo.example.com" ->
        ["_acme-challenge.foo.example.com", "foo.example.com", "example.com"]

    The bare TLD is never a candidate — we would never hold NS authority there,
    and its NS set tells us nothing useful about the record we care about.
    """
    name = normalize_name(fqdn)
    if not name:
        return []
    labels = name.split(".")
    return [".".join(labels[i:]) for i in range(0, max(len(labels) - 1, 1))]


def _default_resolver(timeout=QUERY_TIMEOUT):
    """A system-configured resolver, used only to LOOK UP the authority."""
    resolver = dns_resolver.Resolver()
    resolver.timeout = timeout
    resolver.lifetime = timeout
    return resolver


def _pinned_resolver(addresses, timeout=QUERY_TIMEOUT):
    """A resolver that talks ONLY to the given nameserver addresses."""
    resolver = dns_resolver.Resolver(configure=False)
    resolver.nameservers = list(addresses)
    resolver.timeout = timeout
    resolver.lifetime = timeout
    return resolver


def _rdata_host(rdata):
    """Hostname out of an NS rdata."""
    target = getattr(rdata, "target", None)
    if target is not None:
        return normalize_name(target)
    return normalize_name(rdata)


def _rdata_address(rdata):
    """IP address out of an A/AAAA rdata."""
    address = getattr(rdata, "address", None)
    if address:
        return str(address)
    return str(rdata).strip()


def find_zone_nameservers(fqdn, timeout=QUERY_TIMEOUT):
    """
    Walk up the labels of `fqdn` and return the closest zone that has NS records.

    Returns objict(zone, nameservers, error). `nameservers` are hostnames.
    """
    if not DNS_AVAILABLE:
        return objict(zone=None, nameservers=[], error="dnspython not installed")

    candidates = zone_candidates(fqdn)
    if not candidates:
        return objict(zone=None, nameservers=[], error="invalid name")

    resolver = _default_resolver(timeout)
    last_error = None
    for candidate in candidates:
        try:
            answers = resolver.resolve(candidate, "NS")
        except dns_resolver.NXDOMAIN:
            # The name itself does not exist — its parent still might.
            continue
        except dns_resolver.NoAnswer:
            # Exists, but is not a zone cut — keep walking up.
            continue
        except Exception as err:
            last_error = str(err)
            continue

        hosts = []
        for rdata in answers:
            host = _rdata_host(rdata)
            if host and host not in hosts:
                hosts.append(host)
        if hosts:
            return objict(zone=candidate, nameservers=hosts, error=None)

    if last_error is None:
        last_error = f"no authoritative nameservers found for {normalize_name(fqdn)}"
    return objict(zone=None, nameservers=[], error=last_error)


def resolve_nameserver_addresses(hosts, timeout=QUERY_TIMEOUT):
    """Resolve NS hostnames to addresses (A first, AAAA only when there is no A)."""
    if not DNS_AVAILABLE:
        return []

    resolver = _default_resolver(timeout)
    addresses = []
    for host in hosts:
        for rtype in ("A", "AAAA"):
            found = False
            try:
                answers = resolver.resolve(host, rtype)
            except Exception:
                continue
            for rdata in answers:
                address = _rdata_address(rdata)
                if address:
                    found = True
                    if address not in addresses:
                        addresses.append(address)
            if found:
                break
    return addresses


def query_txt(fqdn, nameservers=None, timeout=QUERY_TIMEOUT):
    """
    Query TXT for `fqdn` against the zone's authoritative nameservers.

    `nameservers` may be passed as a list of addresses to skip the authority
    lookup. Returns objict(txt_values, zone, nameservers, error) — the field is
    `txt_values` and not `values` because objict is a dict subclass and `.values`
    would resolve to the dict method instead of our data. A name that simply has
    no TXT record yet is NOT an error — it returns an empty `txt_values` list.
    """
    if not DNS_AVAILABLE:
        return objict(txt_values=[], zone=None, nameservers=[], error="dnspython not installed")

    name = normalize_name(fqdn)
    zone = None
    addresses = list(nameservers) if nameservers else []

    if not addresses:
        found = find_zone_nameservers(name, timeout=timeout)
        if found.error:
            return objict(txt_values=[], zone=None, nameservers=[], error=found.error)
        zone = found.zone
        addresses = resolve_nameserver_addresses(found.nameservers, timeout=timeout)
        if not addresses:
            return objict(
                txt_values=[], zone=zone, nameservers=[],
                error=f"could not resolve any nameserver address for zone {zone}")

    resolver = _pinned_resolver(addresses, timeout=timeout)
    try:
        answers = resolver.resolve(name, "TXT")
    except dns_resolver.NXDOMAIN:
        return objict(txt_values=[], zone=zone, nameservers=addresses, error=None)
    except dns_resolver.NoAnswer:
        return objict(txt_values=[], zone=zone, nameservers=addresses, error=None)
    except Exception as err:
        return objict(txt_values=[], zone=zone, nameservers=addresses, error=str(err))

    values = []
    for rdata in answers:
        value = txt_value(rdata)
        if value and value not in values:
            values.append(value)
    return objict(txt_values=values, zone=zone, nameservers=addresses, error=None)


def wait_for_txt(fqdn, expected_values, timeout=DEFAULT_TIMEOUT, interval=DEFAULT_INTERVAL):
    """
    Poll the authoritative nameservers until every expected TXT value is visible.

    Returns (ok, seen_values).

    `ok` is True only when EVERY value in `expected_values` is present — a
    subset check, not equality, because a wildcard and its base domain
    legitimately share one `_acme-challenge` name carrying two values, and other
    unrelated TXT values may live on the same name.

    On timeout the last-seen values are returned so the caller can log what the
    authority actually served. An empty `expected_values` fails closed —
    (False, []) with no query — since an empty expectation would otherwise be
    trivially satisfied by any answer.
    """
    if isinstance(expected_values, str):
        expected_values = [expected_values]

    expected = set()
    for value in expected_values or []:
        value = strip_quotes(value)
        if value:
            expected.add(value)
    if not expected:
        return False, []

    deadline = time.monotonic() + max(timeout or 0, 0)
    seen = []
    while True:
        result = query_txt(fqdn)
        seen = result.txt_values
        if expected.issubset(set(seen)):
            return True, seen
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False, seen
        time.sleep(max(min(interval, remaining), 0))
