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
    ]}
    with mock.patch.object(aws_check, "_setting", side_effect=_setting_values()):
        report = aws_check.AWSCheckRunner(
            clients={"sns": sns, "cloudwatch": mock.Mock()}, apply=True, yes=True,
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
            clients={"s3": s3}, apply=True, yes=True, probe_s3=True,
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
