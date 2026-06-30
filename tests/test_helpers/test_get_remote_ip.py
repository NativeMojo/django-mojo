"""Regression tests for mojo.helpers.request.get_remote_ip — IP-spoofing fix (ITEM-009).

get_remote_ip must derive request.ip from the proxy-authoritative X-Real-IP (nginx's
asgi.inc sets it to $remote_addr and overwrites any client-supplied value), NOT from the
client-controlled X-Forwarded-For. These tests pin that: a forged X-Forwarded-For must
never win, and the resolved value must be normalized. Each case exercises the public
get_remote_ip so it fails on the old (leftmost-XFF) code and passes once fixed.
"""
from testit import helpers as th


class _Req:
    """Minimal request stand-in — get_remote_ip only reads request.META."""

    def __init__(self, **meta):
        self.META = dict(meta)


@th.django_unit_test("get_remote_ip: X-Real-IP wins over a forged X-Forwarded-For")
def test_xreal_ip_beats_forged_xff(opts):
    from mojo.helpers.request import get_remote_ip
    req = _Req(
        HTTP_X_FORWARDED_FOR="203.0.113.7",   # attacker-supplied, leftmost
        HTTP_X_REAL_IP="70.184.70.39",        # nginx-authoritative true client
        REMOTE_ADDR="10.0.0.1",
    )
    assert get_remote_ip(req) == "70.184.70.39", \
        "must trust nginx X-Real-IP, not the client-forged X-Forwarded-For"


@th.django_unit_test("get_remote_ip: X-Forwarded-For is never trusted, falls back to REMOTE_ADDR")
def test_forged_xff_ignored(opts):
    from mojo.helpers.request import get_remote_ip
    req = _Req(
        HTTP_X_FORWARDED_FOR="203.0.113.7",   # must be ignored entirely
        REMOTE_ADDR="198.51.100.42",
    )
    assert get_remote_ip(req) == "198.51.100.42", \
        "X-Forwarded-For must not set request.ip; fall back to REMOTE_ADDR"


@th.django_unit_test("get_remote_ip: falls back to REMOTE_ADDR when no proxy header")
def test_remote_addr_fallback(opts):
    from mojo.helpers.request import get_remote_ip
    req = _Req(REMOTE_ADDR="198.51.100.42")
    assert get_remote_ip(req) == "198.51.100.42", \
        "with no X-Real-IP, REMOTE_ADDR is the source"


@th.django_unit_test("get_remote_ip: normalizes the X-Real-IP value")
def test_xreal_ip_normalization(opts):
    from mojo.helpers.request import get_remote_ip
    cases = [
        ("  70.184.70.39  ", "70.184.70.39"),  # surrounding whitespace
        ("1.2.3.4:5678", "1.2.3.4"),           # IPv4:port -> strip port
        ("::ffff:1.2.3.4", "1.2.3.4"),         # IPv4-mapped IPv6 -> IPv4
        ("[2001:db8::1]", "2001:db8::1"),       # bracketed IPv6 -> unwrap
        ("2001:db8::1", "2001:db8::1"),         # bare IPv6 -> unchanged
    ]
    for raw, expected in cases:
        req = _Req(HTTP_X_REAL_IP=raw)          # no XFF/REMOTE_ADDR: old code returns None here
        assert get_remote_ip(req) == expected, \
            "X-Real-IP %r should normalize to %r" % (raw, expected)


@th.django_unit_test("get_remote_ip: a malformed X-Real-IP falls back, never returns garbage")
def test_malformed_xreal_falls_back(opts):
    from mojo.helpers.request import get_remote_ip
    req = _Req(HTTP_X_REAL_IP="not-an-ip", REMOTE_ADDR="198.51.100.42")
    assert get_remote_ip(req) == "198.51.100.42", \
        "an unparseable X-Real-IP must fall back to REMOTE_ADDR, not return garbage"
