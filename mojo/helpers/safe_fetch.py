"""
SSRF-safe outbound fetch for caller-supplied URLs.

Any framework code that fetches a URL somebody else chose — a user-supplied
page, a remote client's published metadata document, a self-probe of a
configured base URL — should go through this module rather than calling
``requests`` directly. It refuses private and cloud-internal addresses even
when a public-looking hostname resolves to one, re-checks every redirect hop
(absolute, relative and scheme-relative), and caps redirects, body size and
socket time.

The name resolver and the HTTP transport are injectable, so every branch can be
exercised without touching the network or patching process-wide state.

Known limits (see ``docs/django_developer/helpers/safe_fetch.md``):
  - check-then-connect: the guard's lookup and the transport's own lookup are
    two separate DNS queries, so a rebinding between them is not caught
  - the byte cap counts decoded bytes
  - ``timeout`` bounds each socket operation, not the whole transfer
"""
import ipaddress
import socket
from urllib.parse import urljoin, urlparse

import requests
from objict import objict
from urllib3.exceptions import LocationParseError
from urllib3.util import parse_url

DEFAULT_TIMEOUT = 10
MAX_REDIRECTS = 3
# Cap response body at 1MB before parsing to prevent memory exhaustion
MAX_RAW_BYTES = 1_048_576
DEFAULT_USER_AGENT = "django-mojo/1.0"
DEFAULT_SCHEMES = ("http", "https")

# How much is pulled off the socket at a time while honouring the byte cap
CHUNK_SIZE = 65536

# Never replayed to an origin other than the one the caller addressed
CREDENTIAL_HEADERS = ("authorization", "cookie", "proxy-authorization")

