"""
Email (SES) integration for dnsman-held domains.

Everything runs in-process with the AWS and GoDaddy edges patched:
`route53.upsert_record` / `route53.find_zone_id` for Route 53, the `DNSManager`
class for GoDaddy, and `ses_domain.onboard_domain` for the SES calls. No live
AWS or GoDaddy request is ever made.
"""

from unittest.mock import patch, MagicMock

from testit import helpers as th


SES = "mojo.helpers.aws.ses_domain"
ROUTE53 = "mojo.helpers.aws.route53"
GODADDY = "mojo.helpers.dns.godaddy"

R53_DOMAIN = "dnsman-email-r53.com"
GD_DOMAIN = "dnsman-email-gd.com"
GD_BROKEN_DOMAIN = "dnsman-email-gd-broken.com"
UNMANAGED_DOMAIN = "dnsman-email-unmanaged.com"
GRAPH_DOMAIN = "dnsman-email-graph.com"

CRED_NAME = "dnsman email test credential"
BROKEN_CRED_NAME = "dnsman email test credential (unverified)"

GD_API_KEY = "gd-key-1234"
GD_API_SECRET = "gd-secret-5678"

RAW_AWS_KEY = "AKIATESTKEY001234"
ZONE_ID = "ZR53EMAILTEST"


def _records(domain):
    """The record set SES asks for: verification TXT, a DKIM CNAME, MAIL FROM MX + SPF."""
    from mojo.helpers.aws.ses_domain import DnsRecord

    return [
        DnsRecord(type="TXT", name=f"_amazonses.{domain}",
                  value="ses-verification-token", ttl=600),
        DnsRecord(type="CNAME", name=f"tok1._domainkey.{domain}",
                  value="tok1.dkim.amazonses.com", ttl=600),
        DnsRecord(type="MX", name=f"feedback.{domain}",
                  value="10 feedback-smtp.us-east-1.amazonses.com", ttl=600),
        DnsRecord(type="TXT", name=f"feedback.{domain}",
                  value="v=spf1 include:amazonses.com ~all", ttl=600),
    ]


def _upserts_by_key(upsert):
    """Index the patched route53.upsert_record calls by (type, name)."""
    indexed = {}
    for call in upsert.call_args_list:
        zone_id, rtype, name, values = call.args[0], call.args[1], call.args[2], call.args[3]
        indexed[(rtype, name)] = {
            "zone_id": zone_id,
            "record_values": values,
            "kwargs": call.kwargs,
        }
    return indexed


@th.django_unit_setup()
def setup_email_fixtures(opts):
    """
    Build the fixture rows.

    The suite runs against a long-lived database, so every row this setup
    creates is deleted first.
    """
    from mojo.apps.dnsman.models import Domain, DnsCredential
    from mojo.apps.aws.models import EmailDomain

    domain_names = [R53_DOMAIN, GD_DOMAIN, GD_BROKEN_DOMAIN, UNMANAGED_DOMAIN, GRAPH_DOMAIN]
    Domain.objects.filter(name__in=domain_names).delete()
    EmailDomain.objects.filter(name__in=domain_names).delete()
    DnsCredential.objects.filter(name__in=[CRED_NAME, BROKEN_CRED_NAME]).delete()

    credential = DnsCredential(name=CRED_NAME, provider="godaddy",
                               is_active=True, verified=True)
    credential.set_credentials(GD_API_KEY, GD_API_SECRET)
    credential.save()
    opts.credential_id = credential.id

    broken = DnsCredential(name=BROKEN_CRED_NAME, provider="godaddy",
                           is_active=True, verified=False)
    broken.set_credentials(GD_API_KEY, GD_API_SECRET)
    broken.save()

    opts.r53_domain_id = Domain.objects.create(
        name=R53_DOMAIN, provider="route53", status="active",
        hosted_zone_id=ZONE_ID, verified=True).id
    opts.gd_domain_id = Domain.objects.create(
        name=GD_DOMAIN, provider="godaddy", status="active",
        credential=credential, verified=True).id
    opts.gd_broken_domain_id = Domain.objects.create(
        name=GD_BROKEN_DOMAIN, provider="godaddy", status="active",
        credential=broken, verified=True).id

    opts.email_r53_id = EmailDomain.objects.create(
        name=R53_DOMAIN, region="us-east-1", dns_mode="manual").id
    opts.email_unmanaged_id = EmailDomain.objects.create(
        name=UNMANAGED_DOMAIN, region="us-east-1", dns_mode="manual").id

    graph_domain = EmailDomain(name=GRAPH_DOMAIN, region="us-east-1")
    graph_domain.save()
    graph_domain.set_aws_key(RAW_AWS_KEY)
    graph_domain.set_aws_secret("super-secret-value")
    graph_domain.save()
    opts.graph_email_domain_id = graph_domain.id


