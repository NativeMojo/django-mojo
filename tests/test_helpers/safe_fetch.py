"""
Unit tests for mojo.helpers.safe_fetch — the framework's SSRF guard for
outbound fetches of caller-supplied URLs.

Contract this file enforces:
  - The blocked-network list is pinned; literal private/reserved addresses are
    refused, public ones are not
  - A hostname is judged by what it RESOLVES to, and an IP literal never
    reaches the resolver
  - The initial URL's scheme, hostname and address verdict are checked before
    the transport is touched at all
  - Every redirect hop — absolute, relative or scheme-relative — gets the same
    scheme and address check as the first URL
  - Redirect count, body size and transport exceptions are bounded and mapped
    to stable strings; nothing raises out of safe_fetch

Everything runs against an injected resolver and an injected transport: no
network, no patching of process-wide state.
"""
from testit import helpers as th
from testit.helpers import assert_eq, assert_true

TESTIT_TIER = "core"  # #2792 tier curation


# ---------------------------------------------------------------------------
# Scripted transport / resolver
# ---------------------------------------------------------------------------

def _response(status, headers=None, body=b""):
    """A bare requests.Response the helper can drive like a live one."""
    import requests
    from requests.structures import CaseInsensitiveDict

    resp = requests.Response()
    resp.status_code = status
    # Must be case-insensitive: Response.is_redirect tests `"location" in headers`
    resp.headers = CaseInsensitiveDict(headers or {})
    resp._content = body
    resp._content_consumed = True
    resp.encoding = "utf-8"
    return resp


class _ChunkedResponse:
    """A streaming response that records how much of the body was pulled."""

    def __init__(self, status, headers, chunks):
        import requests
        from requests.structures import CaseInsensitiveDict

        self._resp = requests.Response()
        self._resp.status_code = status
        self._resp.headers = CaseInsensitiveDict(headers or {})
        self._resp._content = b""
        self._resp._content_consumed = True
        self._resp.encoding = "utf-8"
        self.chunks = chunks
        self.consumed = 0
        self.closed_after = None

    # the surface safe_fetch touches
    @property
    def is_redirect(self):
        return self._resp.is_redirect

    @property
    def headers(self):
        return self._resp.headers

    @property
    def status_code(self):
        return self._resp.status_code

    @property
    def text(self):
        return self._resp.text

    def _set_content(self, value):
        self._resp._content = value

    _content = property(lambda self: self._resp._content, _set_content)

    def iter_content(self, chunk_size=1):
        for chunk in self.chunks:
            self.consumed += len(chunk)
            yield chunk

    def close(self):
        self.closed_after = self.consumed


class _Transport:
    """Maps URL -> response or exception, and records every get() call."""

    def __init__(self, routes=None, default=None):
        self.routes = routes or {}
        self.default = default
        self.calls = []

    def get(self, url, **kwargs):
        self.calls.append((url, kwargs))
        answer = self.routes.get(url, self.default)
        assert answer is not None, f"transport asked for an unscripted URL: {url}"
        if isinstance(answer, Exception):
            raise answer
        return answer

    @property
    def urls(self):
        return [url for url, _ in self.calls]


class _ExplodingTransport:
    def get(self, url, **kwargs):
        raise AssertionError(f"transport must not be called, but was asked for {url}")


def _resolver(mapping):
    def resolve(hostname):
        return mapping.get(hostname, [])
    return resolve


def _exploding_resolver(hostname):
    raise AssertionError(f"resolver must not be called, but was asked for {hostname}")


# ---------------------------------------------------------------------------
# Address / hostname judgement
# ---------------------------------------------------------------------------

@th.django_unit_test("safe_fetch: the blocked-network list is pinned")
def test_blocked_networks_pinned(opts):
    from mojo.helpers import safe_fetch as sf

    expected = [
        "0.0.0.0/8", "10.0.0.0/8", "100.64.0.0/10", "127.0.0.0/8",
        "169.254.0.0/16", "172.16.0.0/12", "192.168.0.0/16", "240.0.0.0/4",
        "::1/128", "fc00::/7", "fe80::/10", "2002::/16",
    ]
    assert_eq(
        [str(net) for net in sf.BLOCKED_NETWORKS], expected,
        "BLOCKED_NETWORKS must stay exactly the twelve networks the guard was built on — "
        "a silent removal reopens an SSRF sink",
    )


