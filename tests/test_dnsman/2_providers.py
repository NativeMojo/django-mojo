"""
Provider dispatch + adapter parity (mojo/apps/dnsman/services/dns.py).

Everything runs in-process with the provider transports patched:
  - Route53 -> the functions in `mojo.helpers.aws.route53`
  - GoDaddy -> the `requests` module inside `mojo.helpers.dns.godaddy`. The REAL
    `DNSManager` runs, so its URL building and payload shaping are what these
    tests observe; only the socket is mocked.

No AWS call and no GoDaddy call is ever made.

The recurring theme is that ONE logical operation has to become the right
provider-specific call on each side: FQDN vs relative label ("@" for the apex),
Route53's quoted/255-chunked TXT vs GoDaddy's raw text, and a PUT that replaces
the whole record set on both.
"""

from unittest.mock import patch, MagicMock

from objict import objict
from testit import helpers as th


GD_HELPER = "mojo.helpers.dns.godaddy"
R53 = "mojo.helpers.aws.route53"
PROBE = "mojo.helpers.dns.probe"

R53_DOMAIN = "r53.dnsmanprov.com"
GD_DOMAIN = "gd.dnsmanprov.com"
PENDING_DOMAIN = "pending.dnsmanprov.com"
CRED_NAME = "dnsman provider test credential"

TEST_DOMAINS = [R53_DOMAIN, GD_DOMAIN, PENDING_DOMAIN]


def _response(payload, content=b"[]"):
    """A requests-like response carrying a decoded JSON body."""
    resp = MagicMock()
    resp.content = content
    resp.json.return_value = payload
    resp.status_code = 200
    return resp


def _entry(rtype, name, data, ttl=600):
    return {"type": rtype, "name": name, "data": data, "ttl": ttl}


def _record(rtype, name, record_values, ttl=300):
    """A Route53 helper record set. The value field is never called `values`."""
    return objict(type=rtype, name=name, record_values=list(record_values), ttl=ttl,
                  set_identifier=None, alias=None)


@th.django_unit_setup()
def setup_providers(opts):
    """
    Long-lived database: delete everything this setup is about to create FIRST.
    """
    from mojo.apps.dnsman.models import DnsCredential, Domain

    Domain.objects.filter(name__in=TEST_DOMAINS).delete()
    DnsCredential.objects.filter(name=CRED_NAME).delete()

    credential = DnsCredential(name=CRED_NAME, provider="godaddy",
                               is_active=True, verified=True)
    credential.set_credentials("gd-test-key", "gd-test-secret")
    credential.save()
    opts.credential = credential

    opts.r53_domain = Domain.objects.create(
        name=R53_DOMAIN, provider="route53", status="active",
        hosted_zone_id="ZTESTZONE", verified=True)
    opts.gd_domain = Domain.objects.create(
        name=GD_DOMAIN, provider="godaddy", status="active",
        credential=credential, verified=True)
    opts.pending_domain = Domain.objects.create(
        name=PENDING_DOMAIN, provider="route53", status="pending",
        hosted_zone_id="ZPENDING")


# ---------------------------------------------------------------------------
# adapter resolution
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_get_adapter_picks_the_provider(opts):
    from mojo.apps.dnsman.services import dns

    with patch(f"{GD_HELPER}.requests"):
        assert dns.get_adapter(opts.r53_domain).name == "route53", (
            "Expected a route53 Domain to resolve to the Route53 adapter")
        assert dns.get_adapter(opts.gd_domain).name == "godaddy", (
            "Expected a godaddy Domain to resolve to the GoDaddy adapter")


@th.django_unit_test()
def test_get_adapter_refuses_an_unknown_provider(opts):
    from mojo.apps.dnsman.models import Domain
    from mojo.apps.dnsman.services import dns
    from mojo import errors as me

    domain = Domain(name="unknown.dnsmanprov.com", provider="bind9", status="active")
    raised = None
    try:
        dns.get_adapter(domain)
    except me.ValueException as err:
        raised = err

    assert raised is not None, "Expected an unknown provider to be refused"
    assert "bind9" in str(raised), (
        f"Expected the refusal to name the unknown provider, got {raised}")


