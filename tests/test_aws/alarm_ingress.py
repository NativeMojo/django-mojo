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
def setup_alarm_ingress(opts):
    _clear_state()


@th.django_unit_test()
def test_sns_topic_is_checked_before_certificate_fetch(opts):
    from django.test import RequestFactory
    from mojo.apps.aws.services import sns

    envelope = _envelope(
        "topic-denied",
        _payload("ALARM", "OK", __import__("django.utils.timezone", fromlist=["now"]).now()),
        topic="arn:aws:sns:us-east-1:123456789012:not-allowed",
    )
    request = RequestFactory().generic(
        "POST", "/api/aws/cloudwatch/sns/alarm",
        data=json.dumps(envelope), content_type="text/plain",
    )
    with mock.patch.object(sns, "_certificate") as certificate:
        with th.assert_raises(sns.SNSAuthorizationError):
            sns.verify_envelope(envelope, [TOPIC], request=request)
    assert not certificate.called, "A disallowed topic must be rejected before certificate I/O"

    oversized = RequestFactory().generic(
        "POST", "/api/aws/cloudwatch/sns/alarm",
        data=b"{" + (b"x" * (sns.MAX_ENVELOPE_BYTES + 1)),
        content_type="text/plain",
    )
    with th.assert_raises(sns.SNSPayloadError):
        sns.parse_request(oversized)

    unsafe = _envelope(
        "unsafe-cert",
        _payload("ALARM", "OK", __import__("django.utils.timezone", fromlist=["now"]).now()),
    )
    unsafe["SigningCertURL"] = "http://169.254.169.254/latest/meta-data/"
    with mock.patch.object(sns.requests, "get") as get:
        with th.assert_raises(sns.SNSAuthorizationError):
            sns.verify_envelope(unsafe, [TOPIC])
    assert not get.called, "An unsafe certificate URL must be rejected without network I/O"


@th.django_unit_test()
def test_sns_signature_version_and_confirmation_url_are_strict(opts):
    from cryptography.hazmat.primitives.asymmetric import rsa, padding
    from cryptography.hazmat.primitives import hashes
    from mojo.apps.aws.services import sns

    class Certificate:
        def __init__(self, public_key):
            self._public_key = public_key

        def public_key(self):
            return self._public_key

    from django.utils import timezone
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    envelope = _envelope("signed", _payload("ALARM", "OK", timezone.now()))
    envelope["Signature"] = base64.b64encode(
        key.sign(sns._canonical(envelope), padding.PKCS1v15(), hashes.SHA256())
    ).decode("ascii")
    with mock.patch.object(sns, "_certificate", return_value=Certificate(key.public_key())):
        verified = sns.verify_envelope(envelope, [TOPIC])
    assert verified is envelope, "SignatureVersion 2 must verify with SHA-256"

    confirmation = dict(envelope)
    confirmation.update({
        "Type": "SubscriptionConfirmation",
        "Token": "signed-token",
        "Message": "confirm",
    })
    query = urlencode({
        "Action": "ConfirmSubscription",
        "TopicArn": TOPIC,
        "Token": "signed-token",
    })
    confirmation["SubscribeURL"] = f"https://sns.us-east-1.amazonaws.com/?{query}"
    response = mock.Mock(status_code=200)
    with mock.patch.object(sns.requests, "get", return_value=response) as get:
        status = sns.confirm_subscription(confirmation)
    assert status == 200, "A matching HTTPS SNS confirmation URL must succeed"
    assert get.call_args.kwargs["allow_redirects"] is False, "SNS confirmation must refuse redirects"

    confirmation["SubscribeURL"] = "https://example.com/?" + query
    with th.assert_raises(sns.SNSAuthorizationError):
        sns.confirm_subscription(confirmation)


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
def test_default_rules_do_not_open_cloudwatch_incident(opts):
    from django.utils import timezone
    from mojo.apps.aws.models import CloudWatchAlarmTransition
    from mojo.apps.aws.services.cloudwatch_alarms import process_notification
    from mojo.apps.incident.models import Event, Incident, RuleSet

    _clear_state()
    RuleSet.ensure_catchall_rules()
    result = process_notification(
        _envelope("default-policy", _payload("ALARM", "OK", timezone.now()))
    )
    event = Event.objects.get(category="aws:cloudwatch:alarm")
    transition = CloudWatchAlarmTransition.objects.get(pk=result["transition_id"])
    assert event.incident_id is None, "CloudWatch must not fall through to the seeded wildcard RuleSet"
    assert transition.dispatch_status == "complete", "No-policy alarm must need no handler dispatch"
    assert not Incident.objects.filter(category="aws:cloudwatch:alarm").exists(), \
        "Enabling the receiver alone must not create an incident"


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
def test_payload_is_bounded_and_stale_delivery_does_not_regress(opts):
    from django.utils import timezone
    from mojo.apps.aws.models import CloudWatchAlarm
    from mojo.apps.aws.services.cloudwatch_alarms import (
        CloudWatchPayloadError,
        normalize,
        process_notification,
    )
    from mojo.apps.incident.models import Event

    _clear_state()
    now = timezone.now()
    process_notification(_envelope("newer", _payload("OK", "ALARM", now)))
    process_notification(_envelope(
        "older",
        _payload(
            "ALARM", "OK", now - timedelta(minutes=5),
            extras={
                "AlarmDescription": "secret-description",
                "QueryString": "fields @message | filter password",
                "AccessKeyId": "must-not-persist",
            },
        ),
    ))
    alarm = CloudWatchAlarm.objects.get(alarm_arn=ALARM_ARN)
    stale = Event.objects.get(metadata__stale=True)
    persisted = json.dumps(stale.metadata)
    assert alarm.current_state == "OK", "An older signed transition must not regress current state"
    assert stale.incident_id is None, "A stale transition must remain an audit-only Event"
    assert "secret-description" not in persisted, "Alarm descriptions must not be persisted"
    assert "password" not in persisted, "Metric/log query text must not be persisted"
    assert "must-not-persist" not in persisted, "Credential-like extra fields must not be persisted"
    invalid = _payload("BOGUS", "OK", now)
    with th.assert_raises(CloudWatchPayloadError):
        normalize(invalid)


@th.django_unit_test()
def test_delayed_ticket_job_skips_recovered_incident(opts):
    from django.utils import timezone
    from mojo.apps.aws.services.cloudwatch_alarms import process_notification
    from mojo.apps.incident.handlers.event_handlers import execute_handler
    from mojo.apps.incident.models import BundleBy, RuleSet, Ticket
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
    process_notification(
        _envelope("late-ok", _payload("OK", "ALARM", started + timedelta(minutes=1)))
    )
    job = Job.objects.get(idempotency_key__startswith=f"aws-cw:{first['transition_id']}:")
    with mock.patch(
        "mojo.apps.incident.handlers.event_handlers.TicketHandler._push_to_maestro"
    ) as push:
        execute_handler(job)
    assert not Ticket.objects.filter(category="cloudwatch-test").exists(), \
        "A ticket job delayed past recovery must not create stale human work"
    assert not push.called, "A recovered incident must not be pushed to Maestro"