@th.django_unit_test("safe_fetch: is_blocked_ip refuses private/reserved literals")
def test_is_blocked_ip_literals(opts):
    import ipaddress
    from mojo.helpers import safe_fetch as sf

    blocked = [
        "127.0.0.1", "10.0.0.1", "172.16.0.1", "192.168.1.1", "169.254.169.254",
        "0.0.0.0", "100.64.0.1", "240.0.0.1",
        "::1", "fc00::1", "fe80::1", "2002::1",
        "::ffff:127.0.0.1", "::ffff:169.254.169.254",
    ]
    for value in blocked:
        assert_true(
            sf.is_blocked_ip(ipaddress.ip_address(value)),
            f"{value} must be blocked — it is private, reserved or an IPv4-mapped form of one",
        )
        assert_true(
            sf.is_blocked_ip(value),
            f"is_blocked_ip must accept the string form of {value} too",
        )

    for value in ["8.8.8.8", "2606:4700::1111"]:
        assert_true(
            not sf.is_blocked_ip(ipaddress.ip_address(value)),
            f"{value} is a public address and must not be blocked",
        )


@th.django_unit_test("safe_fetch: an IP literal is judged without consulting the resolver")
def test_is_private_hostname_literal_skips_resolver(opts):
    from mojo.helpers import safe_fetch as sf

    assert_true(
        sf.is_private_hostname("127.0.0.1", resolver=_exploding_resolver),
        "a literal loopback address must be refused directly, never resolved",
    )
    assert_true(
        not sf.is_private_hostname("8.8.8.8", resolver=_exploding_resolver),
        "a literal public address must be accepted directly, never resolved",
    )


@th.django_unit_test("safe_fetch: a hostname is judged by every address it resolves to")
def test_is_private_hostname_resolved(opts):
    from mojo.helpers import safe_fetch as sf

    cases = [
        (["93.184.216.34", "10.0.0.5"], True,
         "one private answer among many must condemn the hostname"),
        (["93.184.216.34"], False,
         "a hostname resolving only to public addresses is acceptable"),
        ([], False,
         "is_private_hostname reports private only — unresolvable is not private"),
        (["not-an-ip"], True,
         "an address the guard cannot parse must fail closed as private"),
    ]
    for answers, expected, msg in cases:
        assert_eq(
            sf.is_private_hostname("host.test", resolver=_resolver({"host.test": answers})),
            expected, msg,
        )


# ---------------------------------------------------------------------------
# Initial URL guards — all of them run before the transport
# ---------------------------------------------------------------------------

@th.django_unit_test("safe_fetch: refuses bad schemes and missing hosts without fetching")
def test_safe_fetch_refuses_bad_scheme_and_missing_host(opts):
    from mojo.helpers import safe_fetch as sf

    cases = [
        ("ftp://x/", "Unsupported scheme 'ftp'. Only http and https are allowed."),
        ("javascript:alert(1)", "Unsupported scheme 'javascript'. Only http and https are allowed."),
        ("http:///p", "Invalid URL — no hostname found"),
        ("http://[::1/", "Invalid URL — no hostname found"),
    ]
    for url, expected in cases:
        result, err = sf.safe_fetch(
            url, resolver=_exploding_resolver, transport=_ExplodingTransport())
        assert result is None, f"{url} must not produce a result"
        assert_eq(err, expected, f"wrong refusal string for {url}")


@th.django_unit_test("safe_fetch: refuses an initial host that resolves private")
def test_safe_fetch_refuses_private_initial_host(opts):
    from mojo.helpers import safe_fetch as sf

    transport = _Transport()
    result, err = sf.safe_fetch(
        "http://evil.test/",
        resolver=_resolver({"evil.test": ["10.0.0.1"]}),
        transport=transport,
    )
    assert result is None, "a hostname resolving to a private address must not be fetched"
    assert_eq(err, "Cannot fetch private or internal addresses", "wrong refusal string")
    assert_eq(transport.urls, [], "the transport must never be reached for a private host")


