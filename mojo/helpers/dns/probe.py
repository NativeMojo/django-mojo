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

import ipaddress
import math
import re
import time

try:
    import dns.exception as dns_exception
    import dns.nameserver as dns_nameserver
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

# The bounded address reader is separate from the legacy authority probes.
MAX_RESOLVED_ADDRESSES = 16
_MAX_ADDRESS_NAMESERVERS = 16
_HOST_LABEL_RE = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z")


class BoundedResolveError(Exception):
    """A stable DNS failure code, without resolver text or host details."""

    def __init__(self, code):
        self.code = code
        super().__init__(code)
        self.__suppress_context__ = True


def _address_budget(deadline, clock):
    try:
        now = clock()
        if (isinstance(deadline, bool) or not isinstance(deadline, (int, float))
                or not math.isfinite(deadline) or not math.isfinite(now)):
            raise ValueError()
        remaining = deadline - now
    except Exception:
        raise BoundedResolveError("invalid_deadline") from None
    if remaining <= 0:
        raise BoundedResolveError("deadline_exceeded") from None
    return remaining


def _address_hostname(hostname):
    if not isinstance(hostname, str) or not hostname or len(hostname) > 254:
        raise BoundedResolveError("invalid_hostname")
    # Scoped IPv6 needs interface lookup and is not a plain numeric endpoint.
    if "%" in hostname:
        raise BoundedResolveError("invalid_hostname")
    try:
        return str(ipaddress.ip_address(hostname)), True
    except ValueError:
        pass
    try:
        name = hostname.removesuffix(".").encode("idna").decode("ascii").lower()
    except UnicodeError:
        raise BoundedResolveError("invalid_hostname") from None
    if (not name or len(name) > 253
            or not all(_HOST_LABEL_RE.fullmatch(label) for label in name.split("."))):
        raise BoundedResolveError("invalid_hostname")
    return name, False


def _address_resolver(resolver_factory):
    try:
        resolver = resolver_factory()
    except Exception:
        raise BoundedResolveError("resolver_unavailable") from None
    try:
        nameservers = resolver.nameservers
        if not isinstance(nameservers, (list, tuple)):
            raise BoundedResolveError("resolver_unsupported")
        if not nameservers:
            raise BoundedResolveError("resolver_unavailable")
        if (len(nameservers) > _MAX_ADDRESS_NAMESERVERS
                or not callable(resolver.resolve)):
            raise BoundedResolveError("resolver_unsupported")
        for nameserver in nameservers:
            # DoH/DoT/DoQ can bootstrap names or perform TLS work outside the
            # DNS query lifetime. Only the ordinary numeric DNS transport fits.
            if type(nameserver) is dns_nameserver.Do53Nameserver:
                nameserver = nameserver.address
            if (not isinstance(nameserver, str) or len(nameserver) > 45
                    or "%" in nameserver):
                raise BoundedResolveError("resolver_unsupported")
            ipaddress.ip_address(nameserver)
    except BoundedResolveError:
        raise
    except Exception:
        raise BoundedResolveError("resolver_unsupported") from None
    return resolver


def resolve_addresses(hostname, deadline, *, resolver_factory=None, clock=None):
    """Resolve A and AAAA under one absolute monotonic deadline.

    Return at most 16 canonical numeric addresses, deduplicated and sorted
    IPv4 first, then numerically within each family. Private addresses remain
    usable: callers own address policy. Numeric literals and ``localhost``
    (including a trailing dot) do not query DNS. All paths require an unexpired
    deadline. Names use IDNA, bounded host labels, no search suffixes, and only
    numeric DNS/53 nameservers; encrypted/custom resolver transports are refused.

    ``resolver_factory`` returns a fresh dnspython-compatible resolver without
    performing network work. ``clock`` defaults to ``time.monotonic``. Each A/
    AAAA query receives the remaining lifetime and socket timeout; late results
    and partial results on lookup failure are rejected. No system socket lookup
    or detached work is used as a fallback. Errors are ``BoundedResolveError``
    with a stable ``code`` (also the exception text), never raw resolver details.
    """
    if clock is None:
        clock = time.monotonic
    _address_budget(deadline, clock)
    name, numeric = _address_hostname(hostname)
    if numeric:
        _address_budget(deadline, clock)
        return [name]
    if name == "localhost":
        _address_budget(deadline, clock)
        return ["127.0.0.1", "::1"]
    if not DNS_AVAILABLE:
        raise BoundedResolveError("resolver_unavailable")
    if resolver_factory is None:
        resolver_factory = dns_resolver.Resolver
    _address_budget(deadline, clock)
    resolver = _address_resolver(resolver_factory)
    addresses = set()
    answer_count = 0
    for rtype, version in (("A", 4), ("AAAA", 6)):
        remaining = _address_budget(deadline, clock)
        try:
            resolver.timeout = remaining
            resolver.lifetime = remaining
            # Absolute names and search=False prevent system search expansion.
            answers = resolver.resolve(name + ".", rtype, lifetime=remaining, search=False)
        except (dns_resolver.NXDOMAIN, dns_resolver.NoAnswer):
            _address_budget(deadline, clock)
            continue
        except dns_exception.Timeout:
            _address_budget(deadline, clock)
            raise BoundedResolveError("dns_timeout") from None
        except Exception:
            _address_budget(deadline, clock)
            raise BoundedResolveError("dns_lookup_failed") from None
        _address_budget(deadline, clock)
        try:
            if answers is None or isinstance(answers, (str, bytes, dict)):
                raise BoundedResolveError("dns_answer_invalid")
            for answer in answers:
                _address_budget(deadline, clock)
                answer_count += 1
                if answer_count > MAX_RESOLVED_ADDRESSES:
                    raise BoundedResolveError("dns_answer_overflow")
                value = getattr(answer, "address", answer)
                if not isinstance(value, str) or len(value) > 45 or "%" in value:
                    raise BoundedResolveError("dns_answer_invalid")
                address = ipaddress.ip_address(value)
                if address.version != version:
                    raise BoundedResolveError("dns_answer_invalid")
                addresses.add(address)
        except BoundedResolveError:
            raise
        except Exception:
            raise BoundedResolveError("dns_answer_invalid") from None
    _address_budget(deadline, clock)
    if not addresses:
        raise BoundedResolveError("dns_no_answer")
    ordered = sorted(addresses, key=lambda item: (item.version, int(item)))
    return [str(address) for address in ordered]


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


