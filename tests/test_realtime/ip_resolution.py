"""Regression tests for the WebSocket client-IP resolver — IP-spoofing fix (ITEM-010).

`WebSocketHandler.resolve_remote_ip()` / `get_remote_ip(scope)` must prefer the
proxy-authoritative `X-Real-IP` and never trust the client-controllable `X-Forwarded-For`,
RFC 7239 `Forwarded`, or `scope["client"]`. Each case fails on the old (spoofable) priority
order and passes once fixed.

The resolver methods only read `scope` / `self.websocket`, so we exercise them on a bare
handler built with `object.__new__` (the real `__init__` needs Redis).
"""
from testit import helpers as th


class _FakeTransport:
    def __init__(self, peername):
        self._peername = peername

    def get_extra_info(self, name, default=None):
        return self._peername if name == "peername" else default


class _FakeWS:
    """Minimal ASGI websocket stand-in — only the attrs the resolver reads."""

    def __init__(self, scope=None, request_headers=None, transport=None):
        self.scope = scope
        self.request_headers = request_headers
        self.transport = transport


def _bare_handler():
    # Bypass __init__ (which opens a Redis connection); the resolver methods don't need it.
    from mojo.apps.realtime.handler import WebSocketHandler
    return object.__new__(WebSocketHandler)


@th.django_unit_test("ws get_remote_ip: X-Real-IP wins over a forged X-Forwarded-For")
def test_xreal_ip_beats_forged_xff(opts):
    scope = {"client": None,
             "headers": [(b"x-real-ip", b"70.184.70.39"),
                         (b"x-forwarded-for", b"203.0.113.7")]}
    assert _bare_handler().get_remote_ip(scope) == "70.184.70.39", \
        "must trust X-Real-IP, not the client-forged X-Forwarded-For"


@th.django_unit_test("ws get_remote_ip: X-Real-IP wins over a forged RFC 7239 Forwarded")
def test_xreal_ip_beats_forged_forwarded(opts):
    scope = {"client": None,
             "headers": [(b"x-real-ip", b"70.184.70.39"),
                         (b"forwarded", b"for=203.0.113.99")]}
    assert _bare_handler().get_remote_ip(scope) == "70.184.70.39", \
        "must trust X-Real-IP, not the client-forged Forwarded header"


@th.django_unit_test("ws get_remote_ip: scope[client] is demoted below X-Real-IP")
def test_scope_client_demoted_below_xreal(opts):
    scope = {"client": ("203.0.113.7", 0),
             "headers": [(b"x-real-ip", b"70.184.70.39")]}
    assert _bare_handler().get_remote_ip(scope) == "70.184.70.39", \
        "X-Real-IP must outrank scope[client] (which can be XFF-derived/spoofable)"


@th.django_unit_test("ws get_remote_ip: X-Real-IP value is normalized")
def test_xreal_ip_normalized(opts):
    scope = {"client": None, "headers": [(b"x-real-ip", b"::ffff:1.2.3.4")]}
    assert _bare_handler().get_remote_ip(scope) == "1.2.3.4", \
        "IPv4-mapped IPv6 X-Real-IP should normalize to plain IPv4"


@th.django_unit_test("ws resolve_remote_ip: request_headers prefers X-Real-IP over X-Forwarded-For")
def test_resolve_request_headers_prefers_xreal(opts):
    h = _bare_handler()
    h.websocket = _FakeWS(scope=None,
                          request_headers={"x-forwarded-for": "203.0.113.7",
                                           "x-real-ip": "70.184.70.39"})
    assert h.resolve_remote_ip() == "70.184.70.39", \
        "the request_headers fallback must prefer X-Real-IP, not leftmost X-Forwarded-For"


@th.django_unit_test("ws resolve_remote_ip: falls back to transport peer when no X-Real-IP")
def test_resolve_transport_fallback(opts):
    h = _bare_handler()
    h.websocket = _FakeWS(scope=None, request_headers=None,
                          transport=_FakeTransport(("1.2.3.4", 5678)))
    assert h.resolve_remote_ip() == "1.2.3.4", \
        "with no X-Real-IP, the transport peer is the last-resort source"
