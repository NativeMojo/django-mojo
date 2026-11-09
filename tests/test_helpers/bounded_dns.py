"""Bounded address lookups use test-owned resolvers and a monotonic clock.

No socket, resolver singleton, environment, or framework settings are patched.
The fake owns every lookup outcome, including failures that must never trigger
an alternate network lookup.
"""
from testit import helpers as th


class _Clock:
    def __init__(self, now=100):
        self.now = now

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


class _Address:
    def __init__(self, address):
        self.address = address


class _Resolver:
    def __init__(self, responses, *, clock=None, durations=None, nameservers=None):
        self.responses = responses
        self.clock = clock
        self.durations = durations or {}
        self.nameservers = ["192.0.2.53"] if nameservers is None else nameservers
        self.timeout = 999
        self.lifetime = 999
        self.calls = []

    def resolve(self, name, rtype, **kwargs):
        self.calls.append((name, rtype, kwargs, self.timeout, self.lifetime))
        if self.clock:
            self.clock.advance(self.durations.get(rtype, 0))
        response = self.responses[rtype]
        if isinstance(response, Exception):
            raise response
        return response


class _Factory:
    def __init__(self, resolver, *, clock=None, duration=0):
        self.resolver = resolver
        self.clock = clock
        self.duration = duration
        self.calls = 0

    def __call__(self):
        self.calls += 1
        if self.clock:
            self.clock.advance(self.duration)
        if isinstance(self.resolver, Exception):
            raise self.resolver
        return self.resolver


def _failure(probe, call, code):
    try:
        call()
    except probe.BoundedResolveError as error:
        assert error.code == code, f"Expected bounded DNS code {code}, got {error.code}"
        assert str(error) == code, "Bounded DNS errors must contain only their stable code"
        assert error.__suppress_context__, "Bounded DNS errors must suppress raw resolver context"
        return error
    raise AssertionError(f"Expected bounded DNS failure {code}, but resolution succeeded")


@th.django_unit_test()
def test_address_queries_share_remaining_deadline(opts):
    from mojo.helpers.dns import probe

    clock = _Clock()
    resolver = _Resolver({
        "A": [_Address("10.0.0.9"), _Address("10.0.0.2"), _Address("10.0.0.9")],
        "AAAA": [_Address("2001:db8::2"), _Address("2001:0db8:0:0:0:0:0:1")],
    }, clock=clock, durations={"A": 2, "AAAA": 1})
    factory = _Factory(resolver)
    addresses = probe.resolve_addresses(
        "Cache.Example.", 110, resolver_factory=factory, clock=clock)

    assert addresses == ["10.0.0.2", "10.0.0.9", "2001:db8::1", "2001:db8::2"], (
        "Private and public addresses must be canonical, deduplicated, and ordered deterministically")
    assert factory.calls == 1, "A and AAAA must share one fresh resolver"
    assert resolver.calls == [
        ("cache.example.", "A", {"lifetime": 10, "search": False}, 10, 10),
        ("cache.example.", "AAAA", {"lifetime": 8, "search": False}, 8, 8),
    ], "AAAA must receive only the budget left after A; search expansion must be disabled"


@th.django_unit_test()
def test_expired_budget_never_starts_resolver_work(opts):
    from mojo.helpers.dns import probe

    clock = _Clock()
    resolver = _Resolver({"A": [], "AAAA": []})
    factory = _Factory(resolver)
    _failure(probe, lambda: probe.resolve_addresses(
        "host.example", 100, resolver_factory=factory, clock=clock), "deadline_exceeded")
    assert factory.calls == 0, "An expired deadline must not even construct a resolver"
    assert resolver.calls == [], "An expired deadline must issue no A or AAAA lookup"

    slow_factory = _Factory(resolver, clock=clock, duration=2)
    _failure(probe, lambda: probe.resolve_addresses(
        "host.example", 101, resolver_factory=slow_factory, clock=clock), "deadline_exceeded")
    assert slow_factory.calls == 1, "Resolver construction may occur only while budget remains"
    assert resolver.calls == [], "Expiry during local resolver construction must prevent its first query"


@th.django_unit_test()
def test_expiry_between_queries_and_late_answers_fail_closed(opts):
    from mojo.helpers.dns import probe

    clock = _Clock()

    class Answers:
        def __iter__(self):
            yield _Address("10.0.0.1")
            clock.advance(2)

    resolver = _Resolver({"A": Answers(), "AAAA": [_Address("::1")]})
    _failure(probe, lambda: probe.resolve_addresses(
        "host.example", 101, resolver_factory=_Factory(resolver), clock=clock), "deadline_exceeded")
    assert [call[1] for call in resolver.calls] == ["A"], (
        "Expiry after consuming A must prevent the AAAA query and discard partial addresses")

    for response in ([_Address("10.0.0.1")], OSError("raw resolver host secret")):
        clock = _Clock()
        resolver = _Resolver({"A": response, "AAAA": []}, clock=clock, durations={"A": 2})
        _failure(probe, lambda: probe.resolve_addresses(
            "host.example", 101, resolver_factory=_Factory(resolver), clock=clock), "deadline_exceeded")
        assert [call[1] for call in resolver.calls] == ["A"], (
            "A late answer or error must be rejected without querying the second family")