# ---------------------------------------------------------------------------
# apply_dns_records_route53 — the twin of apply_dns_records_godaddy
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_apply_dns_records_route53_upserts_every_record(opts):
    from mojo.helpers.aws import ses_domain

    with patch(f"{ROUTE53}.upsert_record") as upsert, \
            patch(f"{ROUTE53}.find_zone_id") as find_zone:
        applied = ses_domain.apply_dns_records_route53(
            R53_DOMAIN, _records(R53_DOMAIN), zone_id=ZONE_ID)

    assert applied is True, f"Expected apply_dns_records_route53 to report success, got {applied!r}"
    assert find_zone.call_count == 0, (
        "An explicit zone_id must not trigger a hosted-zone lookup")
    assert upsert.call_count == 4, (
        f"Expected one upsert per record, got {upsert.call_count}")

    calls = _upserts_by_key(upsert)

    verification = calls.get(("TXT", f"_amazonses.{R53_DOMAIN}"))
    assert verification is not None, (
        f"Expected the SES verification TXT to be upserted, got {sorted(calls.keys())}")
    assert verification["record_values"] == ['"ses-verification-token"'], (
        "Route 53 requires TXT values to be quoted — an unquoted SES token "
        f"silently fails verification, got {verification['record_values']}")
    assert verification["zone_id"] == ZONE_ID, (
        f"Expected the records to land in {ZONE_ID}, got {verification['zone_id']}")
    assert verification["kwargs"].get("zone_name") == R53_DOMAIN, (
        "Expected the zone name to be passed so the in-zone guard can run without "
        f"an extra API call, got {verification['kwargs'].get('zone_name')}")
    assert verification["kwargs"].get("ttl") == 600, (
        f"Expected the record's TTL to be honored, got {verification['kwargs'].get('ttl')}")

    spf = calls.get(("TXT", f"feedback.{R53_DOMAIN}"))
    assert spf is not None, "Expected the MAIL FROM SPF TXT record to be upserted"
    assert spf["record_values"] == ['"v=spf1 include:amazonses.com ~all"'], (
        f"Expected the SPF value to be quoted for Route 53, got {spf['record_values']}")

    mx = calls.get(("MX", f"feedback.{R53_DOMAIN}"))
    assert mx is not None, "Expected the MAIL FROM MX record to be upserted"
    assert mx["record_values"] == ["10 feedback-smtp.us-east-1.amazonses.com"], (
        "An MX value carries its priority in the value string and must be passed "
        f"through verbatim and unquoted, got {mx['record_values']}")

    dkim = calls.get(("CNAME", f"tok1._domainkey.{R53_DOMAIN}"))
    assert dkim is not None, "Expected the DKIM CNAME to be upserted"
    assert dkim["record_values"] == ["tok1.dkim.amazonses.com"], (
        f"Expected the DKIM CNAME target to be sent unquoted, got {dkim['record_values']}")


