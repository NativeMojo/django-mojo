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


def _target_group(name="api", kind="net", attached=True, tg_id="0123456789abcdef",
                  lb_id="fedcba9876543210"):
    """One describe_target_groups row, with the real ARN shapes CloudWatch keys on."""
    region_account = "us-east-1:123456789012"
    return {
        "TargetGroupName": name,
        "TargetGroupArn": (f"arn:aws:elasticloadbalancing:{region_account}:"
                           f"targetgroup/{name}/{tg_id}"),
        "LoadBalancerArns": ([f"arn:aws:elasticloadbalancing:{region_account}:"
                              f"loadbalancer/{kind}/{name}-lb/{lb_id}"] if attached else []),
    }


def _owned_operations_sns(topic, confirmed=True):
    """An owned, discovered operations topic with an HTTPS receiver subscription."""
    sns = mock.Mock()
    sns.list_topics.return_value = {"Topics": [{"TopicArn": topic}]}
    sns.list_tags_for_resource.return_value = {"Tags": [
        {"Key": "managed-by", "Value": "django-mojo"},
        {"Key": "purpose", "Value": "cloudwatch-incidents"},
        {"Key": "deployment", "Value": "test"},
    ]}
    sns.list_subscriptions_by_topic.return_value = {"Subscriptions": [{
        "Protocol": "https", "Endpoint": "https://api.example.com/api/aws/cloudwatch/sns/alarm",
        "SubscriptionArn": "arn:aws:sns:subscription/confirmed" if confirmed else "PendingConfirmation",
    }]}
    return sns


def _owned_alarm_tags():
    return {"Tags": [
        {"Key": "managed-by", "Value": "django-mojo"},
        {"Key": "purpose", "Value": "cloudwatch-incidents"},
        {"Key": "deployment", "Value": "test"},
    ]}


def _cloudwatch(metrics=None, **attrs):
    """
    A cloudwatch stub whose list_metrics answers explicitly.

    Without this, a bare Mock returns a Mock from list_metrics(...).get("Metrics")
    — which is truthy — so every test would silently acquire the deployment-wide
    certificate alarm and assert against a profile it never meant to build.
    """
    cloudwatch = mock.Mock(**attrs)
    cloudwatch.list_metrics.return_value = {"Metrics": metrics or []}
    return cloudwatch


def _stale_alarm_cloudwatch(alarm):
    """describe_alarms answers the stale-detection sweep with `alarm`, the desired loop with nothing."""
    cloudwatch = _cloudwatch()

    def describe_alarms(**kwargs):
        if kwargs.get("AlarmTypes"):
            return {"MetricAlarms": [alarm]}
        return {"MetricAlarms": []}

    cloudwatch.describe_alarms.side_effect = describe_alarms
    cloudwatch.list_tags_for_resource.return_value = _owned_alarm_tags()
    return cloudwatch


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
def test_provider_boundary_exposes_only_bounded_iam_evidence(opts):
    import traceback
    from botocore.exceptions import ClientError
    from mojo.helpers.aws.provider_call import ProviderCallError, ProviderCaller

    secret = "AKIAABCDEFGHIJKLMNOP"
    denied = ClientError({
        "Error": {"Code": "AccessDenied", "Message": f"credential={secret}"},
        "ResponseMetadata": {"HTTPStatusCode": 403, "RequestId": "request-12345678"},
    }, "PutBucketCors")
    logger = mock.Mock()
    caller = ProviderCaller(logger)
    caught = None
    try:
        caller.call("s3.put_bucket_cors", lambda: (_ for _ in ()).throw(denied),
                    "s3:PutBucketCORS", mutation=True)
    except ProviderCallError as exc:
        caught = exc
    assert caught is not None, "The provider boundary must return a typed safe failure"
    detail = caught.detail()
    assert detail["provider_code"] == "AccessDenied", f"Expected safe AWS code, got {detail}"
    assert detail["iam_action"] == "s3:PutBucketCORS", f"Expected exact IAM action, got {detail}"
    assert detail["request_id"] == "request-12345678", f"Expected bounded request id, got {detail}"
    assert secret not in str(detail) and secret not in str(logger.method_calls), \
        "Raw provider messages and credentials must not reach details or logs"
    rendered = "".join(traceback.format_exception(caught))
    assert secret not in rendered and "credential=" not in rendered, \
        "The safe provider exception must not retain a raw botocore cause in its traceback"

    sns_denied = ClientError({
        "Error": {"Code": "AuthorizationError", "Message": "not authorized"},
        "ResponseMetadata": {"HTTPStatusCode": 403},
    }, "SetTopicAttributes")
    try:
        caller.call(
            "sns.set_topic_attributes",
            lambda: (_ for _ in ()).throw(sns_denied),
            "sns:SetTopicAttributes",
            mutation=True,
        )
    except ProviderCallError as exc:
        assert exc.detail().get("iam_action") == "sns:SetTopicAttributes", \
            f"Every HTTP 403 denial must retain the exact safe IAM action: {exc.detail()}"
    else:
        raise AssertionError("The SNS authorization denial must be mapped safely")


@th.django_unit_test()
def test_aws_check_provider_paths_keep_exact_denied_actions(opts):
    from botocore.exceptions import ClientError
    from mojo.apps.aws.services import aws_check
    from mojo.apps.fileman.models import FileManager

    denied = lambda operation: ClientError({
        "Error": {"Code": "AuthorizationError", "Message": "raw private text"},
        "ResponseMetadata": {"HTTPStatusCode": 403},
    }, operation)
    FileManager.objects.filter(
        user__isnull=True, group__isnull=True, backend_type=FileManager.AWS_S3,
    ).delete()
    manager = FileManager.objects.create(
        name="provider-boundary-s3", backend_type=FileManager.AWS_S3,
        backend_url="s3://provider-boundary-s3", is_active=True, is_default=True,
        is_public=False)
    s3 = mock.Mock()
    s3.head_bucket.side_effect = denied("HeadBucket")
    s3_report = aws_check.AWSCheckRunner(
        clients={"s3": s3, "sts": _verified_sts()}).run(["s3"])
    s3_item = next(row for row in s3_report["items"] if row["code"] == "bucket.denied")
    assert s3_item["details"]["iam_action"] == "s3:ListBucket", \
        f"AWS Check S3 denial lost its exact IAM action: {s3_item}"
    manager.delete()

    sns = mock.Mock()
    sns.list_topics.side_effect = denied("ListTopics")
    cloudwatch = mock.Mock()
    cloudwatch.list_metrics.return_value = {"Metrics": []}
    report = aws_check.AWSCheckRunner(clients={
        "sns": sns, "cloudwatch": cloudwatch, **_discovery_clients(),
    }).run(["monitoring"])
    sns_item = next(row for row in report["items"] if row["code"] == "monitoring.denied")
    assert sns_item["details"]["iam_action"] == "sns:ListTopics", \
        f"AWS Check SNS denial lost its exact IAM action: {sns_item}"

    cloudwatch.list_metrics.side_effect = denied("ListMetrics")
    cloudwatch.list_metrics.return_value = None
    sns.list_topics.side_effect = None
    sns.list_topics.return_value = {"Topics": []}
    report = aws_check.AWSCheckRunner(clients={
        "sns": sns, "cloudwatch": cloudwatch, **_discovery_clients(),
    }).run(["monitoring"])
    cw_item = next(row for row in report["items"] if row["code"] == "alarms.cert_metric_unknown")
    assert cw_item["details"]["iam_action"] == "cloudwatch:ListMetrics", \
        f"AWS Check CloudWatch denial lost its exact IAM action: {cw_item}"