@th.django_unit_test()
def test_missing_records_are_distinct_from_lookup_failures(opts):
    import dns.exception
    import dns.resolver
    from mojo.helpers.dns import probe

    resolver = _Resolver({"A": dns.resolver.NoAnswer(), "AAAA": dns.resolver.NXDOMAIN()})
    _failure(probe, lambda: probe.resolve_addresses(
        "absent.invalid", 110, resolver_factory=_Factory(resolver), clock=_Clock()), "dns_no_answer")
    assert len(resolver.calls) == 2, "An absent RRset must still allow checking the other address family"

    for response, code in (
            (OSError("raw private lookup detail"), "dns_lookup_failed"),
            (dns.resolver.NoNameservers(), "dns_lookup_failed"),
            (dns.exception.Timeout(), "dns_timeout")):
        resolver = _Resolver({"A": [_Address("10.0.0.1")], "AAAA": response})
        factory = _Factory(resolver)
        _failure(probe, lambda: probe.resolve_addresses(
            "absent.invalid", 110, resolver_factory=factory, clock=_Clock()), code)
        assert factory.calls == 1, "A failed resolver must never be replaced with a fallback resolver"
        assert [call[1] for call in resolver.calls] == ["A", "AAAA"], (
            "Lookup failure must discard earlier addresses without retrying through another lookup path")


@th.django_unit_test()
def test_empty_missing_malformed_and_wrong_family_answers(opts):
    from mojo.helpers.dns import probe

    cases = (
        ([], "dns_no_answer"),
        (None, "dns_answer_invalid"),
        ("10.0.0.1", "dns_answer_invalid"),
        ({"address": "10.0.0.1"}, "dns_answer_invalid"),
        ([_Address(None)], "dns_answer_invalid"),
        ([_Address("host.example")], "dns_answer_invalid"),
        ([_Address("fe80::1%eth0")], "dns_answer_invalid"),
        ([_Address("2001:db8::1")], "dns_answer_invalid"),
    )
    for answers, code in cases:
        resolver = _Resolver({"A": answers, "AAAA": []})
        _failure(probe, lambda: probe.resolve_addresses(
            "host.example", 110, resolver_factory=_Factory(resolver), clock=_Clock()), code)

    resolver = _Resolver({"A": [], "AAAA": [_Address("10.0.0.1")]})
    _failure(probe, lambda: probe.resolve_addresses(
        "host.example", 110, resolver_factory=_Factory(resolver), clock=_Clock()), "dns_answer_invalid")


@th.django_unit_test()
def test_answer_cap_bounds_iteration_across_both_families(opts):
    from mojo.helpers.dns import probe

    resolver = _Resolver({
        "A": [_Address(f"10.0.0.{index}") for index in range(1, 9)],
        "AAAA": [_Address(f"2001:db8::{index}") for index in range(1, 9)],
    })
    result = probe.resolve_addresses(
        "host.example", 110, resolver_factory=_Factory(resolver), clock=_Clock())
    assert len(result) == 16, "Exactly 16 address records across A and AAAA must remain usable"

    consumed = []

    class TooManyAnswers:
        def __iter__(self):
            for index in range(100):
                consumed.append(index)
                yield _Address("10.0.0.1")

    resolver = _Resolver({"A": TooManyAnswers(), "AAAA": []})
    _failure(probe, lambda: probe.resolve_addresses(
        "host.example", 110, resolver_factory=_Factory(resolver), clock=_Clock()), "dns_answer_overflow")
    assert len(consumed) == 17, "Duplicate answers must not bypass the iteration cap"
    assert len(resolver.calls) == 1, "An overflowing first family must prevent a second DNS query"

    resolver = _Resolver({
        "A": [_Address(f"10.0.0.{index}") for index in range(1, 17)],
        "AAAA": [_Address("::1")],
    })
    _failure(probe, lambda: probe.resolve_addresses(
        "host.example", 110, resolver_factory=_Factory(resolver), clock=_Clock()), "dns_answer_overflow")


