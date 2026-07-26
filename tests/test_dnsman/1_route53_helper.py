"""
Unit tests for mojo/helpers/aws/route53.py.

Everything runs in-process with the two boto client factories
(`_domains_client` / `_dns_client`) patched, so no AWS call is ever made.
"""

from unittest.mock import patch, MagicMock

from testit import helpers as th


MODULE = "mojo.helpers.aws.route53"


def _client(**responses):
    """Build a MagicMock boto client with canned return values."""
    client = MagicMock()
    for name, value in responses.items():
        getattr(client, name).return_value = value
    return client


def _prices(tld="com", price=13.0, currency="USD"):
    return {
        "Prices": [{
            "Name": tld,
            "RegistrationPrice": {"Price": price, "Currency": currency},
            "RenewalPrice": {"Price": price, "Currency": currency},
        }]
    }


def _settings(**overrides):
    """Patch settings.get in-process (th.server_settings is for the separate
    server process and does nothing for these direct module calls)."""
    from mojo.helpers.settings import settings as settings_obj

    real_get = settings_obj.get

    def patched_get(name, *args, **kwargs):
        if name in overrides:
            return overrides[name]
        return real_get(name, *args, **kwargs)

    return patch.object(settings_obj, "get", side_effect=patched_get)


# ---------------------------------------------------------------------------
# client factories
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_domains_client_is_pinned_to_us_east_1(opts):
    """The route53domains endpoint only exists in us-east-1."""
    from mojo.helpers.aws import route53

    with patch(f"{MODULE}.get_session") as get_session:
        route53._domains_client("KEY", "SECRET")

    assert get_session.call_count == 1, "Expected _domains_client to build exactly one session"
    args = get_session.call_args.args
    assert args[0] == "KEY", f"Expected the access key to be passed through, got {args[0]}"
    assert args[1] == "SECRET", f"Expected the secret key to be passed through, got {args[1]}"
    assert args[2] == "us-east-1", (
        f"Route53 Domains is us-east-1 only, but the session was built in {args[2]}")
    session = get_session.return_value
    assert session.client.call_args.args[0] == "route53domains", (
        "Expected _domains_client to build a route53domains client")


@th.django_unit_test()
def test_dns_client_uses_the_requested_region(opts):
    """The hosted-zone client honors an explicit region."""
    from mojo.helpers.aws import route53

    with patch(f"{MODULE}.get_session") as get_session:
        route53._dns_client("KEY", "SECRET", "eu-west-1")

    args = get_session.call_args.args
    assert args[2] == "eu-west-1", (
        f"Expected _dns_client to honor the explicit region, got {args[2]}")
    session = get_session.return_value
    assert session.client.call_args.args[0] == "route53", (
        "Expected _dns_client to build a route53 client")


# ---------------------------------------------------------------------------
# availability — the tri-state
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_check_availability_available(opts):
    from mojo.helpers.aws import route53

    route53._price_cache.clear()
    client = _client(
        check_domain_availability={"Availability": "AVAILABLE"},
        list_prices=_prices())

    with patch(f"{MODULE}._domains_client", return_value=client):
        result = route53.check_availability("Example-Site.COM")

    assert result.name == "example-site.com", (
        f"Expected the name to be normalized to lowercase, got {result.name}")
    assert result.status == "AVAILABLE", (
        f"Expected the raw AWS enum to be preserved, got {result.status}")
    assert result.available is True, (
        f"Expected AVAILABLE to map to True, got {result.available!r}")
    assert result.price == 13.0, f"Expected the registration price, got {result.price}"
    assert result.currency == "USD", f"Expected USD currency, got {result.currency}"
    assert result.tld == "com", f"Expected tld 'com', got {result.tld}"
    assert result.tld_supported is True, "Expected a priced TLD to report tld_supported=True"
    assert result.privacy_supported is True, "Expected .com to support WHOIS privacy"
    sent = client.check_domain_availability.call_args.kwargs
    assert sent["DomainName"] == "example-site.com", (
        f"Expected the normalized name to reach AWS, got {sent.get('DomainName')}")


@th.django_unit_test()
def test_check_availability_unavailable(opts):
    from mojo.helpers.aws import route53

    route53._price_cache.clear()
    for status in ("UNAVAILABLE", "UNAVAILABLE_PREMIUM", "UNAVAILABLE_RESTRICTED", "RESERVED"):
        client = _client(
            check_domain_availability={"Availability": status},
            list_prices=_prices())
        with patch(f"{MODULE}._domains_client", return_value=client):
            result = route53.check_availability("taken.com")
        assert result.available is False, (
            f"Expected {status} to map to available=False, got {result.available!r}")
        assert result.status == status, (
            f"Expected the raw status {status} to be preserved, got {result.status}")