@th.django_unit_test()
def test_provider_boundary_reports_sts_ses_and_discovery_denials_safely(opts):
    from botocore.exceptions import ClientError
    from mojo.apps.aws.services import aws_check
    from mojo.apps.aws.services.aws_setup import AWSSetupService
    from mojo.helpers.aws.provider_call import ProviderCallError, ProviderCaller

    sentinel = "credential=SHOULD-NEVER-ESCAPE"

    def denied(operation):
        return ClientError({
            "Error": {"Code": "AuthorizationError", "Message": sentinel},
            "ResponseMetadata": {"HTTPStatusCode": 403, "RequestId": "safe-request-1234"},
        }, operation)

    logger = mock.Mock()
    provider = ProviderCaller(logger)
    sts = mock.Mock()
    sts.get_caller_identity.side_effect = denied("GetCallerIdentity")
    identity_report = aws_check.AWSCheckRunner(
        clients={"sts": sts}, provider=provider).run(["identity"])
    identity_item = next(row for row in identity_report["items"]
                         if row["code"] == "sts.denied")
    assert identity_item["details"]["iam_action"] == "sts:GetCallerIdentity"

    ses = mock.Mock()
    ses.list_identities.side_effect = denied("ListIdentities")
    try:
        AWSSetupService(clients={"ses": ses}, provider=provider).discover_verified_domains()
    except ProviderCallError as exc:
        assert exc.detail()["iam_action"] == "ses:ListIdentities", \
            f"SES denial lost its exact action: {exc.detail()}"
    else:
        raise AssertionError("SES discovery denial must cross ProviderCaller")

    action_by_service = {
        "ec2": "ec2:DescribeInstances",
        "rds": "rds:DescribeDBInstances",
        "elasticache": "elasticache:DescribeCacheClusters",
        "elbv2": "elasticloadbalancing:DescribeTargetGroups",
    }
    for service, action in action_by_service.items():
        clients = _discovery_clients()
        clients[service].get_paginator.return_value.paginate.side_effect = denied(
            action.rsplit(":", 1)[-1])
        runner = aws_check.AWSCheckRunner(clients=clients, provider=provider)
        runner._desired_alarms()
        expected_code = ("resources.elbv2_denied" if service == "elbv2"
                         else "resources.discovery_denied")
        item = next(row for row in runner.results if row["code"] == expected_code)
        assert item["details"]["iam_action"] == action, \
            f"{service} denial lost {action}: {item}"
        assert sentinel not in str(item), f"{service} leaked the raw provider error"
    assert sentinel not in str(identity_report) and sentinel not in str(logger.method_calls), \
        "Raw STS/SES/discovery provider messages must not reach reports or logs"


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
def test_setup_ses_discovery_is_complete_and_paginated(opts):
    from mojo.apps.aws.services.aws_setup import AWSSetupService

    ses = mock.Mock()

    def list_identities(**kwargs):
        if kwargs.get("NextToken") == "next-page":
            return {"Identities": ["second.example"]}
        return {"Identities": ["first.example"], "NextToken": "next-page"}

    ses.list_identities.side_effect = list_identities
    ses.get_identity_verification_attributes.side_effect = lambda **kwargs: {
        "VerificationAttributes": {
            name: {"VerificationStatus": "Success"}
            for name in kwargs["Identities"]
        }}
    discovered = AWSSetupService(clients={"ses": ses}).discover_verified_domains()
    assert discovered == ["first.example", "second.example"], \
        f"SES discovery must follow every provider page: {discovered}"
    assert ses.list_identities.call_count == 2, \
        "SES discovery stopped before the continuation token"


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
            clients={"sts": _verified_sts(), "sns": sns, "cloudwatch": _cloudwatch(),
                     **_discovery_clients()},
            apply=True, yes=True,
        ).run(["monitoring"])
    codes = [item["code"] for item in report["items"]]
    assert "sns.topic_not_allowlisted" in codes, f"Missing exact allowlist must be pending, got {codes}"
    assert sns.subscribe.call_count == 0, "The receiver must not be subscribed before static allowlisting"


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
def test_create_system_bucket_installs_wildcard_direct_upload_cors(opts):
    from mojo.apps.fileman.models import FileManager
    from mojo.apps.aws.services import aws_check

    FileManager.objects.filter(
        user__isnull=True, group__isnull=True, backend_type=FileManager.AWS_S3,
    ).delete()
    s3 = mock.Mock()
    runner = aws_check.AWSCheckRunner(region="us-east-1", clients={"s3": s3})
    owned = dict(aws_check.OWNERSHIP_TAGS, deployment="test")

    with mock.patch.object(runner, "_bucket_tags", side_effect=[{}, owned]), \
            mock.patch.object(runner, "_deployment_slug", return_value="test"):
        manager = runner._create_bucket("system-media")

    assert manager.allowed_origins == ["*"], \
        f"system direct-upload manager should persist wildcard origins, got {manager.allowed_origins}"
    cors = s3.put_bucket_cors.call_args.kwargs["CORSConfiguration"]["CORSRules"]
    assert cors[0]["AllowedOrigins"] == ["*"], \
        f"system media bucket should allow presigned uploads from every browser origin, got {cors}"
    assert {"PUT", "POST", "HEAD"}.issubset(set(cors[0]["AllowedMethods"])), \
        f"system media CORS should support both direct-upload signing modes, got {cors}"
    manager.delete()


@th.django_unit_test()
def test_apply_repairs_missing_system_bucket_direct_upload_cors(opts):
    from mojo.apps.fileman.models import FileManager
    from mojo.apps.aws.services import aws_check

    FileManager.objects.filter(
        user__isnull=True, group__isnull=True, backend_type=FileManager.AWS_S3,
    ).delete()
    manager = FileManager.objects.create(
        name="aws-check-cors-repair", backend_type=FileManager.AWS_S3,
        backend_url="s3://system-media", is_active=True, is_default=True,
        is_public=False, supports_direct_upload=True,
    )
    s3 = mock.Mock()
    s3.head_bucket.return_value = {}
    s3.get_public_access_block.return_value = {"PublicAccessBlockConfiguration": {
        "BlockPublicAcls": True, "IgnorePublicAcls": True,
        "BlockPublicPolicy": True, "RestrictPublicBuckets": True,
    }}
    s3.get_bucket_cors.side_effect = aws_check.ClientError({
        "Error": {"Code": "NoSuchCORSConfiguration", "Message": "missing"},
    }, "GetBucketCors")

    try:
        report = aws_check.AWSCheckRunner(
            clients={"sts": _verified_sts(), "s3": s3}, apply=True, yes=True,
        ).run(["s3"])
        manager.refresh_from_db()
        configured = [item for item in report["items"] if item["code"] == "bucket.cors_configured"]
        assert configured and configured[0]["status"] == "pass", \
            f"apply should report repaired direct-upload CORS, got {report}"
        assert manager.allowed_origins == ["*"], \
            f"repaired system manager should persist wildcard origins, got {manager.allowed_origins}"
        s3.put_bucket_cors.assert_called_once()
    finally:
        manager.delete()


