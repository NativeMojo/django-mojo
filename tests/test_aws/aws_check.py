import importlib
import io
from types import SimpleNamespace
from unittest import mock

from testit import helpers as th


def _setting_values(**overrides):
    values = {
        "AWS_REGION": "us-east-1",
        "BASE_URL": "https://api.example.com",
        "AWS_MONITORING_NAME": "test",
        "AWS_CLOUDWATCH_ALARM_TOPIC_ARNS": [],
        "AWS_KEY": None,
        "AWS_SECRET": None,
    }
    values.update(overrides)
    return lambda name, default=None, kind=None: values.get(name, default)


def _verified_sts():
    sts = mock.Mock()
    sts.get_caller_identity.return_value = {
        "Account": "123456789012",
        "Arn": "arn:aws:sts::123456789012:assumed-role/app/runner",
    }
    return sts


@th.django_unit_test()
def test_identity_is_bounded_and_redacted(opts):
    from mojo.apps.aws.services import aws_check

    sts = mock.Mock()
    sts.get_caller_identity.return_value = {
        "Account": "123456789012",
        "Arn": "arn:aws:sts::123456789012:assumed-role/app/runner",
    }
    with mock.patch.object(aws_check, "_setting", side_effect=_setting_values()):
        report = aws_check.AWSCheckRunner(clients={"sts": sts}).run(["identity"])
    assert report["overall"] == "pass", f"Valid STS identity should pass, got {report}"
    assert report["items"][0]["details"]["account"] == "123456789012", \
        "The safe AWS account id should be reported"


@th.django_unit_test()
def test_monitoring_stops_before_subscription_until_static_allowlist_matches(opts):
    from mojo.apps.aws.services import aws_check

    topic = "arn:aws:sns:us-east-1:123456789012:django-mojo-test-operations"
    sns = mock.Mock()
    sns.list_topics.return_value = {"Topics": [{"TopicArn": topic}]}
    sns.list_tags_for_resource.return_value = {"Tags": [
        {"Key": "managed-by", "Value": "django-mojo"},
        {"Key": "purpose", "Value": "cloudwatch-incidents"},
        {"Key": "deployment", "Value": "test"},
    ]}
    with mock.patch.object(aws_check, "_setting", side_effect=_setting_values()):
        report = aws_check.AWSCheckRunner(
            clients={"sts": _verified_sts(), "sns": sns, "cloudwatch": mock.Mock()},
            apply=True, yes=True,
        ).run(["monitoring"])
    codes = [item["code"] for item in report["items"]]
    assert "sns.topic_not_allowlisted" in codes, f"Missing exact allowlist must be pending, got {codes}"
    assert sns.subscribe.call_count == 0, "The receiver must not be subscribed before static allowlisting"


@th.django_unit_test()
def test_s3_probe_reports_exact_cleanup_failure(opts):
    from mojo.apps.fileman.models import FileManager
    from mojo.apps.aws.services import aws_check

    FileManager.objects.filter(
        user__isnull=True, group__isnull=True, backend_type=FileManager.AWS_S3,
    ).delete()
    manager = FileManager.objects.create(
        name="aws-check-test", backend_type=FileManager.AWS_S3,
        backend_url="s3://aws-check-test", is_active=True, is_default=True,
        is_public=False,
    )
    s3 = mock.Mock()
    s3.head_bucket.return_value = {}
    s3.get_public_access_block.return_value = {"PublicAccessBlockConfiguration": {
        "BlockPublicAcls": True, "IgnorePublicAcls": True,
        "BlockPublicPolicy": True, "RestrictPublicBuckets": True,
    }}
    s3.get_bucket_cors.return_value = {"CORSRules": [{"AllowedOrigins": ["https://example.com"]}]}
    s3.get_object.return_value = {"Body": io.BytesIO(b"probe")}
    s3.delete_object.side_effect = RuntimeError("cleanup denied")
    with mock.patch.object(aws_check.uuid, "uuid4", side_effect=[SimpleNamespace(hex="sentinel"), SimpleNamespace(hex="probe")]):
        report = aws_check.AWSCheckRunner(
            clients={"sts": _verified_sts(), "s3": s3},
            apply=True, yes=True, probe_s3=True,
        ).run(["s3"])
    cleanup = next(item for item in report["items"] if item["code"] == "bucket.probe_cleanup_failed")
    assert cleanup["details"]["key"] == "__django_mojo_aws_check__/sentinel", \
        f"Cleanup failure must name the exact sentinel, got {cleanup}"
    manager.delete()