@th.django_unit_test("safe_fetch: an unresolvable host is refused before the transport")
def test_safe_fetch_unresolvable_host_refused_without_transport(opts):
    from mojo.helpers import safe_fetch as sf

    transport = _Transport()
    result, err = sf.safe_fetch(
        "http://gone.test/", resolver=_resolver({}), transport=transport)
    assert result is None, "an unresolvable host must be refused, not handed to the transport"
    assert_eq(err, "Could not connect to gone.test", "wrong refusal string for an unresolvable host")
    assert_eq(transport.urls, [], "refusing must happen before any request is made")

    # Same fail-closed rule on a redirect hop
    hop = _Transport(routes={
        "http://host.test/": _response(302, {"Location": "http://gone.test/x"}),
    })
    result, err = sf.safe_fetch(
        "http://host.test/",
        resolver=_resolver({"host.test": ["93.184.216.34"]}),
        transport=hop,
    )
    assert result is None, "an unresolvable redirect target must be refused"
    assert_eq(err, "Could not connect to gone.test", "wrong refusal string for an unresolvable hop")
    assert_eq(hop.urls, ["http://host.test/"], "the hop must never be requested")


# ---------------------------------------------------------------------------
# Redirects
# ---------------------------------------------------------------------------

@th.django_unit_test("safe_fetch: absolute redirect targets are re-checked")
def test_safe_fetch_absolute_redirect_rechecked(opts):
    from mojo.helpers import safe_fetch as sf

    public = _resolver({
        "host.test": ["93.184.216.34"],
        "other.test": ["93.184.216.35"],
    })
    transport = _Transport(routes={
        "https://host.test/": _response(302, {"Location": "https://other.test/x"}),
        "https://other.test/x": _response(200, {"Content-Type": "text/html"}, b"done"),
    })
    result, err = sf.safe_fetch("https://host.test/", resolver=public, transport=transport)
    assert err is None, f"a public redirect chain must succeed, got {err}"
    assert_eq(result.url, "https://other.test/x", "result.url must be the final URL")
    assert_eq(result.text, "done", "the final hop's body must be returned")
    assert_eq(
        transport.urls, ["https://host.test/", "https://other.test/x"],
        "both hops must be requested, in order",
    )

    private = _resolver({
        "host.test": ["93.184.216.34"],
        "other.test": ["10.0.0.5"],
    })
    blocked = _Transport(routes={
        "https://host.test/": _response(302, {"Location": "https://other.test/x"}),
    })
    result, err = sf.safe_fetch("https://host.test/", resolver=private, transport=blocked)
    assert result is None, "a redirect into a private address must not be followed"
    assert_eq(err, "Redirect target is a private or internal address", "wrong redirect refusal")
    assert_eq(blocked.urls, ["https://host.test/"], "the private hop must never be requested")


@th.django_unit_test("safe_fetch: relative and scheme-relative redirects resolve and re-check")
def test_safe_fetch_relative_and_scheme_relative_redirects(opts):
    from mojo.helpers import safe_fetch as sf

    transport = _Transport(routes={
        "https://host.test/a": _response(302, {"Location": "/next"}),
        "https://host.test/next": _response(200, {"Content-Type": "text/html"}, b"ok"),
    })
    result, err = sf.safe_fetch(
        "https://host.test/a",
        resolver=_resolver({"host.test": ["93.184.216.34"]}),
        transport=transport,
    )
    assert err is None, f"a relative redirect must be followed, got {err}"
    assert_eq(
        transport.urls, ["https://host.test/a", "https://host.test/next"],
        "a relative Location must be resolved against the current hop, not handed over bare",
    )
    assert_eq(result.url, "https://host.test/next", "result.url must be the resolved relative target")

    scheme_relative = _Transport(routes={
        "https://host.test/a": _response(302, {"Location": "//internal.test/x"}),
    })
    result, err = sf.safe_fetch(
        "https://host.test/a",
        resolver=_resolver({"host.test": ["93.184.216.34"], "internal.test": ["192.168.1.9"]}),
        transport=scheme_relative,
    )
    assert result is None, "a scheme-relative redirect to a private host must be refused"
    assert_eq(err, "Redirect target is a private or internal address", "wrong refusal string")
    assert_eq(
        scheme_relative.urls, ["https://host.test/a"],
        "the scheme-relative private hop must never be requested",
    )


