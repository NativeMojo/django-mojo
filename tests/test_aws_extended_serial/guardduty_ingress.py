"""Split out of tests/test_aws/guardduty_ingress.py (maestro #1839).

These tests mutate django.conf.settings (the GuardDuty/CloudWatch topic
allowlists) and patch the shared mojo.apps.incident TicketHandler — both
process-global, so unsafe under the parallel default tier.
"""
import base64
import json
from datetime import timedelta
from unittest import mock

from testit import helpers as th


TOPIC = "arn:aws:sns:us-east-1:123456789012:guardduty"


FINDING_ID = "abcdef0123456789abcdef0123456789"


DETECTOR_ID = "0123456789abcdef0123456789abcdef"


ACCOUNT = "123456789012"


REGION = "us-east-1"


FINDING_TYPE = "UnauthorizedAccess:EC2/SSHBruteForce"


SCOPE = "aws:guardduty"


REMOTE_IP = "198.51.100.7"


def _inbound_action(direction="INBOUND"):
    return {
        "actionType": "NETWORK_CONNECTION",
        "networkConnectionAction": {
            "connectionDirection": direction,
            "protocol": "TCP",
            "blocked": False,
            "localPortDetails": {"port": 22, "portName": "SSH"},
            "remotePortDetails": {"port": 41234},
            "remoteIpDetails": {
                "ipAddressV4": REMOTE_IP,
                "country": {"countryName": "Elbonia"},
            },
        },
    }


def _resource():
    return {
        "resourceType": "Instance",
        "instanceDetails": {
            "instanceId": "i-0abc1234def567890",
            "instanceType": "t3.micro",
            "tags": [{"key": "Name", "value": "web-1"}],
        },
    }


def _finding(updated, severity=8.0, finding_id=FINDING_ID, resource=None,
             action=None, extras=None, detector_id=DETECTOR_ID):
    from django.utils import timezone

    detail = {
        "id": finding_id,
        "type": FINDING_TYPE,
        "severity": severity,
        "title": "SSH brute force attack against an EC2 instance",
        "description": f"{REMOTE_IP} is performing SSH brute force attacks",
        "accountId": ACCOUNT,
        "region": REGION,
        "createdAt": timezone.now().isoformat(),
        "updatedAt": updated.isoformat(),
        "resource": _resource() if resource is None else resource,
        "service": {
            "detectorId": detector_id,
            "count": 3,
            "action": _inbound_action() if action is None else action,
        },
    }
    detail.update(extras or {})
    return detail


def _eventbridge(detail, source="aws.guardduty", detail_type="GuardDuty Finding"):
    from django.utils import timezone

    return {
        "version": "0",
        "id": "3f4c1d2e-0000-4000-8000-abcdefabcdef",
        "detail-type": detail_type,
        "source": source,
        "account": ACCOUNT,
        "time": timezone.now().isoformat(),
        "region": REGION,
        "resources": [],
        "detail": detail,
    }


def _envelope(message_id, message, topic=TOPIC):
    from django.utils import timezone

    return {
        "Type": "Notification",
        "MessageId": message_id,
        "TopicArn": topic,
        "Message": json.dumps(message),
        "Timestamp": timezone.now().isoformat(),
        "SignatureVersion": "2",
        "SigningCertURL": (
            "https://sns.us-east-1.amazonaws.com/"
            "SimpleNotificationService-test.pem"
        ),
        "Signature": base64.b64encode(b"invalid until signed").decode("ascii"),
    }


def _sign(envelope, key):
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding
    from mojo.apps.aws.services import sns

    envelope["Signature"] = base64.b64encode(
        key.sign(sns._canonical(envelope), padding.PKCS1v15(), hashes.SHA256())
    ).decode("ascii")
    return envelope


class _Certificate:
    def __init__(self, public_key):
        self._public_key = public_key

    def public_key(self):
        return self._public_key


def _clear_state():
    from mojo.apps.aws.models import GuardDutyFinding
    from mojo.apps.incident.models import (
        Event, Incident, MaestroItemLink, RuleSet, Ticket,
    )
    from mojo.apps.jobs.models import Job

    Job.objects.filter(idempotency_key__startswith="aws-gd:").delete()
    MaestroItemLink.objects.filter(ticket__category="guardduty-test").delete()
    Ticket.objects.filter(category="guardduty-test").delete()
    GuardDutyFinding.objects.all().delete()
    Event.objects.filter(scope=SCOPE).delete()
    Incident.objects.filter(scope=SCOPE).delete()
    RuleSet.objects.filter(category=SCOPE).delete()


def _events():
    from mojo.apps.incident.models import Event
    return Event.objects.filter(scope=SCOPE).order_by("id")


def _incidents():
    from mojo.apps.incident.models import Incident
    return Incident.objects.filter(scope=SCOPE).order_by("id")


def _handler_jobs():
    from mojo.apps.jobs.models import Job
    return Job.objects.filter(idempotency_key__startswith="aws-gd:")


def _ticket_policy():
    """A RuleSet with no conditions, so every GuardDuty level reaches it."""
    from mojo.apps.incident.models import BundleBy, RuleSet
    return RuleSet.objects.create(
        category=SCOPE,
        name="GuardDuty test policy",
        bundle_by=BundleBy.MODEL_NAME_AND_ID,
        bundle_minutes=None,
        handler="ticket://?category=guardduty-test&board=17",
    )


@th.django_unit_setup()
def setup_guardduty_ingress_extended(opts):
    _clear_state()