@th.django_unit_test()
def test_email_audit_can_be_strictly_non_persistent(opts):
    from mojo.apps.aws.models import EmailDomain
    from mojo.apps.aws.services import email_ops

    EmailDomain.objects.filter(name="aws-check.example").delete()
    domain = EmailDomain.objects.create(name="aws-check.example", status="pending")
    audit = SimpleNamespace(
        domain=domain.name, region="us-east-1", status="ok", audit_pass=True,
        checks={"ses_verified": True}, items=[],
    )
    with mock.patch.object(email_ops, "audit_domain_config", return_value=audit):
        result = email_ops.audit_email_domain(domain.pk, persist=False)
    domain.refresh_from_db()
    assert result.audit_pass is True, "The read-only audit should return the AWS result"
    assert domain.status == "pending", "persist=False must not update EmailDomain readiness fields"
    domain.delete()


@th.django_unit_test()
def test_aws_check_command_json_and_failure_exit(opts):
    from django.core.management import call_command
    from django.core.management.base import CommandError

    module = importlib.import_module("mojo.apps.aws.management.commands.aws-check")
    passed = {
        "schema_version": 1, "generated_at": "2026-01-01T00:00:00+00:00",
        "region": "us-east-1", "overall": "pass",
        "counts": {"pass": 1, "warn": 0, "fail": 0, "pending": 0, "skip": 0},
        "items": [],
    }
    with mock.patch.object(module.AWSCheckRunner, "run", return_value=passed):
        output = io.StringIO()
        call_command("aws-check", "--json", stdout=output)
    assert '"schema_version": 1' in output.getvalue(), "JSON mode must emit the versioned schema"

    failed = dict(passed, overall="fail", counts={"pass": 0, "warn": 0, "fail": 1, "pending": 0, "skip": 0})
    with mock.patch.object(module.AWSCheckRunner, "run", return_value=failed):
        try:
            call_command("aws-check", "--check", stdout=io.StringIO())
        except CommandError as exc:
            assert exc.returncode == 1, f"Required failures must exit 1, got {exc.returncode}"
        else:
            assert False, "A required readiness failure must raise CommandError"


@th.django_unit_test()
def test_aws_check_yes_requires_apply(opts):
    from django.core.management import call_command
    from django.core.management.base import CommandError

    try:
        call_command("aws-check", "--yes", stdout=io.StringIO())
    except CommandError as exc:
        assert exc.returncode == 2, f"Invalid CLI combinations must exit 2, got {exc.returncode}"
    else:
        assert False, "--yes without --apply must be rejected"


@th.django_unit_test()
def test_runner_uses_static_credentials_unless_profile_selected(opts):
    from mojo.apps.aws.services import aws_check

    values = _setting_values(
        AWS_REGION="us-west-2", AWS_KEY="configured-key", AWS_SECRET="configured-secret",
    )
    with mock.patch.object(aws_check, "_setting", side_effect=values), \
            mock.patch.object(aws_check, "get_session") as get_session:
        aws_check.AWSCheckRunner()._session()
        get_session.assert_called_once_with(
            access_key="configured-key", secret_key="configured-secret",
            region="us-west-2", profile=None,
        )

        get_session.reset_mock()
        aws_check.AWSCheckRunner(profile="operators")._session()
        get_session.assert_called_once_with(
            access_key=None, secret_key=None, region="us-west-2", profile="operators",
        )


