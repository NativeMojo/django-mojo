"""The deployment-wide certificate-expiry metric.

One CloudWatch datapoint — the fewest days remaining across every certificate
this deployment is responsible for — feeds a single alarm that catches every
cause of a stalled renewal at once: publisher down, challenge misrouted,
credentials wrong, delegation record deleted.

Nothing here touches AWS. `cloudwatch.put_metric_data` takes an injected client,
and these tests patch the module function outright and assert on what it was
handed.
"""

from datetime import timedelta
from unittest import mock

from testit import helpers as th


DOMAIN_NAME = "cert-metric.example"

# Far enough in the past that no certificate any other test creates can be lower,
# so assertions on the deployment-wide minimum stay exact without this module
# having to own the whole table.
SENTINEL_DAYS = -9000


def _reset():
    """
    Clear only THIS domain's certificates.

    The metric is deployment-wide, so it would be tempting to clear the whole
    table — but `edge.Vhost.certificate` is a PROTECTED foreign key, so deleting
    another module's certificates raises, and test modules run in parallel
    anyway. Instead every fixture below uses absurdly-past expiry dates
    (`SENTINEL_DAYS`), which are guaranteed to dominate the global minimum no
    matter what other rows exist. That keeps the real ORM query — including the
    status filter this module exists to pin — under test.
    """
    from mojo.apps.dnsman.models import Certificate, Domain

    Certificate.objects.filter(domain__name=DOMAIN_NAME).delete()
    Domain.objects.filter(name=DOMAIN_NAME).delete()
    return Domain.objects.create(
        name=DOMAIN_NAME, provider="route53", status="active",
        hosted_zone_id="Z-CERT-METRIC", verified=True)


def _certificate(domain, common_name, days=None, status="active"):
    """
    A certificate expiring `days` from now, or with no not_after at all.

    The 30-second cushion matters: `timedelta.days` truncates toward negative
    infinity, so a certificate created exactly `days` out reads as `days - 1`
    once any wall-clock time has passed. The cushion keeps the fixture saying
    what it means without weakening the assertions to ranges.
    """
    from mojo.apps.dnsman.models import Certificate
    from mojo.helpers import dates

    not_after = None if days is None else dates.utcnow() + timedelta(days=days, seconds=30)
    return Certificate.objects.create(
        domain=domain, common_name=common_name, sans=[common_name],
        status=status, not_after=not_after)


def _publish():
    """Run the job with put_metric_data stubbed; return the recorded calls."""
    from objict import objict

    from mojo.apps.dnsman import asyncjobs
    from mojo.helpers.aws import cloudwatch

    calls = []

    def record(namespace, metric_name, value, dimensions=None, unit="None", **kwargs):
        calls.append(dict(namespace=namespace, metric_name=metric_name,
                          value=value, dimensions=dimensions, unit=unit))

    with mock.patch.object(cloudwatch, "put_metric_data", record):
        result = asyncjobs.publish_certificate_expiry_metric(objict(payload={}))
    return calls, result


@th.django_unit_test("the published value is the soonest expiry across all certificates")
def test_publisher_emits_minimum_days_remaining(opts):
    domain = _reset()
    _certificate(domain, "a.cert-metric.example", days=SENTINEL_DAYS + 20)
    _certificate(domain, "b.cert-metric.example", days=SENTINEL_DAYS)
    _certificate(domain, "c.cert-metric.example", days=SENTINEL_DAYS + 10)

    calls, result = _publish()

    assert len(calls) == 1, f"exactly one datapoint should be published, got {calls}"
    assert calls[0]["value"] == SENTINEL_DAYS, (
        "the alarm watches the WORST certificate, so the published value must be "
        f"the minimum days remaining; got {calls[0]['value']}"
    )
    assert calls[0]["metric_name"] == "MinDaysToExpiry", \
        f"unexpected metric name {calls[0]['metric_name']}"
    assert calls[0]["namespace"] == "DjangoMojo/Certificates", \
        f"unexpected namespace {calls[0]['namespace']}"
    assert f"days={SENTINEL_DAYS}" in result, \
        f"the job should report what it published, got {result}"