@th.django_unit_test()
def test_s3_configured_region_mismatch_fails(opts):
    from mojo.apps.fileman.models import FileManager
    from mojo.apps.aws.services import aws_check

    FileManager.objects.filter(
        user__isnull=True, group__isnull=True, backend_type=FileManager.AWS_S3,
    ).delete()
    manager = FileManager.objects.create(
        name="aws-check-region-test", backend_type=FileManager.AWS_S3,
        backend_url="s3://aws-check-region-test", is_active=True, is_default=True,
        is_public=False,
    )
    manager.set_aws_region("us-west-2")
    manager.save()
    s3 = mock.Mock()
    s3.head_bucket.return_value = {"ResponseMetadata": {"HTTPHeaders": {
        "x-amz-bucket-region": "us-east-1",
    }}}
    report = aws_check.AWSCheckRunner(
        region="us-west-2", clients={"s3": s3},
    ).run(["s3"])
    mismatch = next(item for item in report["items"] if item["code"] == "bucket.region_mismatch")
    assert mismatch["status"] == "fail", f"Region mismatch must fail, got {mismatch}"
    s3.get_public_access_block.assert_not_called()
    manager.delete()


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
            clients={"sns": sns, "cloudwatch": _cloudwatch(), **_discovery_clients()},
        ).run(["monitoring"])
    assert report["overall"] == "fail", f"Cross-deployment topic must conflict, got {report}"
    assert "sns.topic_conflict" in [item["code"] for item in report["items"]]

    sns.list_tags_for_resource.return_value["Tags"][-1]["Value"] = "test"
    sns.list_subscriptions_by_topic.return_value = {"Subscriptions": [{
        "Protocol": "https", "Endpoint": "https://api.example.com/api/aws/cloudwatch/sns/alarm",
        "SubscriptionArn": "arn:aws:sns:subscription/confirmed",
    }]}
    cloudwatch = _cloudwatch()
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
        sns_topic_inbound_arn=None, receiving_enabled=False, metadata={}, save=mock.Mock(),
    )
    report = SimpleNamespace(items=[
        SimpleNamespace(resource="ses.identity.verification", current="Success", status="ok"),
        SimpleNamespace(resource="ses.identity.dkim", current={
            "Enabled": True, "VerificationStatus": "Success",
        }, status="ok"),
    ])
    runner = aws_check.AWSCheckRunner(clients={"ses": ses, "sns": sns})

    created = runner._create_missing_email_resources(domain, report)
    assert created == ["topic-reference:bounce"], \
        f"The existing operator mapping should be adopted without creating a topic, got {created}"
    ses.set_identity_notification_topic.assert_not_called()
    sns.create_topic.assert_not_called()
    domain.save.assert_called_once()

    sns.list_topics.return_value = {"Topics": [{"TopicArn": domain.sns_topic_bounce_arn}]}
    created = runner._create_missing_email_resources(domain, report)
    assert created == [], f"A healthy rerun must be a no-op, got {created}"
    sns.create_topic.assert_not_called()


@th.django_unit_test()
def test_email_uses_selected_context_and_rejects_unowned_topic_collision(opts):
    from mojo.apps.aws.services import aws_check

    domain = SimpleNamespace(
        name="example.com", aws_key=None, aws_secret=None, aws_region="us-east-1",
        receiving_enabled=False, sns_topic_bounce_arn=None,
        sns_topic_complaint_arn="arn:configured-complaint",
        sns_topic_delivery_arn="arn:configured-delivery", sns_topic_inbound_arn=None,
        metadata={}, save=mock.Mock(),
    )
    runner = aws_check.AWSCheckRunner(profile="operators")
    with mock.patch.object(runner, "_client") as selected_client:
        runner._email_client_factory(domain)(
            "ses", access_key="settings-key", secret_key="settings-secret", region="us-east-1",
        )
    selected_client.assert_called_once_with("ses")

    topic = "arn:aws:sns:us-east-1:123456789012:ses-example-com-bounce"
    ses, sns = mock.Mock(), mock.Mock()
    ses.get_identity_notification_attributes.return_value = {
        "NotificationAttributes": {"example.com": {}},
    }
    sns.list_topics.return_value = {"Topics": [{"TopicArn": topic}]}
    sns.list_tags_for_resource.return_value = {"Tags": []}
    report = SimpleNamespace(items=[
        SimpleNamespace(resource="ses.identity.verification", current="Success", status="ok"),
        SimpleNamespace(resource="ses.identity.dkim", current={
            "Enabled": True, "VerificationStatus": "Success",
        }, status="ok"),
    ])
    runner = aws_check.AWSCheckRunner(clients={"ses": ses, "sns": sns})
    try:
        runner._create_missing_email_resources(domain, report)
    except RuntimeError as exc:
        assert "not owned" in str(exc), f"Unexpected collision error: {exc}"
    else:
        assert False, "An unowned same-name SES topic must not be adopted"
    domain.save.assert_not_called()
    sns.create_topic.assert_not_called()


@th.django_unit_test()
def test_desired_alarms_unchanged_for_existing_resources(opts):
    """
    The alarm dicts emitted for EC2/RDS/ElastiCache must stay byte-identical.

    check_monitoring compares every desired alarm field-by-field against what is
    already in CloudWatch and reports `alarms.drifted` on any difference. A
    refactor that changes an emitted field would make every deployment report
    drift on alarms that are in fact correct, so this pins the exact output.
    """
    from mojo.apps.aws.services import aws_check

    clients = _discovery_clients(
        instances=[{"InstanceId": "i-abc", "State": {"Name": "running"},
                    "InstanceType": "m5.large"}],
        db_instances=[{"DBInstanceIdentifier": "plain-pg", "DBInstanceStatus": "available",
                       "Engine": "postgres", "DBInstanceClass": "db.m5.large"}],
        cache_clusters=[{"CacheClusterId": "cache-1", "CacheClusterStatus": "available"}],
    )
    with mock.patch.object(aws_check, "_setting", side_effect=_setting_values()):
        desired = aws_check.AWSCheckRunner(clients=clients)._desired_alarms()

    by_name = {alarm["AlarmName"]: alarm for alarm in desired}
    expected = {
        "django-mojo/test/ec2/i-abc/status": {
            "AlarmName": "django-mojo/test/ec2/i-abc/status",
            "Namespace": "AWS/EC2", "MetricName": "StatusCheckFailed",
            "Dimensions": [{"Name": "InstanceId", "Value": "i-abc"}],
            "Statistic": "Maximum", "ComparisonOperator": "GreaterThanOrEqualToThreshold",
            "Threshold": 1, "Period": 60, "EvaluationPeriods": 2,
            "DatapointsToAlarm": 2, "TreatMissingData": "notBreaching",
        },
        "django-mojo/test/ec2/i-abc/cpu": {
            "AlarmName": "django-mojo/test/ec2/i-abc/cpu",
            "Namespace": "AWS/EC2", "MetricName": "CPUUtilization",
            "Dimensions": [{"Name": "InstanceId", "Value": "i-abc"}],
            "Statistic": "Average", "ComparisonOperator": "GreaterThanOrEqualToThreshold",
            "Threshold": 90, "Period": 300, "EvaluationPeriods": 3,
            "DatapointsToAlarm": 3, "TreatMissingData": "notBreaching",
        },
        "django-mojo/test/rds/plain-pg/cpu": {
            "AlarmName": "django-mojo/test/rds/plain-pg/cpu",
            "Namespace": "AWS/RDS", "MetricName": "CPUUtilization",
            "Dimensions": [{"Name": "DBInstanceIdentifier", "Value": "plain-pg"}],
            "Statistic": "Average", "ComparisonOperator": "GreaterThanOrEqualToThreshold",
            "Threshold": 90, "Period": 300, "EvaluationPeriods": 3,
            "DatapointsToAlarm": 3, "TreatMissingData": "notBreaching",
        },
        "django-mojo/test/rds/plain-pg/free-storage": {
            "AlarmName": "django-mojo/test/rds/plain-pg/free-storage",
            "Namespace": "AWS/RDS", "MetricName": "FreeStorageSpace",
            "Dimensions": [{"Name": "DBInstanceIdentifier", "Value": "plain-pg"}],
            "Statistic": "Average", "ComparisonOperator": "LessThanOrEqualToThreshold",
            "Threshold": 10 * 1024 ** 3, "Period": 300, "EvaluationPeriods": 3,
            "DatapointsToAlarm": 3, "TreatMissingData": "notBreaching",
        },
        "django-mojo/test/elasticache/cache-1/cpu": {
            "AlarmName": "django-mojo/test/elasticache/cache-1/cpu",
            "Namespace": "AWS/ElastiCache", "MetricName": "CPUUtilization",
            "Dimensions": [{"Name": "CacheClusterId", "Value": "cache-1"}],
            "Statistic": "Average", "ComparisonOperator": "GreaterThanOrEqualToThreshold",
            "Threshold": 90, "Period": 300, "EvaluationPeriods": 3,
            "DatapointsToAlarm": 3, "TreatMissingData": "notBreaching",
        },
    }
    for name, alarm in expected.items():
        assert name in by_name, \
            f"The {name} alarm disappeared from the profile; got {sorted(by_name)}"
        assert dict(by_name[name]) == alarm, (
            f"The {name} alarm dict changed shape, which makes every existing "
            f"deployment report drift.\nexpected {alarm}\ngot      {dict(by_name[name])}"
        )