@th.django_unit_test()
def test_adopt_bucket_requires_private_public_access_block(opts):
    from mojo.apps.aws.services import aws_check

    s3 = mock.Mock()
    s3.list_buckets.return_value = {"Buckets": [{"Name": "owned-bucket"}]}
    s3.get_bucket_location.return_value = {"LocationConstraint": "us-west-2"}
    s3.get_public_access_block.return_value = {
        "PublicAccessBlockConfiguration": {"BlockPublicAcls": True},
    }
    runner = aws_check.AWSCheckRunner(region="us-west-2", clients={"s3": s3})

    try:
        runner._adopt_bucket("owned-bucket")
    except RuntimeError as exc:
        assert "public-access block" in str(exc), f"Unexpected adoption error: {exc}"
    else:
        assert False, "Unsafe bucket adoption should fail"
    s3.put_bucket_tagging.assert_not_called()


@th.django_unit_test()
def test_apply_section_fails_before_mutation_without_verified_identity(opts):
    from botocore.exceptions import NoCredentialsError
    from mojo.apps.aws.services import aws_check

    sts = mock.Mock()
    sts.get_caller_identity.side_effect = NoCredentialsError()
    sns = mock.Mock()
    with mock.patch.object(aws_check, "_setting", side_effect=_setting_values()):
        report = aws_check.AWSCheckRunner(
            clients={"sts": sts, "sns": sns}, apply=True, yes=True,
        ).run(["monitoring"])
    assert report["overall"] == "fail", f"Unverified apply identity must fail, got {report}"
    assert report["items"][0]["code"] == "mutation.credentials.missing"
    sns.create_topic.assert_not_called()
    sns.subscribe.assert_not_called()


@th.django_unit_test()
def test_monitoring_requires_deployment_ownership_and_detects_dimension_drift(opts):
    from mojo.apps.aws.services import aws_check

    topic = "arn:aws:sns:us-east-1:123456789012:django-mojo-test-operations"
    sns = mock.Mock()
    sns.list_topics.return_value = {"Topics": [{"TopicArn": topic}]}
    sns.list_tags_for_resource.return_value = {"Tags": [
        {"Key": "managed-by", "Value": "django-mojo"},
        {"Key": "purpose", "Value": "cloudwatch-incidents"},
        {"Key": "deployment", "Value": "another-deployment"},
    ]}
    with mock.patch.object(aws_check, "_setting", side_effect=_setting_values()):
        report = aws_check.AWSCheckRunner(
            clients={"sns": sns, "cloudwatch": mock.Mock()},
        ).run(["monitoring"])
    assert report["overall"] == "fail", f"Cross-deployment topic must conflict, got {report}"
    assert "sns.topic_conflict" in [item["code"] for item in report["items"]]

    sns.list_tags_for_resource.return_value["Tags"][-1]["Value"] = "test"
    sns.list_subscriptions_by_topic.return_value = {"Subscriptions": [{
        "Protocol": "https", "Endpoint": "https://api.example.com/api/aws/cloudwatch/sns/alarm",
        "SubscriptionArn": "arn:aws:sns:subscription/confirmed",
    }]}
    cloudwatch = mock.Mock()
    desired = {
        "AlarmName": "django-mojo/test/ec2/i-expected/cpu",
        "Namespace": "AWS/EC2", "MetricName": "CPUUtilization",
        "Dimensions": [{"Name": "InstanceId", "Value": "i-expected"}],
        "Statistic": "Average", "ComparisonOperator": "GreaterThanOrEqualToThreshold",
        "Threshold": 90, "Period": 300, "EvaluationPeriods": 3,
        "DatapointsToAlarm": 3, "TreatMissingData": "notBreaching",
    }
    current = dict(desired, AlarmArn="arn:aws:cloudwatch:alarm/test",
                   Dimensions=[{"Name": "InstanceId", "Value": "i-other"}],
                   AlarmActions=[topic], OKActions=[topic])
    cloudwatch.describe_alarms.return_value = {"MetricAlarms": [current]}
    cloudwatch.list_tags_for_resource.return_value = {"Tags": [
        {"Key": "managed-by", "Value": "django-mojo"},
        {"Key": "purpose", "Value": "cloudwatch-incidents"},
        {"Key": "deployment", "Value": "test"},
    ]}
    values = _setting_values(AWS_CLOUDWATCH_ALARM_TOPIC_ARNS=[topic])
    with mock.patch.object(aws_check, "_setting", side_effect=values), \
            mock.patch.object(aws_check.AWSCheckRunner, "_desired_alarms", return_value=[desired]):
        report = aws_check.AWSCheckRunner(
            clients={"sns": sns, "cloudwatch": cloudwatch},
        ).run(["monitoring"])
    drift = next(item for item in report["items"] if item["code"] == "alarms.drifted")
    assert desired["AlarmName"] in drift["details"]["alarm_names"]
    cloudwatch.put_metric_alarm.assert_not_called()


