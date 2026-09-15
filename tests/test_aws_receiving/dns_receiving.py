"""SES receiving DNS regressions; all provider calls are mocked."""

TESTIT_TIER = "bug"

from types import SimpleNamespace
from unittest.mock import patch

from testit import helpers as th


SES = "mojo.helpers.aws.ses_domain"
GODADDY = "mojo.helpers.dns.godaddy"
EMAIL_OPS = "mojo.apps.aws.services.email_ops"
DNS_EMAIL = "mojo.apps.dnsman.services.email"
DOMAIN = "ses-receiving-dns.example.com"
REGION = "us-west-2"


def _email_domain(mode="manual", receiving=True):
    return SimpleNamespace(
        name=DOMAIN, region=REGION, receiving_enabled=receiving,
        s3_inbound_bucket="ses-receiving-test", s3_inbound_prefix="mail/",
        dns_mode=mode, aws_key="", aws_secret="", metadata={})


def _reconcile_result():
    return SimpleNamespace(
        topic_arns={}, receipt_rule="catchall", rule_set="receiving", notes=[])


@th.django_unit_test()
def test_receiving_onboard_adds_apex_mx_without_changing_sending_records(opts):
    from mojo.helpers.aws import ses_domain

    results = []
    with patch(f"{SES}._request_ses_verification_and_dkim", return_value=("verify", ["dkim"])), \
            patch(f"{SES}.reconcile_domain_config", return_value=_reconcile_result()):
        for receiving in (False, True):
            results.append(ses_domain.onboard_domain(
                DOMAIN, region=REGION, receiving_enabled=receiving,
                s3_bucket="ses-receiving-test", ensure_mail_from=True, ttl=900))

    sending, receiving = results
    assert receiving.dns_records[:-1] == sending.dns_records, (
        "Enabling receiving must preserve verification, DKIM and MAIL FROM records")
    assert receiving.dns_records[-1] == ses_domain.DnsRecord(
        "MX", DOMAIN, f"10 inbound-smtp.{REGION}.amazonaws.com", 900), (
        "Receiving must add the apex inbound MX in the configured SES region")
    assert all(r.name != DOMAIN for r in sending.dns_records), (
        "Sending-only onboarding must not change apex MX routing")


@th.django_unit_test()
def test_godaddy_receiving_mx_priority_and_complete_record_sets(opts):
    from mojo.helpers.aws import ses_domain

    records = [
        ses_domain.DnsRecord("MX", DOMAIN, f"10 inbound-smtp.{REGION}.amazonaws.com"),
        ses_domain.DnsRecord("MX", DOMAIN + ".", "20 backup.example.com", 900),
        ses_domain.DnsRecord("MX", f"feedback.{DOMAIN}",
                             f"10 feedback-smtp.{REGION}.amazonses.com"),
        ses_domain.DnsRecord("TXT", f"_amazonses.{DOMAIN}", "first"),
        ses_domain.DnsRecord("TXT", f"_amazonses.{DOMAIN}", "second"),
    ]
    with patch(f"{GODADDY}.DNSManager") as factory:
        manager = factory.return_value
        manager.is_domain_active.return_value = True
        result = ses_domain.apply_dns_records_godaddy(DOMAIN, records, "key", "secret")

    assert result is True, "A successful GoDaddy apply must retain its boolean result"
    assert factory.call_args.kwargs == {"raise_on_error": True}, (
        "DNS writes must raise provider HTTP errors instead of reporting success")
    calls = {
        (call.kwargs["record_type"], call.kwargs["name"]): call.kwargs["entries"]
        for call in manager.put_records.call_args_list
    }
    assert len(calls) == manager.put_records.call_count == 3, (
        "One GoDaddy PUT must carry every value for each complete record set")
    assert calls[("MX", "@")] == [
        {"data": f"inbound-smtp.{REGION}.amazonaws.com", "priority": 10, "ttl": 600},
        {"data": "backup.example.com", "priority": 20, "ttl": 900},
    ], "GoDaddy apex MX needs '@' and a separate integer priority per value"
    assert calls[("MX", "feedback")] == [
        {"data": f"feedback-smtp.{REGION}.amazonses.com", "priority": 10, "ttl": 600},
    ], "MAIL FROM must retain its separate feedback MX record"
    assert calls[("TXT", "_amazonses")] == [
        {"data": "first", "ttl": 600}, {"data": "second", "ttl": 600},
    ], "Grouped TXT records must preserve all values without adding MX fields"


@th.django_unit_test()
def test_godaddy_receiving_dns_provider_failure_is_propagated(opts):
    from requests.exceptions import HTTPError
    from mojo.helpers.aws import ses_domain

    error = HTTPError("DNS write rejected")
    with patch(f"{GODADDY}.DNSManager") as factory:
        factory.return_value.is_domain_active.return_value = True
        factory.return_value.put_records.side_effect = error
        try:
            ses_domain.apply_dns_records_godaddy(
                DOMAIN, [ses_domain.DnsRecord("MX", DOMAIN, "10 mx.example.com")],
                "key", "secret")
        except HTTPError as exc:
            assert exc is error, "The DNS provider failure must reach the caller"
        else:
            raise AssertionError("A failed DNS PUT must not report successful receiving setup")