# Private/reserved IP ranges that should never be fetched (SSRF protection)
BLOCKED_NETWORKS = [
    ipaddress.ip_network("0.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("100.64.0.0/10"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("240.0.0.0/4"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),
    ipaddress.ip_network("2002::/16"),
]


def is_blocked_ip(ip):
    """
    Check if an IP address is private/reserved. Handles IPv4-mapped IPv6.

    Accepts an ``ipaddress`` address object or an address string; a string
    that is not an address is treated as blocked.
    """
    if isinstance(ip, str):
        try:
            ip = ipaddress.ip_address(ip)
        except ValueError:
            # Fail closed — an address the guard cannot read is one it cannot clear
            return True
    # Unwrap IPv4-mapped IPv6 addresses (e.g. ::ffff:127.0.0.1 -> 127.0.0.1)
    check = ip.ipv4_mapped if hasattr(ip, "ipv4_mapped") and ip.ipv4_mapped else ip
    if check.is_private or check.is_loopback or check.is_link_local or check.is_reserved or check.is_multicast:
        return True
    for network in BLOCKED_NETWORKS:
        if check in network:
            return True
    return False


def _resolve(hostname):
    """Default resolver — every address ``getaddrinfo`` knows for a hostname."""
    try:
        results = socket.getaddrinfo(hostname, None)
    except (socket.gaierror, UnicodeError):
        return []
    return [sockaddr[0] for _, _, _, _, sockaddr in results]


def _host_verdict(hostname, resolver=None):
    """
    Judge a hostname: None (acceptable), "private" or "unresolvable".

    An IP literal is judged directly and the resolver is never consulted. An
    address the resolver returns that ``ipaddress`` cannot parse counts as
    private — the guard fails closed rather than admitting the unknown.
    """
    try:
        literal = ipaddress.ip_address(hostname)
    except ValueError:
        literal = None
    if literal is not None:
        return "private" if is_blocked_ip(literal) else None

    resolve = resolver if resolver is not None else _resolve
    addresses = list(resolve(hostname))
    if not addresses:
        return "unresolvable"
    for address in addresses:
        if is_blocked_ip(address):
            return "private"
    return None


def _safe_verdict(hostname, resolver):
    """
    ``_host_verdict`` that cannot throw.

    A resolver is I/O — the default one raises OSError families the guard has
    no business enumerating, and an injected one can raise anything. Any
    failure counts as unresolvable, which refuses the fetch before the
    transport is contacted.
    """
    try:
        return _host_verdict(hostname, resolver)
    except Exception:
        return "unresolvable"


def _authority(url):
    """The raw authority substring, read as permissively as any parser might."""
    rest = url.split("://", 1)[1] if "://" in url else url
    for separator in ("/", "?", "#"):
        cut = rest.find(separator)
        if cut != -1:
            rest = rest[:cut]
    return rest


def _transport_host(url):
    """
    The host the transport will actually dial, read with the transport's parser.

    urlparse and urllib3 disagree about where an authority ends: urllib3 stops
    at a backslash, so urlparse reads ``http://127.0.0.1\\@evil.com/`` as
    ``evil.com`` while requests connects to ``127.0.0.1``. Guarding urlparse's
    answer would clear a host nobody ever contacts, so the check is done on
    urllib3's. A backslash anywhere in the authority is refused outright —
    nothing legitimate needs one — and an unparsable or host-less URL returns
    None so the caller fails closed.
    """
    if "\\" in _authority(url):
        return None
    try:
        host = parse_url(url).host
    except LocationParseError:
        return None
    if not host:
        return None
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    return host.lower()


def _origin(url):
    """(scheme, host, port) as the transport sees it — for cross-origin checks."""
    try:
        parts = parse_url(url)
    except LocationParseError:
        return None
    return (parts.scheme, (parts.host or "").lower(), parts.port)


def is_private_hostname(hostname, resolver=None):
    """Resolve a hostname and report whether it points at a private/reserved IP."""
    return _host_verdict(hostname, resolver) == "private"


def safe_fetch(url, timeout=DEFAULT_TIMEOUT, max_bytes=MAX_RAW_BYTES,
               max_redirects=MAX_REDIRECTS, headers=None, allow_hosts=None,
               schemes=DEFAULT_SCHEMES, resolver=None, transport=None):
    """
    Fetch a caller-supplied URL with SSRF protection. Returns ``(result, error)``.

    Exactly one of the two is None, and nothing raises for a bad URL or a
    network failure. ``result`` is an objict with ``url`` (final URL after
    redirects), ``status_code``, ``headers``, ``content`` (bytes, capped),
    ``text`` and ``truncated``.

    ``allow_hosts`` names hostnames exempt from the private/unresolvable
    refusal **at the initial URL only** — a redirect an attacker chose never
    inherits the exemption. Entries are unbracketed host values and the
    exemption covers every port on that host; never build the list from
    user-controlled input. ``schemes`` is applied at the initial URL and at
    every hop, and accepts a bare string. ``resolver(hostname)`` returns an
    iterable of address strings; ``transport`` is anything exposing
    ``requests.Session.get``. Credential headers are dropped on a hop that
    crosses to a different origin.
    """
    # A bare string would make the scheme test a substring test ("http" in "https")
    schemes = (schemes,) if isinstance(schemes, str) else tuple(schemes)

    allowed_hosts = set()
    if allow_hosts:
        allowed_hosts = set(str(entry).lower() for entry in allow_hosts)

    try:
        scheme = urlparse(url).scheme
    except ValueError:
        return None, "Invalid URL — no hostname found"

    if scheme not in schemes:
        return None, "Unsupported scheme '{}'. Only {} are allowed.".format(
            scheme, " and ".join(schemes))

    hostname = _transport_host(url)
    if not hostname:
        return None, "Invalid URL — no hostname found"

    if hostname not in allowed_hosts:
        verdict = _safe_verdict(hostname, resolver)
        if verdict == "private":
            return None, "Cannot fetch private or internal addresses"
        if verdict == "unresolvable":
            return None, f"Could not connect to {hostname}"

    send_headers = {"User-Agent": DEFAULT_USER_AGENT}
    if headers:
        send_headers.update(headers)

    session = transport if transport is not None else requests.Session()
    owns_session = transport is None
    current_url = url
    try:
        for _ in range(max_redirects + 1):
            resp = session.get(
                current_url,
                timeout=timeout,
                headers=send_headers,
                allow_redirects=False,
                stream=True,
            )

            if resp.is_redirect and "location" in resp.headers:
                # Relative and scheme-relative targets resolve against the hop
                # we are standing on, then get the same check as the first URL.
                location = resp.headers["location"]
                resp.close()
                try:
                    target = urljoin(current_url, location)
                    hop_scheme = urlparse(target).scheme
                except ValueError:
                    # urljoin itself rejects e.g. an unbalanced IPv6 bracket
                    return None, "Redirect target is not a valid URL"
                hop_hostname = _transport_host(target)
                if not hop_hostname:
                    return None, "Redirect target is not a valid URL"
                if hop_scheme not in schemes:
                    return None, f"Redirect to unsupported scheme '{hop_scheme}'"
                # allow_hosts deliberately does NOT apply here: the hop is
                # chosen by the response, so exempting it would hand an
                # attacker a port scan of the allowed host.
                verdict = _safe_verdict(hop_hostname, resolver)
                if verdict == "private":
                    return None, "Redirect target is a private or internal address"
                if verdict == "unresolvable":
                    return None, f"Could not connect to {hop_hostname}"
                if _origin(target) != _origin(current_url):
                    # Credentials the caller set for their origin must not be
                    # replayed to whatever host the redirect names.
                    send_headers = dict(
                        (name, value) for name, value in send_headers.items()
                        if name.lower() not in CREDENTIAL_HEADERS
                    )
                current_url = target
                continue

            # Not a redirect — read the body with a byte cap
            raw = b""
            for chunk in resp.iter_content(chunk_size=CHUNK_SIZE):
                raw += chunk
                if len(raw) > max_bytes:
                    break
            resp.close()
            truncated = len(raw) > max_bytes
            content = raw[:max_bytes]
            resp._content = content
            return objict(
                url=current_url,
                status_code=resp.status_code,
                headers=resp.headers,
                content=content,
                text=resp.text,
                truncated=truncated,
            ), None

        return None, f"Too many redirects (max {max_redirects})"
    except requests.exceptions.ConnectionError:
        # ConnectTimeout is both a ConnectionError and a Timeout; connect wins.
        return None, f"Could not connect to {hostname}"
    except requests.exceptions.Timeout:
        return None, f"Request timed out after {timeout}s"
    except requests.exceptions.RequestException:
        return None, "Request failed"
    finally:
        if owns_session:
            session.close()
