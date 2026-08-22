"""Split out of tests/test_aws/aws_check.py (maestro #1839, then #2558).

Most tests here go through `_setup_admin_identity`, which resets the
protected installation Setting rows (identity, BASE_URL, MONITORING_TOPICS,
...) and their Redis cache — global state every parallel module reads. The
#2558 additions instead patch process-global module/class attributes that
have no injection seam. Either way the coverage runs in the opt-in serial
tier.
"""
import importlib
import io
from types import SimpleNamespace
from unittest import mock

from testit import helpers as th


def _discovery_clients(db_instances=None, instances=None, cache_clusters=None,
                       target_groups=None):
    """Mock ec2/rds/elasticache/elbv2 paginators so _desired_alarms builds no real boto clients."""
    ec2, rds, elasticache, elbv2 = (mock.Mock(), mock.Mock(), mock.Mock(), mock.Mock())
    ec2.get_paginator.return_value.paginate.return_value = [
        {"Reservations": [{"Instances": instances or []}]},
    ]
    rds.get_paginator.return_value.paginate.return_value = [
        {"DBInstances": db_instances or []},
    ]
    elasticache.get_paginator.return_value.paginate.return_value = [
        {"CacheClusters": cache_clusters or []},
    ]
    elbv2.get_paginator.return_value.paginate.return_value = [
        {"TargetGroups": target_groups or []},
    ]
    return {"ec2": ec2, "rds": rds, "elasticache": elasticache, "elbv2": elbv2}


def _setup_admin_identity(email):
    from mojo.apps.account.models import Setting, User
    from mojo.apps.account.services import system_settings

    redis = Setting._redis()
    if redis:
        redis.hdel(Setting._redis_key(None), *system_settings.protected_keys())
    Setting.objects.filter(key__in=system_settings.protected_keys()).delete()
    User.objects.filter(email=email).delete()
    user = User.objects.create_user(username=email, email=email, password="Setup_admin_99")
    user.is_active = True
    user.is_superuser = True
    user.save(update_fields=["is_active", "is_superuser"])
    system_settings.installation_identity(user)
    system_settings.set_value(user, system_settings.BASE_URL, "https://api.example.com")
    return user


def _missing(operation, code):
    from botocore.exceptions import ClientError
    return ClientError({"Error": {"Code": code, "Message": "missing"},
                        "ResponseMetadata": {"HTTPStatusCode": 404}}, operation)


def _safe_bucket_client(bucket="existing-media"):
    s3 = mock.Mock()
    s3.list_buckets.return_value = {
        "Owner": {"ID": "canonical-owner"}, "Buckets": [{"Name": bucket}]}
    s3.get_bucket_location.return_value = {"LocationConstraint": None}
    s3.get_bucket_website.side_effect = _missing("GetBucketWebsite", "NoSuchWebsiteConfiguration")
    tags = {}

    def get_tags(**kwargs):
        if not tags:
            raise _missing("GetBucketTagging", "NoSuchTagSet")
        return {"TagSet": [{"Key": key, "Value": value}
                           for key, value in sorted(tags.items())]}

    def put_tags(**kwargs):
        tags.update({row["Key"]: row["Value"]
                     for row in kwargs["Tagging"]["TagSet"]})
        return {}

    s3.get_bucket_tagging.side_effect = get_tags
    s3.put_bucket_tagging.side_effect = put_tags
    s3.get_bucket_policy.side_effect = _missing("GetBucketPolicy", "NoSuchBucketPolicy")
    s3.get_bucket_policy_status.return_value = {"PolicyStatus": {"IsPublic": False}}
    s3.get_bucket_acl.return_value = {
        "Owner": {"ID": "canonical-owner"},
        "Grants": [{"Grantee": {"Type": "CanonicalUser", "ID": "canonical-owner"},
                    "Permission": "FULL_CONTROL"}],
    }
    public_access = {}
    s3.get_public_access_block.side_effect = lambda **kwargs: {
        "PublicAccessBlockConfiguration": dict(public_access)}

    def put_public_access(**kwargs):
        public_access.update(kwargs["PublicAccessBlockConfiguration"])
        return {}

    s3.put_public_access_block.side_effect = put_public_access
    return s3


def _setup_sts():
    sts = mock.Mock()
    sts.get_caller_identity.return_value = {
        "Account": "123456789012", "Arn": "arn:aws:iam::123456789012:role/setup",
        "UserId": "setup",
    }
    return sts


@th.django_unit_test()
def test_setup_adopts_existing_private_bucket_and_merges_concurrent_cors(opts):
    from mojo.apps.aws.services.aws_setup import AWSSetupService
    from mojo.apps.fileman.models import FileManager

    actor = _setup_admin_identity("aws-setup-s3@test.com")
    FileManager.objects.filter(user=None, group=None, backend_type=FileManager.AWS_S3).delete()
    s3 = _safe_bucket_client()
    unrelated = {"AllowedOrigins": ["https://tenant.example"], "AllowedMethods": ["GET"],
                 "AllowedHeaders": ["authorization"]}
    current = [unrelated]

    def get_cors(**kwargs):
        return {"CORSRules": list(current)}

    injected = {"AllowedOrigins": ["https://concurrent.example"],
                "AllowedMethods": ["HEAD"], "AllowedHeaders": ["*"]}
    puts = 0

    def put_cors(**kwargs):
        nonlocal puts
        puts += 1
        current[:] = kwargs["CORSConfiguration"]["CORSRules"]
        if puts == 1:
            current[:] = [row for row in current
                          if row.get("AllowedOrigins") != ["*"]]
            current.append(injected)
        return {}

    s3.get_bucket_cors.side_effect = get_cors
    s3.put_bucket_cors.side_effect = put_cors
    service = AWSSetupService(
        clients={"s3": s3, "sts": _setup_sts()}, region="us-east-1")
    manager = service.adopt_bucket(actor, "existing-media")
    assert manager.backend_url == "s3://existing-media", f"Expected exact adopted bucket, got {manager.backend_url}"
    assert manager.is_public is False and manager.supports_direct_upload, \
        "The adopted system FileManager must be private and direct-upload capable"
    assert manager.allowed_origins == ["*"], "System media must allow wildcard browser CORS"
    assert unrelated in current, "CORS repair must preserve unrelated rules"
    assert injected in current, "A concurrent unrelated CORS rule must survive the retry"
    assert puts == 2, "Concurrent CORS drift must be reread and repaired exactly once"
    assert any(rule.get("AllowedOrigins") == ["*"] for rule in current), \
        "CORS repair must add the wildcard direct-upload rule"
    block = s3.put_public_access_block.call_args.kwargs["PublicAccessBlockConfiguration"]
    assert all(block.values()), f"Every S3 Block Public Access control must remain enabled: {block}"
    assert not s3.put_bucket_policy.called, "Bucket adoption must never create a public bucket policy"