@th.django_unit_test()
def test_godaddy_invalid_mx_fails_before_any_record_write(opts):
    from mojo.helpers.aws import ses_domain

    with patch(f"{GODADDY}.DNSManager") as factory:
        for value in ("mx.example.com", "invalid mx.example.com", "65536 mx.example.com"):
            records = [
                ses_domain.DnsRecord("TXT", f"_amazonses.{DOMAIN}", "verify"),
                ses_domain.DnsRecord("MX", DOMAIN, value),
            ]
            try:
                ses_domain.apply_dns_records_godaddy(DOMAIN, records, "key", "secret")
            except ValueError:
                pass
            else:
                raise AssertionError(f"Malformed MX must be rejected: {value}")
        assert factory.call_count == 0, "All MX input must validate before any provider call"


@th.django_unit_test()
def test_reconcile_receiving_dispatches_only_apex_mx_for_managed_dns(opts):
    from mojo.apps.aws.services import email_ops

    for provider in ("route53", "godaddy"):
        dns_domain = SimpleNamespace(provider=provider, is_active=True)
        with patch(f"{EMAIL_OPS}._get_domain", return_value=_email_domain(provider)), \
                patch(f"{EMAIL_OPS}.reconcile_domain_config", return_value=_reconcile_result()), \
                patch(f"{DNS_EMAIL}.require_domain", return_value=dns_domain) as require, \
                patch(f"{DNS_EMAIL}.apply_records", return_value=SimpleNamespace(provider=provider)) as apply:
            result = email_ops.reconcile_email_domain(1)

        assert require.call_args.args == (DOMAIN,), "Managed receiving must resolve its held domain"
        assert apply.call_count == 1, "Receiving reconcile must apply DNS exactly once"
        applied_domain, records = apply.call_args.args
        assert applied_domain is dns_domain, "DNS dispatch must use the domain's actual provider"
        assert [(r.type, r.name, r.value) for r in records] == [
            ("MX", DOMAIN, f"10 inbound-smtp.{REGION}.amazonaws.com")
        ], "Reconcile must only update receiving MX, preserving sending DNS records"
        assert any(provider in note and "receiving MX" in note for note in result.notes), (
            "The existing notes response must name the provider that applied receiving MX")


@th.django_unit_test()
def test_reconcile_manual_receiving_returns_exact_dns_instruction(opts):
    from mojo.apps.aws.services import email_ops

    with patch(f"{EMAIL_OPS}._get_domain", return_value=_email_domain()), \
            patch(f"{EMAIL_OPS}.reconcile_domain_config", return_value=_reconcile_result()), \
            patch(f"{DNS_EMAIL}.require_domain") as require, \
            patch(f"{DNS_EMAIL}.apply_records") as apply:
        result = email_ops.reconcile_email_domain(1)

    assert require.call_count == apply.call_count == 0, "Manual mode must leave DNS under operator control"
    assert result.notes == [
        f"Apply receiving DNS manually: MX {DOMAIN} = 10 inbound-smtp.{REGION}.amazonaws.com (TTL 600)"
    ], "Manual receiving needs an actionable record type, owner, priority, destination and TTL"


@th.django_unit_test()
def test_reconcile_sending_only_does_not_change_receiving_dns(opts):
    from mojo.apps.aws.services import email_ops

    with patch(f"{EMAIL_OPS}._get_domain", return_value=_email_domain("route53", receiving=False)), \
            patch(f"{EMAIL_OPS}.reconcile_domain_config", return_value=_reconcile_result()), \
            patch(f"{DNS_EMAIL}.require_domain") as require, \
            patch(f"{DNS_EMAIL}.apply_records") as apply:
        result = email_ops.reconcile_email_domain(1)

    assert require.call_count == apply.call_count == 0, "Sending-only reconcile must not touch apex MX"
    assert result.notes == [], "Sending-only reconcile must not tell the operator to change receiving DNS"


@th.django_unit_test()
def test_reconcile_managed_dns_failure_never_becomes_manual_success(opts):
    from mojo.apps.aws.services import email_ops

    for failure in ("missing", "inactive", "provider"):
        dns_domain = SimpleNamespace(provider="route53", is_active=failure != "inactive")
        with patch(f"{EMAIL_OPS}._get_domain", return_value=_email_domain("route53")), \
                patch(f"{EMAIL_OPS}.reconcile_domain_config", return_value=_reconcile_result()), \
                patch(f"{DNS_EMAIL}.require_domain", return_value=dns_domain) as require, \
                patch(f"{DNS_EMAIL}.apply_records") as apply:
            if failure == "missing":
                require.side_effect = ValueError("Register or adopt the domain in dnsman")
            elif failure == "provider":
                apply.side_effect = ValueError("DNS provider rejected receiving MX")
            try:
                email_ops.reconcile_email_domain(1)
            except (ValueError, email_ops.InvalidConfiguration):
                pass
            else:
                raise AssertionError(f"Managed DNS {failure} must fail receiving reconciliation")
            if failure != "provider":
                assert apply.call_count == 0, "Missing or inactive managed domains must never write DNS"