@th.django_unit_test()
def test_mojo_provider_is_certificate_only_not_general_dns(opts):
    from mojo.apps.dnsman.models import Domain
    from mojo.apps.dnsman.services import dns

    domain = Domain(
        name="delegated-only.example", provider="mojo", status="active",
        verified=True)
    assert domain.requires_credential is False, \
        "delegated ACME must not ask a tenant for provider credentials"
    try:
        dns.get_adapter(domain)
    except Exception as error:
        assert "unknown dns provider" in str(error).lower(), \
            f"mojo must stay outside general DNS CRUD, got {error}"
    else:
        assert False, "mojo must never resolve to a general DnsProvider adapter"


# ---------------------------------------------------------------------------
# parity — the same logical upsert on both providers
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_route53_upsert_sends_the_fqdn(opts):
    """Route53 speaks FQDN, and the zone id comes from the Domain row."""
    from mojo.apps.dnsman.services import dns

    with patch(f"{R53}.upsert_record", return_value="C123") as upsert:
        result = dns.upsert_record(
            opts.r53_domain, "txt", "_acme-challenge", ["digest-one"], ttl=60)

    assert result.change_id == "C123", (
        f"Expected the provider change id to be returned, got {result.change_id}")
    assert result.provider == "route53", (
        f"Expected the provider to be reported, got {result.provider}")
    assert upsert.call_count == 1, (
        f"Expected exactly one Route53 write, got {upsert.call_count}")

    args = upsert.call_args.args
    kwargs = upsert.call_args.kwargs
    assert args[0] == "ZTESTZONE", (
        f"Expected the Domain's hosted zone id, got {args[0]}")
    assert args[1] == "TXT", f"Expected the record type upper-cased, got {args[1]}"
    assert args[2] == f"_acme-challenge.{R53_DOMAIN}", (
        f"Expected Route53 to receive the FQDN, got {args[2]}")
    assert args[3] == ["digest-one"], (
        f"Expected the value list to be passed through, got {args[3]}")
    assert kwargs["zone_name"] == R53_DOMAIN, (
        f"Expected the zone name for the in-zone guard, got {kwargs.get('zone_name')}")
    assert kwargs["ttl"] == 60, f"Expected the requested TTL, got {kwargs.get('ttl')}"


@th.django_unit_test()
def test_route53_adapter_uses_injected_safe_provider_boundary(opts):
    import traceback
    from botocore.exceptions import ClientError
    from mojo.apps.dnsman.services.providers.route53_provider import Route53Provider
    from mojo.helpers.aws.provider_call import ProviderCallError, ProviderCaller

    api = MagicMock()
    api.upsert_record.return_value = "C-INJECTED"
    boundary = MagicMock()
    boundary.call.side_effect = lambda operation, callback, iam_action="", mutation=False: callback()
    adapter = Route53Provider(opts.r53_domain, provider=boundary, route53_api=api)
    assert adapter.upsert_record("TXT", f"_acme-challenge.{R53_DOMAIN}", ["digest"]) == "C-INJECTED"
    call = boundary.call.call_args
    assert call.args[0] == "route53.upsert_record" \
        and call.args[2] == "route53:ChangeResourceRecordSets" \
        and call.kwargs["mutation"] is True, \
        f"The adapter write must cross the injectable boundary with exact IAM evidence: {call}"

    secret = "https://signed.example/?X-Amz-Credential=SECRET-SENTINEL"
    api.upsert_record.side_effect = ClientError({
        "Error": {"Code": "AccessDenied", "Message": secret},
        "ResponseMetadata": {"HTTPStatusCode": 403, "RequestId": "route53-request-1"},
    }, "ChangeResourceRecordSets")
    safe_logger = MagicMock()
    adapter = Route53Provider(
        opts.r53_domain, provider=ProviderCaller(safe_logger), route53_api=api)
    try:
        adapter.upsert_record("TXT", f"_acme-challenge.{R53_DOMAIN}", ["digest"])
    except ProviderCallError as exc:
        assert exc.detail()["iam_action"] == "route53:ChangeResourceRecordSets"
        rendered = "".join(traceback.format_exception(exc))
        assert secret not in rendered and secret not in str(safe_logger.method_calls), \
            "Raw Route53 provider text must not reach tracebacks or logs"
    else:
        raise AssertionError("A Route53 denial must leave the adapter as a safe typed error")