@th.django_unit_test()
def test_apply_dns_records_route53_resolves_the_zone_when_not_given(opts):
    from mojo.helpers.aws import ses_domain

    with patch(f"{ROUTE53}.upsert_record") as upsert, \
            patch(f"{ROUTE53}.find_zone_id", return_value="ZLOOKEDUP") as find_zone:
        ses_domain.apply_dns_records_route53(R53_DOMAIN, _records(R53_DOMAIN)[:1])

    assert find_zone.call_count == 1, (
        f"Expected the hosted zone to be looked up once, got {find_zone.call_count}")
    assert upsert.call_args.args[0] == "ZLOOKEDUP", (
        f"Expected the looked-up zone id to be used, got {upsert.call_args.args[0]}")


@th.django_unit_test()
def test_apply_dns_records_route53_without_a_zone_raises(opts):
    from mojo.helpers.aws import ses_domain

    raised = None
    with patch(f"{ROUTE53}.upsert_record") as upsert, \
            patch(f"{ROUTE53}.find_zone_id", return_value=None):
        try:
            ses_domain.apply_dns_records_route53(R53_DOMAIN, _records(R53_DOMAIN))
        except ValueError as err:
            raised = err

    assert raised is not None, (
        "Expected a domain with no hosted zone to raise rather than silently apply nothing")
    assert upsert.call_count == 0, (
        "Expected no record write when the hosted zone could not be resolved")


@th.django_unit_test()
def test_apply_dns_records_route53_collapses_a_shared_record_set(opts):
    """
    An UPSERT replaces the whole record set, so two records sharing a
    (type, name) pair must go out as ONE change carrying both values.
    """
    from mojo.helpers.aws.ses_domain import DnsRecord
    from mojo.helpers.aws import ses_domain

    records = [
        DnsRecord(type="TXT", name=f"_acme-challenge.{R53_DOMAIN}", value="digest-one", ttl=60),
        DnsRecord(type="TXT", name=f"_acme-challenge.{R53_DOMAIN}", value="digest-two", ttl=60),
    ]

    with patch(f"{ROUTE53}.upsert_record") as upsert:
        ses_domain.apply_dns_records_route53(R53_DOMAIN, records, zone_id=ZONE_ID)

    assert upsert.call_count == 1, (
        f"Expected one change for a shared record set, got {upsert.call_count} — "
        "separate upserts would leave only the last value")
    assert upsert.call_args.args[3] == ['"digest-one"', '"digest-two"'], (
        f"Expected both quoted values in one record set, got {upsert.call_args.args[3]}")


# ---------------------------------------------------------------------------
# provider dispatch — the caller never picks a provider
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_apply_records_dispatches_a_route53_domain_to_route53(opts):
    from mojo.apps.dnsman.models import Domain
    from mojo.apps.dnsman.services import email as dnsman_email

    domain_obj = Domain.objects.get(pk=opts.r53_domain_id)

    with patch(f"{ROUTE53}.upsert_record") as upsert, \
            patch(f"{GODADDY}.DNSManager") as manager:
        result = dnsman_email.apply_records(domain_obj, _records(R53_DOMAIN))

    assert manager.call_count == 0, (
        "A route53-backed domain must never reach the GoDaddy API")
    assert upsert.call_count == 4, (
        f"Expected every record to be upserted through Route 53, got {upsert.call_count}")
    assert upsert.call_args_list[0].args[0] == ZONE_ID, (
        "Expected the Domain's stored hosted_zone_id to be used, got "
        f"{upsert.call_args_list[0].args[0]}")
    assert result.provider == "route53", (
        f"Expected the dispatch to report the route53 provider, got {result.provider}")
    assert result.applied == 4, f"Expected 4 records applied, got {result.applied}"