@th.django_unit_test()
def test_setup_s3_inventory_is_complete_exact_and_fail_closed(opts):
    from mojo.apps.account.services import system_settings
    from mojo.apps.aws.services.aws_setup import AWSSetupService

    actor = _setup_admin_identity("aws-setup-s3-matrix@test.com")
    identity = system_settings.read_installation_identity()
    names = ["safe-media", "website-media", "tenant-media", "foreign-media",
             "unknown-policy", "not-principal", "public-status", "wrong-region",
             "conflicting-tags", "public-acl", "foreign-grant"]
    s3 = _safe_bucket_client("unused")

    def list_buckets(**kwargs):
        page = names[3:] if kwargs.get("ContinuationToken") == "page-2" else names[:3]
        return {"Owner": {"ID": "canonical-owner"},
                "Buckets": [{"Name": name} for name in page],
                "IsTruncated": not kwargs, "NextContinuationToken": "page-2" if not kwargs else None}

    s3.list_buckets.side_effect = list_buckets
    s3.get_bucket_website.side_effect = lambda **kwargs: (
        {"IndexDocument": {"Suffix": "index.html"}}
        if kwargs["Bucket"] == "website-media" else
        (_ for _ in ()).throw(_missing("GetBucketWebsite", "NoSuchWebsiteConfiguration")))
    s3.get_bucket_location.side_effect = lambda **kwargs: {
        "LocationConstraint": (
            "us-west-2" if kwargs["Bucket"] == "wrong-region" else None)}

    def tagging(**kwargs):
        if kwargs["Bucket"] == "tenant-media":
            return {"TagSet": [{"Key": "tenant-id", "Value": "other"}]}
        if kwargs["Bucket"] == "conflicting-tags":
            return {"TagSet": [
                {"Key": "managed-by", "Value": "another-platform"},
                {"Key": "django-mojo-installation", "Value": identity["uuid"]},
            ]}
        raise _missing("GetBucketTagging", "NoSuchTagSet")

    s3.get_bucket_tagging.side_effect = tagging

    def acl(**kwargs):
        owner = "foreign-owner" if kwargs["Bucket"] == "foreign-media" else "canonical-owner"
        grant = {
            "Grantee": {"Type": "CanonicalUser", "ID": owner},
            "Permission": "FULL_CONTROL"}
        if kwargs["Bucket"] == "public-acl":
            grant = {"Grantee": {"Type": "Group", "URI": (
                "http://acs.amazonaws.com/groups/global/AllUsers")},
                     "Permission": "READ"}
        if kwargs["Bucket"] == "foreign-grant":
            grant = {"Grantee": {"Type": "CanonicalUser", "ID": "other-owner"},
                     "Permission": "READ"}
        return {"Owner": {"ID": owner}, "Grants": [grant]}

    s3.get_bucket_acl.side_effect = acl

    def policy(**kwargs):
        if kwargs["Bucket"] == "unknown-policy":
            return {"Policy": __import__("json").dumps({"Statement": [{
                "Effect": "Allow", "Principal": {"Federated": "accounts.example"},
                "Action": "s3:GetObject", "Resource": "*"}]})}
        if kwargs["Bucket"] == "not-principal":
            return {"Policy": __import__("json").dumps({"Statement": [{
                "Effect": "Allow", "NotPrincipal": {"AWS": "arn:aws:iam::123456789012:root"},
                "Action": "s3:GetObject", "Resource": "*"}]})}
        raise _missing("GetBucketPolicy", "NoSuchBucketPolicy")

    s3.get_bucket_policy.side_effect = policy
    s3.get_bucket_policy_status.side_effect = lambda **kwargs: {
        "PolicyStatus": {"IsPublic": kwargs["Bucket"] == "public-status"}}
    service = AWSSetupService(clients={"s3": s3, "sts": _setup_sts()})
    result = service.discover_buckets()
    assert [row["name"] for row in result["candidates"]] == ["safe-media"], \
        f"Only the fully-proven private bucket may be offered: {result}"
    assert sorted(result["rejected"]) == sorted(names[1:]), \
        f"Every unsafe bucket must be named as rejected: {result}"
    assert s3.list_buckets.call_count == 2, "Candidate discovery must consume every inventory page"
    exact = service.discover_buckets(exact_name="safe-media")
    assert [row["name"] for row in exact["candidates"]] == ["safe-media"], \
        "Exact adoption must not accept a prefix or another page's bucket"


@th.django_unit_test()
def test_setup_s3_resume_skips_confirmed_provider_mutations(opts):
    from mojo.apps.aws.services.aws_setup import AWSSetupService
    from mojo.apps.fileman.models import FileManager

    actor = _setup_admin_identity("aws-setup-s3-resume@test.com")
    FileManager.objects.filter(user=None, group=None, backend_type=FileManager.AWS_S3).delete()
    s3 = _safe_bucket_client("resume-media")
    cors = []
    s3.get_bucket_cors.side_effect = lambda **kwargs: {"CORSRules": list(cors)}
    s3.put_bucket_cors.side_effect = lambda **kwargs: cors.__setitem__(
        slice(None), kwargs["CORSConfiguration"]["CORSRules"])
    service = AWSSetupService(clients={"s3": s3, "sts": _setup_sts()})
    original_save = FileManager.save
    failed = {"value": False}

    def fail_once(instance, *args, **kwargs):
        if not failed["value"]:
            failed["value"] = True
            raise RuntimeError("simulated crash after provider convergence")
        return original_save(instance, *args, **kwargs)

    with mock.patch.object(FileManager, "save", autospec=True, side_effect=fail_once):
        try:
            service.adopt_bucket(actor, "resume-media")
        except RuntimeError:
            pass
        else:
            raise AssertionError("The interrupted DB save fixture must fail once")
    provider_counts = (s3.put_public_access_block.call_count,
                       s3.put_bucket_tagging.call_count, s3.put_bucket_cors.call_count)
    manager = service.adopt_bucket(actor, "resume-media")
    assert manager.backend_url == "s3://resume-media"
    assert provider_counts == (s3.put_public_access_block.call_count,
                               s3.put_bucket_tagging.call_count,
                               s3.put_bucket_cors.call_count), \
        "Resume must reread and skip every already-confirmed S3 mutation"