@th.django_unit_test()
def test_check_availability_indeterminate_is_none(opts):
    """PENDING / DONT_KNOW mean the registry did not answer — never False."""
    from mojo.helpers.aws import route53

    route53._price_cache.clear()
    for status in ("PENDING", "DONT_KNOW"):
        client = _client(
            check_domain_availability={"Availability": status},
            list_prices=_prices())
        with patch(f"{MODULE}._domains_client", return_value=client):
            result = route53.check_availability("maybe.com")
        assert result.available is None, (
            f"Expected {status} to map to available=None (retry), got {result.available!r}")
        assert result.status == status, (
            f"Expected the raw status {status} to be preserved, got {result.status}")


@th.django_unit_test()
def test_check_availability_invalid_name_raises(opts):
    from mojo.helpers.aws import route53
    from mojo import errors as me

    route53._price_cache.clear()
    client = _client(
        check_domain_availability={"Availability": "INVALID_NAME_FOR_TLD"},
        list_prices=_prices())

    raised = None
    with patch(f"{MODULE}._domains_client", return_value=client):
        try:
            route53.check_availability("bad--name.com")
        except me.ValueException as err:
            raised = err

    assert raised is not None, "Expected INVALID_NAME_FOR_TLD to raise a ValueException"
    assert "not a valid domain name" in str(raised), (
        f"Expected an actionable validation message, got {raised}")


@th.django_unit_test()
def test_check_availability_unsupported_tld_never_raises(opts):
    """A list_prices miss must degrade to tld_supported=False, not an exception."""
    from mojo.helpers.aws import route53

    route53._price_cache.clear()
    client = MagicMock()
    client.check_domain_availability.return_value = {"Availability": "AVAILABLE"}
    client.list_prices.side_effect = Exception("UnsupportedTLD: pizza")

    with patch(f"{MODULE}._domains_client", return_value=client):
        result = route53.check_availability("example.pizza")

    assert result.tld_supported is False, (
        "Expected a list_prices miss to report tld_supported=False")
    assert result.price is None, f"Expected no price for an unsupported TLD, got {result.price}"
    assert result.currency is None, "Expected no currency for an unsupported TLD"
    assert result.available is True, (
        "Expected the raw availability answer to survive an unsupported TLD")


@th.django_unit_test()
def test_list_prices_empty_response_is_unsupported(opts):
    from mojo.helpers.aws import route53

    route53._price_cache.clear()
    client = _client(list_prices={"Prices": []})
    with patch(f"{MODULE}._domains_client", return_value=client):
        result = route53.list_prices("pizza")

    assert result.supported is False, "Expected an empty Prices list to mean unsupported"
    assert result.registration_price is None, "Expected no price for an unsupported TLD"


# ---------------------------------------------------------------------------
# price cache
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_list_prices_caches_per_tld(opts):
    """The second lookup for a TLD must be served without an AWS call."""
    from mojo.helpers.aws import route53

    route53._price_cache.clear()
    client = _client(list_prices=_prices())

    with patch(f"{MODULE}._domains_client", return_value=client):
        first = route53.list_prices("com")
        second = route53.list_prices("com")
        route53.list_prices("net")

    assert first.registration_price == 13.0, (
        f"Expected the fetched price on the first call, got {first.registration_price}")
    assert second.registration_price == 13.0, (
        f"Expected the cached price on the second call, got {second.registration_price}")
    assert client.list_prices.call_count == 2, (
        "Expected one AWS call for 'com' (second served from cache) plus one "
        f"for 'net', got {client.list_prices.call_count}")


@th.django_unit_test()
def test_list_prices_cache_expires(opts):
    """An entry older than the TTL must be refetched, not served."""
    from mojo.helpers.aws import route53

    route53._price_cache.clear()
    client = _client(list_prices=_prices())

    with patch(f"{MODULE}._domains_client", return_value=client):
        route53.list_prices("com")
        fetched_at, cached = route53._price_cache["com"]
        # Rewind the stored timestamp far past any sane TTL — no sleeping.
        route53._price_cache["com"] = (fetched_at - 10 * 366 * 86400, cached)
        route53.list_prices("com")

    assert client.list_prices.call_count == 2, (
        f"Expected an expired entry to refetch, got {client.list_prices.call_count} calls")


@th.django_unit_test()
def test_list_prices_ttl_zero_disables_cache(opts):
    """ROUTE53_PRICE_CACHE_HOURS <= 0 is the escape hatch: no hits, no stores."""
    from mojo.helpers.aws import route53

    route53._price_cache.clear()
    client = _client(list_prices=_prices())

    with _settings(ROUTE53_PRICE_CACHE_HOURS=0):
        with patch(f"{MODULE}._domains_client", return_value=client):
            route53.list_prices("com")
            route53.list_prices("com")

    assert client.list_prices.call_count == 2, (
        f"Expected TTL<=0 to disable caching, got {client.list_prices.call_count} calls")
    assert "com" not in route53._price_cache, (
        "Expected TTL<=0 to store nothing in the cache")