@th.django_unit_test()
def test_target_group_alarm_uses_arn_suffix_dimensions(opts):
    """
    CloudWatch keys ELBv2 metrics on the ARN *suffix*, never the full ARN.

    A full ARN is accepted by put_metric_alarm and then never receives a
    datapoint, so the alarm sits green forever — the failure this asserts away.
    """
    from mojo.apps.aws.services import aws_check

    clients = _discovery_clients(target_groups=[
        _target_group(name="api", kind="net"),
        _target_group(name="web", kind="app", tg_id="aaaa1111", lb_id="bbbb2222"),
    ])
    with mock.patch.object(aws_check, "_setting", side_effect=_setting_values()):
        desired = aws_check.AWSCheckRunner(clients=clients)._desired_alarms()

    by_name = {alarm["AlarmName"]: alarm for alarm in desired}
    net = by_name.get("django-mojo/test/elbv2/targetgroup~api~0123456789abcdef/healthy-hosts")
    assert net is not None, (
        "The target-group alarm name must carry the ~-escaped suffix; "
        f"got {sorted(by_name)}"
    )
    assert net["Dimensions"] == [
        {"Name": "TargetGroup", "Value": "targetgroup/api/0123456789abcdef"},
        {"Name": "LoadBalancer", "Value": "net/api-lb/fedcba9876543210"},
    ], f"Network LB dimensions must be bare ARN suffixes, got {net['Dimensions']}"
    assert net["Namespace"] == "AWS/NetworkELB", \
        f"A net/ load balancer publishes to AWS/NetworkELB, got {net['Namespace']}"

    web = by_name["django-mojo/test/elbv2/targetgroup~web~aaaa1111/healthy-hosts"]
    assert web["Dimensions"][1] == {"Name": "LoadBalancer", "Value": "app/web-lb/bbbb2222"}, \
        f"Application LB suffix must start at app/, got {web['Dimensions']}"
    assert web["Namespace"] == "AWS/ApplicationELB", \
        f"An app/ load balancer publishes to AWS/ApplicationELB, got {web['Namespace']}"


@th.django_unit_test()
def test_target_group_without_load_balancer_is_skipped(opts):
    from mojo.apps.aws.services import aws_check

    clients = _discovery_clients(target_groups=[_target_group(name="orphan", attached=False)])
    with mock.patch.object(aws_check, "_setting", side_effect=_setting_values()):
        desired = aws_check.AWSCheckRunner(clients=clients)._desired_alarms()
    assert desired == [], (
        "An unattached target group publishes no metrics, so alarming on it would "
        f"sit in INSUFFICIENT_DATA forever; got {desired}"
    )


@th.django_unit_test()
def test_empty_target_group_is_caught_by_healthy_host_alarm(opts):
    """
    The outage UnHealthyHostCount cannot see.

    With every target deregistered the group reports 0 unhealthy hosts, which
    under notBreaching reads as permanently healthy. HealthyHostCount < 1 with
    breaching is the only signal that fires.
    """
    from mojo.apps.aws.services import aws_check

    clients = _discovery_clients(target_groups=[_target_group()])
    with mock.patch.object(aws_check, "_setting", side_effect=_setting_values()):
        desired = aws_check.AWSCheckRunner(clients=clients)._desired_alarms()

    by_metric = {alarm["MetricName"]: alarm for alarm in desired}
    healthy = by_metric.get("HealthyHostCount")
    assert healthy is not None, \
        f"Every target group needs a HealthyHostCount alarm, got {sorted(by_metric)}"
    assert healthy["ComparisonOperator"] == "LessThanThreshold" and healthy["Threshold"] == 1, (
        "The tier is gone when fewer than one host is healthy; "
        f"got {healthy['ComparisonOperator']} {healthy['Threshold']}"
    )
    assert healthy["Statistic"] == "Minimum", \
        f"A Minimum statistic catches the worst datapoint in the window, got {healthy['Statistic']}"
    assert healthy["TreatMissingData"] == "breaching", (
        "A load balancer that stops reporting HealthyHostCount is the outage, so "
        f"missing data must breach; got {healthy['TreatMissingData']}"
    )

    unhealthy = by_metric["UnHealthyHostCount"]
    assert unhealthy["TreatMissingData"] == "notBreaching", (
        "UnHealthyHostCount is the partial-degradation signal and must not page on "
        f"quiet; got {unhealthy['TreatMissingData']}"
    )


@th.django_unit_test()
def test_credit_balance_alarm_only_on_burstable(opts):
    """
    Only burstable families publish CPUCreditBalance.

    An alarm on a non-burstable instance would never receive a datapoint and,
    under notBreaching, would read as permanently green.
    """
    from mojo.apps.aws.services import aws_check

    clients = _discovery_clients(
        instances=[
            {"InstanceId": "i-burst", "State": {"Name": "running"}, "InstanceType": "t3.medium"},
            {"InstanceId": "i-fixed", "State": {"Name": "running"}, "InstanceType": "m5.large"},
        ],
        db_instances=[
            {"DBInstanceIdentifier": "db-burst", "DBInstanceStatus": "available",
             "Engine": "postgres", "DBInstanceClass": "db.t4g.medium"},
            {"DBInstanceIdentifier": "db-fixed", "DBInstanceStatus": "available",
             "Engine": "postgres", "DBInstanceClass": "db.m5.large"},
        ],
    )
    with mock.patch.object(aws_check, "_setting", side_effect=_setting_values()):
        desired = aws_check.AWSCheckRunner(clients=clients)._desired_alarms()

    credited = {alarm["Dimensions"][0]["Value"] for alarm in desired
                if alarm["MetricName"] == "CPUCreditBalance"}
    assert credited == {"i-burst", "db-burst"}, (
        "Exactly the burstable EC2 and RDS resources earn a CPUCreditBalance alarm; "
        f"got {sorted(credited)}"
    )
    alarm = next(a for a in desired if a["MetricName"] == "CPUCreditBalance")
    assert alarm["ComparisonOperator"] == "LessThanOrEqualToThreshold", (
        "Credits are exhausted on the way DOWN; a GreaterThan comparison would "
        f"never fire; got {alarm['ComparisonOperator']}"
    )
    assert alarm["Threshold"] == 20, f"Default credit floor should be 20, got {alarm['Threshold']}"


