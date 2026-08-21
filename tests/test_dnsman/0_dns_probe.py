"""
Authoritative DNS probe (mojo/helpers/dns/probe.py) + the additive
`raise_on_error` flag on the GoDaddy DNSManager.

Everything here is in-process with dnspython (and requests, for GoDaddy)
replaced through the probe's ``dns=``/``query=``/``sleep=`` and the GoDaddy
manager's ``http=`` injection seams (item #2558) — no DNS query and no HTTP
request is ever made, and nothing process-global is patched.
"""
import types

from testit import helpers as th


# ---------------------------------------------------------------------------
# fake dnspython
# ---------------------------------------------------------------------------

class FakeNS(object):
    """Stands in for an NS rdata (dnspython exposes `.target`)."""
    def __init__(self, target):
        self.target = target + "."

    def __str__(self):
        return self.target


class FakeCNAME(FakeNS):
    """CNAME and NS both expose their destination through ``.target``."""


class FakeAddress(object):
    """Stands in for an A/AAAA rdata (dnspython exposes `.address`)."""
    def __init__(self, address):
        self.address = address

    def __str__(self):
        return self.address


class FakeTXT(object):
    """
    Stands in for a TXT rdata.

    `strings` mirrors dnspython: a tuple of byte chunks, because a TXT record
    longer than 255 bytes arrives split. `text` is the alternate rendering
    (quoted chunks) used to prove the string fallback path normalizes too.
    """
    def __init__(self, strings=None, text=None):
        if strings is not None:
            self.strings = tuple(strings)
        self._text = text

    def __str__(self):
        if self._text is not None:
            return self._text
        return " ".join('"%s"' % s.decode() for s in self.strings)


def _fake_dns(answers, created=None):
    """
    Build a stand-in for the `dns.resolver` module.

    `answers` maps (name, rdtype) -> list of rdata, or an Exception to raise.
    A missing key raises NXDOMAIN. Every Resolver built is appended to
    `created` so a test can assert what the query was actually pinned to.
    """
    import dns.resolver as real_resolver

    class FakeResolver(object):
        def __init__(self, configure=True):
            self.configure = configure
            self.nameservers = []
            self.timeout = None
            self.lifetime = None
            self.queries = []
            if created is not None:
                created.append(self)

        def resolve(self, name, rdtype):
            key = (str(name).strip().lower().rstrip("."), rdtype)
            self.queries.append(key)
            result = answers.get(key)
            if result is None:
                raise real_resolver.NXDOMAIN()
            if isinstance(result, Exception):
                raise result
            return result

    return types.SimpleNamespace(
        Resolver=FakeResolver,
        NXDOMAIN=real_resolver.NXDOMAIN,
        NoAnswer=real_resolver.NoAnswer,
    )


# The zone under test: example.com is authoritative, foo.example.com is not a
# zone cut, and the challenge name itself does not exist yet.
def _zone_answers(txt_records=None):
    import dns.resolver as real_resolver

    answers = {
        ("foo.example.com", "NS"): real_resolver.NoAnswer(),
        ("example.com", "NS"): [FakeNS("ns1.example.net"), FakeNS("ns2.example.net")],
        ("ns1.example.net", "A"): [FakeAddress("192.0.2.10")],
        ("ns2.example.net", "A"): [FakeAddress("192.0.2.11")],
    }
    if txt_records is not None:
        answers[("_acme-challenge.foo.example.com", "TXT")] = txt_records
    return answers


def _cname_answers(target="opaque.acme-hub.example.net", chained=None):
    """Two authoritative cuts plus the tenant's direct CNAME."""
    import dns.resolver as real_resolver

    answers = {
        ("_acme-challenge.example.com", "NS"): real_resolver.NoAnswer(),
        ("example.com", "NS"): [FakeNS("ns1.example.net")],
        ("ns1.example.net", "A"): [FakeAddress("192.0.2.10")],
        ("_acme-challenge.example.com", "CNAME"): [FakeCNAME(target)],
        (target, "NS"): real_resolver.NoAnswer(),
        ("acme-hub.example.net", "NS"): [FakeNS("ns1.hub.example")],
        ("ns1.hub.example", "A"): [FakeAddress("192.0.2.20")],
    }
    if chained is not None:
        answers[(target, "CNAME")] = [FakeCNAME(chained)]
    return answers


# ---------------------------------------------------------------------------
# zone walk-up
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_zone_candidates_walk_up(opts):
    from mojo.helpers.dns import probe

    got = probe.zone_candidates("_acme-challenge.foo.example.com")
    assert got == [
        "_acme-challenge.foo.example.com",
        "foo.example.com",
        "example.com",
    ], f"Expected longest-first walk-up stopping above the TLD, got {got}"

    assert "com" not in got, "Expected the bare TLD to never be a zone candidate"

    got = probe.zone_candidates("EXAMPLE.COM.")
    assert got == ["example.com"], f"Expected normalization (lowercase, no trailing dot), got {got}"


