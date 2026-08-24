"""
The GoDaddy transport helper (`mojo/helpers/dns/godaddy.py`) and the one place
its shape actually bites: challenge cleanup on a GoDaddy zone.

Two bugs are pinned here, both of which used to pass a naive test and fail in
production:

1. **Array bodies.** `get_domains`, `get_records` and `get_record` all answer
   with a JSON ARRAY. `objict` subclasses `dict`, so `objict([...])` goes through
   `dict(sequence)` and raises `ValueError` — but `objict([])` is just `{}`. An
   EMPTY zone therefore looked perfectly healthy while every real one blew up,
   which is why every read test below uses a NON-EMPTY body.

2. **Single-value writes.** `edit_record` built a one-element payload, so it
   could not express a multi-value record set — and a GoDaddy PUT replaces the
   WHOLE (type, name) set, so a wildcard and its apex sharing one
   `_acme-challenge` name could never both be published.

Everything runs in-process with `mojo.helpers.dns.godaddy.requests` patched. No
GoDaddy call is ever made.
"""

TESTIT_TIER = "bug"

from unittest.mock import patch

from testit import helpers as th


GD_HELPER = "mojo.helpers.dns.godaddy"

GD_DOMAIN = "gd.dnsmanhelper.com"
CRED_NAME = "dnsman godaddy helper test credential"


class FakeResponse(object):
    """A requests-like response with a REAL bytes body, not a mock attribute."""

    def __init__(self, payload, status_code=200, content=b'[{"x":1}]'):
        self._payload = payload
        self.status_code = status_code
        self.content = content

    def json(self):
        return self._payload

    def raise_for_status(self):
        if self.status_code >= 400:
            import requests
            raise requests.exceptions.HTTPError(f"{self.status_code} error")


def _entry(rtype, name, data, ttl=600):
    return {"type": rtype, "name": name, "data": data, "ttl": ttl}


def _manager():
    from mojo.helpers.dns.godaddy import DNSManager

    # Strict, so nothing below can be passing because an error was swallowed.
    return DNSManager("key", "secret", raise_on_error=True)


@th.django_unit_setup()
def setup_godaddy_helper(opts):
    """Long-lived database: delete what this setup creates BEFORE creating it."""
    from mojo.apps.dnsman.models import DnsCredential, Domain

    Domain.objects.filter(name=GD_DOMAIN).delete()
    DnsCredential.objects.filter(name=CRED_NAME).delete()

    credential = DnsCredential(name=CRED_NAME, provider="godaddy",
                               is_active=True, verified=True)
    credential.set_credentials("gd-helper-key", "gd-helper-secret")
    credential.save()
    opts.credential = credential

    opts.gd_domain = Domain.objects.create(
        name=GD_DOMAIN, provider="godaddy", status="active",
        credential=credential, verified=True)


# ---------------------------------------------------------------------------
# array bodies — the reads that used to raise for every non-empty zone
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_get_records_parses_a_non_empty_array(opts):
    """
    THE regression. A zone with records answers with a JSON array, and wrapping
    that in objict raises ValueError. An empty-body test passes either way and
    proves nothing, so this body deliberately carries records.
    """
    body = [
        _entry("A", "www", "1.2.3.4"),
        _entry("TXT", "_acme-challenge", "digest-apex", ttl=600),
        _entry("TXT", "_acme-challenge", "digest-wildcard", ttl=600),
    ]

    with patch(f"{GD_HELPER}.requests") as requests_mock:
        requests_mock.get.return_value = FakeResponse(body)
        records = _manager().get_records("example.com")

    assert isinstance(records, list), (
        f"Expected a JSON array to come back as a list, got {type(records).__name__}: "
        f"{records!r}")
    assert len(records) == 3, f"Expected all three entries, got {len(records)}: {records}"
    assert [entry.data for entry in records] == [
        "1.2.3.4", "digest-apex", "digest-wildcard"], (
        f"Expected each entry's value to survive parsing, got {records}")
    assert records[1].type == "TXT", (
        f"Expected attribute access on each parsed entry, got {records[1]}")
    assert records[0].ttl == 600, f"Expected the TTL to be parsed, got {records[0]}"