def _default_resolver(timeout=QUERY_TIMEOUT, dns=None):
    """A system-configured resolver, used only to LOOK UP the authority."""
    if dns is None:
        dns = dns_resolver
    resolver = dns.Resolver()
    resolver.timeout = timeout
    resolver.lifetime = timeout
    return resolver


def _pinned_resolver(addresses, timeout=QUERY_TIMEOUT, dns=None):
    """A resolver that talks ONLY to the given nameserver addresses."""
    if dns is None:
        dns = dns_resolver
    resolver = dns.Resolver(configure=False)
    resolver.nameservers = list(addresses)
    resolver.timeout = timeout
    resolver.lifetime = timeout
    return resolver


def _rdata_host(rdata):
    """Hostname out of an NS or CNAME rdata."""
    target = getattr(rdata, "target", None)
    if target is not None:
        return normalize_name(target)
    return normalize_name(rdata)


def query_cname(fqdn, nameservers=None, timeout=QUERY_TIMEOUT, *, dns=None):
    """Query the directly attached CNAME RRset at authoritative servers.

    A recursive answer is not ownership proof: it may be stale, synthesized,
    or served from a cache after the authoritative value changed.  Resolve the
    closest public DNS cut, pin a resolver to those server addresses, and ask
    for CNAME specifically.  Missing names/RRsets are clean empty answers;
    authority discovery and transport failures remain distinguishable.

    ``dns`` is an injection seam for tests: a stand-in for the
    ``dns.resolver`` module (None means the real dnspython resolver).
    """
    if not DNS_AVAILABLE:
        return objict(targets=[], error="dnspython not installed")
    if dns is None:
        dns = dns_resolver

    name = normalize_name(fqdn)
    if not name:
        return objict(targets=[], error="invalid name")
    zone = None
    addresses = list(nameservers) if nameservers else []
    if not addresses:
        found = find_zone_nameservers(name, timeout=timeout, dns=dns)
        if found.error:
            return objict(targets=[], zone=None, nameservers=[], error=found.error)
        zone = found.zone
        addresses = resolve_nameserver_addresses(
            found.nameservers, timeout=timeout, dns=dns)
        if not addresses:
            return objict(
                targets=[], zone=zone, nameservers=[],
                error=f"could not resolve any nameserver address for zone {zone}")

    try:
        answers = _pinned_resolver(addresses, timeout, dns=dns).resolve(
            name, "CNAME")
    except (dns.NXDOMAIN, dns.NoAnswer):
        return objict(
            targets=[], zone=zone, nameservers=addresses, error=None)
    except Exception as err:
        return objict(
            targets=[], zone=zone, nameservers=addresses, error=str(err))

    targets = []
    for rdata in answers:
        target = _rdata_host(rdata)
        if target and target not in targets:
            targets.append(target)
    return objict(
        targets=targets, zone=zone, nameservers=addresses, error=None)


def verify_one_hop_cname(source, expected_target, timeout=QUERY_TIMEOUT,
                         dns=None):
    """Prove ``source`` is exactly one public CNAME hop to ``expected_target``.

    Returns ``objict(ok, error)`` without leaking unrelated DNS answers.  A
    target that is itself a CNAME is refused: accepting it would make the
    tenant proof depend on a second, mutable hop outside the stored contract.
    """
    source = normalize_name(source)
    expected_target = normalize_name(expected_target)
    if not source or not expected_target or source == expected_target:
        return objict(ok=False, error="invalid CNAME delegation")

    first = query_cname(source, timeout=timeout, dns=dns)
    if first.error:
        return objict(ok=False, error="CNAME lookup failed")
    if first.targets != [expected_target]:
        return objict(ok=False, error="CNAME delegation does not match the allocation")

    second = query_cname(expected_target, timeout=timeout, dns=dns)
    if second.error:
        return objict(ok=False, error="CNAME target lookup failed")
    if second.targets:
        return objict(ok=False, error="CNAME delegation must be exactly one hop")
    return objict(ok=True, error=None)