@th.django_unit_test()
def test_find_zone_nameservers_finds_closest_authoritative_zone(opts):
    from mojo.helpers.dns import probe

    created = []
    fake = _fake_dns(_zone_answers(), created=created)
    found = probe.find_zone_nameservers(
        "_acme-challenge.foo.example.com", dns=fake)

    assert found.error is None, f"Expected no error resolving the zone, got {found.error}"
    assert found.zone == "example.com", \
        f"Expected the walk-up to land on example.com, got {found.zone}"
    assert found.nameservers == ["ns1.example.net", "ns2.example.net"], \
        f"Expected both NS hostnames without trailing dots, got {found.nameservers}"

    queried = created[0].queries
    assert queried[0] == ("_acme-challenge.foo.example.com", "NS"), \
        f"Expected the deepest name tried first, got {queried}"
    assert ("foo.example.com", "NS") in queried, \
        f"Expected the intermediate label to be tried before the apex, got {queried}"


@th.django_unit_test()
def test_find_zone_nameservers_reports_error_when_no_zone_found(opts):
    from mojo.helpers.dns import probe

    fake = _fake_dns({})  # every candidate NXDOMAINs
    found = probe.find_zone_nameservers(
        "_acme-challenge.nowhere.example.com", dns=fake)

    assert found.zone is None, f"Expected no zone when nothing answers NS, got {found.zone}"
    assert found.nameservers == [], f"Expected no nameservers, got {found.nameservers}"
    assert found.error, "Expected an error message when no authoritative zone is found"


@th.django_unit_test()
def test_verify_one_hop_cname_accepts_exact_direct_target(opts):
    from mojo.helpers.dns import probe

    answers = _cname_answers()
    fake = _fake_dns(answers)
    result = probe.verify_one_hop_cname(
        "_acme-challenge.example.com.",
        "OPAQUE.acme-hub.example.net.", dns=fake)

    assert result.ok is True, f"Expected the exact direct CNAME to pass, got {result}"


@th.django_unit_test()
def test_verify_one_hop_cname_rejects_wrong_or_chained_target(opts):
    import dns.resolver as real_resolver
    from mojo.helpers.dns import probe

    wrong = _cname_answers(target="wrong.example.net")
    wrong.update({
        ("wrong.example.net", "NS"): real_resolver.NoAnswer(),
        ("example.net", "NS"): [FakeNS("ns1.wrong.example")],
        ("ns1.wrong.example", "A"): [FakeAddress("192.0.2.30")],
    })
    result = probe.verify_one_hop_cname(
        "_acme-challenge.example.com", "opaque.acme-hub.example.net",
        dns=_fake_dns(wrong))
    assert result.ok is False, "Expected a CNAME to the wrong target to fail"

    chained = _cname_answers(chained="third.example.net")
    result = probe.verify_one_hop_cname(
        "_acme-challenge.example.com", "opaque.acme-hub.example.net",
        dns=_fake_dns(chained))
    assert result.ok is False, "Expected a second CNAME hop to fail"
    assert "one hop" in result.error, f"Expected a bounded one-hop refusal, got {result.error}"


# ---------------------------------------------------------------------------
# querying the authoritative servers directly
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_query_txt_pins_resolver_to_authoritative_addresses(opts):
    from mojo.helpers.dns import probe

    created = []
    fake = _fake_dns(_zone_answers([FakeTXT(strings=[b"token-abc"])]), created=created)
    result = probe.query_txt("_acme-challenge.foo.example.com", dns=fake)

    assert result.error is None, f"Expected no error, got {result.error}"
    assert result.txt_values == ["token-abc"], f"Expected the TXT value, got {result.txt_values}"
    assert result.zone == "example.com", f"Expected the resolved zone, got {result.zone}"
    assert result.nameservers == ["192.0.2.10", "192.0.2.11"], \
        f"Expected the NS hostnames resolved to addresses, got {result.nameservers}"

    txt_resolvers = [r for r in created if ("_acme-challenge.foo.example.com", "TXT") in r.queries]
    assert len(txt_resolvers) == 1, "Expected exactly one resolver to issue the TXT query"
    pinned = txt_resolvers[0]
    assert pinned.configure is False, \
        "Expected the TXT query to use an unconfigured resolver, not the system recursive one"
    assert pinned.nameservers == ["192.0.2.10", "192.0.2.11"], \
        f"Expected the TXT query pinned to the zone's authoritative servers, got {pinned.nameservers}"