@th.django_unit_test("safe_fetch: redirects to another scheme or an unparsable URL are refused")
def test_safe_fetch_redirect_to_other_scheme_refused(opts):
    from mojo.helpers import safe_fetch as sf

    public = _resolver({"host.test": ["93.184.216.34"]})

    transport = _Transport(routes={
        "https://host.test/": _response(302, {"Location": "ftp://x/y"}),
    })
    result, err = sf.safe_fetch("https://host.test/", resolver=public, transport=transport)
    assert result is None, "a redirect to ftp:// must be refused"
    assert_eq(err, "Redirect to unsupported scheme 'ftp'", "wrong refusal string for a scheme change")

    broken = _Transport(routes={
        "https://host.test/": _response(302, {"Location": "http://[::1/"}),
    })
    result, err = sf.safe_fetch("https://host.test/", resolver=public, transport=broken)
    assert result is None, "an unparsable redirect target must be refused"
    assert_eq(err, "Redirect target is not a valid URL", "wrong refusal string for a broken target")


@th.django_unit_test("safe_fetch: the schemes parameter applies to the initial URL and every hop")
def test_safe_fetch_schemes_per_hop(opts):
    from mojo.helpers import safe_fetch as sf

    result, err = sf.safe_fetch(
        "http://host.test/", schemes=("https",),
        resolver=_exploding_resolver, transport=_ExplodingTransport())
    assert result is None, "http must be refused when only https is allowed"
    assert_eq(
        err, "Unsupported scheme 'http'. Only https are allowed.",
        "wrong refusal string for an https-only caller",
    )

    transport = _Transport(routes={
        "https://host.test/": _response(302, {"Location": "http://public.test/"}),
    })
    result, err = sf.safe_fetch(
        "https://host.test/", schemes=("https",),
        resolver=_resolver({"host.test": ["93.184.216.34"], "public.test": ["93.184.216.35"]}),
        transport=transport,
    )
    assert result is None, "an https-only fetch must not be downgraded by a redirect"
    assert_eq(err, "Redirect to unsupported scheme 'http'", "wrong refusal string for a downgrade")


@th.django_unit_test("safe_fetch: the redirect cap is honoured exactly")
def test_safe_fetch_redirect_cap(opts):
    from mojo.helpers import safe_fetch as sf

    public = _resolver({"host.test": ["93.184.216.34"]})

    transport = _Transport(default=_response(302, {"Location": "https://host.test/loop"}))
    result, err = sf.safe_fetch(
        "https://host.test/", max_redirects=2, resolver=public, transport=transport)
    assert result is None, "an endless redirect loop must not produce a result"
    assert_eq(err, "Too many redirects (max 2)", "wrong cap string")
    assert_eq(len(transport.calls), 3, "max_redirects=2 must allow exactly three requests")

    once = _Transport(default=_response(302, {"Location": "https://host.test/loop"}))
    result, err = sf.safe_fetch(
        "https://host.test/", max_redirects=0, resolver=public, transport=once)
    assert result is None, "max_redirects=0 must refuse to follow anything"
    assert_eq(err, "Too many redirects (max 0)", "wrong cap string for max_redirects=0")
    assert_eq(len(once.calls), 1, "max_redirects=0 must make exactly one request")


# ---------------------------------------------------------------------------
# Body handling
# ---------------------------------------------------------------------------