@th.django_unit_test()
def test_signature_and_allowlist_are_isolated_from_cloudwatch(opts):
    from cryptography.hazmat.primitives.asymmetric import rsa
    from django.conf import settings as django_settings
    from django.test import RequestFactory
    from django.utils import timezone
    from mojo.apps.aws.rest.sns import on_guardduty_finding
    from mojo.apps.aws.services import sns

    _clear_state()
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    envelope = _sign(
        _envelope("gd-valid", _eventbridge(_finding(timezone.now()))), key,
    )

    def _post(payload):
        return RequestFactory().generic(
            "POST", "/api/aws/guardduty/sns/finding",
            data=json.dumps(payload), content_type="text/plain",
        )

    sentinel = object()
    gd_original = getattr(
        django_settings, "AWS_GUARDDUTY_FINDING_TOPIC_ARNS", sentinel,
    )
    cw_original = getattr(
        django_settings, "AWS_CLOUDWATCH_ALARM_TOPIC_ARNS", sentinel,
    )
    try:
        django_settings.AWS_GUARDDUTY_FINDING_TOPIC_ARNS = [TOPIC]
        django_settings.AWS_CLOUDWATCH_ALARM_TOPIC_ARNS = []
        with mock.patch.object(
            sns, "_certificate", return_value=_Certificate(key.public_key()),
        ):
            accepted = on_guardduty_finding(_post(envelope))
        assert accepted.status_code == 200, (
            "A signed notification from an allowlisted topic must be accepted, "
            f"got {accepted.status_code}"
        )

        django_settings.AWS_GUARDDUTY_FINDING_TOPIC_ARNS = []
        with mock.patch.object(sns, "_certificate") as certificate:
            denied = on_guardduty_finding(_post(envelope))
        assert denied.status_code == 403, (
            "An empty GuardDuty topic allowlist must fail closed, got "
            f"{denied.status_code}"
        )
        assert certificate.called is False, (
            "A denied topic must be rejected before any certificate I/O"
        )

        django_settings.AWS_CLOUDWATCH_ALARM_TOPIC_ARNS = [TOPIC]
        with mock.patch.object(
            sns, "_certificate", return_value=_Certificate(key.public_key()),
        ):
            cross = on_guardduty_finding(_post(envelope))
        assert cross.status_code == 403, (
            "A topic allowlisted only for CloudWatch alarms must NOT be able "
            "to deliver findings (or confirm a subscription) on the GuardDuty "
            f"receiver; the two allowlists are independent. Got {cross.status_code}"
        )

        django_settings.AWS_GUARDDUTY_FINDING_TOPIC_ARNS = [TOPIC]
        tampered = dict(envelope)
        tampered["MessageId"] = "gd-tampered"
        with mock.patch.object(
            sns, "_certificate", return_value=_Certificate(key.public_key()),
        ):
            forged = on_guardduty_finding(_post(tampered))
        assert forged.status_code == 403, (
            "A signed envelope whose MessageId was altered after signing must "
            f"be rejected, got {forged.status_code}"
        )
    finally:
        for name, original in (
            ("AWS_GUARDDUTY_FINDING_TOPIC_ARNS", gd_original),
            ("AWS_CLOUDWATCH_ALARM_TOPIC_ARNS", cw_original),
        ):
            if original is sentinel:
                if hasattr(django_settings, name):
                    delattr(django_settings, name)
            else:
                setattr(django_settings, name, original)


@th.django_unit_test()
def test_each_occurrence_publishes_its_own_handler_job(opts):
    from django.utils import timezone
    from mojo.apps.aws.models import GuardDutyFinding
    from mojo.apps.aws.services.guardduty_findings import process_notification
    from mojo.apps.incident.handlers.event_handlers import execute_handler
    from mojo.apps.incident.services.lifecycle import resolve_incident
    from mojo.apps.incident.models import Ticket

    _clear_state()
    _ticket_policy()
    started = timezone.now()
    process_notification(_envelope("gd-occ-1", _eventbridge(_finding(started))))

    finding = GuardDutyFinding.objects.get()
    first_event = _events().get()
    first_job = _handler_jobs().get()
    assert first_job.idempotency_key.startswith(
        f"aws-gd:{finding.pk}:{first_event.pk}"
    ), (
        f"Occurrence 1 must be keyed aws-gd:{finding.pk}:{first_event.pk}, got "
        f"{first_job.idempotency_key}"
    )

    with mock.patch(
        "mojo.apps.incident.handlers.event_handlers.TicketHandler._push_to_maestro"
    ) as push:
        execute_handler(first_job)
    ticket = Ticket.objects.get(category="guardduty-test")
    assert ticket.incident_id == finding.active_incident_id, (
        "The configured ticket handler must link human work to the finding's incident"
    )
    assert push.call_count == 1, "Board routing must occur only through TicketHandler"

    resolve_incident(
        _incidents().first(), note="operator closed it", kind="guardduty:test",
    )
    process_notification(
        _envelope("gd-occ-2", _eventbridge(_finding(started + timedelta(hours=2))))
    )

    finding.refresh_from_db()
    second_event = _events().order_by("id").last()
    assert _handler_jobs().count() == 2, (
        "A NEW occurrence must publish its OWN handler job. Job.idempotency_key "
        "is globally unique and jobs.publish returns the pre-existing job on "
        "collision, so a finding-pk-only prefix would silently publish nothing "
        f"here. Got {_handler_jobs().count()} job(s)."
    )
    assert _handler_jobs().filter(
        idempotency_key__startswith=f"aws-gd:{finding.pk}:{second_event.pk}",
    ).exists(), (
        f"Occurrence 2 must be keyed aws-gd:{finding.pk}:{second_event.pk}"
    )