@th.django_unit_test()
def test_list_prices_never_caches_failures(opts):
    """A transient AWS failure must not pin a TLD unsupported for the TTL."""
    from mojo.helpers.aws import route53

    route53._price_cache.clear()
    client = MagicMock()
    client.list_prices.side_effect = Exception("throttled")

    with patch(f"{MODULE}._domains_client", return_value=client):
        first = route53.list_prices("com")
        second = route53.list_prices("com")
        # AWS recovers: the very next call must see the real answer.
        client.list_prices.side_effect = None
        client.list_prices.return_value = _prices()
        third = route53.list_prices("com")

    assert first.supported is False and second.supported is False, (
        "Expected the failure path to keep reporting unsupported")
    assert client.list_prices.call_count == 3, (
        f"Expected every call to retry after failures, got {client.list_prices.call_count}")
    assert third.supported is True, (
        "Expected the first successful answer after a failure to come through")
    assert third.registration_price == 13.0, (
        f"Expected the recovered price, got {third.registration_price}")


@th.django_unit_test()
def test_list_prices_cache_serves_copies(opts):
    """Mutating a returned result must not corrupt the cached entry."""
    from mojo.helpers.aws import route53

    route53._price_cache.clear()
    client = _client(list_prices=_prices())

    with patch(f"{MODULE}._domains_client", return_value=client):
        first = route53.list_prices("com")
        first.registration_price = 999.0
        second = route53.list_prices("com")

    assert second.registration_price == 13.0, (
        "Expected the cache to serve copies — a caller's mutation leaked into "
        f"the cached entry (got {second.registration_price})")


# ---------------------------------------------------------------------------
# domain suggestions
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_get_domain_suggestions_maps_the_tristate(opts):
    from mojo.helpers.aws import route53

    client = _client(get_domain_suggestions={"SuggestionsList": [
        {"DomainName": "Example-One.COM.", "Availability": "AVAILABLE"},
        {"DomainName": "example-two.net", "Availability": "UNAVAILABLE"},
        {"DomainName": "example-three.io", "Availability": "PENDING"},
    ]})

    with patch(f"{MODULE}._domains_client", return_value=client):
        rows = route53.get_domain_suggestions("Example.COM", count="5", only_available=False)

    sent = client.get_domain_suggestions.call_args.kwargs
    assert sent["DomainName"] == "example.com", (
        f"Expected the normalized name to reach AWS, got {sent.get('DomainName')}")
    assert sent["SuggestionCount"] == 5 and isinstance(sent["SuggestionCount"], int), (
        f"Expected the count to be coerced to int, got {sent.get('SuggestionCount')!r}")
    assert sent["OnlyAvailable"] is False, (
        f"Expected only_available to pass through as a bool, got {sent.get('OnlyAvailable')!r}")

    assert [r.name for r in rows] == [
        "example-one.com", "example-two.net", "example-three.io"], (
        f"Expected normalized suggestion names in AWS order, got {[r.name for r in rows]}")
    assert rows[0].available is True, (
        f"Expected AVAILABLE to map to True, got {rows[0].available!r}")
    assert rows[1].available is False, (
        f"Expected UNAVAILABLE to map to False, got {rows[1].available!r}")
    assert rows[2].available is None, (
        f"Expected PENDING to map to None, got {rows[2].available!r}")
    assert rows[0].status == "AVAILABLE", (
        f"Expected the raw AWS enum to be preserved, got {rows[0].status}")


@th.django_unit_test()
def test_get_domain_suggestions_skips_garbage_and_never_raises_per_row(opts):
    """A bad row is dropped; INVALID_NAME_FOR_TLD maps to None, not a raise."""
    from mojo.helpers.aws import route53

    client = _client(get_domain_suggestions={"SuggestionsList": [
        {"DomainName": "bad name/slash.com", "Availability": "AVAILABLE"},
        {"DomainName": "good-one.com", "Availability": "INVALID_NAME_FOR_TLD"},
        {"DomainName": "good-two.com", "Availability": "AVAILABLE"},
    ]})

    with patch(f"{MODULE}._domains_client", return_value=client):
        rows = route53.get_domain_suggestions("example.com")

    assert [r.name for r in rows] == ["good-one.com", "good-two.com"], (
        f"Expected the unusable row to be skipped, got {[r.name for r in rows]}")
    assert rows[0].available is None, (
        "Expected INVALID_NAME_FOR_TLD to map to None on a suggestion row "
        f"(never a raise), got {rows[0].available!r}")
    assert rows[1].available is True, (
        f"Expected the good row to survive, got {rows[1].available!r}")