@th.django_unit_test("an expired certificate that failed renewal still counts")
def test_publisher_counts_expired_failed_certificates(opts):
    """
    The regression that protects the whole control.

    `certs._record_issue_failure` moves a certificate to FAILED once its
    material is no longer valid. Filtering the publisher on ACTIVE alone would
    therefore DROP a certificate at the exact moment it expires: the published
    minimum jumps to the next-soonest certificate and CloudWatch reports the
    alarm recovered while TLS is actually broken.
    """
    domain = _reset()
    _certificate(domain, "healthy.cert-metric.example",
                 days=SENTINEL_DAYS + 500, status="active")
    _certificate(domain, "dead.cert-metric.example", days=SENTINEL_DAYS, status="failed")

    calls, _ = _publish()

    assert len(calls) == 1, f"exactly one datapoint should be published, got {calls}"
    assert calls[0]["value"] == SENTINEL_DAYS, (
        "an expired certificate whose renewal failed must keep pinning the metric; "
        f"publishing {SENTINEL_DAYS + 500} here would send an OK notification at the "
        f"moment TLS broke, got {calls[0]['value']}"
    )


@th.django_unit_test("an empty result publishes nothing rather than a healthy sentinel")
def test_publisher_skips_when_no_certificates(opts):
    """
    Driven through the aggregate rather than an empty table.

    Emptying the Certificate table is not available: `edge.Vhost.certificate` is
    a PROTECTED foreign key, and other modules hold rows. Patching the aggregate
    exercises the same branch — what the job does when nothing matches.
    """
    from mojo.apps.dnsman.models import Certificate

    _reset()
    with mock.patch.object(Certificate.objects, "filter") as scoped:
        scoped.return_value.aggregate.return_value = {"soonest": None}
        calls, result = _publish()

    assert calls == [], (
        "a deployment with no certificates must look un-set-up (the alarm sits in "
        f"INSUFFICIENT_DATA), not healthy; got {calls}"
    )
    assert "published=0" in result, f"the job should say it published nothing, got {result}"


@th.django_unit_test("pending, issuing and revoked certificates are excluded")
def test_publisher_excludes_null_not_after_and_revoked(opts):
    domain = _reset()
    _certificate(domain, "live.cert-metric.example",
                 days=SENTINEL_DAYS + 500, status="active")
    _certificate(domain, "pending.cert-metric.example", days=None, status="pending")
    _certificate(domain, "issuing.cert-metric.example", days=None, status="issuing")
    _certificate(domain, "revoked.cert-metric.example", days=SENTINEL_DAYS, status="revoked")

    calls, _ = _publish()

    assert len(calls) == 1, f"exactly one datapoint should be published, got {calls}"
    assert calls[0]["value"] == SENTINEL_DAYS + 500, (
        "a revoked certificate is deliberately retired and must not pin the metric, "
        "and pending/issuing rows have no not_after to contribute; "
        f"got {calls[0]['value']}"
    )


@th.django_unit_test("the publisher and the alarm agree on the deployment dimension")
def test_publisher_and_alarm_agree_on_dimension(opts):
    """
    CloudWatch treats the dimension set as part of a metric's identity, so a
    publisher and an alarm that disagree produce an alarm watching a metric
    nobody writes — green forever, and invisible.
    """
    from mojo.apps.aws.services import aws_check

    domain = _reset()
    _certificate(domain, "dim.cert-metric.example", days=SENTINEL_DAYS)

    calls, _ = _publish()
    published = calls[0]["dimensions"]

    cloudwatch = mock.Mock()
    cloudwatch.list_metrics.return_value = {"Metrics": [{"MetricName": "MinDaysToExpiry"}]}
    alarms = aws_check.AWSCheckRunner()._desired_deployment_alarms(cloudwatch)

    assert len(alarms) == 1, f"one deployment-wide alarm should be desired, got {alarms}"
    assert alarms[0]["Dimensions"] == published, (
        "the alarm must watch exactly the dimension set the publisher writes; "
        f"alarm={alarms[0]['Dimensions']} published={published}"
    )
    assert alarms[0]["Namespace"] == calls[0]["namespace"], \
        "publisher and alarm must share a namespace"
    assert alarms[0]["MetricName"] == calls[0]["metric_name"], \
        "publisher and alarm must share a metric name"