@th.django_unit_test()
def test_numeric_literals_and_localhost_bypass_dns(opts):
    import ipaddress
    from mojo.helpers.dns import probe

    factory = _Factory(AssertionError("numeric/local names must not reach DNS"))
    for hostname, expected in (
            ("10.0.0.1", ["10.0.0.1"]),
            ("2001:0db8:0:0:0:0:0:1", ["2001:db8::1"]),
            ("::ffff:192.0.2.1", [str(ipaddress.IPv6Address("::ffff:c000:201"))]),
            ("LOCALHOST.", ["127.0.0.1", "::1"])):
        result = probe.resolve_addresses(hostname, 110, resolver_factory=factory, clock=_Clock())
        assert result == expected, f"Literal/local hostname {hostname} must return fixed numeric endpoints"
    assert factory.calls == 0, "Numeric literals and localhost must never construct or query DNS"
    _failure(probe, lambda: probe.resolve_addresses(
        "127.0.0.1", 100, resolver_factory=factory, clock=_Clock()), "deadline_exceeded")


@th.django_unit_test()
def test_hostname_and_deadline_validation_precedes_dns(opts):
    from mojo.helpers.dns import probe

    factory = _Factory(AssertionError("invalid input must not reach DNS"))
    for hostname in (None, 123, "", ".", " host.example", "host.example ",
                     "host..example", "-host.example", "host-.example", "a_b.example",
                     "[::1]", "fe80::1%eth0", "x" * 64 + ".example",
                     ".".join(["x" * 63] * 4), "x" * 255):
        _failure(probe, lambda: probe.resolve_addresses(
            hostname, 110, resolver_factory=factory, clock=_Clock()), "invalid_hostname")
    for deadline in (None, True, "110", float("nan"), float("inf")):
        _failure(probe, lambda: probe.resolve_addresses(
            "host.example", deadline, resolver_factory=factory, clock=_Clock()), "invalid_deadline")
    assert factory.calls == 0, "Malformed input must not enter resolver construction or network work"

    resolver = _Resolver({"A": [_Address("10.0.0.1")], "AAAA": []})
    probe.resolve_addresses("bücher.example", 110, resolver_factory=_Factory(resolver), clock=_Clock())
    assert resolver.calls[0][0] == "xn--bcher-kva.example.", (
        "IDNA hostnames must resolve through their bounded ASCII absolute name")


@th.django_unit_test()
def test_forbidden_nameservers_never_trigger_bootstrap_or_fallback(opts):
    import dns.nameserver
    from mojo.helpers.dns import probe

    for nameservers in (
            ["resolver.example"], ["https://resolver.example/dns-query"],
            ["https://192.0.2.53/dns-query"], ["fe80::53%eth0"], [object()],
            [dns.nameserver.DoHNameserver("https://resolver.example/dns-query")],
            [dns.nameserver.DoHNameserver("https://resolver.example/dns-query", bootstrap_address="192.0.2.53")],
            [dns.nameserver.DoTNameserver("192.0.2.53")],
            [dns.nameserver.DoQNameserver("192.0.2.53")],
            [dns.nameserver.Do53Nameserver("resolver.example")],
            ["192.0.2.53"] * 17, "192.0.2.53"):
        resolver = _Resolver({"A": [], "AAAA": []}, nameservers=nameservers)
        _failure(probe, lambda: probe.resolve_addresses(
            "host.invalid", 110, resolver_factory=_Factory(resolver), clock=_Clock()), "resolver_unsupported")
        assert resolver.calls == [], "Unsupported nameservers must be rejected before any DNS/TLS bootstrap"

    resolver = _Resolver({"A": [], "AAAA": []}, nameservers=[])
    _failure(probe, lambda: probe.resolve_addresses(
        "host.invalid", 110, resolver_factory=_Factory(resolver), clock=_Clock()), "resolver_unavailable")
    assert resolver.calls == [], "Missing nameservers must never fall back to the process system resolver"

    factory = _Factory(OSError("raw resolver configuration detail"))
    _failure(probe, lambda: probe.resolve_addresses(
        "host.invalid", 110, resolver_factory=factory, clock=_Clock()), "resolver_unavailable")
    assert factory.calls == 1, "Resolver construction failure must not trigger an alternate resolver"


@th.django_unit_test()
def test_numeric_dns_transports_and_unusable_resolver_outcomes(opts):
    import dns.nameserver
    from mojo.helpers.dns import probe

    for nameservers in (["2001:db8::53"], [dns.nameserver.Do53Nameserver("192.0.2.53")]):
        resolver = _Resolver({"A": [_Address("10.0.0.1")], "AAAA": []}, nameservers=nameservers)
        result = probe.resolve_addresses(
            "host.example", 110, resolver_factory=_Factory(resolver), clock=_Clock())
        assert result == ["10.0.0.1"], "Numeric IPv4/IPv6 ordinary DNS transports must remain supported"
    for resolver in (None, object()):
        _failure(probe, lambda: probe.resolve_addresses(
            "host.invalid", 110, resolver_factory=_Factory(resolver), clock=_Clock()), "resolver_unsupported")