@th.django_unit_test()
def test_godaddy_upsert_sends_the_relative_label(opts):
    """
    The SAME logical upsert on GoDaddy: relative label, raw TXT, and a PUT
    carrying the full desired value list (GoDaddy's PUT replaces the whole set,
    so sending only the new value would silently drop the others).
    """
    from mojo.apps.dnsman.services import dns

    with patch(f"{GD_HELPER}.requests") as requests_mock:
        result = dns.upsert_record(
            opts.gd_domain, "txt", "_acme-challenge", ["digest-one"], ttl=60)

    assert result.provider == "godaddy", (
        f"Expected the provider to be reported, got {result.provider}")
    assert result.change_id is None, (
        f"GoDaddy issues no change id, so None is expected, got {result.change_id}")
    assert requests_mock.put.call_count == 1, (
        f"Expected exactly one GoDaddy PUT, got {requests_mock.put.call_count}")

    url = requests_mock.put.call_args.args[0]
    assert url.endswith(f"/domains/{GD_DOMAIN}/records/TXT/_acme-challenge"), (
        f"Expected the relative label in the GoDaddy record URL, got {url}")

    payload = requests_mock.put.call_args.kwargs["json"]
    assert payload == [{"data": "digest-one", "ttl": 600}], (
        f"Expected a single raw value at GoDaddy's minimum TTL, got {payload}")


@th.django_unit_test()
def test_apex_becomes_at_sign_on_godaddy_and_fqdn_on_route53(opts):
    from mojo.apps.dnsman.services import dns

    with patch(f"{R53}.upsert_record", return_value="C1") as upsert:
        dns.upsert_record(opts.r53_domain, "A", "@", ["1.2.3.4"])
    assert upsert.call_args.args[2] == R53_DOMAIN, (
        f"Expected the apex to reach Route53 as the bare zone name, "
        f"got {upsert.call_args.args[2]}")

    with patch(f"{GD_HELPER}.requests") as requests_mock:
        dns.upsert_record(opts.gd_domain, "A", GD_DOMAIN, ["1.2.3.4"])
    url = requests_mock.put.call_args.args[0]
    assert url.endswith("/records/A/@"), (
        f"Expected the apex to become '@' for GoDaddy, got {url}")


@th.django_unit_test()
def test_multi_value_txt_is_one_record_set_with_two_values(opts):
    """
    A wildcard and its apex share ONE `_acme-challenge` name and need BOTH
    digests present at the same time. Each provider must therefore receive one
    write carrying two values — not two writes.
    """
    from mojo.apps.dnsman.services import dns

    values = ["digest-apex", "digest-wildcard"]

    with patch(f"{R53}.upsert_record", return_value="C1") as upsert:
        dns.upsert_record(opts.r53_domain, "TXT", "_acme-challenge", values)
    assert upsert.call_count == 1, (
        f"Expected ONE Route53 record-set write, got {upsert.call_count}")
    assert upsert.call_args.args[3] == values, (
        f"Expected both digests in a single Route53 write, got {upsert.call_args.args[3]}")

    with patch(f"{GD_HELPER}.requests") as requests_mock:
        dns.upsert_record(opts.gd_domain, "TXT", "_acme-challenge", values)
    assert requests_mock.put.call_count == 1, (
        f"Expected ONE GoDaddy PUT, got {requests_mock.put.call_count}")
    payload = requests_mock.put.call_args.kwargs["json"]
    assert [entry["data"] for entry in payload] == values, (
        f"Expected the full replacement list in one PUT, got {payload}")