@th.django_unit_test()
def test_aws_registry_builds_typed_late_choice_operations(opts):
    from django.test import RequestFactory
    from mojo.apps.account.models import SystemSetupOperation
    from mojo.apps.account.services import system_setup
    from mojo.apps.aws.services.aws_setup import AWSSetupService

    actor = _setup_admin_identity("aws-setup-registry@test.com")
    SystemSetupOperation.objects.filter(
        status__in=SystemSetupOperation.ACTIVE_STATUSES).delete()
    request = RequestFactory().post(
        "/api/account/admin/setup/create", HTTP_ORIGIN="https://api.example.com",
        HTTP_HOST="api.example.com", secure=True)
    request.user = actor
    request.bearer = "bearer"
    request.api_key = None
    with mock.patch.object(AWSSetupService, "discover_buckets", return_value={
            "candidates": [{"name": "existing-media"}], "rejected": [], "complete": True}):
        operation, _ = system_setup.create(
            request, "fix", section="aws_s3", replay_key="aws-s3-choice")
    assert [row["id"] for row in operation.steps] == [
        "section:aws_s3", "final_readiness"], \
        f"An AWS section operation must not silently become django-only: {operation.steps}"
    operation = system_setup.advance(request, operation.pk)
    step = operation.steps[operation.cursor]
    assert step["state"] == "waiting_for_choice" \
        and step["choice_schema"]["properties"]["bucket"]["enum"] == ["existing-media"], \
        f"S3 discovery must become a typed late choice: {step}"
    system_setup.choose(
        request, operation.pk, step["id"], step["definition_version"],
        step["choice_revision"], {"bucket": "existing-media", "adopt_existing": True})

    SystemSetupOperation.objects.filter(pk=operation.pk).delete()
    with mock.patch.object(
            AWSSetupService, "discover_verified_domains",
            return_value=["verified.example"]):
        email, _ = system_setup.create(
            request, "fix", section="aws_email", replay_key="aws-email-choice")
    email = system_setup.advance(request, email.pk)
    step = email.steps[email.cursor]
    properties = step["choice_schema"]["properties"]
    assert properties["domain"]["enum"] == ["verified.example"] \
        and properties["sender"]["format"] == "email", \
        f"SES setup must expose a verified-domain and typed sender choice: {step}"
    for forged in (
            {"domain": "forged.example", "sender": "ops@forged.example"},
            {"domain": "verified.example", "sender": "not-an-address"}):
        try:
            system_setup.choose(
                request, email.pk, step["id"], step["definition_version"],
                step["choice_revision"], forged)
        except Exception:
            pass
        else:
            raise AssertionError(f"Forged SES choice must be rejected: {forged}")
    system_setup.choose(
        request, email.pk, step["id"], step["definition_version"],
        step["choice_revision"],
        {"domain": "verified.example", "sender": "ops@verified.example"})

    SystemSetupOperation.objects.filter(pk=email.pk).delete()
    topic = "arn:aws:sns:us-east-1:123456789012:django-mojo-owned-operations"
    with mock.patch.object(AWSSetupService, "monitoring_topic_adoption", return_value={
            "topic_arn": topic, "topic_name": "django-mojo-owned-operations"}):
        monitoring, _ = system_setup.create(
            request, "fix", section="aws_monitoring", replay_key="aws-topic-choice")
    monitoring = system_setup.advance(request, monitoring.pk)
    step = monitoring.steps[monitoring.cursor]
    assert step["choice_schema"]["properties"]["topic_arn"]["enum"] == [topic] \
        and step["choice_schema"]["properties"]["adopt_existing_topic"]["enum"] == [True], \
        f"Legacy SNS ownership adoption must require exact affirmative input: {step}"
    SystemSetupOperation.objects.filter(pk=monitoring.pk).delete()


@th.django_unit_test()
def test_setup_imports_verified_ses_sender_and_preserves_custom_templates(opts):
    from mojo.apps.aws.models import EmailDomain, EmailTemplate, Mailbox
    from mojo.apps.aws.services.aws_setup import AWSSetupService, check_email

    actor = _setup_admin_identity("aws-setup-ses@test.com")
    EmailDomain.objects.filter(name="verified.example").delete()
    Mailbox.objects.filter(email="ops@verified.example").delete()
    custom, _ = EmailTemplate.objects.get_or_create(name="invite")
    custom.subject_template = "CUSTOM SUBJECT"
    custom.save(update_fields=["subject_template", "modified"])
    ses = mock.Mock()
    ses.list_identities.return_value = {"Identities": ["pending.example", "verified.example"]}
    ses.get_identity_verification_attributes.return_value = {"VerificationAttributes": {
        "pending.example": {"VerificationStatus": "Pending"},
        "verified.example": {"VerificationStatus": "Success"},
    }}
    mailbox = AWSSetupService(clients={"ses": ses}).configure_email(
        actor, "verified.example", "ops@verified.example")
    assert mailbox.is_system_default and mailbox.allow_outbound, \
        "The selected verified sender must become the outbound system default"
    assert mailbox.domain.status == "verified", "The existing verified SES identity must be imported"
    custom.refresh_from_db()
    assert custom.subject_template == "CUSTOM SUBJECT", \
        "Missing-only template installation must preserve customized templates"
    readiness = check_email({"aws_clients": {"ses": ses}})
    assert all(row["status"] == "pass" for row in readiness), \
        f"An imported verified identity, sender, and shipped templates must be ready: {readiness}"
    rejected = None
    try:
        AWSSetupService(clients={"ses": ses}).configure_email(
            actor, "verified.example", "@verified.example")
    except Exception as exc:
        rejected = exc
    assert rejected is not None, "A sender without a local part must be rejected before persistence"
    try:
        AWSSetupService(clients={"ses": ses}).configure_email(
            actor, "verified.example", "ops@other.example")
    except Exception:
        pass
    else:
        raise AssertionError("A valid-looking sender on a forged domain must be rejected")


@th.django_unit_test()
def test_monitoring_reconcile_is_repeatable_and_persists_protected_allowlist(opts):
    from mojo.apps.account.services import system_settings
    from mojo.apps.aws.services.aws_setup import AWSSetupService

    actor = _setup_admin_identity("aws-setup-monitoring@test.com")
    identity = system_settings.read_installation_identity()
    topic = f"arn:aws:sns:us-east-1:123456789012:django-mojo-{identity['slug']}-operations"
    sns = mock.Mock()
    sns.list_topics.return_value = {"Topics": [{"TopicArn": topic}]}
    expected_tags = [
        {"Key": "managed-by", "Value": "django-mojo"},
        {"Key": "purpose", "Value": "cloudwatch-incidents"},
        {"Key": "deployment", "Value": identity["slug"]},
        {"Key": "django-mojo-installation", "Value": identity["uuid"]},
    ]
    sns.list_tags_for_resource.return_value = {"Tags": expected_tags}
    topic_policy = {"Version": "2012-10-17", "Statement": [
        {"Sid": "KeepMe", "Effect": "Deny"}]}
    sns.get_topic_attributes.side_effect = lambda **kwargs: {
        "Attributes": {"Policy": __import__("json").dumps(topic_policy)}}

    def set_policy(**kwargs):
        topic_policy.clear()
        topic_policy.update(__import__("json").loads(kwargs["AttributeValue"]))
        return {}

    sns.set_topic_attributes.side_effect = set_policy
    subscriptions = []
    sns.list_subscriptions_by_topic.side_effect = lambda **kwargs: {"Subscriptions": list(subscriptions)}

    def subscribe(**kwargs):
        subscriptions.append({"Protocol": kwargs["Protocol"], "Endpoint": kwargs["Endpoint"],
                              "SubscriptionArn": "arn:aws:sns:subscription/one"})
        return {"SubscriptionArn": "arn:aws:sns:subscription/one"}

    sns.subscribe.side_effect = subscribe
    cloudwatch = mock.Mock()
    cloudwatch.list_metrics.return_value = {"Metrics": []}
    alarms = {}
    cloudwatch.describe_alarms.side_effect = lambda **kwargs: {"MetricAlarms": [
        alarms[name] for name in kwargs.get("AlarmNames", []) if name in alarms]}

    def put_alarm(**kwargs):
        alarm = {key: value for key, value in kwargs.items() if key != "Tags"}
        alarm.update({
            "AlarmArn": f"arn:aws:cloudwatch:us-east-1:123456789012:alarm:{kwargs['AlarmName']}",
            "StateValue": alarms.get(kwargs["AlarmName"], {}).get("StateValue", "OK"),
        })
        alarms[kwargs["AlarmName"]] = alarm

    def set_state(**kwargs):
        alarms[kwargs["AlarmName"]]["StateValue"] = kwargs["StateValue"]
        return {}

    cloudwatch.put_metric_alarm.side_effect = put_alarm
    cloudwatch.set_alarm_state.side_effect = set_state
    cloudwatch.list_tags_for_resource.return_value = {"Tags": expected_tags}
    discovery = _discovery_clients(instances=[{
        "State": {"Name": "running"}, "InstanceId": "i-setup", "InstanceType": "m6i.large"}])
    service = AWSSetupService(clients={
        "sns": sns, "cloudwatch": cloudwatch, "sts": _setup_sts(), **discovery})
    challenge = "a" * 32
    cutoff = __import__("django.utils.timezone", fromlist=["now"]).now()
    service.reconcile_monitoring(actor, challenge, cutoff)
    service.reconcile_monitoring(actor, challenge, cutoff)
    assert system_settings.get_value(system_settings.MONITORING_TOPICS) == [topic], \
        "The owned topic must be persisted through the protected setter"
    assert sns.subscribe.call_count == 1, "Reruns must not create duplicate HTTPS subscriptions"
    assert cloudwatch.put_metric_alarm.call_count == 3, \
        "Setup must create the probe plus the real EC2 status and CPU alarms exactly once"
    assert any(name.endswith("/ec2/i-setup/status") for name in alarms), \
        f"The real serving-node status alarm must be converged: {sorted(alarms)}"
    assert any(name.endswith("/ec2/i-setup/cpu") for name in alarms), \
        f"The real serving-node CPU alarm must be converged: {sorted(alarms)}"
    drifted_name = next(name for name in alarms if name.endswith("/ec2/i-setup/cpu"))
    alarms[drifted_name]["Threshold"] = -1
    service.reconcile_monitoring(actor, challenge, cutoff)
    assert cloudwatch.put_metric_alarm.call_count == 4 \
        and alarms[drifted_name]["Threshold"] == 90, \
        "An owned alarm's full desired configuration must be repaired idempotently"
    alarms[drifted_name]["ActionsEnabled"] = False
    alarms[drifted_name]["InsufficientDataActions"] = [topic]
    service.reconcile_monitoring(actor, challenge, cutoff)
    assert cloudwatch.put_metric_alarm.call_count == 5 \
        and alarms[drifted_name]["ActionsEnabled"] is True \
        and alarms[drifted_name]["InsufficientDataActions"] == [], \
        "Disabled actions and injected insufficient-data actions must be repaired"
    alarms[drifted_name]["Unit"] = "Seconds"
    service.reconcile_monitoring(actor, challenge, cutoff)
    assert cloudwatch.put_metric_alarm.call_count == 6 \
        and "Unit" not in alarms[drifted_name], \
        "An unintended explicit metric Unit must be removed during convergence"
    assert cloudwatch.set_alarm_state.call_count == 1, \
        "A resumed ambiguous probe must not replay an already-observed ALARM mutation"
    assert any(row.get("Sid") == "KeepMe" for row in topic_policy["Statement"]), \
        "Monitoring setup must preserve unrelated SNS policy statements"
    owned = next(row for row in topic_policy["Statement"]
                 if row.get("Sid") == "DjangoMojoCloudWatchPublish")
    assert owned["Principal"] == {"Service": "cloudwatch.amazonaws.com"}, \
        "The owned publish grant must be restricted to CloudWatch"
    assert owned["Condition"]["StringEquals"]["AWS:SourceAccount"] == "123456789012", \
        "The owned publish grant must be restricted to the selected AWS account"