@th.django_unit_test()
def test_evictions_alarm_is_greater_than_zero(opts):
    from mojo.apps.aws.services import aws_check

    clients = _discovery_clients(cache_clusters=[
        {"CacheClusterId": "cache-1", "CacheClusterStatus": "available"},
    ])
    with mock.patch.object(aws_check, "_setting", side_effect=_setting_values()):
        desired = aws_check.AWSCheckRunner(clients=clients)._desired_alarms()

    evictions = next((a for a in desired if a["MetricName"] == "Evictions"), None)
    assert evictions is not None, \
        f"ElastiCache clusters need an eviction alarm, got {[a['MetricName'] for a in desired]}"
    assert evictions["Threshold"] == 0 and evictions["ComparisonOperator"] == "GreaterThanThreshold", (
        "ANY sustained eviction means the working set no longer fits — this is not a "
        f"high-water mark; got {evictions['ComparisonOperator']} {evictions['Threshold']}"
    )
    assert evictions["Statistic"] == "Sum", \
        f"Evictions are counted over the period, not averaged, got {evictions['Statistic']}"


@th.django_unit_test()
def test_connection_alarm_uses_forgiving_default_and_honours_override(opts):
    """
    The DatabaseConnections default is deliberately high.

    A default tuned to the smallest instance class would fire constantly on a
    healthy medium, and a chronically-firing alarm gets muted — which silences
    the whole operations topic, defeating every other alarm in the profile.
    """
    from mojo.apps.aws.services import aws_check

    clients = _discovery_clients(db_instances=[
        {"DBInstanceIdentifier": "pg", "DBInstanceStatus": "available",
         "Engine": "postgres", "DBInstanceClass": "db.t3.medium"},
    ])
    with mock.patch.object(aws_check, "_setting", side_effect=_setting_values()):
        desired = aws_check.AWSCheckRunner(clients=clients)._desired_alarms()
    connections = next(a for a in desired if a["MetricName"] == "DatabaseConnections")
    assert connections["Threshold"] == 500, \
        f"The shipped default must be forgiving (500), got {connections['Threshold']}"
    assert connections["ComparisonOperator"] == "GreaterThanOrEqualToThreshold", \
        f"Connection exhaustion is an upward breach, got {connections['ComparisonOperator']}"

    values = _setting_values(AWS_CHECK_RDS_MAX_CONNECTIONS=90)
    with mock.patch.object(aws_check, "_setting", side_effect=values):
        desired = aws_check.AWSCheckRunner(clients=clients)._desired_alarms()
    connections = next(a for a in desired if a["MetricName"] == "DatabaseConnections")
    assert connections["Threshold"] == 90, (
        "An operator on a small instance class must be able to lower the ceiling; "
        f"got {connections['Threshold']}"
    )


@th.django_unit_test()
def test_cert_alarm_treats_missing_data_as_breaching(opts):
    """
    Metric absence IS the signal for certificate expiry.

    Every other resource alarm uses notBreaching, because a quiet metric is
    normal. Here a dead publisher must alarm, or the monitoring fails silently
    in exactly the scenario it exists to catch.
    """
    from mojo.apps.aws.services import aws_check

    cloudwatch = _cloudwatch(metrics=[{"MetricName": "MinDaysToExpiry"}])
    with mock.patch.object(aws_check, "_setting", side_effect=_setting_values()):
        alarms = aws_check.AWSCheckRunner()._desired_deployment_alarms(cloudwatch)
        resource_alarms = aws_check.AWSCheckRunner(
            clients=_discovery_clients(
                instances=[{"InstanceId": "i-a", "State": {"Name": "running"},
                            "InstanceType": "t3.small"}],
                cache_clusters=[{"CacheClusterId": "c-1", "CacheClusterStatus": "available"}],
            ),
        )._desired_alarms()

    assert len(alarms) == 1, f"exactly one deployment-wide alarm, got {alarms}"
    assert alarms[0]["TreatMissingData"] == "breaching", (
        "a publisher that stops running must page; notBreaching here would delete "
        f"the control entirely, got {alarms[0]['TreatMissingData']}"
    )
    leaked = [a["AlarmName"] for a in resource_alarms
              if a["TreatMissingData"] == "breaching" and "healthy-hosts" not in a["AlarmName"]]
    assert leaked == [], (
        "breaching is reserved for signals whose absence is the failure — the "
        f"certificate metric and HealthyHostCount; it leaked to {leaked}"
    )


@th.django_unit_test()
def test_cert_alarm_threshold_direction(opts):
    """Inverted, this alarm never fires and every other assertion still passes."""
    from mojo.apps.aws.services import aws_check

    cloudwatch = _cloudwatch(metrics=[{"MetricName": "MinDaysToExpiry"}])
    with mock.patch.object(aws_check, "_setting", side_effect=_setting_values()):
        alarm = aws_check.AWSCheckRunner()._desired_deployment_alarms(cloudwatch)[0]

    assert alarm["ComparisonOperator"] == "LessThanOrEqualToThreshold", (
        "days-remaining breaches on the way DOWN; a GreaterThan comparison would "
        f"never fire, got {alarm['ComparisonOperator']}"
    )
    assert alarm["Statistic"] == "Minimum", \
        f"the worst certificate in the window is the signal, got {alarm['Statistic']}"
    assert alarm["Threshold"] == 14, f"default expiry threshold should be 14 days, got {alarm}"
    assert alarm["Period"] == 3600 and alarm["EvaluationPeriods"] == 3, (
        "the window must span three hourly publishes so one missed cron run does "
        f"not page, got period={alarm['Period']} evaluations={alarm['EvaluationPeriods']}"
    )


@th.django_unit_test()
def test_cert_alarm_not_created_until_metric_is_published(opts):
    from mojo.apps.aws.services import aws_check

    cloudwatch = _cloudwatch(metrics=[])
    runner = aws_check.AWSCheckRunner()
    with mock.patch.object(aws_check, "_setting", side_effect=_setting_values()):
        alarms = runner._desired_deployment_alarms(cloudwatch)

    assert alarms == [], (
        "a breaching alarm created before its publisher has ever run goes straight "
        f"to ALARM and pages during bootstrap; got {alarms}"
    )
    codes = [item["code"] for item in runner.results]
    assert "alarms.cert_metric_unpublished" in codes, \
        f"the missing publisher must be reported, not silent; got {codes}"