# ---------------------------------------------------------------------------
# TXT quoting — both directions
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_route53_txt_values_are_unquoted_on_read(opts):
    """
    Route53 stores TXT as quoted, 255-chunked character-strings. Callers get raw
    text back — a leaked quote or an unjoined chunk silently breaks SES
    verification and ACME validation.
    """
    from mojo.apps.dnsman.services import dns

    stored = [_record("TXT", f"_acme-challenge.{R53_DOMAIN}", ['"abc" "def"']),
              _record("A", f"www.{R53_DOMAIN}", ["1.2.3.4"], ttl=60)]

    with patch(f"{R53}.list_records", return_value=stored):
        records = dns.list_records(opts.r53_domain)

    assert len(records) == 2, f"Expected both record sets, got {len(records)}"
    assert records[0].record_values == ["abcdef"], (
        f"Expected the quoted chunks to be joined into raw text, got {records[0].record_values}")
    assert "values" not in records[0], (
        "objict subclasses dict, so a 'values' key is shadowed by dict.values — "
        "the field must stay named 'record_values'")
    assert records[1].record_values == ["1.2.3.4"], (
        f"Expected non-TXT values to pass through untouched, got {records[1].record_values}")


@th.django_unit_test()
def test_godaddy_txt_values_are_written_and_read_raw(opts):
    """GoDaddy stores TXT raw — a Route53-shaped value must be unquoted first."""
    from mojo.apps.dnsman.services import dns

    with patch(f"{GD_HELPER}.requests") as requests_mock:
        dns.upsert_record(opts.gd_domain, "TXT", "_acme-challenge", ['"abc" "def"'])
    payload = requests_mock.put.call_args.kwargs["json"]
    assert payload[0]["data"] == "abcdef", (
        f"Expected the quoted/chunked value to be unquoted for GoDaddy, got {payload}")

    with patch(f"{GD_HELPER}.requests") as requests_mock:
        requests_mock.get.return_value = _response([
            _entry("TXT", "_acme-challenge", "abcdef"),
        ])
        records = dns.list_records(opts.gd_domain)

    assert records[0].record_values == ["abcdef"], (
        f"Expected GoDaddy's raw TXT text to be returned as-is, got {records[0].record_values}")


@th.django_unit_test()
def test_godaddy_list_records_groups_entries_into_record_sets(opts):
    """
    GoDaddy returns one entry PER VALUE; Route53 returns record SETS. Both
    adapters must hand callers the same shape, with FQDN names.
    """
    from mojo.apps.dnsman.services import dns

    with patch(f"{GD_HELPER}.requests") as requests_mock:
        requests_mock.get.return_value = _response([
            _entry("TXT", "_acme-challenge", "digest-apex"),
            _entry("TXT", "_acme-challenge", "digest-wildcard"),
            _entry("A", "@", "1.2.3.4", ttl=3600),
        ])
        records = dns.list_records(opts.gd_domain)

    assert len(records) == 2, (
        f"Expected the two TXT entries to collapse into one record set, got {len(records)}")
    assert records[0].name == f"_acme-challenge.{GD_DOMAIN}", (
        f"Expected the relative label to be expanded to an FQDN, got {records[0].name}")
    assert records[0].record_values == ["digest-apex", "digest-wildcard"], (
        f"Expected both values in one set, got {records[0].record_values}")
    assert records[1].name == GD_DOMAIN, (
        f"Expected '@' to be expanded to the zone apex, got {records[1].name}")


# ---------------------------------------------------------------------------
# deletes
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_godaddy_delete_puts_back_the_remaining_values(opts):
    """GoDaddy has no delete API — removing a value means PUTting what is left."""
    from mojo.apps.dnsman.services import dns

    with patch(f"{GD_HELPER}.requests") as requests_mock:
        requests_mock.get.return_value = _response([
            _entry("TXT", "_acme-challenge", "digest-apex"),
            _entry("TXT", "_acme-challenge", "digest-wildcard"),
        ])
        dns.delete_record(opts.gd_domain, "TXT", "_acme-challenge", ["digest-apex"])

    assert requests_mock.put.call_count == 1, (
        f"Expected the remaining values to be PUT back, got {requests_mock.put.call_count} PUT(s)")
    payload = requests_mock.put.call_args.kwargs["json"]
    assert [entry["data"] for entry in payload] == ["digest-wildcard"], (
        f"Expected only the surviving value to remain, got {payload}")