@th.django_unit_test()
def test_monitoring_reconcile_merges_a_file_configured_allowlist_entry(opts):
    """Fix must not orphan an alarm topic that lives only in ``django.conf``.

    ``settings.get`` prefers a protected database row's whole value over the
    deployment file, and the SNS receiver reads the allowlist that way.  So a
    Fix that writes a row holding only the portal's own ARN silently removes a
    deployment-configured topic from the effective allowlist, and the receiver
    starts 403ing its deliveries while the alarms keep billing.

    The deployment plane is simulated through ``system_settings._static_value``
    on purpose: mutating ``django.conf.settings`` is process-global, and test
    modules run as threads in one process, so a concurrent module would see the
    foreign ARN.
    """
    import base64
    from cryptography.hazmat.primitives.asymmetric import rsa, padding
    from cryptography.hazmat.primitives import hashes
    from django.test import RequestFactory
    from django.utils import timezone
    from mojo.apps.account.services import system_settings
    from mojo.apps.aws.rest.sns import on_cloudwatch_alarm
    from mojo.apps.aws.services import cloudwatch_alarms, sns as sns_service
    from mojo.apps.aws.services.aws_setup import AWSSetupService
    from mojo.helpers.settings import settings

    actor = _setup_admin_identity("aws-setup-merge@test.com")
    identity = system_settings.read_installation_identity()
    portal_topic = (
        f"arn:aws:sns:us-east-1:123456789012:django-mojo-{identity['slug']}-operations")
    # The tofu django_conf_fragment emits a bare quoted string, not a JSON list.
    file_topic = "arn:aws:sns:us-east-1:123456789012:deployment-configured-operations"
    foreign_topic = "arn:aws:sns:us-east-1:123456789012:never-allowlisted"

    sns = mock.Mock()
    sns.list_topics.return_value = {"Topics": [{"TopicArn": portal_topic}]}
    expected_tags = [
        {"Key": "managed-by", "Value": "django-mojo"},
        {"Key": "purpose", "Value": "cloudwatch-incidents"},
        {"Key": "deployment", "Value": identity["slug"]},
        {"Key": "django-mojo-installation", "Value": identity["uuid"]},
    ]
    sns.list_tags_for_resource.return_value = {"Tags": expected_tags}
    topic_policy = {"Version": "2012-10-17", "Statement": []}
    sns.get_topic_attributes.side_effect = lambda **kwargs: {
        "Attributes": {"Policy": __import__("json").dumps(topic_policy)}}

    def set_policy(**kwargs):
        topic_policy.clear()
        topic_policy.update(__import__("json").loads(kwargs["AttributeValue"]))
        return {}

    sns.set_topic_attributes.side_effect = set_policy
    subscriptions = []
    sns.list_subscriptions_by_topic.side_effect = lambda **kwargs: {
        "Subscriptions": list(subscriptions)}

    def subscribe(**kwargs):
        subscriptions.append({"Protocol": kwargs["Protocol"], "Endpoint": kwargs["Endpoint"],
                              "SubscriptionArn": "arn:aws:sns:subscription/merge"})
        return {"SubscriptionArn": "arn:aws:sns:subscription/merge"}

    sns.subscribe.side_effect = subscribe
    cloudwatch = mock.Mock()
    cloudwatch.list_metrics.return_value = {"Metrics": []}
    alarms = {}
    cloudwatch.describe_alarms.side_effect = lambda **kwargs: {"MetricAlarms": [
        alarms[name] for name in kwargs.get("AlarmNames", []) if name in alarms]}

    def put_alarm(**kwargs):
        alarm = {key: value for key, value in kwargs.items() if key != "Tags"}
        alarm.update({
            "AlarmArn": f"arn:aws:cloudwatch:us-east-1:123456789012:alarm:{kwargs['AlarmName']}",
            "StateValue": alarms.get(kwargs["AlarmName"], {}).get("StateValue", "OK"),
        })
        alarms[kwargs["AlarmName"]] = alarm

    def set_state(**kwargs):
        alarms[kwargs["AlarmName"]]["StateValue"] = kwargs["StateValue"]
        return {}

    cloudwatch.put_metric_alarm.side_effect = put_alarm
    cloudwatch.set_alarm_state.side_effect = set_state
    cloudwatch.list_tags_for_resource.return_value = {"Tags": expected_tags}
    discovery = _discovery_clients()
    service = AWSSetupService(clients={
        "sns": sns, "cloudwatch": cloudwatch, "sts": _setup_sts(), **discovery})
    challenge = "c" * 32
    cutoff = timezone.now()

    deployment = {"value": file_topic}

    def _deployment_static(key, default=None, kind=None):
        # Only the simulated key is intercepted; every other read stays real so
        # a concurrently running test module is unaffected by this patch.
        if key == system_settings.MONITORING_TOPICS:
            return deployment["value"]
        return settings.get_static(key, default, kind=kind)

    class Certificate:
        def __init__(self, public_key):
            self._public_key = public_key

        def public_key(self):
            return self._public_key

    def _signed(message_id, topic, key):
        envelope = {
            "Type": "Notification",
            "MessageId": message_id,
            "TopicArn": topic,
            "Message": __import__("json").dumps({
                "AlarmName": "merge-probe",
                "AlarmArn": "arn:aws:cloudwatch:us-east-1:123456789012:alarm:merge-probe",
                "AWSAccountId": "123456789012",
                "NewStateValue": "ALARM",
                "OldStateValue": "OK",
                "NewStateReason": "state changed to ALARM",
                "StateChangeTime": timezone.now().isoformat(),
                "Region": "US East (N. Virginia)",
                "Trigger": {"Namespace": "AWS/ApplicationELB",
                            "MetricName": "HTTPCode_Target_5XX_Count", "Dimensions": []},
            }),
            "Timestamp": timezone.now().isoformat(),
            "SignatureVersion": "2",
            "SigningCertURL": ("https://sns.us-east-1.amazonaws.com/"
                               "SimpleNotificationService-test.pem"),
            # _canonical requires the field to exist before it is replaced.
            "Signature": base64.b64encode(b"unsigned").decode("ascii"),
        }
        envelope["Signature"] = base64.b64encode(
            key.sign(sns_service._canonical(envelope), padding.PKCS1v15(),
                     hashes.SHA256())).decode("ascii")
        return RequestFactory().generic(
            "POST", "/api/aws/cloudwatch/sns/alarm",
            data=__import__("json").dumps(envelope), content_type="text/plain",
            HTTP_X_AMZ_SNS_MESSAGE_TYPE="Notification",
            HTTP_X_AMZ_SNS_MESSAGE_ID=message_id,
            HTTP_X_AMZ_SNS_TOPIC_ARN=topic,
        )

    with mock.patch.object(system_settings, "_static_value", _deployment_static):
        assert system_settings.get_value(system_settings.MONITORING_TOPICS) is None, \
            "The precondition is an installation with no protected allowlist row at all"
        assert system_settings._static_value(
            system_settings.MONITORING_TOPICS, [], kind="list") == file_topic, \
            "The precondition is an allowlist configured only in the deployment file"

        service.reconcile_monitoring(actor, challenge, cutoff)

        effective = settings.get(system_settings.MONITORING_TOPICS, [], kind="list")
        assert sorted(effective) == sorted([file_topic, portal_topic]), \
            ("Fix must merge the deployment-configured topic into the allowlist "
             f"rather than shadowing it: {effective}")

        signing_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        certificate = Certificate(signing_key.public_key())
        for topic in (file_topic, portal_topic):
            request = _signed(f"merge-{topic.rsplit(':', 1)[-1]}", topic, signing_key)
            with mock.patch.object(sns_service, "_certificate", return_value=certificate), \
                    mock.patch.object(cloudwatch_alarms, "process_notification",
                                      return_value={"accepted": True}) as processed:
                response = on_cloudwatch_alarm(request)
            assert response.status_code == 200 and processed.called, \
                (f"The alarm receiver must accept the merged topic {topic}: "
                 f"{response.status_code}")
        denied_request = _signed("merge-foreign", foreign_topic, signing_key)
        with mock.patch.object(sns_service, "_certificate") as never_read:
            denied = on_cloudwatch_alarm(denied_request)
        assert denied.status_code == 403 and not never_read.called, \
            "A topic in neither plane must still fail closed before certificate I/O"

        # Recovery: an installation already shadowed by an earlier Fix must heal
        # on the next one, not stay broken forever.
        system_settings.set_value(
            actor, system_settings.MONITORING_TOPICS, [portal_topic])
        service.reconcile_monitoring(actor, challenge, cutoff)
        recovered = system_settings.get_value(system_settings.MONITORING_TOPICS) or []
        assert sorted(recovered) == sorted([file_topic, portal_topic]), \
            f"Rerunning Fix must recover an already-superseded deployment topic: {recovered}"

        # A typo in an operator's conf file must not fail the whole Fix.
        deployment["value"] = "not-an-arn"
        system_settings.set_value(actor, system_settings.MONITORING_TOPICS, [])
        service.reconcile_monitoring(actor, challenge, cutoff)
        assert system_settings.get_value(system_settings.MONITORING_TOPICS) == [portal_topic], \
            "A malformed deployment entry must be dropped, not abort Fix or be persisted"