@th.django_unit_test()
def test_list_metrics_denied_does_not_abort_monitoring(opts):
    """
    cloudwatch:ListMetrics is not in the documented least-privilege grant, so on
    every deployment that upgrades without adding it this call would raise. It
    must degrade to one pending item, never take the whole section down with it.
    """
    from botocore.exceptions import ClientError

    from mojo.apps.aws.services import aws_check

    topic = "arn:aws:sns:us-east-1:123456789012:django-mojo-test-operations"
    cloudwatch = _cloudwatch()
    cloudwatch.describe_alarms.return_value = {"MetricAlarms": []}
    cloudwatch.list_metrics.side_effect = ClientError(
        {"Error": {"Code": "AccessDenied", "Message": "no ListMetrics"}}, "ListMetrics")
    clients = {
        "sns": _owned_operations_sns(topic), "cloudwatch": cloudwatch,
        **_discovery_clients(instances=[
            {"InstanceId": "i-a", "State": {"Name": "running"}, "InstanceType": "m5.large"},
        ]),
    }
    values = _setting_values(AWS_CLOUDWATCH_ALARM_TOPIC_ARNS=[topic])
    with mock.patch.object(aws_check, "_setting", side_effect=values):
        report = aws_check.AWSCheckRunner(clients=clients).run(["monitoring"])

    codes = [item["code"] for item in report["items"]]
    assert "monitoring.denied" not in codes, (
        "one optional lookup must not abort the section — that would take the SNS "
        f"audit and the whole alarm inventory down with it; got {codes}"
    )
    assert "alarms.cert_metric_unknown" in codes, \
        f"the denied lookup must be reported as pending, got {codes}"
    assert "sns.topic_owned" in codes and "alarms.profile" in codes, (
        "the rest of the monitoring section must still have run; "
        f"got {codes}"
    )


@th.django_unit_test()
def test_desired_alarms_skips_free_storage_space_on_aurora(opts):
    from mojo.apps.aws.services import aws_check

    clients = _discovery_clients(db_instances=[
        {"DBInstanceIdentifier": "aurora-pg", "DBInstanceStatus": "available",
         "Engine": "aurora-postgresql"},
        {"DBInstanceIdentifier": "plain-pg", "DBInstanceStatus": "available",
         "Engine": "postgres"},
    ])
    with mock.patch.object(aws_check, "_setting", side_effect=_setting_values()):
        desired = aws_check.AWSCheckRunner(clients=clients)._desired_alarms()
    signals = {(alarm["Dimensions"][0]["Value"], alarm["MetricName"]) for alarm in desired}
    assert ("aurora-pg", "FreeStorageSpace") not in signals, (
        "Aurora never publishes FreeStorageSpace, so the alarm would sit permanently green; "
        f"got {sorted(signals)}"
    )
    assert ("plain-pg", "FreeStorageSpace") in signals, (
        "Non-Aurora RDS engines must keep their FreeStorageSpace alarm; "
        f"got {sorted(signals)}"
    )
    assert ("aurora-pg", "CPUUtilization") in signals, (
        f"Aurora instances must still get the CPU alarm; got {sorted(signals)}"
    )


@th.django_unit_test()
def test_aurora_engine_matching_covers_every_variant(opts):
    from mojo.apps.aws.services import aws_check

    for engine in ("aurora", "aurora-mysql", "aurora-postgresql", "AURORA-MYSQL", " aurora-mysql "):
        assert aws_check._is_aurora_engine(engine) is True, \
            f"{engine!r} is an Aurora engine and must not get a FreeStorageSpace alarm"
    for engine in ("postgres", "mysql", "mariadb", "oracle-ee", "sqlserver-ex",
                   "custom-oracle-ee", "db2-ae", "", None):
        assert aws_check._is_aurora_engine(engine) is False, \
            f"{engine!r} is not Aurora and must keep its FreeStorageSpace alarm"


@th.django_unit_test()
def test_aurora_storage_gap_is_reported_not_silent(opts):
    from mojo.apps.aws.services import aws_check

    topic = "arn:aws:sns:us-east-1:123456789012:django-mojo-test-operations"
    cloudwatch = _cloudwatch()
    cloudwatch.describe_alarms.return_value = {"MetricAlarms": []}
    clients = {
        "sns": _owned_operations_sns(topic), "cloudwatch": cloudwatch,
        **_discovery_clients(db_instances=[{
            "DBInstanceIdentifier": "aurora-only", "DBInstanceStatus": "available",
            "Engine": "aurora-mysql",
        }]),
    }
    values = _setting_values(AWS_CLOUDWATCH_ALARM_TOPIC_ARNS=[topic])
    with mock.patch.object(aws_check, "_setting", side_effect=values):
        report = aws_check.AWSCheckRunner(clients=clients).run(["monitoring"])
    codes = [item["code"] for item in report["items"]]
    gap = next((item for item in report["items"]
                if item["code"] == "alarms.aurora_storage_unmonitored"), None)
    assert gap is not None, \
        f"An Aurora-only deployment must not report zero storage monitoring silently, got {codes}"
    assert gap["status"] == "warn", f"The Aurora storage gap must be a warning, got {gap}"
    assert "aurora-only" in gap["details"]["instance_ids"], \
        f"The gap must name the unmonitored instance, got {gap}"


@th.django_unit_test()
def test_monitoring_reports_stale_aurora_free_storage_alarm(opts):
    from mojo.apps.aws.services import aws_check

    topic = "arn:aws:sns:us-east-1:123456789012:django-mojo-test-operations"
    name = "django-mojo/test/rds/aurora-pg/free-storage"
    cloudwatch = _stale_alarm_cloudwatch({
        "AlarmName": name, "AlarmArn": "arn:aws:cloudwatch:alarm/stale",
        "MetricName": "FreeStorageSpace", "Namespace": "AWS/RDS",
    })
    clients = {
        "sns": _owned_operations_sns(topic), "cloudwatch": cloudwatch,
        **_discovery_clients(db_instances=[{
            "DBInstanceIdentifier": "aurora-pg", "DBInstanceStatus": "available",
            "Engine": "aurora-postgresql",
        }]),
    }
    values = _setting_values(AWS_CLOUDWATCH_ALARM_TOPIC_ARNS=[topic])
    with mock.patch.object(aws_check, "_setting", side_effect=values):
        report = aws_check.AWSCheckRunner(clients=clients).run(["monitoring"])
    codes = [item["code"] for item in report["items"]]
    stale = next((item for item in report["items"]
                  if item["code"] == "alarms.stale_aurora_storage"), None)
    assert stale is not None, \
        f"A permanently-green Aurora FreeStorageSpace alarm must be reported, got {codes}"
    assert stale["details"]["alarm_names"] == [name], \
        f"The report must name the exact stale alarm, got {stale}"
    assert "delete-alarms" in stale["remediation"], \
        f"Remediation must give the manual delete command, got {stale['remediation']!r}"
    cloudwatch.delete_alarms.assert_not_called()


@th.django_unit_test()
def test_stale_detection_makes_no_call_when_no_aurora_is_discovered(opts):
    from mojo.apps.aws.services import aws_check

    topic = "arn:aws:sns:us-east-1:123456789012:django-mojo-test-operations"
    cloudwatch = _cloudwatch()
    cloudwatch.describe_alarms.return_value = {"MetricAlarms": []}
    clients = {
        "sns": _owned_operations_sns(topic), "cloudwatch": cloudwatch,
        **_discovery_clients(db_instances=[{
            "DBInstanceIdentifier": "plain-pg", "DBInstanceStatus": "available",
            "Engine": "postgres",
        }]),
    }
    values = _setting_values(AWS_CLOUDWATCH_ALARM_TOPIC_ARNS=[topic])
    with mock.patch.object(aws_check, "_setting", side_effect=values):
        report = aws_check.AWSCheckRunner(clients=clients).run(["monitoring"])
    codes = [item["code"] for item in report["items"]]
    assert "alarms.stale_aurora_storage" not in codes, \
        f"No Aurora instance exists, so nothing can be stale, got {codes}"
    assert "alarms.aurora_storage_unmonitored" not in codes, \
        f"A non-Aurora fleet keeps its storage alarm and has no gap, got {codes}"
    for call in cloudwatch.describe_alarms.call_args_list:
        assert call.kwargs.get("AlarmNames"), (
            "describe_alarms without AlarmNames returns every alarm in the account; "
            f"got {call.kwargs}"
        )
        assert "AlarmTypes" not in call.kwargs, \
            f"The stale sweep must not run when no Aurora was discovered, got {call.kwargs}"