@th.django_unit_test()
def test_get_record_parses_a_non_empty_array(opts):
    """The per-(type, name) read is an array too: one entry PER VALUE."""
    body = [
        _entry("TXT", "_acme-challenge", "digest-apex"),
        _entry("TXT", "_acme-challenge", "digest-wildcard"),
    ]

    with patch(f"{GD_HELPER}.requests") as requests_mock:
        requests_mock.get.return_value = FakeResponse(body)
        records = _manager().get_record("example.com", "TXT", "_acme-challenge")

    assert isinstance(records, list), (
        f"Expected a list for the per-record read, got {type(records).__name__}")
    assert sorted(entry.data for entry in records) == [
        "digest-apex", "digest-wildcard"], (
        f"Expected both values of the record SET, got {records}")


@th.django_unit_test()
def test_get_domains_parses_a_non_empty_array(opts):
    body = [{"domain": "one.example", "status": "ACTIVE"},
            {"domain": "two.example", "status": "ACTIVE"}]

    with patch(f"{GD_HELPER}.requests") as requests_mock:
        requests_mock.get.return_value = FakeResponse(body)
        domains = _manager().get_domains()

    assert isinstance(domains, list), (
        f"Expected the account domain list to be a list, got {type(domains).__name__}")
    assert [entry.domain for entry in domains] == ["one.example", "two.example"], (
        f"Expected each domain to be parsed, got {domains}")


@th.django_unit_test()
def test_an_empty_zone_still_reads_as_empty(opts):
    """The case that HID the bug: it must keep working, and stay a list."""
    with patch(f"{GD_HELPER}.requests") as requests_mock:
        requests_mock.get.return_value = FakeResponse([], content=b"[]")
        records = _manager().get_records("example.com")

    assert records == [], f"Expected an empty zone to read as an empty list, got {records!r}"


@th.django_unit_test()
def test_object_bodies_are_still_a_single_objict(opts):
    """A mapping endpoint must not become a list — get_domain_info is an object."""
    with patch(f"{GD_HELPER}.requests") as requests_mock:
        requests_mock.get.return_value = FakeResponse(
            {"domain": "example.com", "status": "ACTIVE"}, content=b"{}")
        info = _manager().get_domain_info("example.com")

    assert info.status == "ACTIVE", f"Expected attribute access on the object body, got {info}"
    assert not isinstance(info, list), (
        f"Expected a JSON object to stay a single objict, got {type(info).__name__}")


# ---------------------------------------------------------------------------
# multi-value writes
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_edit_record_writes_every_value_in_one_put(opts):
    """
    A GoDaddy PUT replaces the whole (type, name) set, so a two-value set has to
    go out in ONE call carrying BOTH values. The old single-element payload made
    a wildcard + apex `_acme-challenge` impossible to publish.
    """
    values = ["digest-apex", "digest-wildcard"]

    with patch(f"{GD_HELPER}.requests") as requests_mock:
        requests_mock.put.return_value = FakeResponse(None, content=b"")
        _manager().edit_record("example.com", "TXT", "_acme-challenge", values, 600)

    assert requests_mock.put.call_count == 1, (
        f"Expected exactly one PUT for the whole set, got {requests_mock.put.call_count}")
    payload = requests_mock.put.call_args.kwargs["json"]
    assert payload == [{"data": "digest-apex", "ttl": 600},
                       {"data": "digest-wildcard", "ttl": 600}], (
        f"Expected one payload entry per value, got {payload}")


@th.django_unit_test()
def test_edit_record_still_accepts_a_scalar(opts):
    """
    `add_record` delegates here and every existing caller passes a single value,
    including `mojo.helpers.aws.ses_domain.apply_dns_records_godaddy`.
    """
    with patch(f"{GD_HELPER}.requests") as requests_mock:
        requests_mock.put.return_value = FakeResponse(None, content=b"")
        _manager().add_record("example.com", "TXT", "_amazonses", "ses-token", 3600)

    payload = requests_mock.put.call_args.kwargs["json"]
    assert payload == [{"data": "ses-token", "ttl": 3600}], (
        f"Expected a scalar value to stay a one-element payload, got {payload}")