@th.django_unit_test()
def test_monitoring_proof_requires_delivery_after_this_operation(opts):
    from django.utils import timezone
    from mojo.apps.account.models import SystemSetupOperation
    from mojo.apps.account.services import system_settings
    from mojo.apps.aws.models import CloudWatchAlarm, CloudWatchAlarmTransition
    from mojo.apps.aws.services import aws_setup
    from mojo.apps.aws.services.aws_setup import AWSSetupService, delivery_probe_alarm_name

    actor = _setup_admin_identity("aws-setup-proof@test.com")
    identity = system_settings.read_installation_identity()
    topic = f"arn:aws:sns:us-east-1:123456789012:django-mojo-{identity['slug']}-operations"
    system_settings.set_value(actor, system_settings.MONITORING_TOPICS, [topic])
    expected_tags = [
        {"Key": "managed-by", "Value": "django-mojo"},
        {"Key": "purpose", "Value": "cloudwatch-incidents"},
        {"Key": "deployment", "Value": identity["slug"]},
        {"Key": "django-mojo-installation", "Value": identity["uuid"]},
    ]
    sns = mock.Mock()
    sns.list_topics.return_value = {"Topics": [{"TopicArn": topic}]}
    sns.list_tags_for_resource.return_value = {"Tags": expected_tags}
    sns.list_subscriptions_by_topic.return_value = {"Subscriptions": [{
        "Protocol": "https", "Endpoint": "https://api.example.com/api/aws/cloudwatch/sns/alarm",
        "SubscriptionArn": "arn:aws:sns:subscription/confirmed",
    }]}
    challenge = "b" * 32
    probe_name = delivery_probe_alarm_name(challenge)
    probe_arn = f"arn:aws:cloudwatch:us-east-1:123456789012:alarm:{probe_name}"
    cloudwatch = mock.Mock()
    cloudwatch.list_metrics.return_value = {"Metrics": []}
    probe = {
        "AlarmName": probe_name, "AlarmArn": probe_arn,
        "Namespace": "DjangoMojo/Setup", "MetricName": "DeliveryProbe",
        "Statistic": "Maximum", "Period": 60, "EvaluationPeriods": 1,
        "DatapointsToAlarm": 1, "Threshold": 0,
        "ComparisonOperator": "GreaterThanThreshold",
        "TreatMissingData": "notBreaching", "Dimensions": [],
        "ActionsEnabled": True, "AlarmActions": [topic], "OKActions": [topic],
        "InsufficientDataActions": [], "StateValue": "OK",
    }
    cloudwatch.describe_alarms.side_effect = lambda **kwargs: {
        "MetricAlarms": [probe] if probe_name in kwargs.get("AlarmNames", []) else []}
    cloudwatch.list_tags_for_resource.return_value = {"Tags": expected_tags}
    CloudWatchAlarm.objects.filter(alarm_key="a" * 64).delete()
    alarm = CloudWatchAlarm.objects.create(alarm_key="a" * 64, alarm_arn=probe_arn)
    CloudWatchAlarmTransition.objects.filter(topic_arn=topic).delete()
    old = CloudWatchAlarmTransition.objects.create(
        alarm=alarm, topic_arn=topic, sns_message_id="old-proof",
        old_state="OK", new_state="ALARM", state_changed_at=timezone.now(),
        is_delivery_probe=True)
    cutoff = timezone.now()
    CloudWatchAlarmTransition.objects.filter(pk=old.pk).update(
        created=cutoff - __import__("datetime").timedelta(seconds=1))
    clients = {"sns": sns, "cloudwatch": cloudwatch, "sts": _setup_sts(),
               **_discovery_clients()}
    service = AWSSetupService(clients=clients)
    policy = service._desired_topic_policy(
        {"Version": "2012-10-17", "Statement": []}, topic,
        "123456789012", identity)
    sns.get_topic_attributes.return_value = {
        "Attributes": {"Policy": __import__("json").dumps(policy)}}
    assert service._desired_topic_policy(
        policy, topic, "123456789012", identity) == policy, \
        "The already-safe owned topic policy must be exact"
    assert aws_setup._alarm_matches(probe, {
        key: value for key, value in probe.items()
        if key not in ("AlarmArn", "StateValue")}), \
        "The persisted probe alarm must match its full desired configuration"
    assert service._desired_monitoring_alarms(cloudwatch) == [], \
        "This fixture intentionally has no real resource alarms"
    assert not service.monitoring_proven(challenge, cutoff), \
        "A transition from before this operation's probe attempt must not prove delivery"
    CloudWatchAlarmTransition.objects.create(
        alarm=alarm, topic_arn=topic, sns_message_id="premature-ok",
        old_state="ALARM", new_state="OK", state_changed_at=timezone.now(),
        is_delivery_probe=True)
    assert not service.monitoring_proven(challenge, cutoff), \
        "A one-state or out-of-order probe must not prove delivery"
    alarm_receipt = CloudWatchAlarmTransition.objects.create(
        alarm=alarm, topic_arn=topic, sns_message_id="new-alarm",
        old_state="OK", new_state="ALARM", state_changed_at=timezone.now(),
        is_delivery_probe=True)
    CloudWatchAlarmTransition.objects.create(
        alarm=alarm, topic_arn=topic, sns_message_id="new-ok",
        old_state="ALARM", new_state="OK",
        state_changed_at=alarm_receipt.state_changed_at +
        __import__("datetime").timedelta(milliseconds=1), is_delivery_probe=True)
    proof_alarm, proof_ok = service._probe_evidence(topic, probe_arn, cutoff)
    assert proof_alarm is not None and proof_ok is not None, \
        "The exact ordered receipts must be selected as operation evidence"
    probe["Unit"] = "Seconds"
    assert not service.monitoring_proven(challenge, cutoff), \
        "Proof must reject an explicit Unit when the intended alarm has no Unit"
    probe.pop("Unit")
    assert service.monitoring_proven(challenge, cutoff), \
        "Only the operation's ordered ALARM then OK receipts should prove delivery"
    SystemSetupOperation.objects.filter(status__in=SystemSetupOperation.ACTIVE_STATUSES).delete()
    operation = SystemSetupOperation.objects.create(
        created_by=actor, mode="fix", section="aws_monitoring", status="reconciling",
        replay_fingerprint="b" * 64, bound_origin="https://api.example.com",
        steps=[{"id": "section:aws_monitoring", "state": "reconciling",
                "probe_challenge": challenge, "probe_cutoff": cutoff.isoformat()}])
    outcome = aws_setup.reconcile_monitoring(
        {"operation": operation, "actor": actor, "aws_clients": clients}, {})
    assert outcome["status"] == "proven", \
        "Delivery between fixer return and the next advance must use persisted proof intent"