@th.django_unit_test()
def test_godaddy_delete_of_the_last_record_raises(opts):
    """
    GoDaddy rejects an empty replace, so a true delete of the last record of a
    type is impossible. That must be an explicit error, never a silent no-op.
    """
    from mojo.apps.dnsman.services import dns
    from mojo import errors as me

    raised = None
    with patch(f"{GD_HELPER}.requests") as requests_mock:
        requests_mock.get.return_value = _response([
            _entry("TXT", "_acme-challenge", "digest-apex"),
        ])
        try:
            dns.delete_record(opts.gd_domain, "TXT", "_acme-challenge", ["digest-apex"])
        except me.ValueException as err:
            raised = err
        put_calls = requests_mock.put.call_count

    assert raised is not None, (
        "Expected deleting the last record of a type to raise on GoDaddy")
    assert "last record" in str(raised).lower(), (
        f"Expected the error to explain the provider limitation, got {raised}")
    assert put_calls == 0, (
        "Expected no write to be attempted when the delete cannot be expressed")


@th.django_unit_test()
def test_godaddy_delete_of_a_missing_record_raises(opts):
    from mojo.apps.dnsman.services import dns
    from mojo import errors as me

    raised = None
    with patch(f"{GD_HELPER}.requests") as requests_mock:
        requests_mock.get.return_value = _response([])
        try:
            dns.delete_record(opts.gd_domain, "TXT", "_gone")
        except me.ValueException as err:
            raised = err
        put_calls = requests_mock.put.call_count

    assert raised is not None, "Expected deleting a non-existent record to raise, not no-op"
    assert put_calls == 0, "Expected no write for a record that does not exist"


@th.django_unit_test()
def test_route53_delete_passes_the_values_through(opts):
    from mojo.apps.dnsman.services import dns

    with patch(f"{R53}.delete_record", return_value="C9") as delete:
        result = dns.delete_record(
            opts.r53_domain, "TXT", "_acme-challenge", ["digest-one"])

    assert result.change_id == "C9", f"Expected the change id, got {result.change_id}"
    args = delete.call_args.args
    assert args[2] == f"_acme-challenge.{R53_DOMAIN}", (
        f"Expected the FQDN on the Route53 delete, got {args[2]}")
    assert delete.call_args.kwargs["values"] == ["digest-one"], (
        f"Expected the values to be passed through, "
        f"got {delete.call_args.kwargs.get('values')}")


# ---------------------------------------------------------------------------
# clears — the same logical retirement on both providers
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_route53_clear_is_a_real_delete(opts):
    """
    Route53 CAN delete, so a clear is exactly a delete — no placeholder, no
    difference from the precise removal callers already got.
    """
    from mojo.apps.dnsman.services import dns

    with patch(f"{R53}.delete_record", return_value="C7") as delete:
        result = dns.clear_record(
            opts.r53_domain, "TXT", "_acme-challenge", ["digest-one"])

    assert result.change_id == "C7", f"Expected the change id, got {result.change_id}"
    assert delete.call_count == 1, (
        f"Expected a clear on Route53 to be one real delete, got {delete.call_count}")
    assert delete.call_args.kwargs["values"] == ["digest-one"], (
        f"Expected only the named values to be removed, "
        f"got {delete.call_args.kwargs.get('values')}")


@th.django_unit_test()
def test_godaddy_clear_of_the_last_record_writes_a_placeholder(opts):
    """
    GoDaddy cannot delete a last record — `delete_record` says so. A CLEAR must
    still leave nothing live, so the set is overwritten with one inert value.
    Otherwise an `_acme-challenge` TXT survives issuance and gains a stale
    digest on every renewal.
    """
    from mojo.apps.dnsman.services import dns
    from mojo.apps.dnsman.services.providers.godaddy_provider import RETIRED_VALUE

    with patch(f"{GD_HELPER}.requests") as requests_mock:
        requests_mock.get.return_value = _response([
            _entry("TXT", "_acme-challenge", "digest-apex"),
        ])
        result = dns.clear_record(
            opts.gd_domain, "TXT", "_acme-challenge", ["digest-apex"])

    assert result.provider == "godaddy", (
        f"Expected the provider to be reported, got {result.provider}")
    assert requests_mock.put.call_count == 1, (
        f"Expected the record to be overwritten, got {requests_mock.put.call_count} PUT(s)")
    payload = requests_mock.put.call_args.kwargs["json"]
    assert [entry["data"] for entry in payload] == [RETIRED_VALUE], (
        f"Expected exactly one inert placeholder value, got {payload}")
    assert "digest-apex" not in str(payload), (
        f"The planted digest must not survive the clear, got {payload}")


