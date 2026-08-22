import json
import os
import subprocess
import sys
import tempfile
from unittest import mock
from urllib.parse import urlsplit

from botocore.config import Config
from testit import helpers as th


def _presign_in_isolated_environment(environment):
    script = """
import json
import os
import sys
from pathlib import Path
from urllib.parse import urlsplit

sys.path.insert(0, str(Path.cwd() / "testproject" / "config"))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "settings")
import django
django.setup()

from mojo.helpers.aws.client import get_client

client = get_client(
    "s3", access_key="AKIAPRESIGNTEST",
    secret_key="presign-test-secret", region="us-west-2")
url = client.generate_presigned_url(
    "put_object", ExpiresIn=60,
    Params={"Bucket": "bucket", "Key": "key"})
parsed = urlsplit(url)
print(json.dumps({
    "netloc": parsed.netloc,
    "path": parsed.path,
    "sigv4": "X-Amz-Signature=" in url,
}))
"""
    result = subprocess.run(
        [sys.executable, "-c", script], env=environment,
        capture_output=True, text=True, check=True)
    return json.loads(result.stdout)


def _aws_test_environment(**overrides):
    environment = {
        key: value for key, value in os.environ.items()
        if not key.startswith("AWS_")
    }
    environment.update(overrides)
    return environment


@th.django_unit_test()
def test_aws_session_uses_default_chain_and_rejects_partial_settings(opts):
    from botocore.exceptions import PartialCredentialsError
    from mojo.helpers.aws import client

    # Injected via get_session's session_cls seam: patching client.boto3
    # mutates the process-wide boto3 module under the parallel runner (#2558).
    session_cls = mock.Mock()
    client.get_session(region="us-west-2", session_cls=session_cls)
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
def test_s3_environment_endpoint_keeps_compatible_path_addressing(opts):
    environment = _aws_test_environment(
        AWS_ENDPOINT_URL_S3="https://storage.example.test",
        AWS_EC2_METADATA_DISABLED="true",
    )
    presign = _presign_in_isolated_environment(environment)

    assert presign["netloc"] == "storage.example.test", (
        "A service endpoint selected through AWS_ENDPOINT_URL_S3 must remain "
        f"the request host, got {presign['netloc']}")
    assert presign["path"] == "/bucket/key", (
        "A service endpoint selected through AWS_ENDPOINT_URL_S3 must retain "
        f"botocore's compatible path addressing, got {presign['path']}")
    assert presign["sigv4"], \
        "An environment endpoint must retain SigV4 signing"


@th.django_unit_test()
def test_s3_shared_config_endpoint_keeps_compatible_path_addressing(opts):
    config_text = """\
[default]
region = us-west-2
services = local-services

[services local-services]
s3 =
  endpoint_url = https://storage.example.test
"""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".ini") as config_file:
        config_file.write(config_text)
        config_file.flush()
        environment = _aws_test_environment(
            AWS_CONFIG_FILE=config_file.name,
            AWS_EC2_METADATA_DISABLED="true",
        )
        presign = _presign_in_isolated_environment(environment)

    assert presign["netloc"] == "storage.example.test", (
        "A service endpoint selected through shared AWS config must remain "
        f"the request host, got {presign['netloc']}")
    assert presign["path"] == "/bucket/key", (
        "A service endpoint selected through shared AWS config must retain "
        f"botocore's compatible path addressing, got {presign['path']}")
    assert presign["sigv4"], \
        "A shared-config endpoint must retain SigV4 signing"


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