@th.django_unit_test()
def test_existing_untagged_operations_topic_requires_explicit_adoption(opts):
    from mojo.apps.account.services import system_settings
    from mojo.apps.aws.services.aws_setup import AWSSetupService

    actor = _setup_admin_identity("aws-setup-topic-adoption@test.com")
    identity = system_settings.read_installation_identity()
    topic = f"arn:aws:sns:us-east-1:123456789012:django-mojo-{identity['slug']}-operations"
    sns = mock.Mock()
    sns.list_topics.return_value = {"Topics": [{"TopicArn": topic}]}
    sns.list_tags_for_resource.return_value = {"Tags": []}
    sns.get_topic_attributes.return_value = {"Attributes": {"Policy": __import__("json").dumps({
        "Version": "2012-10-17", "Statement": []})}}
    cloudwatch = mock.Mock()
    service = AWSSetupService(clients={
        "sns": sns, "cloudwatch": cloudwatch, "sts": _setup_sts()})
    denied = None
    try:
        service.reconcile_monitoring(
            actor, "d" * 32, __import__("django.utils.timezone", fromlist=["now"]).now(),
            send_probe=False)
    except Exception as exc:
        denied = exc
    assert denied is not None, "An existing untagged same-name topic must require a late adoption choice"
    assert not sns.tag_resource.called and not sns.set_topic_attributes.called, \
        "A refused legacy topic must remain byte-for-byte untouched"


@th.django_unit_test()
def test_monitoring_refuses_unsafe_publish_policy_before_legacy_adoption(opts):
    from mojo.apps.account.services import system_settings
    from mojo.apps.aws.services.aws_setup import AWSSetupService, _sns_publish_policy_is_safe

    actor = _setup_admin_identity("aws-setup-topic-policy@test.com")
    identity = system_settings.read_installation_identity()
    topic = f"arn:aws:sns:us-east-1:123456789012:django-mojo-{identity['slug']}-operations"
    unsafe = {"Version": "2012-10-17", "Statement": [{
        "Sid": "AttackerPublish", "Effect": "Allow", "Principal": "*",
        "Action": "sns:Publish", "Resource": topic}]}
    sns = mock.Mock()
    sns.list_topics.return_value = {"Topics": [{"TopicArn": topic}]}
    sns.list_tags_for_resource.return_value = {"Tags": []}
    sns.get_topic_attributes.return_value = {
        "Attributes": {"Policy": __import__("json").dumps(unsafe)}}
    service = AWSSetupService(clients={
        "sns": sns, "cloudwatch": mock.Mock(), "sts": _setup_sts()})
    try:
        service.reconcile_monitoring(
            actor, "e" * 32, __import__("django.utils.timezone", fromlist=["now"]).now(),
            send_probe=False, choice={"adopt_existing_topic": True, "topic_arn": topic})
    except Exception:
        pass
    else:
        raise AssertionError("Unsafe publish grants must block explicit legacy adoption")
    assert not sns.tag_resource.called and not sns.set_topic_attributes.called, \
        "Unsafe legacy policy must remain untouched and unowned"
    for action in (
            "sns:SetTopicAttributes", "sns:AddPermission", "sns:RemovePermission",
            "sns:Subscribe", "sns:GetTopicAttributes", "sns:ListSubscriptions"):
        policy = {"Statement": [{
            "Effect": "Allow", "Principal": "*", "Action": action,
            "Resource": topic}]}
        assert not _sns_publish_policy_is_safe(
            policy, "123456789012", "us-east-1", identity["slug"], topic), \
            f"Public topic authority must block adoption even when it is not Publish: {action}"
    cross_account = {"Statement": [{
        "Effect": "Allow",
        "Principal": {"AWS": "arn:aws:iam::999999999999:root"},
        "Action": "sns:Subscribe", "Resource": topic}]}
    assert not _sns_publish_policy_is_safe(
        cross_account, "123456789012", "us-east-1", identity["slug"], topic), \
        "Cross-account subscription authority must block adoption"
    same_account = {"Statement": [{
        "Effect": "Allow",
        "Principal": {"AWS": "arn:aws:iam::123456789012:root"},
        "Action": ["sns:GetTopicAttributes", "sns:Subscribe"], "Resource": topic}]}
    assert _sns_publish_policy_is_safe(
        same_account, "123456789012", "us-east-1", identity["slug"], topic), \
        "Explicit same-account owner grants must remain preservable"
    gov_topic = topic.replace("arn:aws:", "arn:aws-us-gov:")
    gov_policy = {"Statement": [{
        "Effect": "Allow", "Principal": {"Service": "cloudwatch.amazonaws.com"},
        "Action": "sns:Publish", "Resource": gov_topic,
        "Condition": {"StringEquals": {"AWS:SourceAccount": "123456789012"},
                      "ArnLike": {"AWS:SourceArn": (
                          f"arn:aws-us-gov:cloudwatch:us-east-1:123456789012:alarm:"
                          f"django-mojo/{identity['slug']}/*")}}}]}
    assert _sns_publish_policy_is_safe(
        gov_policy, "123456789012", "us-east-1", identity["slug"], gov_topic), \
        "GovCloud must derive its partition from the validated topic ARN"


