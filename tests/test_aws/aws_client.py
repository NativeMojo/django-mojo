from unittest import mock
from urllib.parse import urlsplit

from botocore.config import Config
from testit import helpers as th


@th.django_unit_test()
def test_aws_session_uses_default_chain_and_rejects_partial_settings(opts):
    from botocore.exceptions import PartialCredentialsError
    from mojo.helpers.aws import client

    with mock.patch.object(client.boto3, "Session") as session_cls:
        client.get_session(region="us-west-2")
        session_cls.assert_called_once_with(region_name="us-west-2")

    try:
        client.get_session(access_key="key-only", region="us-west-2")
    except PartialCredentialsError:
        pass
    else:
        assert False, "A half-configured static credential pair must fail before AWS access"


@th.django_unit_test()
def test_aws_client_applies_bounded_config(opts):
    from mojo.helpers.aws.client import get_client

    session = mock.Mock()
    get_client("sts", session=session, region="us-east-2", timeout=7, max_attempts=2)
    kwargs = session.client.call_args.kwargs
    config = kwargs["config"]
    assert kwargs["region_name"] == "us-east-2", "The selected region must reach the client"
    assert config.connect_timeout == 7, "The connect timeout must be bounded by the caller"
    assert config.read_timeout == 7, "The read timeout must be bounded by the caller"
    assert config.retries["max_attempts"] == 2, "The retry budget must be bounded"
    assert config.s3 is None, "Non-S3 clients must not inherit S3 addressing configuration"


@th.django_unit_test()
def test_s3_presigned_urls_are_sigv4_and_regional(opts):
    """Item #1770 (second defect): with no pinned signature version botocore's
    presigner falls back to SigV2 for S3. SigV2 signs Content-Type, and the
    server presigns without one, so any uploader whose HTTP client adds a
    Content-Type gets 403 SignatureDoesNotMatch. Presigning is pure local
    signing — this touches no network."""
    from mojo.helpers.aws.client import get_client

    client = get_client(
        "s3", access_key="AKIAPRESIGNTEST", secret_key="presign-test-secret",
        region="us-west-2")
    url = client.generate_presigned_url(
        "put_object", ExpiresIn=60, Params={"Bucket": "bucket", "Key": "key"})
    parsed = urlsplit(url)

    assert "X-Amz-Signature=" in url, \
        f"S3 presigned PUT is not SigV4-signed: {url}"
    assert "AWSAccessKeyId=" not in url, (
        "S3 presigning fell back to SigV2, which signs Content-Type and "
        f"breaks any uploader that sends one: {url}")
    assert parsed.netloc == "bucket.s3.us-west-2.amazonaws.com", (
        "Default S3 presigns must target the selected AWS region instead of "
        f"the legacy global endpoint, got {parsed.netloc}")


@th.django_unit_test()
def test_s3_explicit_endpoint_keeps_compatible_path_addressing(opts):
    from mojo.helpers.aws.client import get_client

    client = get_client(
        "s3", access_key="AKIAPRESIGNTEST", secret_key="presign-test-secret",
        region="us-west-2", endpoint_url="https://storage.example.test")
    url = client.generate_presigned_url(
        "put_object", ExpiresIn=60, Params={"Bucket": "bucket", "Key": "key"})
    parsed = urlsplit(url)

    assert parsed.netloc == "storage.example.test", (
        "An explicit S3-compatible endpoint must remain the request host, "
        f"got {parsed.netloc}")
    assert parsed.path == "/bucket/key", (
        "An explicit S3-compatible endpoint must retain botocore's path-style "
        f"addressing, got {parsed.path}")
    assert "X-Amz-Signature=" in url, \
        f"An explicit S3-compatible endpoint must retain SigV4 signing: {url}"


@th.django_unit_test()
def test_s3_caller_config_is_authoritative(opts):
    from mojo.helpers.aws.client import get_client

    supplied = Config(
        connect_timeout=11,
        signature_version="s3v4",
        s3={"addressing_style": "path"},
    )
    session = mock.Mock()
    get_client(
        "s3", session=session, region="us-west-2", timeout=1,
        max_attempts=1, config=supplied)

    kwargs = session.client.call_args.kwargs
    assert kwargs["config"] is supplied, (
        "A caller-supplied Config must be passed through without replacement")
    assert kwargs["config"].s3["addressing_style"] == "path", (
        "A caller-supplied S3 addressing style must remain authoritative")
    assert kwargs["config"].connect_timeout == 11, (
        "Default timeout arguments must not overwrite a caller-supplied Config")


@th.django_unit_test()
def test_cloudwatch_helper_uses_injected_client_factory(opts):
    from mojo.helpers.aws.cloudwatch import CloudWatchHelper

    clients = {name: mock.Mock(name=name) for name in ("cloudwatch", "ec2", "rds", "elasticache")}
    factory = mock.Mock(side_effect=lambda service, **kwargs: clients[service])
    helper = CloudWatchHelper(
        access_key="key", secret_key="secret", region="us-east-1",
        session=mock.Mock(), client_factory=factory, timeout=4,
    )
    assert helper.cw is clients["cloudwatch"], "CloudWatch must use the injected factory"
    assert helper.ec2 is clients["ec2"], "EC2 must use the injected factory"
    assert factory.call_count == 2, "Each lazy client should be constructed once"