@th.django_unit_test("safe_fetch: the body is capped and truncation is reported")
def test_safe_fetch_byte_cap(opts):
    from mojo.helpers import safe_fetch as sf

    public = _resolver({"host.test": ["93.184.216.34"]})
    cap = 1024

    # Streamed in many chunks: the helper must stop pulling once the cap is
    # passed, and close the response — otherwise "capped" only means "sliced
    # after the whole body was already in memory".
    chunk = 256
    streamed = _ChunkedResponse(
        200, {"Content-Type": "text/plain"}, [b"A" * chunk] * 20)
    oversized = _Transport(routes={"https://host.test/": streamed})
    result, err = sf.safe_fetch(
        "https://host.test/", max_bytes=cap, resolver=public, transport=oversized)
    assert err is None, f"an oversized body must be truncated, not refused: {err}"
    assert_eq(len(result.content), cap, "content must be cut to exactly max_bytes")
    assert result.truncated is True, "truncated must be True when more bytes were available"
    assert_eq(len(result.text), cap, "text must decode only the capped bytes")
    assert streamed.consumed <= cap + chunk, (
        "the helper must stop reading within one chunk of the cap — it read "
        f"{streamed.consumed} bytes for a {cap}-byte cap"
    )
    assert_eq(
        streamed.closed_after, streamed.consumed,
        "the response must be closed as soon as the cap is reached, releasing the connection",
    )

    exact = _Transport(routes={
        "https://host.test/": _response(200, {"Content-Type": "text/plain"}, b"B" * cap),
    })
    result, err = sf.safe_fetch(
        "https://host.test/", max_bytes=cap, resolver=public, transport=exact)
    assert err is None, f"a body exactly at the cap must succeed: {err}"
    assert_eq(len(result.content), cap, "a body at the cap must survive intact")
    assert result.truncated is False, "truncated must be False when nothing was cut"


@th.django_unit_test("safe_fetch: non-200 and Location-less 3xx responses come back as results")
def test_safe_fetch_non_200_passthrough(opts):
    from mojo.helpers import safe_fetch as sf

    public = _resolver({"host.test": ["93.184.216.34"]})

    missing = _Transport(routes={
        "https://host.test/": _response(404, {"Content-Type": "text/html"}, b"nope"),
    })
    result, err = sf.safe_fetch("https://host.test/", resolver=public, transport=missing)
    assert err is None, "a 404 is the caller's business, not a fetch error"
    assert_eq(result.status_code, 404, "the status code must be passed through")

    headerless = _Transport(routes={
        "https://host.test/": _response(301, {"Content-Type": "text/html"}, b"moved"),
    })
    result, err = sf.safe_fetch("https://host.test/", resolver=public, transport=headerless)
    assert err is None, "a 3xx without Location must not be treated as an error"
    assert_eq(result.status_code, 301, "a 3xx without Location is returned, not followed")
    assert_eq(len(headerless.calls), 1, "a 3xx without Location must not trigger a second request")


# ---------------------------------------------------------------------------
# Transport failures and call shape
# ---------------------------------------------------------------------------

@th.django_unit_test("safe_fetch: transport exceptions map to stable strings, never escape")
def test_safe_fetch_maps_transport_exceptions(opts):
    import requests
    from mojo.helpers import safe_fetch as sf

    public = _resolver({"host.test": ["93.184.216.34"]})
    cases = [
        (requests.exceptions.ConnectionError("boom"), "Could not connect to host.test"),
        (requests.exceptions.ReadTimeout("slow"), "Request timed out after 5s"),
        (requests.exceptions.ConnectTimeout("slow"), "Could not connect to host.test"),
        (requests.exceptions.RequestException("weird"), "Request failed"),
    ]
    for exc, expected in cases:
        transport = _Transport(default=exc)
        result, err = sf.safe_fetch(
            "https://host.test/", timeout=5, resolver=public, transport=transport)
        assert result is None, f"{type(exc).__name__} must not produce a result"
        assert_eq(err, expected, f"wrong mapping for {type(exc).__name__}")


@th.django_unit_test("safe_fetch: allow_hosts exempts the initial URL only")
def test_safe_fetch_allow_hosts_per_hop(opts):
    from mojo.helpers import safe_fetch as sf

    private = _resolver({"self.test": ["127.0.0.1"], "other.test": ["10.0.0.5"]})

    direct = _Transport(routes={
        "http://self.test:8000/": _response(200, {"Content-Type": "text/html"}, b"self"),
    })
    result, err = sf.safe_fetch(
        "http://self.test:8000/", allow_hosts=["self.test"],
        resolver=_exploding_resolver, transport=direct)
    assert err is None, f"an allowed host must be fetched without an address check: {err}"
    assert_eq(result.text, "self", "the allowed host's body must come back")

    escaping = _Transport(routes={
        "http://self.test/": _response(302, {"Location": "http://other.test/x"}),
    })
    result, err = sf.safe_fetch(
        "http://self.test/", allow_hosts=["self.test"], resolver=private, transport=escaping)
    assert result is None, "the exemption must not extend to a host the redirect escapes to"
    assert_eq(err, "Redirect target is a private or internal address", "wrong refusal string")

    # The exemption covers the URL the CALLER chose, not one the response chose.
    # Honouring it per-hop would let a redirect turn a self-probe into a port
    # scan of the allowed host (http://self.test:6379/ and friends).
    staying = _Transport(routes={
        "http://self.test/": _response(302, {"Location": "http://self.test:6379/x"}),
    })
    result, err = sf.safe_fetch(
        "http://self.test/", allow_hosts=["self.test"], resolver=private, transport=staying)
    assert result is None, "a redirect back to the allowed host must still be checked"
    assert_eq(
        err, "Redirect target is a private or internal address",
        "allow_hosts must not survive a redirect, even one that stays on the same host",
    )
    assert_eq(staying.urls, ["http://self.test/"], "the unchecked hop must never be requested")