@th.django_unit_test()
def test_apply_records_dispatches_a_godaddy_domain_to_godaddy(opts):
    from mojo.apps.dnsman.models import Domain
    from mojo.apps.dnsman.services import email as dnsman_email

    domain_obj = Domain.objects.get(pk=opts.gd_domain_id)
    manager_instance = MagicMock()
    manager_instance.is_domain_active.return_value = True

    with patch(f"{GODADDY}.DNSManager", return_value=manager_instance) as manager, \
            patch(f"{ROUTE53}.upsert_record") as upsert:
        result = dnsman_email.apply_records(domain_obj, _records(GD_DOMAIN))

    assert upsert.call_count == 0, (
        "A godaddy-backed domain must never write to Route 53")
    assert manager.call_count == 1, (
        f"Expected exactly one GoDaddy client to be built, got {manager.call_count}")
    assert manager.call_args.args == (GD_API_KEY, GD_API_SECRET), (
        "Expected the linked DnsCredential to supply the GoDaddy key/secret, got "
        f"{manager.call_args.args}")
    assert manager_instance.add_record.call_count == 4, (
        f"Expected every record to be applied through GoDaddy, got "
        f"{manager_instance.add_record.call_count}")
    assert result.provider == "godaddy", (
        f"Expected the dispatch to report the godaddy provider, got {result.provider}")


@th.django_unit_test()
def test_apply_records_fails_closed_without_a_verified_credential(opts):
    from mojo import errors as me
    from mojo.apps.dnsman.models import Domain
    from mojo.apps.dnsman.services import email as dnsman_email

    domain_obj = Domain.objects.get(pk=opts.gd_broken_domain_id)

    raised = None
    with patch(f"{GODADDY}.DNSManager") as manager:
        try:
            dnsman_email.apply_records(domain_obj, _records(GD_BROKEN_DOMAIN))
        except me.ValueException as err:
            raised = err

    assert raised is not None, (
        "Expected an unverified credential to be refused, not used")
    assert "credential" in str(raised).lower(), (
        f"Expected the refusal to name the credential as the fix, got {raised}")
    assert manager.call_count == 0, (
        "Expected the refusal to happen before any provider call")


# ---------------------------------------------------------------------------
# onboard_email_domain — the one-call path
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_onboard_email_domain_refuses_a_domain_dnsman_does_not_hold(opts):
    from mojo import errors as me
    from mojo.apps.dnsman.services import email as dnsman_email

    raised = None
    with patch(f"{SES}.onboard_domain") as onboard:
        try:
            dnsman_email.onboard_email_domain(opts.email_unmanaged_id)
        except me.ValueException as err:
            raised = err

    assert raised is not None, (
        f"Expected onboarding '{UNMANAGED_DOMAIN}' to be refused — dnsman does not hold it")
    assert raised.status == 404, (
        f"Expected a 404 status for a domain that is not managed here, got {raised.status}")
    assert "not managed by dnsman" in str(raised), (
        f"Expected an actionable message naming dnsman, got {raised}")
    assert onboard.call_count == 0, (
        "Expected SES to be left untouched for a domain dnsman cannot apply records to")


