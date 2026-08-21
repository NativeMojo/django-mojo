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
def test_setup_delivery_probe_is_evidence_only_after_rules_exist(opts):
    from django.utils import timezone
    from mojo.apps.aws.models import CloudWatchAlarmTransition
    from mojo.apps.aws.services import aws_setup
    from mojo.apps.aws.services.cloudwatch_alarms import process_notification
    from mojo.apps.incident.models import Event, Incident, RuleSet

    _clear_state()
    RuleSet.ensure_cloudwatch_rules()
    probe_name = "django-mojo/test-installation/delivery-probe"
    payload = _payload("ALARM", "OK", timezone.now(), extras={"AlarmName": probe_name})
    envelope = _envelope("setup-delivery-probe", payload)
    with mock.patch.object(aws_setup, "is_owned_delivery_probe", return_value=True):
        first = process_notification(envelope)
        second = process_notification(envelope)
    transition = CloudWatchAlarmTransition.objects.get(pk=first["transition_id"])
    assert transition.is_delivery_probe, "The owned System Setup alarm must be marked as probe evidence"
    assert transition.event_id is None and transition.incident_id is None, \
        "A delivery probe must never become an operational event or incident"
    assert transition.dispatch_status == CloudWatchAlarmTransition.DISPATCH_COMPLETE, \
        "Evidence-only probes must never enter handler dispatch"
    assert second["duplicate"] is True, "Probe delivery reruns must remain idempotent"
    assert not Event.objects.filter(category="aws:cloudwatch:alarm").exists(), \
        "Installed incident rules must not turn a setup probe into an event"
    assert not Incident.objects.filter(category="aws:cloudwatch:alarm").exists(), \
        "Installed incident rules must not turn a setup probe into an incident"


@th.django_unit_test()
def test_delivery_probe_migration_defaults_existing_rows_to_operational(opts):
    import importlib
    from django.db import connection, models
    from django.db.migrations.state import ModelState, ProjectState
    from django.utils import timezone
    from mojo.apps.aws.models import CloudWatchAlarmTransition
    from mojo.apps.aws.services.cloudwatch_alarms import process_notification
    from mojo.apps.incident.models import Event

    migration = importlib.import_module(
        "mojo.apps.aws.migrations.0012_cloudwatchalarmtransition_is_delivery_probe")
    operation = migration.Migration.operations[0]
    assert operation.name == "is_delivery_probe" and operation.field.default is False, \
        "The additive migration must backfill every existing transition as operational"
    table = "aws_transition_pre_0012_test"
    from_state = ProjectState()
    from_state.add_model(ModelState(
        app_label="aws", name="CloudWatchAlarmTransition",
        fields=[
            ("id", models.BigAutoField(primary_key=True)),
            ("sns_message_id", models.CharField(max_length=128)),
        ], options={"db_table": table}, bases=(models.Model,)))
    to_state = from_state.clone()
    operation.state_forwards("aws", to_state)
    legacy_model = from_state.apps.get_model("aws", "CloudWatchAlarmTransition")
    migrated_model = to_state.apps.get_model("aws", "CloudWatchAlarmTransition")
    try:
        with connection.schema_editor() as schema_editor:
            schema_editor.create_model(legacy_model)
        legacy_model.objects.create(sns_message_id="pre-0012-transition")
        with connection.schema_editor() as schema_editor:
            operation.database_forwards("aws", schema_editor, from_state, to_state)
        historical = migrated_model.objects.get(sns_message_id="pre-0012-transition")
        assert historical.is_delivery_probe is False, \
            "Migrating the actual 0011-shaped row through 0012 must backfill false"
    finally:
        if table in connection.introspection.table_names():
            with connection.schema_editor() as schema_editor:
                schema_editor.delete_model(migrated_model)

    _clear_state()
    result = process_notification(_envelope(
        "post-0012-operational",
        _payload("ALARM", "OK", timezone.now())))
    transition = CloudWatchAlarmTransition.objects.get(pk=result["transition_id"])
    assert transition.is_delivery_probe is False and transition.event_id is not None, \
        "A normal post-migration alarm must still create an operational Event"
    assert transition.dispatch_status == CloudWatchAlarmTransition.DISPATCH_COMPLETE \
        and Event.objects.filter(pk=transition.event_id).exists(), \
        "The restored latest schema must retain normal event/dispatch semantics"


@th.django_unit_test()
def test_delivery_probe_identity_rejects_wrong_topic_account_and_region(opts):
    from django.utils import timezone
    from mojo.apps.aws.services import aws_setup

    identity = {"uuid": "00000000-0000-0000-0000-000000000001", "slug": "install-one"}
    challenge = "c" * 32
    name = f"django-mojo/install-one/delivery-probe/{challenge}"
    topic = "arn:aws:sns:us-east-1:123456789012:django-mojo-install-one-operations"
    data = {
        "alarm_name": name,
        "alarm_arn": f"arn:aws:cloudwatch:us-east-1:123456789012:alarm:{name}",
        "account": "123456789012", "region": "us-east-1",
        "state_changed_at": timezone.now(),
    }
    with mock.patch.object(aws_setup.system_settings, "read_installation_identity", return_value=identity), \
            mock.patch.object(aws_setup.system_settings, "get_value", return_value=[topic]), \
            mock.patch.object(aws_setup, "_active_probe_identity", return_value={
                "challenge": challenge,
                "cutoff": data["state_changed_at"] - __import__("datetime").timedelta(seconds=1),
                "operation": object()}), \
            mock.patch.object(aws_setup, "_region", return_value="us-east-1"):
        assert aws_setup.is_owned_delivery_probe({"TopicArn": topic}, data), \
            "The exact owned topic/account/region/alarm identity should classify as probe evidence"
        wrong_topic = topic.replace("operations", "other")
        assert not aws_setup.is_owned_delivery_probe({"TopicArn": wrong_topic}, data), \
            "The deterministic name on another topic must remain an operational alarm"
        wrong_account = dict(data, account="999999999999")
        assert not aws_setup.is_owned_delivery_probe({"TopicArn": topic}, wrong_account), \
            "A payload account mismatch must not suppress event dispatch"
        wrong_region = dict(data, region="us-west-2")
        assert not aws_setup.is_owned_delivery_probe({"TopicArn": topic}, wrong_region), \
            "A payload region mismatch must not suppress event dispatch"
        wrong_challenge = dict(data, alarm_name=name[:-1] + "d")
        wrong_challenge["alarm_arn"] = wrong_challenge["alarm_arn"][:-1] + "d"
        assert not aws_setup.is_owned_delivery_probe({"TopicArn": topic}, wrong_challenge), \
            "A different setup challenge must remain an operational alarm"
        preexisting = dict(
            data, state_changed_at=data["state_changed_at"] -
            __import__("datetime").timedelta(seconds=2))
        assert not aws_setup.is_owned_delivery_probe({"TopicArn": topic}, preexisting), \
            "A same-name state transition from before this operation must not prove delivery"


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