# ---------------------------------------------------------------------------
# privacy capability + TXT formatting
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_supports_privacy(opts):
    from mojo.helpers.aws import route53

    assert route53.supports_privacy("com") is True, "Expected .com to support privacy"
    assert route53.supports_privacy(".NET") is True, (
        "Expected leading dots and case to be ignored for .net")
    assert route53.supports_privacy("us") is False, "Expected .us to forbid privacy"
    assert route53.supports_privacy("com.au") is False, "Expected .com.au to forbid privacy"
    assert route53.supports_privacy("ca") is False, "Expected .ca to forbid privacy"


@th.django_unit_test()
def test_format_txt_value_quotes_and_chunks(opts):
    from mojo.helpers.aws import route53

    short = route53.format_txt_value("v=spf1 include:amazonses.com ~all")
    assert short == '"v=spf1 include:amazonses.com ~all"', (
        f"Expected a short TXT value to be wrapped in one pair of quotes, got {short}")

    long_value = "k" * 300
    chunked = route53.format_txt_value(long_value)
    quote_count = chunked.count('"')
    assert quote_count == 4, (
        f"Expected a 300-char value to become two quoted chunks, got {quote_count} quote chars")
    parts = chunked.split('" "')
    assert len(parts) == 2, f"Expected exactly two chunks, got {len(parts)}"
    first_chunk_length = len(parts[0].lstrip('"'))
    assert first_chunk_length == 255, (
        f"Expected the first chunk to be exactly 255 chars, got {first_chunk_length}")

    already = route53.format_txt_value('"pre-quoted"')
    assert already == '"pre-quoted"', (
        f"Expected an already-quoted value to pass through untouched, got {already}")

    assert route53.format_txt_value("") == '""', "Expected an empty TXT value to become \"\""

    assert route53.parse_txt_value(chunked) == long_value, (
        "Expected parse_txt_value to rebuild the original value from its chunks")


# ---------------------------------------------------------------------------
# zone lookup — exact match only
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_find_zone_id_exact_match(opts):
    from mojo.helpers.aws import route53

    client = _client(list_hosted_zones_by_name={"HostedZones": [
        {"Id": "/hostedzone/ZNEIGHBOUR", "Name": "example.com.au.", "Config": {"PrivateZone": False}},
        {"Id": "/hostedzone/ZEXACT", "Name": "example.com.", "Config": {"PrivateZone": False}},
    ]})

    with patch(f"{MODULE}._dns_client", return_value=client):
        zone_id = route53.find_zone_id("Example.COM.")

    assert zone_id == "ZEXACT", (
        f"Expected the exactly-matching zone id, got {zone_id}")
    sent = client.list_hosted_zones_by_name.call_args.kwargs
    assert sent["DNSName"] == "example.com.", (
        f"Expected the normalized fully-qualified name to be queried, got {sent.get('DNSName')}")


@th.django_unit_test()
def test_find_zone_id_ignores_lexically_adjacent_zone(opts):
    """
    list_hosted_zones_by_name returns a lexically ordered page starting at the
    requested name, so a prefix/startswith match would select a neighbouring
    zone and every later record write would land in the wrong zone.
    """
    from mojo.helpers.aws import route53

    client = _client(list_hosted_zones_by_name={"HostedZones": [
        {"Id": "/hostedzone/ZAU", "Name": "example.com.au.", "Config": {"PrivateZone": False}},
        {"Id": "/hostedzone/ZCO", "Name": "example.company.", "Config": {"PrivateZone": False}},
    ]})

    with patch(f"{MODULE}._dns_client", return_value=client):
        zone_id = route53.find_zone_id("example.com")

    assert zone_id is None, (
        f"Expected no match for example.com, but a neighbouring zone was selected: {zone_id}")


@th.django_unit_test()
def test_find_zone_id_prefers_the_public_zone(opts):
    from mojo.helpers.aws import route53

    client = _client(list_hosted_zones_by_name={"HostedZones": [
        {"Id": "/hostedzone/ZPRIVATE", "Name": "example.com.", "Config": {"PrivateZone": True}},
        {"Id": "/hostedzone/ZPUBLIC", "Name": "example.com.", "Config": {"PrivateZone": False}},
    ]})

    with patch(f"{MODULE}._dns_client", return_value=client):
        zone_id = route53.find_zone_id("example.com")

    assert zone_id == "ZPUBLIC", (
        f"Expected the public zone to win over the private one, got {zone_id}")