@th.django_unit_test()
def test_onboard_email_domain_applies_records_through_the_dispatch(opts):
    from mojo.apps.aws.models import EmailDomain
    from mojo.helpers.aws.ses_domain import OnboardResult
    from mojo.apps.dnsman.services import email as dnsman_email

    ses_result = OnboardResult(
        domain=R53_DOMAIN,
        region="us-east-1",
        verification_token="ses-verification-token",
        dkim_tokens=["tok1"],
        dns_records=_records(R53_DOMAIN),
        topic_arns={"bounce": "arn:aws:sns:us-east-1:1:bounce"},
        receipt_rule=None,
        rule_set=None,
        notes=[])

    with patch(f"{SES}.onboard_domain", return_value=ses_result) as onboard, \
            patch(f"{ROUTE53}.upsert_record") as upsert:
        result = dnsman_email.onboard_email_domain(opts.email_r53_id)

    assert onboard.call_count == 1, (
        f"Expected SES onboarding to run once, got {onboard.call_count}")
    assert onboard.call_args.kwargs.get("domain") == R53_DOMAIN, (
        f"Expected the dnsman domain name to be onboarded, got "
        f"{onboard.call_args.kwargs.get('domain')}")
    assert onboard.call_args.kwargs.get("dns_mode") == "manual", (
        "ses_domain.onboard_domain only COMPUTES records — dnsman applies them, so it "
        f"must be called in manual mode, got {onboard.call_args.kwargs.get('dns_mode')}")

    assert upsert.call_count == 4, (
        f"Expected every computed record to be applied to Route 53, got {upsert.call_count}")
    assert result.provider == "route53", (
        f"Expected the result to report the provider used, got {result.provider}")
    assert result.applied.applied == 4, (
        f"Expected 4 records reported as applied, got {result.applied.applied}")
    assert result.verification_token == "ses-verification-token", (
        f"Expected the SES verification token to be returned, got {result.verification_token}")
    assert result.dkim_tokens == ["tok1"], (
        f"Expected the DKIM tokens to be returned, got {result.dkim_tokens}")
    assert result.dnsman_domain_id == opts.r53_domain_id, (
        f"Expected the dnsman Domain id to be reported, got {result.dnsman_domain_id}")

    reloaded = EmailDomain.objects.get(pk=opts.email_r53_id)
    assert reloaded.dns_mode == "route53", (
        "After applying through Route 53 the model must describe reality — "
        f"dns_mode is still {reloaded.dns_mode}")


@th.django_unit_test()
def test_onboard_email_domain_refuses_an_inactive_domain(opts):
    from mojo import errors as me
    from mojo.apps.dnsman.models import Domain
    from mojo.apps.dnsman.services import email as dnsman_email

    domain_obj = Domain.objects.get(pk=opts.r53_domain_id)
    domain_obj.status = "pending"
    domain_obj.save(update_fields=["status", "modified"])

    raised = None
    try:
        with patch(f"{SES}.onboard_domain") as onboard:
            try:
                dnsman_email.onboard_email_domain(opts.email_r53_id)
            except me.ValueException as err:
                raised = err
        assert raised is not None, (
            "Expected onboarding to be refused while the domain is not active")
        assert "not active" in str(raised), (
            f"Expected the refusal to name the domain status, got {raised}")
        assert onboard.call_count == 0, (
            "Expected SES to be left untouched for a domain that cannot receive records")
    finally:
        domain_obj.status = "active"
        domain_obj.save(update_fields=["status", "modified"])


# ---------------------------------------------------------------------------
# EmailDomain graph — the aws_key leak
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_email_domain_default_graph_masks_the_aws_key(opts):
    """
    Regression: the default graph used to expose `aws_key` unmasked, handing the
    full AWS access key id to every caller who could read a domain.
    """
    from mojo.apps.aws.models import EmailDomain

    extra = EmailDomain.RestMeta.GRAPHS["default"].get("extra") or []
    assert "aws_key" not in extra, (
        f"The default graph must never expose the raw aws_key, found it in {extra}")
    assert "aws_key_masked" in extra, (
        f"Expected the default graph to expose aws_key_masked instead, got {extra}")

    domain = EmailDomain.objects.get(pk=opts.graph_email_domain_id)
    data = domain.to_dict(graph="default")

    assert "aws_key" not in data, (
        f"The serialized default graph must not carry an aws_key field, got {sorted(data.keys())}")
    assert "aws_key_masked" in data, (
        f"Expected aws_key_masked in the serialized default graph, got {sorted(data.keys())}")
    assert RAW_AWS_KEY not in str(data), (
        "The raw AWS access key id must not appear anywhere in the default graph")

    expected = ("*" * (len(RAW_AWS_KEY) - 4)) + RAW_AWS_KEY[-4:]
    assert data["aws_key_masked"] == expected, (
        f"Expected the key masked to its last 4 characters, got {data['aws_key_masked']}")
    assert domain.aws_key == RAW_AWS_KEY, (
        "The raw aws_key property must still work for internal callers")