@th.django_unit_test()
def test_godaddy_clear_keeps_the_values_it_was_not_asked_about(opts):
    """A clear is not a purge: values nobody named stay live."""
    from mojo.apps.dnsman.services import dns

    with patch(f"{GD_HELPER}.requests") as requests_mock:
        requests_mock.get.return_value = _response([
            _entry("TXT", "_acme-challenge", "digest-apex"),
            _entry("TXT", "_acme-challenge", "someone-elses-value"),
        ])
        dns.clear_record(opts.gd_domain, "TXT", "_acme-challenge", ["digest-apex"])

    payload = requests_mock.put.call_args.kwargs["json"]
    assert [entry["data"] for entry in payload] == ["someone-elses-value"], (
        f"Expected the unnamed value to survive and no placeholder, got {payload}")


@th.django_unit_test()
def test_godaddy_clear_of_a_missing_record_does_nothing(opts):
    """Already gone is success — a clear must never CREATE a placeholder record."""
    from mojo.apps.dnsman.services import dns

    with patch(f"{GD_HELPER}.requests") as requests_mock:
        requests_mock.get.return_value = _response([])
        result = dns.clear_record(opts.gd_domain, "TXT", "_gone", ["digest-apex"])

    assert result.change_id is None, (
        f"GoDaddy issues no change id, so None is expected, got {result.change_id}")
    assert requests_mock.put.call_count == 0, (
        "Expected no write for a record that does not exist — a clear must not "
        "plant a placeholder where there was nothing")


@th.django_unit_test()
def test_godaddy_clear_refuses_to_placeholder_a_non_text_record(opts):
    """
    A placeholder is only safe for character-string types. Inventing an address
    for an A record would point real traffic somewhere.
    """
    from mojo.apps.dnsman.services import dns
    from mojo import errors as me

    raised = None
    with patch(f"{GD_HELPER}.requests") as requests_mock:
        requests_mock.get.return_value = _response([
            _entry("A", "www", "1.2.3.4"),
        ])
        try:
            dns.clear_record(opts.gd_domain, "A", "www", ["1.2.3.4"])
        except me.ValueException as err:
            raised = err
        put_calls = requests_mock.put.call_count

    assert raised is not None, (
        "Expected clearing the last A record on GoDaddy to raise rather than "
        "invent an address")
    assert put_calls == 0, "Expected no placeholder A record to be written"


# ---------------------------------------------------------------------------
# validation — refused before any provider call
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_out_of_zone_names_are_refused(opts):
    from mojo.apps.dnsman.services import dns
    from mojo import errors as me

    raised = None
    with patch(f"{R53}.upsert_record") as upsert:
        try:
            dns.upsert_record(opts.r53_domain, "A", "www.attacker.com", ["1.2.3.4"])
        except me.ValueException as err:
            raised = err
        calls = upsert.call_count

    assert raised is not None, "Expected an out-of-zone record name to be refused"
    assert "not inside" in str(raised), (
        f"Expected the refusal to name the zone boundary, got {raised}")
    assert calls == 0, "Expected a refused name to never reach the provider"

    raised = None
    with patch(f"{GD_HELPER}.DNSManager") as manager_cls, \
            patch(f"{GD_HELPER}.requests") as requests_mock:
        try:
            dns.upsert_record(opts.gd_domain, "A", "www.attacker.com", ["1.2.3.4"])
        except me.ValueException as err:
            raised = err
        gd_calls = (manager_cls.call_count + requests_mock.get.call_count
                    + requests_mock.put.call_count)

    assert raised is not None, (
        "Expected an out-of-zone record name to be refused on GoDaddy too")
    assert gd_calls == 0, "Expected a refused name to never reach GoDaddy"


