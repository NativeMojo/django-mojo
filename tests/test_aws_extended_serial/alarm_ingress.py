"""Split out of tests/test_aws/alarm_ingress.py (maestro #1839).

These tests mutate django.conf.settings (AWS_CLOUDWATCH_ALARM_TOPIC_ARNS),
patch shared mojo.apps.incident surfaces (TicketHandler._push_to_maestro,
maestro_sync), and their setup resets the protected MONITORING_TOPICS Setting
row — all process-global, so unsafe under the parallel default tier.
"""
import base64
import json
from datetime import timedelta
from unittest import mock
from urllib.parse import urlencode

from testit import helpers as th


TOPIC = "arn:aws:sns:us-east-1:123456789012:operations"


ALARM_ARN = "arn:aws:cloudwatch:us-east-1:123456789012:alarm:api-errors"


def _payload(state, old_state, when, reason=None, extras=None):
    data = {
        "AlarmName": "api-errors",
        "AlarmArn": ALARM_ARN,
        "AWSAccountId": "123456789012",
        "NewStateValue": state,
        "OldStateValue": old_state,
        "NewStateReason": reason or f"state changed to {state}",
        "StateChangeTime": when.isoformat(),
        "Region": "US East (N. Virginia)",
        "Trigger": {
            "Namespace": "AWS/ApplicationELB",
            "MetricName": "HTTPCode_Target_5XX_Count",
            "Dimensions": [
                {"name": "LoadBalancer", "value": "app/example/123"},
            ],
        },
    }
    data.update(extras or {})
    return data


def _envelope(message_id, payload, topic=TOPIC):
    return {
        "Type": "Notification",
        "MessageId": message_id,
        "TopicArn": topic,
        "Message": json.dumps(payload),
        "Timestamp": payload["StateChangeTime"],
        "SignatureVersion": "2",
        "SigningCertURL": (
            "https://sns.us-east-1.amazonaws.com/"
            "SimpleNotificationService-test.pem"
        ),
        "Signature": base64.b64encode(b"invalid until signed").decode("ascii"),
    }


def _confirmation(message_id, token="confirm-token", topic=TOPIC):
    """A SubscriptionConfirmation envelope shaped exactly as AWS SNS sends it."""
    from django.utils import timezone
    from urllib.parse import urlencode

    query = urlencode({
        "Action": "ConfirmSubscription",
        "TopicArn": topic,
        "Token": token,
    })
    return {
        "Type": "SubscriptionConfirmation",
        "MessageId": message_id,
        "TopicArn": topic,
        "Token": token,
        "Message": "You have chosen to subscribe to the topic",
        "SubscribeURL": f"https://sns.us-east-1.amazonaws.com/?{query}",
        "Timestamp": timezone.now().isoformat(),
        "SignatureVersion": "2",
        "SigningCertURL": (
            "https://sns.us-east-1.amazonaws.com/"
            "SimpleNotificationService-test.pem"
        ),
        "Signature": base64.b64encode(b"invalid until signed").decode("ascii"),
    }


def _sign(envelope, key):
    """Sign an envelope in place with the SNS SignatureVersion 2 canonical form."""
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding
    from mojo.apps.aws.services import sns

    envelope["Signature"] = base64.b64encode(
        key.sign(sns._canonical(envelope), padding.PKCS1v15(), hashes.SHA256())
    ).decode("ascii")
    return envelope


class _Certificate:
    """Stand-in for the fetched x509 certificate, exposing only public_key()."""

    def __init__(self, public_key):
        self._public_key = public_key

    def public_key(self):
        return self._public_key


def _clear_state():
    from mojo.apps.aws.models import CloudWatchAlarm, CloudWatchAlarmTransition
    from mojo.apps.incident.models import Event, Incident, MaestroItemLink, RuleSet, Ticket
    from mojo.apps.jobs.models import Job

    Job.objects.filter(idempotency_key__startswith="aws-cw:").delete()
    MaestroItemLink.objects.filter(ticket__category="cloudwatch-test").delete()
    Ticket.objects.filter(category="cloudwatch-test").delete()
    CloudWatchAlarmTransition.objects.all().delete()
    CloudWatchAlarm.objects.all().delete()
    Event.objects.filter(category="aws:cloudwatch:alarm").delete()
    Incident.objects.filter(category="aws:cloudwatch:alarm").delete()
    RuleSet.objects.filter(category__in=["aws:cloudwatch", "aws:cloudwatch:alarm"]).delete()