@th.django_unit_test()
def test_monitoring_explicit_adoption_corrects_base_url_subscription_once(opts):
    from mojo.apps.account.services import system_settings
    from mojo.apps.aws.services.aws_setup import AWSSetupService

    actor = _setup_admin_identity("aws-setup-topic-positive@test.com")
    identity = system_settings.read_installation_identity()
    topic = f"arn:aws:sns:us-east-1:123456789012:django-mojo-{identity['slug']}-operations"
    tags = []
    policy = {"Version": "2012-10-17", "Statement": []}
    subscriptions = [{"Protocol": "https",
                      "Endpoint": "https://old.example/api/aws/cloudwatch/sns/alarm",
                      "SubscriptionArn": "arn:aws:sns:subscription/old"}]
    sns = mock.Mock()
    sns.list_topics.return_value = {"Topics": [{"TopicArn": topic}]}
    sns.list_tags_for_resource.side_effect = lambda **kwargs: {"Tags": list(tags)}
    sns.tag_resource.side_effect = lambda **kwargs: tags.extend(kwargs["Tags"])
    sns.get_topic_attributes.side_effect = lambda **kwargs: {
        "Attributes": {"Policy": __import__("json").dumps(policy)}}

    def set_policy(**kwargs):
        policy.clear()
        policy.update(__import__("json").loads(kwargs["AttributeValue"]))

    sns.set_topic_attributes.side_effect = set_policy
    sns.list_subscriptions_by_topic.side_effect = lambda **kwargs: {
        "Subscriptions": list(subscriptions)}

    def subscribe(**kwargs):
        subscriptions.append({"Protocol": kwargs["Protocol"], "Endpoint": kwargs["Endpoint"],
                              "SubscriptionArn": "arn:aws:sns:subscription/new"})
        return {"SubscriptionArn": "arn:aws:sns:subscription/new"}

    sns.subscribe.side_effect = subscribe
    alarms = {}
    cloudwatch = mock.Mock()
    cloudwatch.describe_alarms.side_effect = lambda **kwargs: {
        "MetricAlarms": [alarms[name] for name in kwargs.get("AlarmNames", []) if name in alarms]}

    def put_alarm(**kwargs):
        alarms[kwargs["AlarmName"]] = {
            **{key: value for key, value in kwargs.items() if key != "Tags"},
            "AlarmArn": ("arn:aws:cloudwatch:us-east-1:123456789012:alarm:"
                         + kwargs["AlarmName"]), "StateValue": "OK"}

    cloudwatch.put_metric_alarm.side_effect = put_alarm
    expected_tags = {**__import__(
        "mojo.apps.aws.services.aws_check", fromlist=["OWNERSHIP_TAGS"]).OWNERSHIP_TAGS,
        "deployment": identity["slug"], "django-mojo-installation": identity["uuid"]}
    cloudwatch.list_tags_for_resource.return_value = {"Tags": [
        {"Key": key, "Value": value} for key, value in expected_tags.items()]}
    service = AWSSetupService(clients={
        "sns": sns, "cloudwatch": cloudwatch, "sts": _setup_sts()})
    service._desired_monitoring_alarms = lambda cloudwatch: []
    choice = {"adopt_existing_topic": True, "topic_arn": topic}
    cutoff = __import__("django.utils.timezone", fromlist=["now"]).now()
    service.reconcile_monitoring(actor, "f" * 32, cutoff, send_probe=False, choice=choice)
    service.reconcile_monitoring(actor, "f" * 32, cutoff, send_probe=False, choice=choice)
    assert sns.tag_resource.call_count == 1, "Explicit ownership adoption must be idempotent"
    assert sns.subscribe.call_count == 1, "The corrected BASE_URL subscription must be added once"
    assert any(row["Endpoint"].startswith("https://old.example") for row in subscriptions), \
        "Subscription correction must preserve the unrelated legacy endpoint"
    assert any(row["Endpoint"].startswith("https://api.example.com") for row in subscriptions), \
        "The current BASE_URL endpoint must be subscribed"