@th.django_unit_test()
def test_apex_ns_and_soa_are_refused(opts):
    from mojo.apps.dnsman.services import dns
    from mojo import errors as me

    for rtype in ("NS", "SOA"):
        raised = None
        with patch(f"{R53}.upsert_record") as upsert:
            try:
                dns.upsert_record(opts.r53_domain, rtype, "@", ["ns-1.awsdns.com"])
            except me.ValueException as err:
                raised = err
            calls = upsert.call_count
        assert raised is not None, f"Expected an apex {rtype} write to be refused"
        assert "apex" in str(raised).lower(), (
            f"Expected the {rtype} refusal to explain it is the apex set, got {raised}")
        assert calls == 0, f"Expected a refused apex {rtype} write to never reach the provider"

    # A delegated sub-zone NS record is still legitimate.
    with patch(f"{R53}.upsert_record", return_value="C1") as upsert:
        dns.upsert_record(opts.r53_domain, "NS", "sub", ["ns-1.awsdns.com"])
    assert upsert.call_count == 1, (
        "Expected a non-apex NS record to be allowed")


@th.django_unit_test()
def test_disallowed_record_types_are_refused(opts):
    from mojo.apps.dnsman.services import dns
    from mojo import errors as me

    raised = None
    with patch(f"{R53}.upsert_record") as upsert:
        try:
            dns.upsert_record(opts.r53_domain, "DNSKEY", "www", ["whatever"])
        except me.ValueException as err:
            raised = err
        calls = upsert.call_count

    assert raised is not None, "Expected a record type outside the allow list to be refused"
    assert "DNSKEY" in str(raised), (
        f"Expected the refusal to name the rejected type, got {raised}")
    assert calls == 0, "Expected a disallowed type to never reach the provider"


@th.django_unit_test()
def test_a_domain_that_is_not_active_refuses_dns_operations(opts):
    from mojo.apps.dnsman.services import dns
    from mojo import errors as me

    for call in ("list", "upsert"):
        raised = None
        with patch(f"{R53}.upsert_record") as upsert, \
                patch(f"{R53}.list_records") as list_records:
            try:
                if call == "list":
                    dns.list_records(opts.pending_domain)
                else:
                    dns.upsert_record(opts.pending_domain, "A", "www", ["1.2.3.4"])
            except me.ValueException as err:
                raised = err
            calls = upsert.call_count + list_records.call_count

        assert raised is not None, (
            f"Expected a non-active domain to refuse the {call} operation")
        assert "not active" in str(raised), (
            f"Expected the refusal to name the status problem, got {raised}")
        assert calls == 0, (
            f"Expected the {call} operation on a non-active domain to never reach the provider")


@th.django_unit_test()
def test_upsert_requires_at_least_one_value(opts):
    from mojo.apps.dnsman.services import dns
    from mojo import errors as me

    raised = None
    with patch(f"{R53}.upsert_record") as upsert:
        try:
            dns.upsert_record(opts.r53_domain, "A", "www", [])
        except me.ValueException as err:
            raised = err
        calls = upsert.call_count

    assert raised is not None, "Expected an empty value list to be refused"
    assert calls == 0, "Expected an empty value list to never reach the provider"


# ---------------------------------------------------------------------------
# propagation
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_route53_propagation_gates_on_insync_then_probes(opts):
    from mojo.apps.dnsman.services import dns

    with patch(f"{R53}.get_change", return_value=objict(insync=True, status="INSYNC")) as change, \
            patch(f"{PROBE}.wait_for_txt", return_value=(True, ["digest-one"])) as wait:
        ok, seen = dns.wait_for_propagation(
            opts.r53_domain, "TXT", "_acme-challenge", ["digest-one"],
            timeout=30, change_id="C123")

    assert ok is True, "Expected a propagated record to report ok"
    assert seen == ["digest-one"], f"Expected the seen values to be returned, got {seen}"
    assert change.call_count == 1, (
        f"Expected Route53's ChangeInfo to be the first gate, got {change.call_count} call(s)")
    assert wait.call_count == 1, (
        "Expected the authoritative probe to run after the change went INSYNC")
    assert wait.call_args.args[0] == f"_acme-challenge.{R53_DOMAIN}", (
        f"Expected the probe to be given the FQDN, got {wait.call_args.args[0]}")
    assert wait.call_args.args[1] == ["digest-one"], (
        f"Expected the probe to be given the raw expected values, got {wait.call_args.args[1]}")