# ---------------------------------------------------------------------------
# record listing — pagination
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_list_records_follows_pagination(opts):
    from mojo.helpers.aws import route53

    page_one = {
        "ResourceRecordSets": [
            {"Name": "example.com.", "Type": "A", "TTL": 300,
             "ResourceRecords": [{"Value": "1.2.3.4"}]},
        ],
        "IsTruncated": True,
        "NextRecordName": "www.example.com.",
        "NextRecordType": "CNAME",
    }
    page_two = {
        "ResourceRecordSets": [
            {"Name": "\\052.example.com.", "Type": "CNAME", "TTL": 60,
             "ResourceRecords": [{"Value": "example.com."}]},
        ],
        "IsTruncated": False,
    }
    client = MagicMock()
    client.list_resource_record_sets.side_effect = [page_one, page_two]

    with patch(f"{MODULE}._dns_client", return_value=client):
        records = route53.list_records("/hostedzone/Z123")

    assert client.list_resource_record_sets.call_count == 2, (
        f"Expected the truncated response to be followed, got "
        f"{client.list_resource_record_sets.call_count} call(s)")
    assert len(records) == 2, f"Expected records from both pages, got {len(records)}"

    first_call = client.list_resource_record_sets.call_args_list[0].kwargs
    assert first_call["HostedZoneId"] == "Z123", (
        f"Expected the zone id to be stripped of its /hostedzone/ prefix, "
        f"got {first_call.get('HostedZoneId')}")

    second_call = client.list_resource_record_sets.call_args_list[1].kwargs
    assert second_call["StartRecordName"] == "www.example.com.", (
        f"Expected NextRecordName to drive the second page, "
        f"got {second_call.get('StartRecordName')}")
    assert second_call["StartRecordType"] == "CNAME", (
        f"Expected NextRecordType to drive the second page, "
        f"got {second_call.get('StartRecordType')}")

    assert records[0].name == "example.com", (
        f"Expected the trailing dot to be stripped, got {records[0].name}")
    assert records[0].record_values == ["1.2.3.4"], (
        f"Expected the record values to be flattened, got {records[0].record_values}")
    assert "values" not in records[0], (
        "objict subclasses dict, so a 'values' key would be shadowed by dict.values — "
        "the field must stay named 'record_values'")
    assert records[1].name == "*.example.com", (
        f"Expected the octal wildcard escape to be decoded, got {records[1].name}")


# ---------------------------------------------------------------------------
# record writes — guards
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_upsert_record_refuses_apex_ns_and_soa(opts):
    from mojo.helpers.aws import route53
    from mojo import errors as me

    client = MagicMock()
    for rtype in ("NS", "SOA"):
        raised = None
        with patch(f"{MODULE}._dns_client", return_value=client):
            try:
                route53.upsert_record("Z1", rtype, "example.com", ["ns-1.awsdns.com"],
                                      zone_name="example.com")
            except me.ValueException as err:
                raised = err
        assert raised is not None, f"Expected an apex {rtype} upsert to be refused"
        assert "apex" in str(raised).lower(), (
            f"Expected the {rtype} refusal to explain it is the apex record, got {raised}")

    assert client.change_resource_record_sets.call_count == 0, (
        "Expected a refused apex change to never reach AWS")


@th.django_unit_test()
def test_delete_record_refuses_apex_ns(opts):
    from mojo.helpers.aws import route53
    from mojo import errors as me

    client = MagicMock()
    raised = None
    with patch(f"{MODULE}._dns_client", return_value=client):
        try:
            route53.delete_record("Z1", "NS", "example.com", values=["ns-1.awsdns.com"],
                                  ttl=172800, zone_name="example.com")
        except me.ValueException as err:
            raised = err

    assert raised is not None, "Expected an apex NS delete to be refused"
    assert client.change_resource_record_sets.call_count == 0, (
        "Expected a refused apex delete to never reach AWS")


@th.django_unit_test()
def test_record_writes_refuse_out_of_zone_names(opts):
    from mojo.helpers.aws import route53
    from mojo import errors as me

    client = MagicMock()
    raised = None
    with patch(f"{MODULE}._dns_client", return_value=client):
        try:
            route53.upsert_record("Z1", "A", "www.attacker.com", ["1.2.3.4"],
                                  zone_name="example.com")
        except me.ValueException as err:
            raised = err

    assert raised is not None, "Expected an out-of-zone name to be refused"
    assert "not inside" in str(raised), (
        f"Expected the refusal to name the zone boundary, got {raised}")

    # A sibling that merely shares a suffix fragment is still out of zone.
    raised = None
    with patch(f"{MODULE}._dns_client", return_value=client):
        try:
            route53.upsert_record("Z1", "A", "notexample.com", ["1.2.3.4"],
                                  zone_name="example.com")
        except me.ValueException as err:
            raised = err
    assert raised is not None, (
        "Expected 'notexample.com' to be refused for the 'example.com' zone")

    assert client.change_resource_record_sets.call_count == 0, (
        "Expected refused out-of-zone changes to never reach AWS")