@th.django_unit_setup()
def setup_alarm_ingress_extended(opts):
    _clear_state()
    # AWS setup tests persist the protected runtime allowlist by design.  This
    # module owns its receiver fixtures, so remove both the DB row and its Redis
    # cache entry before testing the file-settings compatibility path.
    from mojo.apps.account.models import Setting
    from mojo.apps.account.services import system_settings
    row = Setting.objects.filter(
        key=system_settings.MONITORING_TOPICS, group=None).first()
    if row is not None:
        Setting.objects.filter(pk=row.pk).delete()
    redis = Setting._redis()
    if redis:
        redis.hdel(Setting._redis_key(None), system_settings.MONITORING_TOPICS)


@th.django_unit_test()
def test_public_receiver_requires_signature_and_static_allowlist(opts):
    from cryptography.hazmat.primitives.asymmetric import rsa, padding
    from cryptography.hazmat.primitives import hashes
    from django.conf import settings as django_settings
    from django.test import RequestFactory
    from django.utils import timezone
    from mojo.apps.aws.rest.sns import on_cloudwatch_alarm
    from mojo.apps.aws.services import sns

    class Certificate:
        def __init__(self, public_key):
            self._public_key = public_key

        def public_key(self):
            return self._public_key

    _clear_state()
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    envelope = _envelope("receiver-valid", _payload("ALARM", "OK", timezone.now()))
    envelope["Signature"] = base64.b64encode(
        key.sign(sns._canonical(envelope), padding.PKCS1v15(), hashes.SHA256())
    ).decode("ascii")
    sentinel = object()
    original = getattr(django_settings, "AWS_CLOUDWATCH_ALARM_TOPIC_ARNS", sentinel)
    try:
        django_settings.AWS_CLOUDWATCH_ALARM_TOPIC_ARNS = [TOPIC]
        request = RequestFactory().generic(
            "POST", "/api/aws/cloudwatch/sns/alarm",
            data=json.dumps(envelope), content_type="text/plain",
            HTTP_X_AMZ_SNS_MESSAGE_TYPE="Notification",
            HTTP_X_AMZ_SNS_MESSAGE_ID="receiver-valid",
            HTTP_X_AMZ_SNS_TOPIC_ARN=TOPIC,
        )
        with mock.patch.object(sns, "_certificate", return_value=Certificate(key.public_key())):
            response = on_cloudwatch_alarm(request)
        assert response.status_code == 200, \
            f"A valid signed allowlisted notification must succeed, got {response.status_code}"

        django_settings.AWS_CLOUDWATCH_ALARM_TOPIC_ARNS = []
        with mock.patch.object(sns, "_certificate") as certificate:
            denied = on_cloudwatch_alarm(request)
        assert denied.status_code == 403, "An empty static topic allowlist must fail closed"
        assert not certificate.called, "A denied topic must not trigger certificate I/O"

        django_settings.AWS_CLOUDWATCH_ALARM_TOPIC_ARNS = [TOPIC]
        bad = dict(envelope)
        bad["MessageId"] = "receiver-invalid"
        bad_request = RequestFactory().generic(
            "POST", "/api/aws/cloudwatch/sns/alarm",
            data=json.dumps(bad), content_type="text/plain",
        )
        with mock.patch.object(sns, "_certificate", return_value=Certificate(key.public_key())):
            invalid = on_cloudwatch_alarm(bad_request)
        assert invalid.status_code == 403, "A tampered signed envelope must be rejected"
    finally:
        if original is sentinel:
            delattr(django_settings, "AWS_CLOUDWATCH_ALARM_TOPIC_ARNS")
        else:
            django_settings.AWS_CLOUDWATCH_ALARM_TOPIC_ARNS = original