@th.django_unit_test()
def test_route53_propagation_fails_when_the_change_never_syncs(opts):
    from mojo.apps.dnsman.services import dns

    with patch(f"{R53}.get_change", return_value=objict(insync=False, status="PENDING")), \
            patch(f"{PROBE}.wait_for_txt", return_value=(True, [])) as wait:
        ok, seen = dns.wait_for_propagation(
            opts.r53_domain, "TXT", "_acme-challenge", ["digest-one"],
            timeout=0, change_id="C123")

    assert ok is False, "Expected a change that never goes INSYNC to fail propagation"
    assert wait.call_count == 0, (
        "Expected the authoritative probe to be skipped while the change is still PENDING")


@th.django_unit_test()
def test_route53_reconciled_change_id_is_not_polled(opts):
    """A reconciled upsert converged through inventory reconciliation — there is
    no change batch behind it, so the wait must go straight to the probe instead
    of handing the sentinel to GetChange."""
    from mojo.apps.dnsman.services import dns

    with patch(f"{R53}.get_change") as change, \
            patch(f"{PROBE}.wait_for_txt", return_value=(True, ["digest-one"])) as wait:
        ok, seen = dns.wait_for_propagation(
            opts.r53_domain, "TXT", "_acme-challenge", ["digest-one"],
            timeout=30, change_id=dns.RECONCILED_CHANGE_ID)

    assert ok is True, "Expected the probe result for a reconciled change id"
    assert change.call_count == 0, (
        "'reconciled' is not a pollable change id — GetChange must never see it")
    assert wait.call_count == 1, (
        f"Expected the authoritative probe to still run, got {wait.call_count}")


@th.django_unit_test()
def test_godaddy_propagation_is_the_probe_only(opts):
    """Ordinary GoDaddy TXT uses only the authoritative probe."""
    from mojo.apps.dnsman.services import dns

    with patch(f"{GD_HELPER}.requests"), \
            patch(f"{R53}.get_change") as change, \
            patch(f"{PROBE}.wait_for_txt", return_value=(True, ["digest-one"])) as wait:
        ok, seen = dns.wait_for_propagation(
            opts.gd_domain, "TXT", "_amazonses", ["digest-one"], timeout=30)

    assert ok is True, "Expected the GoDaddy probe result to be returned"
    assert seen == ["digest-one"], f"Expected the seen values, got {seen}"
    assert change.call_count == 0, (
        "GoDaddy has no ChangeInfo API — Route53's must never be consulted for it")
    assert wait.call_count == 1, (
        f"Expected exactly one authoritative probe, got {wait.call_count}")
    assert wait.call_args.args[0] == f"_amazonses.{GD_DOMAIN}", (
        f"Expected the probe to be given the FQDN, got {wait.call_args.args[0]}")


@th.django_unit_test()
def test_godaddy_acme_propagation_waits_out_the_provider_ttl(opts):
    """An authoritative hit is not enough for ACME on GoDaddy.

    GoDaddy enforces a 600-second TTL.  A CA secondary validator may therefore
    still have the prior challenge value cached after the authoritative servers
    show the replacement.  The provider gate must wait out that cache window
    before issuance tells the CA to validate.
    """
    from mojo.apps.dnsman.services import dns
    from mojo.apps.dnsman.services.providers.godaddy_provider import MIN_TTL

    with patch(f"{GD_HELPER}.requests"), \
            patch(f"{PROBE}.wait_for_txt", return_value=(True, ["digest-one"])), \
            patch("mojo.apps.dnsman.services.providers.godaddy_provider.time.sleep",
                  create=True) as sleep:
        ok, seen = dns.wait_for_propagation(
            opts.gd_domain, "TXT", "_acme-challenge", ["digest-one"], timeout=30)

    assert ok is True and seen == ["digest-one"], (
        f"Expected the propagated value to survive the cache gate, got {(ok, seen)}")
    sleep.assert_called_once_with(MIN_TTL)