@th.django_unit_test()
def test_query_txt_missing_record_is_not_an_error(opts):
    from mojo.helpers.dns import probe

    # No TXT entry at all -> the fake raises NXDOMAIN, which just means
    # "not planted yet", not a failure of the probe.
    fake = _fake_dns(_zone_answers())
    result = probe.query_txt("_acme-challenge.foo.example.com", dns=fake)

    assert result.error is None, f"Expected a missing TXT record to not be an error, got {result.error}"
    assert result.txt_values == [], f"Expected no values for a missing record, got {result.txt_values}"


# ---------------------------------------------------------------------------
# TXT normalization
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_txt_values_are_normalized_before_comparison(opts):
    from mojo.helpers.dns import probe

    multi = probe.txt_value(FakeTXT(strings=[b"first-half", b"second-half"]))
    assert multi == "first-halfsecond-half", \
        f"Expected multi-chunk TXT strings joined, got {multi!r}"

    quoted = probe.txt_value(FakeTXT(text='"token-abc"'))
    assert quoted == "token-abc", f"Expected surrounding quotes stripped, got {quoted!r}"

    quoted_multi = probe.txt_value(FakeTXT(text='"first-half" "second-half"'))
    assert quoted_multi == "first-halfsecond-half", \
        f"Expected quoted chunks joined and unquoted, got {quoted_multi!r}"

    plain = probe.txt_value(FakeTXT(text="v=spf1 -all"))
    assert plain == "v=spf1 -all", \
        f"Expected an unquoted value with spaces left intact, got {plain!r}"


@th.django_unit_test()
def test_wait_for_txt_matches_quoted_and_chunked_records(opts):
    from mojo.helpers.dns import probe

    # The record on the wire is quoted and split; the expected value the caller
    # passes is the plain digest. Normalization has to bridge the two.
    records = [FakeTXT(text='"digest-part-one" "digest-part-two"')]
    fake = _fake_dns(_zone_answers(records))
    ok, seen = probe.wait_for_txt(
        "_acme-challenge.foo.example.com",
        ['"digest-part-onedigest-part-two"'],
        timeout=0, dns=fake)

    assert ok is True, f"Expected quoted/chunked record to match the expected value, saw {seen}"
    assert seen == ["digest-part-onedigest-part-two"], \
        f"Expected the normalized value reported back, got {seen}"


# ---------------------------------------------------------------------------
# wait_for_txt matching semantics
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_wait_for_txt_subset_match_for_wildcard_and_base(opts):
    from mojo.helpers.dns import probe

    # A wildcard and its base domain share one _acme-challenge name and both
    # digests must be present at once. An unrelated TXT value on the same name
    # must not break the match -> subset, never equality.
    records = [
        FakeTXT(strings=[b"digest-base"]),
        FakeTXT(strings=[b"digest-wildcard"]),
        FakeTXT(strings=[b"some-unrelated-value"]),
    ]
    fake = _fake_dns(_zone_answers(records))
    ok, seen = probe.wait_for_txt(
        "_acme-challenge.foo.example.com",
        ["digest-base", "digest-wildcard"],
        timeout=0, dns=fake)

    assert ok is True, f"Expected both digests present to satisfy the probe, saw {seen}"
    assert len(seen) == 3, f"Expected all three seen values reported, got {seen}"


@th.django_unit_test()
def test_wait_for_txt_partial_match_is_not_ok(opts):
    from mojo.helpers.dns import probe

    records = [FakeTXT(strings=[b"digest-base"])]
    fake = _fake_dns(_zone_answers(records))
    ok, seen = probe.wait_for_txt(
        "_acme-challenge.foo.example.com",
        ["digest-base", "digest-wildcard"],
        timeout=0, dns=fake)

    assert ok is False, "Expected a partial match (one of two digests) to NOT satisfy the probe"
    assert seen == ["digest-base"], f"Expected the partial value reported back, got {seen}"


@th.django_unit_test()
def test_wait_for_txt_timeout_returns_last_seen(opts):
    from mojo.helpers.dns import probe

    records = [FakeTXT(strings=[b"stale-value"])]
    fake = _fake_dns(_zone_answers(records))
    ok, seen = probe.wait_for_txt(
        "_acme-challenge.foo.example.com",
        ["digest-that-never-lands"],
        timeout=0, dns=fake)

    assert ok is False, "Expected a timeout to report failure"
    assert seen == ["stale-value"], \
        f"Expected the last-seen values returned on timeout so the caller can log them, got {seen}"