@th.django_unit_test()
def test_upsert_record_quotes_and_chunks_txt(opts):
    from mojo.helpers.aws import route53

    client = _client(change_resource_record_sets={"ChangeInfo": {"Id": "/change/C123",
                                                                 "Status": "PENDING"}})
    long_value = "k" * 300

    with patch(f"{MODULE}._dns_client", return_value=client):
        change_id = route53.upsert_record(
            "Z1", "txt", "_acme-challenge.Example.com", long_value,
            ttl=60, zone_name="example.com")

    assert change_id == "C123", f"Expected the bare change id, got {change_id}"
    batch = client.change_resource_record_sets.call_args.kwargs["ChangeBatch"]
    change = batch["Changes"][0]
    assert change["Action"] == "UPSERT", f"Expected an UPSERT action, got {change['Action']}"
    rrset = change["ResourceRecordSet"]
    assert rrset["Type"] == "TXT", f"Expected the type to be upper-cased, got {rrset['Type']}"
    assert rrset["Name"] == "_acme-challenge.example.com", (
        f"Expected the record name to be normalized, got {rrset['Name']}")
    assert rrset["TTL"] == 60, f"Expected the requested TTL, got {rrset['TTL']}"
    value = rrset["ResourceRecords"][0]["Value"]
    assert value.startswith('"') and value.endswith('"'), (
        f"Route53 requires quoted TXT values, got {value[:20]}...")
    assert value.count('"') == 4, (
        "Expected a 300-char TXT value to be split into two quoted 255-char chunks")


@th.django_unit_test()
def test_upsert_record_sends_every_value(opts):
    """A wildcard and its base name share one _acme-challenge set with two values."""
    from mojo.helpers.aws import route53

    client = _client(change_resource_record_sets={"ChangeInfo": {"Id": "/change/C7"}})

    with patch(f"{MODULE}._dns_client", return_value=client):
        route53.upsert_record("Z1", "TXT", "_acme-challenge.example.com",
                              ["digest-one", "digest-two"], zone_name="example.com")

    rrset = (client.change_resource_record_sets.call_args.kwargs["ChangeBatch"]
             ["Changes"][0]["ResourceRecordSet"])
    values = [rr["Value"] for rr in rrset["ResourceRecords"]]
    assert values == ['"digest-one"', '"digest-two"'], (
        f"Expected both quoted values in a single record set, got {values}")


@th.django_unit_test()
def test_upsert_record_resolves_the_zone_name_when_not_given(opts):
    from mojo.helpers.aws import route53

    client = _client(
        get_hosted_zone={"HostedZone": {"Id": "/hostedzone/Z1", "Name": "example.com.",
                                        "Config": {"PrivateZone": False}}},
        change_resource_record_sets={"ChangeInfo": {"Id": "/change/C5"}})

    with patch(f"{MODULE}._dns_client", return_value=client):
        change_id = route53.upsert_record("Z1", "A", "www.example.com", "1.2.3.4")

    assert change_id == "C5", f"Expected the change id, got {change_id}"
    assert client.get_hosted_zone.call_args.kwargs["Id"] == "Z1", (
        "Expected the zone name to be read back from the hosted zone")


@th.django_unit_test()
def test_delete_record_reads_back_the_existing_values(opts):
    from mojo.helpers.aws import route53

    client = _client(
        list_resource_record_sets={
            "ResourceRecordSets": [
                {"Name": "www.example.com.", "Type": "A", "TTL": 120,
                 "ResourceRecords": [{"Value": "1.2.3.4"}]},
            ],
            "IsTruncated": False,
        },
        change_resource_record_sets={"ChangeInfo": {"Id": "/change/C9"}})

    with patch(f"{MODULE}._dns_client", return_value=client):
        change_id = route53.delete_record("Z1", "A", "www.example.com", zone_name="example.com")

    assert change_id == "C9", f"Expected the change id, got {change_id}"
    change = client.change_resource_record_sets.call_args.kwargs["ChangeBatch"]["Changes"][0]
    assert change["Action"] == "DELETE", f"Expected a DELETE action, got {change['Action']}"
    rrset = change["ResourceRecordSet"]
    assert rrset["TTL"] == 120, (
        f"Route53 requires the stored TTL on a DELETE, got {rrset['TTL']}")
    assert [rr["Value"] for rr in rrset["ResourceRecords"]] == ["1.2.3.4"], (
        "Expected the stored values to be echoed back on the DELETE")


@th.django_unit_test()
def test_delete_record_missing_record_raises(opts):
    from mojo.helpers.aws import route53
    from mojo import errors as me

    client = _client(list_resource_record_sets={"ResourceRecordSets": [], "IsTruncated": False})

    raised = None
    with patch(f"{MODULE}._dns_client", return_value=client):
        try:
            route53.delete_record("Z1", "A", "gone.example.com", zone_name="example.com")
        except me.ValueException as err:
            raised = err

    assert raised is not None, "Expected deleting a non-existent record to raise, not no-op"
    assert client.change_resource_record_sets.call_count == 0, (
        "Expected no change to be submitted for a record that does not exist")