@th.django_unit_test()
def test_stale_detection_ignores_a_hand_fixed_alarm(opts):
    from mojo.apps.aws.services import aws_check

    topic = "arn:aws:sns:us-east-1:123456789012:django-mojo-test-operations"
    cloudwatch = _stale_alarm_cloudwatch({
        "AlarmName": "django-mojo/test/rds/aurora-pg/free-storage",
        "AlarmArn": "arn:aws:cloudwatch:alarm/hand-fixed",
        "MetricName": "FreeLocalStorage", "Namespace": "AWS/RDS",
    })
    clients = {
        "sns": _owned_operations_sns(topic), "cloudwatch": cloudwatch,
        **_discovery_clients(db_instances=[{
            "DBInstanceIdentifier": "aurora-pg", "DBInstanceStatus": "available",
            "Engine": "aurora-postgresql",
        }]),
    }
    values = _setting_values(AWS_CLOUDWATCH_ALARM_TOPIC_ARNS=[topic])
    with mock.patch.object(aws_check, "_setting", side_effect=values):
        report = aws_check.AWSCheckRunner(clients=clients).run(["monitoring"])
    codes = [item["code"] for item in report["items"]]
    assert "alarms.stale_aurora_storage" not in codes, (
        "An owned alarm already hand-fixed to FreeLocalStorage is preserved, not reported "
        f"for deletion, got {codes}"
    )


@th.django_unit_test()
def test_stale_detection_runs_before_subscription_confirmation(opts):
    from mojo.apps.aws.services import aws_check

    topic = "arn:aws:sns:us-east-1:123456789012:django-mojo-test-operations"
    name = "django-mojo/test/rds/aurora-pg/free-storage"
    cloudwatch = _stale_alarm_cloudwatch({
        "AlarmName": name, "AlarmArn": "arn:aws:cloudwatch:alarm/stale",
        "MetricName": "FreeStorageSpace", "Namespace": "AWS/RDS",
    })
    clients = {
        "sns": _owned_operations_sns(topic, confirmed=False), "cloudwatch": cloudwatch,
        **_discovery_clients(db_instances=[{
            "DBInstanceIdentifier": "aurora-pg", "DBInstanceStatus": "available",
            "Engine": "aurora-postgresql",
        }]),
    }
    with mock.patch.object(aws_check, "_setting", side_effect=_setting_values()):
        report = aws_check.AWSCheckRunner(clients=clients).run(["monitoring"])
    codes = [item["code"] for item in report["items"]]
    assert "sns.topic_not_allowlisted" in codes, \
        f"This fixture must exercise the un-allowlisted early return, got {codes}"
    assert "alarms.stale_aurora_storage" in codes, \
        f"An un-allowlisted deployment must still be told about the false alarm, got {codes}"
    assert "alarms.aurora_storage_unmonitored" in codes, \
        f"An un-allowlisted deployment must still be told about the storage gap, got {codes}"


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


@th.django_unit_test()
def test_dns_section_skips_when_dnsman_is_not_installed(opts):
    """A deployment that does not use dnsman must not have aws-check start failing."""
    from mojo.apps.aws.services import aws_check

    with mock.patch("django.apps.apps.is_installed", return_value=False):
        report = aws_check.AWSCheckRunner().run(["dns"])

    assert report["overall"] == "pass", f"An absent optional app is not a failure, got {report}"
    assert [item["code"] for item in report["items"]] == ["dnsman.not_installed"], \
        f"The section should skip with one item, got {report['items']}"
    assert report["items"][0]["status"] == "skip", \
        f"Absent dnsman is a skip, not a warn, got {report['items'][0]}"


@th.django_unit_test()
def test_dns_audit_flags_staging_directory(opts):
    """
    Staging is the shipped default, so this is the common case, not the edge one.

    An operator bootstrapping against staging and believing they are live is the
    likeliest way to misread this section, so the word has to appear.
    """
    from mojo.apps.aws.services import aws_check
    from mojo.apps.dnsman.services import certs

    staging = "https://acme-staging-v02.api.letsencrypt.org/directory"
    runner = aws_check.AWSCheckRunner()
    with mock.patch.object(certs, "directory_url", return_value=staging):
        runner._check_dns_acme_account()

    item = next(i for i in runner.results if i["code"] == "acme.staging_directory")
    assert item["status"] == "warn", f"Staging must warn, never pass, got {item}"
    assert "STAGING" in item["message"] or "staging" in item["message"], (
        "The message must say staging in words — a bare URL is what gets misread; "
        f"got {item['message']!r}"
    )
    assert item["details"]["staging"] is True, f"--json consumers need the flag, got {item}"


@th.django_unit_test()
def test_dns_bootstrap_requires_group(opts):
    from mojo.apps.aws.services import aws_check
    from mojo.apps.dnsman.services import delegation

    runner = aws_check.AWSCheckRunner(
        apply=True, yes=True, dns_domain="example.com", dns_group="")
    with mock.patch.object(delegation, "initiate") as initiate:
        runner._bootstrap_dns_domain()

    initiate.assert_not_called()
    codes = [item["code"] for item in runner.results]
    assert "dns.group_required" in codes, \
        f"An unnamed owner must fail closed before any mutation, got {codes}"


@th.django_unit_test()
def test_dns_bootstrap_surfaces_cname_before_requesting(opts):
    """The CNAME is the output the operator hands to the domain owner."""
    from mojo.apps.aws.services import aws_check
    from mojo.apps.dnsman.services import certs, delegation

    row = SimpleNamespace(
        domain_name="example.com", source_name="_acme-challenge.example.com",
        target_name="abc123.hub.example.net", state="pending", domain=None)
    runner = aws_check.AWSCheckRunner(
        apply=True, yes=True, dns_domain="example.com", dns_group="7")
    with mock.patch.object(runner, "_resolve_dns_group",
                           return_value=SimpleNamespace(name="tenant", pk=7,
                                                        get_uuid=lambda: "uuid-7")), \
            mock.patch.object(delegation, "initiate", return_value=row), \
            mock.patch.object(delegation, "prove_alias",
                              side_effect=RuntimeError("not propagated yet")), \
            mock.patch.object(certs, "request_certificate") as request_certificate:
        runner._bootstrap_dns_domain()

    cname = next(i for i in runner.results if i["code"] == "delegation.cname_required")
    assert cname["details"]["source_name"] == "_acme-challenge.example.com", (
        "--json consumers need the CNAME as structured fields, not only prose; "
        f"got {cname['details']}"
    )
    assert cname["details"]["target_name"] == "abc123.hub.example.net", \
        f"The CNAME target must be surfaced, got {cname['details']}"
    request_certificate.assert_not_called()