@th.django_unit_test()
def test_put_records_sends_per_entry_ttls_untouched(opts):
    """
    The raw payload door, used when surviving records carry DIFFERENT TTLs and
    collapsing them to one would silently rewrite the ones we are keeping.
    """
    entries = [{"data": "a", "ttl": 600}, {"data": "b", "ttl": 3600}]

    with patch(f"{GD_HELPER}.requests") as requests_mock:
        requests_mock.put.return_value = FakeResponse(None, content=b"")
        _manager().put_records("example.com", "TXT", "keep", entries)

    payload = requests_mock.put.call_args.kwargs["json"]
    assert payload == entries, f"Expected the payload to pass through verbatim, got {payload}"


# ---------------------------------------------------------------------------
# 398 — a GoDaddy zone must not keep a stale _acme-challenge TXT
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_godaddy_challenge_cleanup_leaves_no_live_digest(opts):
    """
    End to end through `certs.cleanup_challenges`.

    On the old code this called `dns.delete_record`, GoDaddy refused to remove
    the last value of the set, and cleanup logged-and-swallowed — leaving the
    digest live in the customer's zone and accumulating another one per renewal.
    Cleanup must now leave nothing resolvable, and still not raise.
    """
    from mojo.apps.dnsman.services import certs
    from mojo.apps.dnsman.services.providers.godaddy_provider import RETIRED_VALUE

    record_name = f"_acme-challenge.{GD_DOMAIN}"
    digests = ["digest-apex", "digest-wildcard"]

    with patch(f"{GD_HELPER}.requests") as requests_mock:
        # The zone holds exactly what issuance planted, and nothing else.
        requests_mock.get.return_value = FakeResponse([
            _entry("TXT", "_acme-challenge", "digest-apex"),
            _entry("TXT", "_acme-challenge", "digest-wildcard"),
        ])
        requests_mock.put.return_value = FakeResponse(None, content=b"")
        # cleanup_challenges swallows, so "did not raise" is asserted by the
        # write below actually having happened.
        certs.cleanup_challenges(opts.gd_domain, [(record_name, digests)])

        put_calls = requests_mock.put.call_count
        payload = (requests_mock.put.call_args.kwargs["json"]
                   if put_calls else None)
        url = requests_mock.put.call_args.args[0] if put_calls else None

    assert put_calls == 1, (
        f"Expected cleanup to write the retired record set exactly once, got "
        f"{put_calls} PUT(s) — zero means the digests were left live in the zone")
    assert url.endswith(f"/domains/{GD_DOMAIN}/records/TXT/_acme-challenge"), (
        f"Expected the retirement to target the challenge record, got {url}")
    values = [entry["data"] for entry in payload]
    assert values == [RETIRED_VALUE], (
        f"Expected the set to be replaced by one inert placeholder, got {payload}")
    for digest in digests:
        assert digest not in str(payload), (
            f"the planted digest {digest} survived cleanup: {payload}")


@th.django_unit_test()
def test_godaddy_challenge_cleanup_survives_a_provider_error(opts):
    """
    The guarantee that must not be traded away: a cleanup problem NEVER fails an
    otherwise successful issuance. `cleanup_challenges` runs in a ``finally``.
    """
    import requests as requests_lib
    from mojo.apps.dnsman.services import certs

    record_name = f"_acme-challenge.{GD_DOMAIN}"

    with patch(f"{GD_HELPER}.requests") as requests_mock:
        requests_mock.get.side_effect = requests_lib.exceptions.ConnectionError(
            "GoDaddy is unreachable")
        # No try/except here on purpose: raising IS the failure this test guards.
        certs.cleanup_challenges(opts.gd_domain, [(record_name, ["digest-apex"])])