@th.django_unit_test("safe_fetch: allow_hosts does not bypass the scheme check")
def test_safe_fetch_allow_hosts_does_not_bypass_schemes(opts):
    from mojo.helpers import safe_fetch as sf

    result, err = sf.safe_fetch(
        "ftp://self.test/", allow_hosts=["self.test"],
        resolver=_exploding_resolver, transport=_ExplodingTransport())
    assert result is None, "an allowed host must still obey the scheme allow-list"
    assert_eq(
        err, "Unsupported scheme 'ftp'. Only http and https are allowed.",
        "allow_hosts exempts the address check only, never the scheme check",
    )


@th.django_unit_test("safe_fetch: headers merge and every transport call is guarded")
def test_safe_fetch_headers_and_transport_kwargs(opts):
    from mojo.helpers import safe_fetch as sf

    public = _resolver({"host.test": ["93.184.216.34"]})

    plain = _Transport(routes={
        "https://host.test/": _response(200, {"Content-Type": "text/html"}, b"x"),
    })
    result, err = sf.safe_fetch("https://host.test/", resolver=public, transport=plain)
    assert err is None, f"the plain fetch must succeed: {err}"
    assert_eq(
        plain.calls[0][1]["headers"]["User-Agent"], sf.DEFAULT_USER_AGENT,
        "the default User-Agent must be sent when the caller supplies none",
    )

    custom = _Transport(routes={
        "https://host.test/": _response(302, {"Location": "/b"}),
        "https://host.test/b": _response(200, {"Content-Type": "text/html"}, b"x"),
    })
    result, err = sf.safe_fetch(
        "https://host.test/",
        headers={"User-Agent": "Mojo-Assistant/1.0", "Accept": "text/html"},
        resolver=public, transport=custom)
    assert err is None, f"the custom-header fetch must succeed: {err}"
    for url, kwargs in custom.calls:
        assert_eq(
            kwargs["headers"]["User-Agent"], "Mojo-Assistant/1.0",
            f"a caller User-Agent must override the default on {url}",
        )
        assert_eq(kwargs["headers"]["Accept"], "text/html", f"extra headers must pass through on {url}")
        assert_eq(
            kwargs["allow_redirects"], False,
            f"allow_redirects must stay False on {url} — the helper follows hops itself",
        )
        assert_eq(kwargs["stream"], True, f"stream must stay True on {url} so the byte cap can bite")


# ---------------------------------------------------------------------------
# Parser differential — the guard must judge the host the transport dials
# ---------------------------------------------------------------------------

@th.django_unit_test("safe_fetch: a backslash authority cannot smuggle a host past the guard")
def test_safe_fetch_refuses_parser_differential_authority(opts):
    from urllib.parse import urlparse
    from urllib3.util import parse_url
    from mojo.helpers import safe_fetch as sf

    hostile = r"http://127.0.0.1\@evil.test/"

    # The premise of the attack: the two parsers read a different host out of
    # this URL. If this ever stops being true the test still has to pass, but
    # the reason it matters would have changed.
    assert_eq(
        urlparse(hostile).hostname, "evil.test",
        "urlparse must be the parser that sees the decoy host — the differential is the bug",
    )
    assert_eq(
        parse_url(hostile).host, "127.0.0.1",
        "urllib3 (what requests dials) must be the parser that sees the real host",
    )

    transport = _Transport()
    result, err = sf.safe_fetch(
        hostile,
        resolver=_resolver({"evil.test": ["93.184.216.34"]}),
        transport=transport,
    )
    assert result is None, (
        "a URL whose authority holds a backslash must be refused — the guard would "
        "clear evil.test while requests connects to 127.0.0.1"
    )
    assert_eq(err, "Invalid URL — no hostname found", "wrong refusal string for the initial URL")
    assert_eq(transport.urls, [], "the transport must never be reached for a smuggled host")


