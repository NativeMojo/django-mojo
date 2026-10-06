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


DENY_ISSUE = "A matching deny may override anonymous GetObject access."
PARTIAL_ISSUE = "The bucket policy has only conditional or partial public access."

# What provisioning turns on (mojo/deploy/provision/storage.py, wanted_block).
PROVISIONED_BLOCK = {
    "BlockPublicAcls": True,
    "IgnorePublicAcls": True,
    "BlockPublicPolicy": True,
    "RestrictPublicBuckets": True,
}


class _PolicyClient:
    def __init__(self, statements=None, policy_error=None, bucket_pab=None):
        self.statements = statements or []
        self.policy_error = policy_error
        self.bucket_pab = bucket_pab or {}

    def get_bucket_policy(self, **kwargs):
        if self.policy_error:
            raise self.policy_error
        return {"Policy": json.dumps({"Version": "2012-10-17", "Statement": self.statements})}

    def get_public_access_block(self, **kwargs):
        return {"PublicAccessBlockConfiguration": self.bucket_pab}


def _backend(statements, policy_error=None, endpoint_url="https://s3.us-east-1.amazonaws.com",
             bucket_pab=None):
    from mojo.apps.fileman.backends.s3 import S3StorageBackend

    backend = object.__new__(S3StorageBackend)
    backend.bucket_name = BUCKET
    backend.folder_path = "fileman/group-123"
    backend.endpoint_url = endpoint_url
    backend.addressing_style = "auto"
    backend._client = _PolicyClient(
        statements=statements, policy_error=policy_error, bucket_pab=bucket_pab)
    # An instance attribute, not a patch: no account-level block in this fixture.
    backend._get_account_public_access_block = lambda: {}
    return backend


def _audit(statements, **kwargs):
    return _backend(statements, **kwargs).check_public_access_for_prefix()


def _assert_unknown_for(label, result, issue):
    """Unknown for the stated reason, not because the policy failed to parse."""
    ok, issues, details = result
    assert_true(not ok and details["status"] == "unknown",
                f"{label} must keep the audit unknown, got {details['status']}")
    assert_eq(issues, [issue], f"{label} must be unknown for the stated reason")
    assert_true("failure" not in details,
                f"{label} must not be reported as a read or parse failure: {details.get('failure')}")


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
              f"a transport-only deny cannot grant access, so no public allow means private; issues={issues}")


@th.django_unit_test("S3 audit: a whole-prefix public allow with the transport-only deny is public")
def test_allow_with_transport_deny_is_public(opts):
    ok, issues, details = _audit([PUBLIC_ALLOW, _transport_deny()])
    assert_eq(details["status"], "public",
              f"anonymous HTTPS reads succeed under a transport-only deny; issues={issues}")
    assert_true(ok, "the audit should answer public")
    assert_true(details.get("transport_only_deny") is True,
                f"the stored evidence should say a transport-only deny was set aside, got {details}")


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
        result = _audit([PUBLIC_ALLOW, deny])
        _assert_unknown_for(label, result, DENY_ISSUE)
        assert_true("transport_only_deny" not in result[2],
                    f"{label} must not be recorded as a transport-only deny")

    result = _audit([PUBLIC_ALLOW, _transport_deny(), dict(PUBLIC_ALLOW, Effect="Deny")])
    _assert_unknown_for("a transport-only deny beside an unconditional deny", result, DENY_ISSUE)

    result = _audit([dict(PUBLIC_ALLOW, Effect="Deny")])
    _assert_unknown_for("an unconditional deny with no public allow", result, DENY_ISSUE)


@th.django_unit_test("S3 audit: the transport-only deny does not excuse a plain-HTTP endpoint")
def test_transport_deny_with_http_endpoint_is_unknown(opts):
    result = _audit([PUBLIC_ALLOW, _transport_deny()], endpoint_url="http://minio.internal:9000")
    _assert_unknown_for("a transport-only deny on a plain-HTTP endpoint", result, DENY_ISSUE)


@th.django_unit_test("S3 audit: an unreadable or malformed policy is still unknown")
def test_unreadable_policy_is_unknown(opts):
    denied = ClientError({"Error": {"Code": "AccessDenied", "Message": "AccessDenied"}}, "test")
    ok, issues, details = _audit([], policy_error=denied)
    assert_true(not ok and details["status"] == "unknown",
                f"an unreadable policy must stay unknown, got {details['status']}")
    assert_eq(issues, ["Unable to read bucket policy safely."],
              "an unreadable policy must be unknown because it could not be read")

    from mojo.apps.fileman.backends.s3 import S3StorageBackend

    backend = object.__new__(S3StorageBackend)
    backend.bucket_name = BUCKET
    backend.folder_path = "fileman/group-123"
    backend.endpoint_url = "https://s3.us-east-1.amazonaws.com"
    backend.addressing_style = "auto"
    client = _PolicyClient()
    client.get_bucket_policy = lambda **kwargs: {"Policy": "[1, 2"}
    backend._client = client
    ok, issues, details = backend.check_public_access_for_prefix()
    assert_true(not ok and details["status"] == "unknown",
                f"a malformed policy must stay unknown, got {details['status']}")
    assert_eq(issues, ["Unable to parse bucket policy safely."],
              "a malformed policy must be unknown because it could not be parsed")