def _rdata_address(rdata):
    """IP address out of an A/AAAA rdata."""
    address = getattr(rdata, "address", None)
    if address:
        return str(address)
    return str(rdata).strip()


def find_zone_nameservers(fqdn, timeout=QUERY_TIMEOUT, *, dns=None):
    """
    Walk up the labels of `fqdn` and return the closest zone that has NS records.

    Returns objict(zone, nameservers, error). `nameservers` are hostnames.

    ``dns`` is an injection seam for tests: a stand-in for the
    ``dns.resolver`` module (None means the real dnspython resolver).
    """
    if not DNS_AVAILABLE:
        return objict(zone=None, nameservers=[], error="dnspython not installed")
    if dns is None:
        dns = dns_resolver

    candidates = zone_candidates(fqdn)
    if not candidates:
        return objict(zone=None, nameservers=[], error="invalid name")

    resolver = _default_resolver(timeout, dns=dns)
    last_error = None
    for candidate in candidates:
        try:
            answers = resolver.resolve(candidate, "NS")
        except dns.NXDOMAIN:
            # The name itself does not exist — its parent still might.
            continue
        except dns.NoAnswer:
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


def resolve_nameserver_addresses(hosts, timeout=QUERY_TIMEOUT, *, dns=None):
    """Resolve NS hostnames to addresses (A first, AAAA only when there is no A)."""
    if not DNS_AVAILABLE:
        return []

    resolver = _default_resolver(timeout, dns=dns)
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


def query_txt(fqdn, nameservers=None, timeout=QUERY_TIMEOUT, *, dns=None):
    """
    Query TXT for `fqdn` against the zone's authoritative nameservers.

    `nameservers` may be passed as a list of addresses to skip the authority
    lookup. Returns objict(txt_values, zone, nameservers, error) — the field is
    `txt_values` and not `values` because objict is a dict subclass and `.values`
    would resolve to the dict method instead of our data. A name that simply has
    no TXT record yet is NOT an error — it returns an empty `txt_values` list.

    ``dns`` is an injection seam for tests: a stand-in for the
    ``dns.resolver`` module (None means the real dnspython resolver).
    """
    if not DNS_AVAILABLE:
        return objict(txt_values=[], zone=None, nameservers=[], error="dnspython not installed")
    if dns is None:
        dns = dns_resolver

    name = normalize_name(fqdn)
    zone = None
    addresses = list(nameservers) if nameservers else []

    if not addresses:
        found = find_zone_nameservers(name, timeout=timeout, dns=dns)
        if found.error:
            return objict(txt_values=[], zone=None, nameservers=[], error=found.error)
        zone = found.zone
        addresses = resolve_nameserver_addresses(
            found.nameservers, timeout=timeout, dns=dns)
        if not addresses:
            return objict(
                txt_values=[], zone=zone, nameservers=[],
                error=f"could not resolve any nameserver address for zone {zone}")

    resolver = _pinned_resolver(addresses, timeout=timeout, dns=dns)
    try:
        answers = resolver.resolve(name, "TXT")
    except dns.NXDOMAIN:
        return objict(txt_values=[], zone=zone, nameservers=addresses, error=None)
    except dns.NoAnswer:
        return objict(txt_values=[], zone=zone, nameservers=addresses, error=None)
    except Exception as err:
        return objict(txt_values=[], zone=zone, nameservers=addresses, error=str(err))

    values = []
    for rdata in answers:
        value = txt_value(rdata)
        if value and value not in values:
            values.append(value)
    return objict(txt_values=values, zone=zone, nameservers=addresses, error=None)


def wait_for_txt(fqdn, expected_values, timeout=DEFAULT_TIMEOUT,
                 interval=DEFAULT_INTERVAL, *, dns=None, query=None,
                 sleep=None):
    """
    Poll the authoritative nameservers until every expected TXT value is visible.

    Returns (ok, seen_values).

    ``dns``, ``query`` and ``sleep`` are injection seams for tests: ``query``
    replaces the per-poll TXT lookup (default ``query_txt``, threaded with
    ``dns``), and ``sleep`` replaces the between-poll wait (default
    ``time.sleep``).

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

    if sleep is None:
        sleep = time.sleep
    deadline = time.monotonic() + max(timeout or 0, 0)
    seen = []
    while True:
        if query is not None:
            result = query(fqdn)
        else:
            result = query_txt(fqdn, dns=dns)
        seen = result.txt_values
        if expected.issubset(set(seen)):
            return True, seen
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            return False, seen
        sleep(max(min(interval, remaining), 0))