@th.django_unit_test()
def test_email_create_missing_preserves_existing_mapping_and_reruns_cleanly(opts):
    from mojo.apps.aws.services import aws_check

    ses, sns = mock.Mock(), mock.Mock()
    sns.list_topics.return_value = {"Topics": []}
    sns.create_topic.return_value = {
        "TopicArn": "arn:aws:sns:us-east-1:123456789012:ses-example-com-bounce",
    }
    sns.list_subscriptions_by_topic.return_value = {"Subscriptions": []}
    ses.get_identity_notification_attributes.return_value = {"NotificationAttributes": {
        "example.com": {
            "BounceTopic": "arn:aws:sns:us-east-1:123456789012:operator-bounce",
            "ComplaintTopic": "arn:configured-complaint", "DeliveryTopic": "arn:configured-delivery",
        },
    }}
    domain = SimpleNamespace(
        name="example.com", sns_topic_bounce_arn=None,
        sns_topic_complaint_arn="arn:configured-complaint",
        sns_topic_delivery_arn="arn:configured-delivery",
        sns_topic_inbound_arn="arn:configured-inbound", metadata={}, save=mock.Mock(),
    )
    report = SimpleNamespace(items=[
        SimpleNamespace(resource="ses.identity.verification", current="Success", status="ok"),
        SimpleNamespace(resource="ses.identity.dkim", current={
            "Enabled": True, "VerificationStatus": "Success",
        }, status="ok"),
    ])
    runner = aws_check.AWSCheckRunner(clients={"ses": ses, "sns": sns})

    created = runner._create_missing_email_resources(domain, report)
    assert created == ["sns-topic:bounce"], f"Only the absent topic should be created, got {created}"
    ses.set_identity_notification_topic.assert_not_called()
    domain.save.assert_called_once()

    sns.list_topics.return_value = {"Topics": [{"TopicArn": domain.sns_topic_bounce_arn}]}
    created = runner._create_missing_email_resources(domain, report)
    assert created == [], f"A healthy rerun must be a no-op, got {created}"
    assert sns.create_topic.call_count == 1


@th.django_unit_test()
def test_network_failure_and_fresh_cron_have_stable_classification(opts):
    from botocore.exceptions import EndpointConnectionError
    from django.utils import timezone
    from mojo.apps.aws.services import aws_check

    with mock.patch.object(
        aws_check.AWSCheckRunner, "check_s3",
        side_effect=EndpointConnectionError(endpoint_url="https://s3.invalid"),
    ):
        report = aws_check.AWSCheckRunner().run(["s3"])
    assert report["items"][0]["code"] == "s3.service_unreachable", report

    now = timezone.now()
    redis = mock.Mock()
    redis.scan_iter.return_value = [b"runner"]
    redis.get.return_value = b"scheduler"
    heartbeat = [{
        "run_id": "recent", "state": "completed",
        "started_at": now.isoformat(), "completed_at": now.isoformat(),
    }]
    with mock.patch("mojo.helpers.cron.get_cron_heartbeats", return_value=heartbeat), \
            mock.patch("mojo.helpers.redis.get_connection", return_value=redis):
        report = aws_check.AWSCheckRunner(now=lambda: now).run(["cron"])
    codes = [item["code"] for item in report["items"]]
    assert "cron.heartbeat_fresh" in codes, f"Fresh heartbeat should pass, got {codes}"
    assert "jobs.health" in codes, f"Jobs health should be reported, got {codes}"