# ---------------------------------------------------------------------------
# registrar operations
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_register_returns_the_operation_id(opts):
    from mojo.helpers.aws import route53

    client = _client(register_domain={"OperationId": "op-123"})
    contact = {"FirstName": "Test", "LastName": "User", "ContactType": "PERSON",
               "CountryCode": "US", "Email": "ops@example.com"}

    with patch(f"{MODULE}._domains_client", return_value=client):
        result = route53.register("Example.COM", contact, years=2, privacy=True)

    assert result.operation_id == "op-123", (
        f"Expected the AWS OperationId, got {result.operation_id}")
    assert result.privacy is True, "Expected privacy to be reported as applied"
    assert result.privacy_downgraded is False, "Expected no privacy downgrade"
    sent = client.register_domain.call_args.kwargs
    assert sent["DomainName"] == "example.com", (
        f"Expected the normalized name, got {sent.get('DomainName')}")
    assert sent["DurationInYears"] == 2, f"Expected 2 years, got {sent.get('DurationInYears')}"
    for field in ("AdminContact", "RegistrantContact", "TechContact"):
        assert sent[field] == contact, f"Expected the contact block on {field}"
    for field in ("PrivacyProtectAdminContact", "PrivacyProtectRegistrantContact",
                  "PrivacyProtectTechContact"):
        assert sent[field] is True, f"Expected {field} to be enabled"


@th.django_unit_test()
def test_register_retries_once_without_privacy(opts):
    """The curated privacy TLD list can go stale — a stale entry must not fail a purchase."""
    from mojo.helpers.aws import route53
    from botocore.exceptions import ClientError

    error = ClientError(
        {"Error": {"Code": "InvalidInput",
                   "Message": "Privacy protection is not supported for this TLD"}},
        "RegisterDomain")
    client = MagicMock()
    client.register_domain.side_effect = [error, {"OperationId": "op-456"}]
    contact = {"FirstName": "Test", "LastName": "User"}

    with patch(f"{MODULE}._domains_client", return_value=client):
        result = route53.register("example.pizza", contact, privacy=True)

    assert client.register_domain.call_count == 2, (
        f"Expected exactly one retry, got {client.register_domain.call_count} call(s)")
    assert result.operation_id == "op-456", (
        f"Expected the retried operation id, got {result.operation_id}")
    assert result.privacy is False, (
        "Expected the result to report the privacy it actually got, not what was asked for")
    assert result.privacy_downgraded is True, "Expected the downgrade to be flagged"
    retried = client.register_domain.call_args.kwargs
    assert retried["PrivacyProtectRegistrantContact"] is False, (
        "Expected the retry to disable privacy")


@th.django_unit_test()
def test_register_propagates_non_privacy_errors(opts):
    from mojo.helpers.aws import route53
    from botocore.exceptions import ClientError

    error = ClientError(
        {"Error": {"Code": "InvalidInput", "Message": "Domain is unavailable"}},
        "RegisterDomain")
    client = MagicMock()
    client.register_domain.side_effect = error

    raised = None
    with patch(f"{MODULE}._domains_client", return_value=client):
        try:
            route53.register("example.com", {"FirstName": "T"}, privacy=True)
        except ClientError as err:
            raised = err

    assert raised is not None, "Expected a non-privacy AWS error to propagate"
    assert client.register_domain.call_count == 1, (
        "Expected no retry for an error unrelated to privacy")


@th.django_unit_test()
def test_operation_detail(opts):
    from mojo.helpers.aws import route53

    client = _client(get_operation_detail={
        "OperationId": "op-1", "Status": "SUCCESSFUL", "DomainName": "example.com",
        "Type": "REGISTER_DOMAIN", "Message": None})

    with patch(f"{MODULE}._domains_client", return_value=client):
        result = route53.operation_detail("op-1")

    assert result.status == "SUCCESSFUL", f"Expected the raw status, got {result.status}"
    assert result.domain_name == "example.com", (
        f"Expected the domain name, got {result.domain_name}")
    assert result.type == "REGISTER_DOMAIN", f"Expected the operation type, got {result.type}"