@th.django_unit_test()
def test_monitoring_creates_and_converges_fresh_owned_topic_once(opts):
    from mojo.apps.account.services import system_settings
    from mojo.apps.aws.services.aws_setup import AWSSetupService

    actor = _setup_admin_identity("aws-setup-topic-fresh@test.com")
    identity = system_settings.read_installation_identity()
    topic = f"arn:aws:sns:us-east-1:123456789012:django-mojo-{identity['slug']}-operations"
    topics, tags, subscriptions = [], [], []
    policy = {"Version": "2012-10-17", "Statement": []}
    sns = mock.Mock()
    sns.list_topics.side_effect = lambda **kwargs: {"Topics": list(topics)}

    def create_topic(**kwargs):
        topics.append({"TopicArn": topic})
        tags.extend(kwargs["Tags"])
        return {"TopicArn": topic}

    sns.create_topic.side_effect = create_topic
    sns.list_tags_for_resource.side_effect = lambda **kwargs: {"Tags": list(tags)}
    sns.get_topic_attributes.side_effect = lambda **kwargs: {
        "Attributes": {"Policy": __import__("json").dumps(policy)}}

    def set_policy(**kwargs):
        policy.clear()
        policy.update(__import__("json").loads(kwargs["AttributeValue"]))

    sns.set_topic_attributes.side_effect = set_policy
    sns.list_subscriptions_by_topic.side_effect = lambda **kwargs: {
        "Subscriptions": list(subscriptions)}

    def subscribe(**kwargs):
        subscriptions.append({
            "Protocol": kwargs["Protocol"], "Endpoint": kwargs["Endpoint"],
            "SubscriptionArn": "arn:aws:sns:subscription/fresh"})
        return {"SubscriptionArn": "arn:aws:sns:subscription/fresh"}

    sns.subscribe.side_effect = subscribe
    alarms = {}
    cloudwatch = mock.Mock()
    cloudwatch.describe_alarms.side_effect = lambda **kwargs: {
        "MetricAlarms": [alarms[name] for name in kwargs.get("AlarmNames", [])
                         if name in alarms]}

    def put_alarm(**kwargs):
        alarms[kwargs["AlarmName"]] = {
            **{key: value for key, value in kwargs.items() if key != "Tags"},
            "AlarmArn": ("arn:aws:cloudwatch:us-east-1:123456789012:alarm:"
                         + kwargs["AlarmName"]), "StateValue": "OK"}

    cloudwatch.put_metric_alarm.side_effect = put_alarm
    cloudwatch.list_tags_for_resource.side_effect = lambda **kwargs: {"Tags": list(tags)}
    service = AWSSetupService(clients={
        "sns": sns, "cloudwatch": cloudwatch, "sts": _setup_sts()})
    service._desired_monitoring_alarms = lambda cloudwatch: []
    cutoff = __import__("django.utils.timezone", fromlist=["now"]).now()
    service.reconcile_monitoring(actor, "1" * 32, cutoff, send_probe=False)
    service.reconcile_monitoring(actor, "1" * 32, cutoff, send_probe=False)
    assert sns.create_topic.call_count == 1, "A fresh owned topic must be created once"
    assert sns.tag_resource.call_count == 0, "Creation tags establish ownership atomically"
    assert sns.set_topic_attributes.call_count == 1, "The safe topic policy must converge once"
    assert sns.subscribe.call_count == 1 and cloudwatch.put_metric_alarm.call_count == 1, \
        "Fresh SNS subscription and probe alarm must converge without replay"


@th.django_unit_test()
def test_legacy_cli_adoption_uses_fail_closed_setup_classifier(opts):
    from mojo.apps.aws.services import aws_check
    from mojo.apps.fileman.models import FileManager

    _setup_admin_identity("aws-check-cli-adopt@test.com")
    FileManager.objects.filter(
        user__isnull=True, group__isnull=True, backend_type=FileManager.AWS_S3,
    ).delete()
    unsafe = _safe_bucket_client("unsafe-owned-bucket")
    unsafe.get_bucket_acl.return_value = {
        "Owner": {"ID": "canonical-owner"},
        "Grants": [{"Grantee": {"Type": "Group", "URI": (
            "http://acs.amazonaws.com/groups/global/AllUsers")},
                    "Permission": "READ"}],
    }
    runner = aws_check.AWSCheckRunner(
        region="us-east-1", clients={"s3": unsafe, "sts": _setup_sts()})

    try:
        runner._adopt_bucket("unsafe-owned-bucket")
    except Exception:
        pass
    else:
        assert False, "Legacy --adopt-bucket must reject a public ACL"
    unsafe.put_bucket_tagging.assert_not_called()
    unsafe.put_public_access_block.assert_not_called()

    safe = _safe_bucket_client("safe-owned-bucket")
    cors = []
    safe.get_bucket_cors.side_effect = lambda **kwargs: {"CORSRules": list(cors)}
    safe.put_bucket_cors.side_effect = lambda **kwargs: cors.__setitem__(
        slice(None), kwargs["CORSConfiguration"]["CORSRules"])
    manager = aws_check.AWSCheckRunner(
        region="us-east-1", clients={"s3": safe, "sts": _setup_sts()},
    )._adopt_bucket("safe-owned-bucket")
    assert manager.backend_url == "s3://safe-owned-bucket" \
        and manager.supports_direct_upload and not manager.is_public, \
        "The compatible safe CLI path must create the same private FileManager"


@th.django_unit_test()
def test_setup_rejects_public_bucket_without_mutation(opts):
    from mojo.apps.aws.services.aws_setup import AWSSetupService

    s3 = _safe_bucket_client("public-media")
    s3.get_bucket_policy.side_effect = None
    s3.get_bucket_policy.return_value = {"Policy": __import__("json").dumps({
        "Statement": [{"Effect": "Allow", "Principal": "*", "Action": "s3:GetObject"}]})}
    result = AWSSetupService(
        clients={"s3": s3, "sts": _setup_sts()}).discover_buckets()
    assert result["candidates"] == [], f"A public-policy bucket must not be adoptable: {result}"
    assert result["rejected"] == ["public-media"], f"Rejected exact name should be reported: {result}"
    assert not s3.put_public_access_block.called and not s3.put_bucket_tagging.called, \
        "Discovery must remain side-effect-free"

    from botocore.exceptions import ClientError
    from mojo.helpers.aws.provider_call import ProviderCallError
    denied = _safe_bucket_client("denied-media")
    denied.get_bucket_location.side_effect = ClientError({
        "Error": {"Code": "AccessDenied", "Message": "private detail"},
        "ResponseMetadata": {"HTTPStatusCode": 403},
    }, "GetBucketLocation")
    try:
        AWSSetupService(
            clients={"s3": denied, "sts": _setup_sts()}).discover_buckets()
    except ProviderCallError as exc:
        assert exc.detail().get("iam_action") == "s3:GetBucketLocation", \
            f"Discovery IAM denial must expose the exact safe action: {exc.detail()}"
    else:
        raise AssertionError("S3 discovery must not misclassify IAM denial as no candidate")


def _verified_sts():
    sts = mock.Mock()
    sts.get_caller_identity.return_value = {
        "Account": "123456789012",
        "Arn": "arn:aws:sts::123456789012:assumed-role/app/runner",
    }
    return sts


# Moved from tests/test_aws/aws_check.py for the #2558 parallel-safety
# promotion: it patches the aws_check module's `uuid` attribute, which is
# process-global state with no injection seam.
@th.django_unit_test()
def test_s3_probe_reports_exact_cleanup_failure(opts):
    from botocore.exceptions import ReadTimeoutError
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
    secret = "https://s3.example/?X-Amz-Credential=CLEANUP-SECRET"
    s3.delete_object.side_effect = ReadTimeoutError(endpoint_url=secret)
    with mock.patch.object(aws_check.uuid, "uuid4", side_effect=[SimpleNamespace(hex="sentinel"), SimpleNamespace(hex="probe")]):
        report = aws_check.AWSCheckRunner(
            clients={"sts": _verified_sts(), "s3": s3},
            apply=True, yes=True, probe_s3=True,
        ).run(["s3"])
    cleanup = next(item for item in report["items"] if item["code"] == "bucket.probe_cleanup_failed")
    assert cleanup["details"]["key"] == "__django_mojo_aws_check__/sentinel", \
        f"Cleanup failure must name the exact sentinel, got {cleanup}"
    assert cleanup["details"]["operation"] == "s3.delete_object", \
        f"Cleanup ambiguity must retain its bounded operation: {cleanup}"
    assert "iam_action" not in cleanup["details"], \
        "Non-authorization failures must not claim a missing IAM action"
    assert cleanup["details"]["mutation_state"] == "unknown", \
        f"A timed-out cleanup mutation must remain explicitly ambiguous: {cleanup}"
    assert secret not in str(cleanup), "Cleanup evidence must not expose the provider endpoint"
    manager.delete()


# Moved from tests/test_aws/aws_check.py for the #2558 parallel-safety
# promotion: it patches `run` on the management command module's AWSCheckRunner
# reference, which is the shared class, not a locally-constructed instance.
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