@th.django_unit_test()
def test_wait_for_txt_polls_until_the_record_appears(opts):
    from objict import objict
    from mojo.helpers.dns import probe

    attempts = []

    def fake_query(fqdn, nameservers=None, timeout=None):
        attempts.append(fqdn)
        if len(attempts) < 3:
            return objict(txt_values=[], zone="example.com", nameservers=[], error=None)
        return objict(txt_values=["digest-base"], zone="example.com", nameservers=[], error=None)

    slept = []
    ok, seen = probe.wait_for_txt(
        "_acme-challenge.foo.example.com", ["digest-base"], timeout=60,
        interval=0, query=fake_query, sleep=lambda s: slept.append(s))

    assert ok is True, f"Expected the probe to succeed once the record appears, saw {seen}"
    assert len(attempts) == 3, f"Expected the probe to keep polling until visible, got {len(attempts)} attempts"
    assert len(slept) == 2, f"Expected a sleep between each retry, got {len(slept)}"


@th.django_unit_test()
def test_wait_for_txt_empty_expectation_fails_closed(opts):
    from mojo.helpers.dns import probe

    called = []

    def fake_query(fqdn, nameservers=None, timeout=None):
        called.append(fqdn)
        raise AssertionError("query_txt must not run when there is nothing to expect")

    ok, seen = probe.wait_for_txt(
        "_acme-challenge.foo.example.com", [], timeout=0, query=fake_query)

    assert ok is False, "Expected an empty expectation to fail closed, not to be trivially satisfied"
    assert seen == [], f"Expected no seen values, got {seen}"
    assert called == [], "Expected no DNS query at all for an empty expectation"


# ---------------------------------------------------------------------------
# GoDaddy raise_on_error
# ---------------------------------------------------------------------------

class FakeResponse(object):
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code
        self.content = b"{}"

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests
            raise requests.exceptions.HTTPError(f"{self.status_code} error")


class FakeHTTP(object):
    """Injected through DNSManager's ``http=`` seam (item #2558) — every verb
    answers with the one scripted response."""

    def __init__(self, response):
        self.response = response

    def get(self, *args, **kwargs):
        return self.response

    def put(self, *args, **kwargs):
        return self.response

    def patch(self, *args, **kwargs):
        return self.response


@th.django_unit_test()
def test_godaddy_default_still_swallows_http_errors(opts):
    """
    Regression guard for the sibling repo (maestro) that consumes this helper:
    the DEFAULT must keep returning the parsed body on an HTTP error.
    """
    from mojo.helpers.dns.godaddy import DNSManager

    bad = FakeResponse({"code": "UNAUTHORIZED", "message": "bad key"}, status_code=401)
    mgr = DNSManager("key", "secret", http=FakeHTTP(bad))
    assert mgr.raise_on_error is False, "Expected raise_on_error to default to False"

    result = mgr.get_domain_info("example.com")

    assert result.code == "UNAUTHORIZED", \
        f"Expected the parsed error body returned unchanged by default, got {result}"


@th.django_unit_test()
def test_godaddy_raise_on_error_raises(opts):
    import requests
    from mojo.helpers.dns.godaddy import DNSManager

    bad = FakeResponse({"code": "UNAUTHORIZED", "message": "bad key"}, status_code=401)
    mgr = DNSManager("key", "secret", raise_on_error=True, http=FakeHTTP(bad))
    assert mgr.raise_on_error is True, "Expected raise_on_error to be set from the constructor"

    raised = False
    try:
        mgr.get_domain_info("example.com")
    except requests.exceptions.HTTPError:
        raised = True

    assert raised, "Expected raise_on_error=True to surface an HTTPError instead of a parsed error body"


@th.django_unit_test()
def test_godaddy_raise_on_error_passes_good_responses_through(opts):
    from mojo.helpers.dns.godaddy import DNSManager

    good = FakeResponse({"status": "ACTIVE", "domain": "example.com"})
    mgr = DNSManager("key", "secret", raise_on_error=True, http=FakeHTTP(good))

    info = mgr.get_domain_info("example.com")
    active = mgr.is_domain_active("example.com")

    assert info.status == "ACTIVE", f"Expected the parsed body on a 200, got {info}"
    assert active is True, "Expected is_domain_active to stay True for an ACTIVE domain"


@th.django_unit_test()
def test_godaddy_write_methods_honor_raise_on_error(opts):
    import requests
    from mojo.helpers.dns.godaddy import DNSManager

    bad = FakeResponse({"code": "INVALID_BODY"}, status_code=422)
    mgr = DNSManager("key", "secret", raise_on_error=True, http=FakeHTTP(bad))

    raised = False
    try:
        mgr.add_record("example.com", "TXT", "_acme-challenge", "digest", 600)
    except requests.exceptions.HTTPError:
        raised = True
    assert raised, "Expected add_record/edit_record to raise when raise_on_error=True"

    # ...and stay silent by default.
    default_mgr = DNSManager("key", "secret", http=FakeHTTP(bad))
    result = default_mgr.add_record("example.com", "TXT", "_acme-challenge", "digest", 600)
    assert result.code == "INVALID_BODY", \
        f"Expected the default to keep returning the parsed body on a write error, got {result}"