@th.django_unit_test()
def test_signed_sns_preamble_covers_every_exit(opts):
    """Pin every status code and body the shared SNS preamble can produce.

    The preamble in front of both public SNS receivers is extracted into
    ``_receive_signed_sns``. Only the 200/403 paths used to be asserted, so a
    refactor could silently change the method check, the payload rejection, the
    transient-failure code, the subscription-confirmation body, or the
    unsupported-type rejection on an unauthenticated public endpoint.
    """
    from cryptography.hazmat.primitives.asymmetric import rsa
    from django.conf import settings as django_settings
    from django.test import RequestFactory
    from django.utils import timezone
    from mojo.apps.aws.rest.sns import on_cloudwatch_alarm
    from mojo.apps.aws.services import sns

    _clear_state()
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    def _post(envelope):
        return RequestFactory().generic(
            "POST", "/api/aws/cloudwatch/sns/alarm",
            data=json.dumps(envelope), content_type="text/plain",
        )

    sentinel = object()
    original = getattr(django_settings, "AWS_CLOUDWATCH_ALARM_TOPIC_ARNS", sentinel)
    try:
        django_settings.AWS_CLOUDWATCH_ALARM_TOPIC_ARNS = [TOPIC]

        get_request = RequestFactory().get("/api/aws/cloudwatch/sns/alarm")
        with mock.patch.object(sns, "parse_request") as parse_request:
            not_allowed = on_cloudwatch_alarm(get_request)
        assert not_allowed.status_code == 405, \
            f"A non-POST delivery must be refused with 405, got {not_allowed.status_code}"
        assert not parse_request.called, \
            "The method check must run before the envelope is parsed"

        unparseable = RequestFactory().generic(
            "POST", "/api/aws/cloudwatch/sns/alarm",
            data=b"this is not json", content_type="text/plain",
        )
        malformed = on_cloudwatch_alarm(unparseable)
        assert malformed.status_code == 400, \
            f"An unparseable SNS envelope must return 400, got {malformed.status_code}"

        notification = _sign(
            _envelope("preamble-transient", _payload("ALARM", "OK", timezone.now())),
            key,
        )
        with mock.patch.object(
            sns, "_certificate", side_effect=sns.SNSTransientError("cert fetch down"),
        ):
            transient = on_cloudwatch_alarm(_post(notification))
        assert transient.status_code == 503, \
            f"A certificate-fetch outage must return a retryable 503, got {transient.status_code}"

        confirmation = _sign(_confirmation("preamble-confirm"), key)
        with mock.patch.object(
            sns, "_certificate", return_value=_Certificate(key.public_key()),
        ), mock.patch.object(
            sns.requests, "get", return_value=mock.Mock(status_code=200),
        ):
            confirmed = on_cloudwatch_alarm(_post(confirmation))
        assert confirmed.status_code == 200, \
            f"A signed allowlisted SubscriptionConfirmation must succeed, got {confirmed.status_code}"
        body = json.loads(confirmed.content)
        assert body.get("status") is True, \
            f"The confirmation body must report status=True, got {body!r}"
        assert body.get("data") == {"confirmed": True, "status_code": 200}, \
            f"The confirmation body shape must stay exactly as AWS operators see it, got {body!r}"

        with mock.patch.object(
            sns, "_certificate", return_value=_Certificate(key.public_key()),
        ), mock.patch.object(
            sns.requests, "get", side_effect=OSError("network down"),
        ):
            confirm_transient = on_cloudwatch_alarm(_post(confirmation))
        assert confirm_transient.status_code == 503, \
            f"A failed confirmation callback must return 503, got {confirm_transient.status_code}"

        forged = _sign(
            dict(
                _confirmation("preamble-forged"),
                SubscribeURL=(
                    "https://example.com/?Action=ConfirmSubscription"
                    f"&TopicArn={TOPIC}&Token=confirm-token"
                ),
            ),
            key,
        )
        with mock.patch.object(
            sns, "_certificate", return_value=_Certificate(key.public_key()),
        ), mock.patch.object(sns.requests, "get") as never_called:
            off_host = on_cloudwatch_alarm(_post(forged))
        assert off_host.status_code == 403, \
            f"A confirmation URL off the AWS SNS host must be refused with 403, got {off_host.status_code}"
        assert not never_called.called, \
            "A refused confirmation URL must never be fetched"

        unsubscribe = _sign(
            dict(_confirmation("preamble-unsubscribe"), Type="UnsubscribeConfirmation"),
            key,
        )
        with mock.patch.object(
            sns, "_certificate", return_value=_Certificate(key.public_key()),
        ):
            unsupported = on_cloudwatch_alarm(_post(unsubscribe))
        assert unsupported.status_code == 400, \
            f"An SNS type this receiver does not handle must return 400, got {unsupported.status_code}"
    finally:
        if original is sentinel:
            delattr(django_settings, "AWS_CLOUDWATCH_ALARM_TOPIC_ARNS")
        else:
            django_settings.AWS_CLOUDWATCH_ALARM_TOPIC_ARNS = original