@th.django_unit_test("safe_fetch: a backslash authority is refused on a redirect hop too")
def test_safe_fetch_refuses_parser_differential_hop(opts):
    from mojo.helpers import safe_fetch as sf

    transport = _Transport(routes={
        "https://host.test/a": _response(302, {"Location": r"//127.0.0.1:1234\@evil.test/"}),
    })
    result, err = sf.safe_fetch(
        "https://host.test/a",
        resolver=_resolver({"host.test": ["93.184.216.34"], "evil.test": ["93.184.216.35"]}),
        transport=transport,
    )
    assert result is None, "the smuggling trick must not work through a redirect either"
    assert_eq(err, "Redirect target is not a valid URL", "wrong refusal string for the hop")
    assert_eq(
        transport.urls, ["https://host.test/a"],
        "the smuggled hop must never be requested",
    )


@th.django_unit_test("safe_fetch: the checked host is the one urllib3 resolves to")
def test_safe_fetch_checks_the_transport_host(opts):
    from mojo.helpers import safe_fetch as sf

    # urlparse leaves an IDN hostname as unicode; urllib3 punycodes it, and
    # punycode is what gets dialed — so that is what must be judged.
    resolver_calls = []

    def resolver(hostname):
        resolver_calls.append(hostname)
        return ["93.184.216.34"]

    transport = _Transport(routes={
        "http://bücher.test/": _response(200, {"Content-Type": "text/html"}, b"ok"),
    })
    result, err = sf.safe_fetch(
        "http://bücher.test/", resolver=resolver, transport=transport)
    assert err is None, f"an international domain must still be fetchable: {err}"
    assert_eq(
        resolver_calls, ["xn--bcher-kva.test"],
        "the guard must judge the punycode host the transport dials, not the unicode spelling",
    )


# ---------------------------------------------------------------------------
# Fail-closed inputs
# ---------------------------------------------------------------------------

@th.django_unit_test("safe_fetch: is_blocked_ip fails closed on input it cannot parse")
def test_is_blocked_ip_unparsable_fails_closed(opts):
    from mojo.helpers import safe_fetch as sf

    for value in ["not-an-ip", "", "127.0.0.1.5", "example.com"]:
        assert_true(
            sf.is_blocked_ip(value),
            f"is_blocked_ip({value!r}) must return True, not raise — a guard that "
            "throws on bad input is a guard the caller skips",
        )


@th.django_unit_test("safe_fetch: a resolver that raises refuses the fetch, it does not escape")
def test_safe_fetch_resolver_exception_is_contained(opts):
    from mojo.helpers import safe_fetch as sf

    def boom(hostname):
        raise OSError("resolver exploded")

    transport = _Transport()
    result, err = sf.safe_fetch("https://host.test/", resolver=boom, transport=transport)
    assert result is None, "a resolver failure must not produce a result"
    assert_eq(err, "Could not connect to host.test", "a resolver failure must fail closed")
    assert_eq(transport.urls, [], "the transport must not be contacted when the guard cannot judge")

    hop = _Transport(routes={
        "https://ok.test/": _response(302, {"Location": "https://host.test/x"}),
    })

    def boom_on_second(hostname):
        if hostname == "ok.test":
            return ["93.184.216.34"]
        raise RuntimeError("resolver exploded")

    result, err = sf.safe_fetch(
        "https://ok.test/", resolver=boom_on_second, transport=hop)
    assert result is None, "a resolver failure on a hop must not produce a result"
    assert_eq(err, "Could not connect to host.test", "a hop resolver failure must fail closed")
    assert_eq(hop.urls, ["https://ok.test/"], "the unjudged hop must never be requested")


