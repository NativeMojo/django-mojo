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

    oversized = _Transport(routes={
        "https://host.test/": _response(200, {"Content-Type": "text/plain"}, b"A" * (cap * 2)),
    })
    result, err = sf.safe_fetch(
        "https://host.test/", max_bytes=cap, resolver=public, transport=oversized)
    assert err is None, f"an oversized body must be truncated, not refused: {err}"
    assert_eq(len(result.content), cap, "content must be cut to exactly max_bytes")
    assert result.truncated is True, "truncated must be True when more bytes were available"
    assert_eq(len(result.text), cap, "text must decode only the capped bytes")

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


@th.django_unit_test("safe_fetch: allow_hosts exempts a host at the initial URL and every hop")
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

    staying = _Transport(routes={
        "http://self.test/": _response(302, {"Location": "/x"}),
        "http://self.test/x": _response(200, {"Content-Type": "text/html"}, b"hop"),
    })
    result, err = sf.safe_fetch(
        "http://self.test/", allow_hosts=["self.test"],
        resolver=_exploding_resolver, transport=staying)
    assert err is None, f"a redirect that stays on the allowed host must be followed: {err}"
    assert_eq(result.url, "http://self.test/x", "the allowed hop must be the final URL")


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
