"""Regression coverage for the S3 public-access audit on buckets that deny plain HTTP."""
import json

from botocore.exceptions import ClientError

from testit import helpers as th
from testit.helpers import assert_eq, assert_true

BUCKET = "audit-test-bucket"

PUBLIC_ALLOW = {
    "Effect": "Allow",
    "Principal": "*",
    "Action": "s3:GetObject",
    "Resource": f"arn:aws:s3:::{BUCKET}/fileman/*",
}


class _PolicyClient:
    def __init__(self, statements=None, policy_error=None):
        self.statements = statements or []
        self.policy_error = policy_error

    def get_bucket_policy(self, **kwargs):
        if self.policy_error:
            raise self.policy_error
        return {"Policy": json.dumps({"Version": "2012-10-17", "Statement": self.statements})}

    def get_public_access_block(self, **kwargs):
        return {"PublicAccessBlockConfiguration": {}}


def _audit(statements, policy_error=None, endpoint_url="https://s3.us-east-1.amazonaws.com"):
    from mojo.apps.fileman.backends.s3 import S3StorageBackend

    backend = object.__new__(S3StorageBackend)
    backend.bucket_name = BUCKET
    backend.folder_path = "fileman/group-123"
    backend.endpoint_url = endpoint_url
    backend.addressing_style = "auto"
    backend._client = _PolicyClient(statements=statements, policy_error=policy_error)
    # An instance attribute, not a patch: no account-level block in this fixture.
    backend._get_account_public_access_block = lambda: {}
    ok, issues, details = backend.check_public_access_for_prefix()
    return ok, issues, details


def _transport_deny():
    from mojo.deploy.provision.storage import secure_transport_policy

    statements = secure_transport_policy(BUCKET)["Statement"]
    assert_eq(len(statements), 1, "provisioning should write exactly one transport statement")
    return dict(statements[0])


@th.django_unit_test("S3 audit: the provisioning transport-only deny alone is private, not unknown")
def test_transport_deny_alone_is_private(opts):
    ok, issues, details = _audit([_transport_deny()])
    assert_true(not ok, "a bucket with no public allow must not be public")
    assert_eq(details["status"], "private",
              f"a deny cannot grant access, so no public allow means private; issues={issues}")


@th.django_unit_test("S3 audit: any deny without a public allow is private, not unknown")
def test_deny_without_allow_is_private(opts):
    deny = dict(PUBLIC_ALLOW, Effect="Deny")
    ok, issues, details = _audit([deny])
    assert_true(not ok, "a bucket with no public allow must not be public")
    assert_eq(details["status"], "private",
              f"a deny cannot grant access, so no public allow means private; issues={issues}")


@th.django_unit_test("S3 audit: a whole-prefix public allow with the transport-only deny is public")
def test_allow_with_transport_deny_is_public(opts):
    ok, issues, details = _audit([PUBLIC_ALLOW, _transport_deny()])
    assert_eq(details["status"], "public",
              f"anonymous HTTPS reads succeed under a transport-only deny; issues={issues}")
    assert_true(ok, "the audit should answer public")


@th.django_unit_test("S3 audit: a whole-prefix public allow with no deny is still public")
def test_allow_alone_is_public(opts):
    ok, issues, details = _audit([PUBLIC_ALLOW])
    assert_true(ok and details["status"] == "public", f"expected public, got {details}; issues={issues}")


@th.django_unit_test("S3 audit: a deny that can override anonymous reads still prevents public")
def test_overriding_deny_is_unknown(opts):
    transport = _transport_deny()
    cases = {
        "unconditional deny": dict(PUBLIC_ALLOW, Effect="Deny"),
        "deny when transport is secure": dict(transport, Condition={"Bool": {"aws:SecureTransport": "true"}}),
        "transport deny with a second key": dict(transport, Condition={
            "Bool": {"aws:SecureTransport": "false", "aws:ViaAWSService": "false"}}),
        "transport deny with a second operator": dict(transport, Condition={
            "Bool": {"aws:SecureTransport": "false"},
            "StringNotEquals": {"aws:Referer": "example"}}),
        "transport deny with IfExists": dict(transport, Condition={
            "BoolIfExists": {"aws:SecureTransport": "false"}}),
        "minimum TLS version deny": dict(transport, Condition={
            "NumericLessThan": {"s3:TlsVersion": "1.2"}}),
        "source address deny": dict(transport, Condition={
            "NotIpAddress": {"aws:SourceIp": "203.0.113.0/24"}}),
    }
    for label, deny in cases.items():
        ok, _, details = _audit([PUBLIC_ALLOW, deny])
        assert_true(not ok and details["status"] == "unknown",
                    f"{label} must keep the audit unknown, got {details['status']}")


@th.django_unit_test("S3 audit: the transport-only deny does not excuse a plain-HTTP endpoint")
def test_transport_deny_with_http_endpoint_is_unknown(opts):
    ok, _, details = _audit([PUBLIC_ALLOW, _transport_deny()], endpoint_url="http://minio.internal:9000")
    assert_true(not ok and details["status"] == "unknown",
                f"unsigned plain-HTTP URLs would be denied, so this must stay unknown, got {details['status']}")


@th.django_unit_test("S3 audit: an unreadable or malformed policy is still unknown")
def test_unreadable_policy_is_unknown(opts):
    denied = ClientError({"Error": {"Code": "AccessDenied", "Message": "AccessDenied"}}, "test")
    ok, _, details = _audit([], policy_error=denied)
    assert_true(not ok and details["status"] == "unknown",
                f"an unreadable policy must stay unknown, got {details['status']}")

    from mojo.apps.fileman.backends.s3 import S3StorageBackend

    backend = object.__new__(S3StorageBackend)
    backend.bucket_name = BUCKET
    backend.folder_path = "fileman/group-123"
    backend.endpoint_url = "https://s3.us-east-1.amazonaws.com"
    backend.addressing_style = "auto"
    client = _PolicyClient()
    client.get_bucket_policy = lambda **kwargs: {"Policy": "[1, 2"}
    backend._client = client
    ok, _, details = backend.check_public_access_for_prefix()
    assert_true(not ok and details["status"] == "unknown",
                f"a malformed policy must stay unknown, got {details['status']}")