@th.django_unit_test()
def test_list_operations_follows_the_page_marker(opts):
    from mojo.helpers.aws import route53

    client = MagicMock()
    client.list_operations.side_effect = [
        {"Operations": [{"OperationId": "op-1", "Status": "SUBMITTED"}],
         "NextPageMarker": "marker-2"},
        {"Operations": [{"OperationId": "op-2", "Status": "SUCCESSFUL"}]},
    ]

    with patch(f"{MODULE}._domains_client", return_value=client):
        operations = route53.list_operations(submitted_since="2026-07-25")

    assert len(operations) == 2, f"Expected operations from both pages, got {len(operations)}"
    assert client.list_operations.call_count == 2, (
        f"Expected the page marker to be followed, got {client.list_operations.call_count} call(s)")
    first = client.list_operations.call_args_list[0].kwargs
    assert first["SubmittedSince"] == "2026-07-25", (
        "Expected SubmittedSince to be passed through for crash-window reconciliation")
    second = client.list_operations.call_args_list[1].kwargs
    assert second["Marker"] == "marker-2", (
        f"Expected the marker on the second page, got {second.get('Marker')}")


@th.django_unit_test()
def test_get_domain_detail(opts):
    from mojo.helpers.aws import route53

    client = _client(get_domain_detail={
        "DomainName": "example.com",
        "Nameservers": [{"Name": "ns-1.awsdns-01.com"}, {"Name": "ns-2.awsdns-02.org"}],
        "AutoRenew": True,
        "AdminPrivacy": True, "RegistrantPrivacy": True, "TechPrivacy": True,
        "CreationDate": "2026-01-01", "ExpirationDate": "2027-01-01",
        "RegistrantContact": {"Email": "ops@example.com"},
    })

    with patch(f"{MODULE}._domains_client", return_value=client):
        result = route53.get_domain_detail("Example.com.")

    assert result.name == "example.com", f"Expected the normalized name, got {result.name}"
    assert result.nameservers == ["ns-1.awsdns-01.com", "ns-2.awsdns-02.org"], (
        f"Expected the nameserver list to be flattened, got {result.nameservers}")
    assert result.auto_renew is True, "Expected auto_renew to be reported"
    assert result.privacy is True, "Expected privacy to mirror the registrant privacy flag"
    assert result.registrant_contact.Email == "ops@example.com", (
        "Expected the registrant contact to be returned as an objict")


@th.django_unit_test()
def test_update_privacy_and_auto_renew(opts):
    from mojo.helpers.aws import route53

    client = _client(update_domain_contact_privacy={"OperationId": "op-p"})

    with patch(f"{MODULE}._domains_client", return_value=client):
        op_id = route53.update_privacy("example.com", False)
        route53.update_auto_renew("example.com", True)
        route53.update_auto_renew("example.com", False)

    assert op_id == "op-p", f"Expected the privacy OperationId, got {op_id}"
    sent = client.update_domain_contact_privacy.call_args.kwargs
    for field in ("AdminPrivacy", "RegistrantPrivacy", "TechPrivacy"):
        assert sent[field] is False, f"Expected {field} to be disabled"
    assert client.enable_domain_auto_renew.call_count == 1, (
        "Expected enable_domain_auto_renew to be used when enabling")
    assert client.disable_domain_auto_renew.call_count == 1, (
        "Expected disable_domain_auto_renew to be used when disabling")


# ---------------------------------------------------------------------------
# zones + change propagation
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_create_hosted_zone(opts):
    from mojo.helpers.aws import route53

    client = _client(create_hosted_zone={
        "HostedZone": {"Id": "/hostedzone/ZNEW", "Name": "example.com."},
        "DelegationSet": {"NameServers": ["ns-1.awsdns-01.com"]},
        "ChangeInfo": {"Id": "/change/C1"},
    })

    with patch(f"{MODULE}._dns_client", return_value=client):
        result = route53.create_hosted_zone("Example.com")

    assert result.zone_id == "ZNEW", f"Expected the bare zone id, got {result.zone_id}"
    assert result.name == "example.com", f"Expected the normalized name, got {result.name}"
    assert result.name_servers == ["ns-1.awsdns-01.com"], (
        f"Expected the delegation set nameservers, got {result.name_servers}")
    sent = client.create_hosted_zone.call_args.kwargs
    assert sent["Name"] == "example.com", f"Expected the normalized name, got {sent.get('Name')}"
    assert sent["CallerReference"], "Expected a caller reference to be generated"


@th.django_unit_test()
def test_get_change_reports_insync(opts):
    from mojo.helpers.aws import route53

    client = _client(get_change={"ChangeInfo": {"Id": "/change/C1", "Status": "INSYNC"}})
    with patch(f"{MODULE}._dns_client", return_value=client):
        result = route53.get_change("/change/C1")

    assert result.change_id == "C1", f"Expected the bare change id, got {result.change_id}"
    assert result.status == "INSYNC", f"Expected the raw status, got {result.status}"
    assert result.insync is True, "Expected insync to be True for an INSYNC change"

    client = _client(get_change={"ChangeInfo": {"Id": "/change/C2", "Status": "PENDING"}})
    with patch(f"{MODULE}._dns_client", return_value=client):
        result = route53.get_change("C2")
    assert result.insync is False, "Expected insync to be False while a change is PENDING"