@th.django_unit_test()
def test_alarm_replay_recovery_and_new_occurrence(opts):
    from django.utils import timezone
    from mojo.apps.aws.models import CloudWatchAlarm, CloudWatchAlarmTransition
    from mojo.apps.aws.services.cloudwatch_alarms import process_notification
    from mojo.apps.incident.models import BundleBy, Event, Incident, RuleSet, Ticket
    from mojo.apps.incident.handlers.event_handlers import execute_handler
    from mojo.apps.jobs.models import Job

    _clear_state()
    RuleSet.objects.create(
        category="aws:cloudwatch",
        name="CloudWatch test policy",
        bundle_by=BundleBy.MODEL_NAME_AND_ID,
        bundle_minutes=None,
        handler="ticket://?category=cloudwatch-test&board=17",
    )
    started = timezone.now()
    first = process_notification(
        _envelope("alarm-1", _payload("ALARM", "OK", started))
    )
    replay = process_notification(
        _envelope("alarm-2", _payload("ALARM", "OK", started))
    )
    assert replay["duplicate"] is True, "A republished logical transition must be a duplicate"
    assert Event.objects.filter(category="aws:cloudwatch:alarm").count() == 1, \
        "A replay must not create a second Event"
    assert Incident.objects.filter(category="aws:cloudwatch:alarm").count() == 1, \
        "A replay must not create a second Incident"
    jobs = Job.objects.filter(idempotency_key__startswith=f"aws-cw:{first['transition_id']}:")
    assert jobs.count() == 1, "A replay must retain one deterministic handler Job"

    job = jobs.get()
    with mock.patch(
        "mojo.apps.incident.handlers.event_handlers.TicketHandler._push_to_maestro"
    ) as push:
        execute_handler(job)
    ticket = Ticket.objects.get(category="cloudwatch-test")
    assert ticket.incident_id is not None, "The configured ticket handler must link local work to the incident"
    assert push.call_count == 1, "Board routing must occur only through TicketHandler"

    insufficient = started + timedelta(minutes=1)
    process_notification(
        _envelope(
            "insufficient-1",
            _payload("INSUFFICIENT_DATA", "ALARM", insufficient),
        )
    )
    alarm = CloudWatchAlarm.objects.get(alarm_arn=ALARM_ARN)
    assert alarm.active_incident_id == ticket.incident_id, \
        "INSUFFICIENT_DATA must preserve the active incident"

    recovered = started + timedelta(minutes=2)
    process_notification(
        _envelope("ok-1", _payload("OK", "INSUFFICIENT_DATA", recovered))
    )
    ticket.incident.refresh_from_db()
    alarm.refresh_from_db()
    assert ticket.incident.status == "resolved", "OK must resolve the exact active incident"
    assert alarm.active_incident_id is None, "OK must clear the active incident pointer"
    assert ticket.notes.filter(metadata__type="cloudwatch_recovery").count() == 1, \
        "Recovery must be recorded on the linked ticket"

    from mojo.apps.incident.services import maestro_sync
    recovery_note = ticket.notes.get(metadata__type="cloudwatch_recovery")
    with mock.patch.object(maestro_sync, "_post", return_value={
        "id": 901,
        "integration_id": "cloudwatch-test",
        "board": 17,
        "url": "/workspaces/test/items/901",
    }), mock.patch.object(
        maestro_sync, "get_config", return_value=("https://maestro.example", "secret")
    ), mock.patch.object(maestro_sync, "enqueue_note") as enqueue_note:
        link = maestro_sync.push_source(ticket, 17)
    enqueue_note.assert_any_call(link.pk, "ticket", recovery_note.pk)

    again = started + timedelta(minutes=3)
    process_notification(_envelope("alarm-3", _payload("ALARM", "OK", again)))
    alarm.refresh_from_db()
    assert alarm.active_incident_id != ticket.incident_id, \
        "A later ALARM after recovery must open a new occurrence"
    assert CloudWatchAlarmTransition.objects.count() == 4, \
        "Every distinct lifecycle transition must remain durable"


@th.django_unit_test()
def test_delayed_ticket_job_skips_recovered_incident(opts):
    from django.utils import timezone
    from mojo.apps.aws.services.cloudwatch_alarms import process_notification
    from mojo.apps.incident.handlers.event_handlers import TicketHandler, execute_handler
    from mojo.apps.incident.models import BundleBy, Event, RuleSet, Ticket
    from mojo.apps.jobs.models import Job

    _clear_state()
    RuleSet.objects.create(
        category="aws:cloudwatch",
        name="Delayed ticket policy",
        bundle_by=BundleBy.MODEL_NAME_AND_ID,
        bundle_minutes=None,
        handler="ticket://?category=cloudwatch-test&board=17",
    )
    started = timezone.now()
    first = process_notification(_envelope("late-alarm", _payload("ALARM", "OK", started)))
    stale_event = Event.objects.select_related("incident").get(
        category="aws:cloudwatch:alarm",
    )
    process_notification(
        _envelope("late-ok", _payload("OK", "ALARM", started + timedelta(minutes=1)))
    )
    job = Job.objects.get(idempotency_key__startswith=f"aws-cw:{first['transition_id']}:")
    with mock.patch(
        "mojo.apps.incident.handlers.event_handlers.TicketHandler._push_to_maestro"
    ) as push:
        TicketHandler(
            None, category="cloudwatch-test", board="17",
        ).run(stale_event)
        execute_handler(job)
    assert not Ticket.objects.filter(category="cloudwatch-test").exists(), \
        "Fresh or stale event objects delayed past recovery must not create human work"
    assert not push.called, "A recovered incident must not be pushed to Maestro"