@th.django_unit_test("S3 audit: the transport-only deny is recognised in the forms AWS accepts, and no others")
def test_transport_deny_value_forms(opts):
    transport = _transport_deny()
    recognised = {
        "boolean False": {"Bool": {"aws:SecureTransport": False}},
        "string False": {"Bool": {"aws:SecureTransport": "False"}},
        "one-element list": {"Bool": {"aws:SecureTransport": ["false"]}},
        "upper-case key name": {"Bool": {"AWS:SecureTransport": "false"}},
    }
    for label, condition in recognised.items():
        ok, issues, details = _audit([PUBLIC_ALLOW, dict(transport, Condition=condition)])
        assert_true(ok and details["status"] == "public",
                    f"{label} is the transport-only deny and should not block public; "
                    f"got {details['status']}, issues={issues}")

    not_recognised = {
        "integer 0": {"Bool": {"aws:SecureTransport": 0}},
        "lower-case operator": {"bool": {"aws:SecureTransport": "false"}},
        "two-element list": {"Bool": {"aws:SecureTransport": ["false", "false"]}},
        "nested list": {"Bool": {"aws:SecureTransport": [["false"]]}},
        "empty condition": {},
        "condition that is not an object": "aws:SecureTransport=false",
    }
    for label, condition in not_recognised.items():
        result = _audit([PUBLIC_ALLOW, dict(transport, Condition=condition)])
        _assert_unknown_for(label, result, DENY_ISSUE)


@th.django_unit_test("S3 audit: provisioning's public access block keeps a public allow private")
def test_provisioned_block_is_private(opts):
    ok, issues, details = _audit([PUBLIC_ALLOW, _transport_deny()], bucket_pab=PROVISIONED_BLOCK)
    assert_true(not ok and details["status"] == "private",
                f"RestrictPublicBuckets makes the policy ineffective, so private; got {details['status']}")
    assert_eq(issues, ["Bucket Public Access Block prevents effective public bucket policies."],
              "private must come from the public access block")


@th.django_unit_test("S3 audit: an allow written with a Not element is unknown, never ignored")
def test_not_element_allow_is_unknown(opts):
    resource = PUBLIC_ALLOW["Resource"]
    role = {"AWS": "arn:aws:iam::123456789012:role/app"}
    cases = {
        "NotPrincipal": {"Effect": "Allow", "NotPrincipal": role,
                         "Action": "s3:GetObject", "Resource": resource},
        "NotAction that leaves GetObject in": {"Effect": "Allow", "Principal": "*",
                                               "NotAction": "s3:DeleteObject", "Resource": resource},
        "NotResource": {"Effect": "Allow", "Principal": "*", "Action": "s3:GetObject",
                        "NotResource": f"arn:aws:s3:::{BUCKET}/other/*"},
    }
    for label, allow in cases.items():
        _assert_unknown_for(f"a public allow with {label}", _audit([allow]), PARTIAL_ISSUE)
        _assert_unknown_for(f"a public allow with {label} and the transport-only deny",
                            _audit([allow, _transport_deny()]), PARTIAL_ISSUE)

    private_cases = {
        "a role allow with NotAction": {"Effect": "Allow", "Principal": role,
                                        "NotAction": "s3:DeleteObject", "Resource": resource},
        "a public NotAction allow that excludes GetObject": {
            "Effect": "Allow", "Principal": "*", "NotAction": "s3:Get*", "Resource": resource},
        "a public NotPrincipal allow on another prefix": {
            "Effect": "Allow", "NotPrincipal": role, "Action": "s3:GetObject",
            "Resource": f"arn:aws:s3:::{BUCKET}/other/*"},
    }
    for label, allow in private_cases.items():
        ok, issues, details = _audit([allow])
        assert_true(not ok and details["status"] == "private",
                    f"{label} cannot grant anonymous reads here and stays private; "
                    f"got {details['status']}, issues={issues}")


@th.django_unit_test("S3 audit: the object path agrees with the policy under a transport-only deny")
def test_object_path_with_transport_deny(opts):
    path = "fileman/group-123/report.pdf"

    def probe(status):
        # Stands in for the authenticated and anonymous HEAD requests, which
        # 12_test_public_access_reconciliation covers against the real method.
        return lambda file_path: (status == "public", [], {
            "bucket": BUCKET, "prefix": "fileman/group-123", "file_path": file_path,
            "method": "object", "status": status})

    backend = _backend([PUBLIC_ALLOW, _transport_deny()])
    backend._audit_existing_object = probe("public")
    ok, issues, details = backend.check_public_access_for_prefix(file_path=path)
    assert_true(ok and details["status"] == "public",
                f"a readable object plus whole-prefix policy evidence is public; "
                f"got {details['status']}, issues={issues}")
    assert_true(details["policy_evidence"].get("transport_only_deny") is True,
                "the policy evidence kept with the object result should name the transport-only deny")

    backend = _backend([PUBLIC_ALLOW, _transport_deny()])
    backend._audit_existing_object = probe("private")
    ok, _, details = backend.check_public_access_for_prefix(file_path=path)
    assert_true(not ok and details["status"] == "private",
                f"an anonymous refusal on a real object wins over the policy; got {details['status']}")

    backend = _backend([PUBLIC_ALLOW, dict(PUBLIC_ALLOW, Effect="Deny")])
    backend._audit_existing_object = probe("public")
    ok, _, details = backend.check_public_access_for_prefix(file_path=path)
    assert_true(not ok and details["status"] == "unknown",
                f"one readable object under an overriding deny must not make the prefix public; "
                f"got {details['status']}")