@th.django_unit_test()
def test_dns_bootstrap_does_not_request_certificate_until_cname_proves(opts):
    """
    The Let's Encrypt rate-limit guard.

    Failed validations are capped at 5 per account per hostname per hour, so
    firing at an unpropagated record burns the budget and blocks retries for an
    hour — during a bootstrap, which is exactly when someone is iterating.
    """
    from mojo.apps.aws.services import aws_check
    from mojo.apps.dnsman.services import certs, delegation

    row = SimpleNamespace(
        domain_name="example.com", source_name="_acme-challenge.example.com",
        target_name="abc123.hub.example.net", state="pending", domain=None)
    runner = aws_check.AWSCheckRunner(
        apply=True, yes=True, dns_domain="example.com", dns_group="7")
    with mock.patch.object(runner, "_resolve_dns_group",
                           return_value=SimpleNamespace(name="tenant", pk=7,
                                                        get_uuid=lambda: "uuid-7")), \
            mock.patch.object(delegation, "initiate", return_value=row), \
            mock.patch.object(delegation, "prove_alias",
                              side_effect=RuntimeError("CNAME does not resolve")), \
            mock.patch.object(delegation, "verify") as verify, \
            mock.patch.object(certs, "request_certificate") as request_certificate:
        runner._bootstrap_dns_domain()

    request_certificate.assert_not_called()
    verify.assert_not_called()
    codes = [item["code"] for item in runner.results]
    assert "delegation.cname_unverified" in codes, \
        f"An unresolved CNAME must be reported as pending, got {codes}"
    unverified = next(i for i in runner.results if i["code"] == "delegation.cname_unverified")
    assert unverified["status"] == "pending", (
        "An unpropagated CNAME is a wait, not a failure — the operator reruns; "
        f"got {unverified['status']}"
    )
    assert "budget" in unverified["remediation"], \
        f"The remediation should say no validation budget was spent, got {unverified['remediation']!r}"


@th.django_unit_test()
def test_dns_errors_redact_the_hub_api_key(opts):
    """#1423 requires the hub key never be logged; _add() is a path to stdout and --json."""
    import json as _json

    from mojo.apps.aws.services import aws_check
    from mojo.apps.dnsman.services import delegation

    secret = "hub-key-super-secret-value"
    values = _setting_values(DNSMAN_ACME_HUB_API_KEY=secret)
    with mock.patch.object(aws_check, "_setting", side_effect=values):
        runner = aws_check.AWSCheckRunner(
            apply=True, yes=True, dns_domain="example.com", dns_group="7")
        with mock.patch.object(runner, "_resolve_dns_group",
                               return_value=SimpleNamespace(name="tenant", pk=7,
                                                        get_uuid=lambda: "uuid-7")), \
                mock.patch.object(delegation, "initiate",
                                  side_effect=RuntimeError(f"hub rejected key {secret}")):
            runner._bootstrap_dns_domain()

    serialized = _json.dumps(runner.results)
    assert secret not in serialized, (
        "The ACME hub API key reached the report in the clear — it would print to "
        "stdout and into --json output"
    )
    assert "[REDACTED]" in serialized, \
        f"The key should be replaced by the redaction marker, got {serialized}"


@th.django_unit_test()
def test_elbv2_denial_does_not_disarm_every_other_alarm(opts):
    """
    elasticloadbalancing:DescribeTargetGroups is new in this version, so every
    deployment that upgrades lacks it until IAM is updated. If that denial
    escaped into the shared discovery guard it would return no alarms at all —
    EC2, RDS and ElastiCache silently unarmed, and no drift or name-conflict
    detection either.
    """
    from botocore.exceptions import ClientError

    from mojo.apps.aws.services import aws_check

    clients = _discovery_clients(
        instances=[{"InstanceId": "i-a", "State": {"Name": "running"},
                    "InstanceType": "m5.large"}],
        db_instances=[{"DBInstanceIdentifier": "pg", "DBInstanceStatus": "available",
                       "Engine": "postgres", "DBInstanceClass": "db.m5.large"}],
    )
    clients["elbv2"].get_paginator.return_value.paginate.side_effect = ClientError(
        {"Error": {"Code": "AccessDenied", "Message": "no DescribeTargetGroups"}},
        "DescribeTargetGroups")

    runner = aws_check.AWSCheckRunner(clients=clients)
    with mock.patch.object(aws_check, "_setting", side_effect=_setting_values()):
        desired = runner._desired_alarms()

    metrics = {alarm["MetricName"] for alarm in desired}
    assert "StatusCheckFailed" in metrics and "CPUUtilization" in metrics, (
        "an ELBv2 permission gap must not disarm the resources we CAN see; "
        f"got {sorted(metrics)}"
    )
    assert not any(m in metrics for m in ("HealthyHostCount", "UnHealthyHostCount")), \
        f"no target group was readable, so no LB alarm should be desired; got {sorted(metrics)}"
    codes = [item["code"] for item in runner.results]
    assert "resources.elbv2_denied" in codes, \
        f"the permission gap must be reported, not silent; got {codes}"
    assert "resources.discovery_denied" not in codes, (
        "the shared discovery guard must not have fired — that is the path that "
        f"returns zero alarms; got {codes}"
    )


@th.django_unit_test()
def test_bootstrap_never_re_proves_a_verified_delegation(opts):
    """
    prove_alias is NOT a free read-only lookup.

    delegation._proof_failure persists STATE_BROKEN for any row that has ever
    verified, and certs._issue_locked then refuses every issuance AND renewal for
    that domain with no self-heal. So a rerun against an already-working domain
    during a transient resolver failure would take that domain's renewals down.
    """
    from mojo.apps.aws.services import aws_check
    from mojo.apps.dnsman.models import AcmeDelegation, Domain
    from mojo.apps.dnsman.services import certs, delegation

    verified = SimpleNamespace(
        domain_name="verified.example", source_name="_acme-challenge.verified.example",
        target_name="abc.hub.example.net", state="verified", domain=None)
    runner = aws_check.AWSCheckRunner(
        apply=True, yes=True, dns_domain="verified.example", dns_group="7")

    with mock.patch.object(runner, "_resolve_dns_group",
                           return_value=SimpleNamespace(name="tenant", pk=7,
                                                        get_uuid=lambda: "uuid-7")), \
            mock.patch.object(Domain.objects, "filter") as domains, \
            mock.patch.object(AcmeDelegation.objects, "filter") as rows, \
            mock.patch.object(delegation, "initiate") as initiate, \
            mock.patch.object(delegation, "prove_alias") as prove_alias, \
            mock.patch.object(certs, "request_certificate") as request_certificate:
        domains.return_value.first.return_value = None
        rows.return_value.exclude.return_value.first.return_value = verified
        runner._bootstrap_dns_domain()

    prove_alias.assert_not_called()
    initiate.assert_not_called()
    codes = [item["code"] for item in runner.results]
    assert "delegation.already_verified" in codes, \
        f"an existing verified delegation should be reported and left alone, got {codes}"
    request_certificate.assert_called_once()


@th.django_unit_test()
def test_bootstrap_refuses_a_domain_owned_by_another_group(opts):
    """
    initiate() only enforces domain ownership when it is GIVEN a domain, and the
    bootstrap passes one. Without that, another tenant's name would get a
    delegation row and a hub allocation before anything refused.
    """
    from mojo.apps.aws.services import aws_check
    from mojo.apps.dnsman.models import Domain
    from mojo.apps.dnsman.services import delegation

    runner = aws_check.AWSCheckRunner(
        apply=True, yes=True, dns_domain="taken.example", dns_group="7")
    with mock.patch.object(runner, "_resolve_dns_group",
                           return_value=SimpleNamespace(name="tenant", pk=7,
                                                        get_uuid=lambda: "uuid-7")), \
            mock.patch.object(Domain.objects, "filter") as domains, \
            mock.patch.object(delegation, "initiate") as initiate:
        domains.return_value.first.return_value = SimpleNamespace(
            name="taken.example", group_id=99)
        runner._bootstrap_dns_domain()

    initiate.assert_not_called()
    codes = [item["code"] for item in runner.results]
    assert "dns.domain_not_owned" in codes, \
        f"a domain owned by another group must fail closed before any write, got {codes}"