@th.django_unit_test("safe_fetch: schemes given as a bare string is not a substring test")
def test_safe_fetch_schemes_as_string(opts):
    from mojo.helpers import safe_fetch as sf

    result, err = sf.safe_fetch(
        "http://host.test/", schemes="https",
        resolver=_exploding_resolver, transport=_ExplodingTransport())
    assert result is None, (
        'schemes="https" must refuse http — a bare string makes `scheme in schemes` '
        "a substring test, and 'http' is a substring of 'https'"
    )
    assert_eq(err, "Unsupported scheme 'http'. Only https are allowed.", "wrong refusal string")

    transport = _Transport(routes={
        "https://host.test/": _response(302, {"Location": "http://public.test/"}),
    })
    result, err = sf.safe_fetch(
        "https://host.test/", schemes="https",
        resolver=_resolver({"host.test": ["93.184.216.34"], "public.test": ["93.184.216.35"]}),
        transport=transport)
    assert result is None, 'schemes="https" must refuse an http hop too'
    assert_eq(err, "Redirect to unsupported scheme 'http'", "wrong refusal string for the hop")


# ---------------------------------------------------------------------------
# Credential headers must not cross an origin
# ---------------------------------------------------------------------------

@th.django_unit_test("safe_fetch: credential headers are dropped on a cross-origin hop")
def test_safe_fetch_drops_credentials_cross_origin(opts):
    from mojo.helpers import safe_fetch as sf

    public = _resolver({"host.test": ["93.184.216.34"], "other.test": ["93.184.216.35"]})
    creds = {
        "Authorization": "Bearer secret",
        "Cookie": "session=secret",
        "Proxy-Authorization": "Basic secret",
        "Accept": "text/html",
    }

    crossing = _Transport(routes={
        "https://host.test/": _response(302, {"Location": "https://other.test/x"}),
        "https://other.test/x": _response(200, {"Content-Type": "text/html"}, b"ok"),
    })
    result, err = sf.safe_fetch(
        "https://host.test/", headers=creds, resolver=public, transport=crossing)
    assert err is None, f"a cross-origin redirect must still be followed: {err}"
    first, second = crossing.calls
    for name in ("Authorization", "Cookie", "Proxy-Authorization"):
        assert name in first[1]["headers"], f"{name} must be sent to the origin the caller chose"
        assert name not in second[1]["headers"], (
            f"{name} must not be replayed to other.test — a redirect the caller never "
            "chose would otherwise harvest the credential"
        )
    assert_eq(
        second[1]["headers"]["Accept"], "text/html",
        "non-credential headers must survive the hop",
    )

    staying = _Transport(routes={
        "https://host.test/a": _response(302, {"Location": "/b"}),
        "https://host.test/b": _response(200, {"Content-Type": "text/html"}, b"ok"),
    })
    result, err = sf.safe_fetch(
        "https://host.test/a", headers=creds, resolver=public, transport=staying)
    assert err is None, f"a same-origin redirect must still be followed: {err}"
    assert_eq(
        staying.calls[1][1]["headers"]["Authorization"], "Bearer secret",
        "a same-origin hop must keep the caller's credentials — nothing left the origin",
    )


# ---------------------------------------------------------------------------
# IPv4-mapped IPv6 unwrapping
# ---------------------------------------------------------------------------

@th.django_unit_test("safe_fetch: IPv4-mapped IPv6 is unwrapped before the network check")
def test_is_blocked_ip_unwraps_ipv4_mapped(opts):
    import ipaddress
    from mojo.helpers import safe_fetch as sf

    # 100.64.0.0/10 is an IPv4Network, so `mapped_v6 in network` is False
    # without the unwrap, and none of the is_private/is_reserved flags catch
    # it either — these cases fail the moment the unwrap is removed.
    for value in ["::ffff:100.64.0.1", "::ffff:240.0.0.1"]:
        naked = ipaddress.ip_address(value)
        assert_true(
            sf.is_blocked_ip(naked),
            f"{value} must be blocked — it is a blocked IPv4 address wearing an IPv6 spelling",
        )
    assert_true(
        not sf.is_blocked_ip(ipaddress.ip_address("::ffff:8.8.8.8")),
        "::ffff:8.8.8.8 maps to a public address and must stay fetchable — "
        "the unwrap must not blanket-block every mapped address",
    )
