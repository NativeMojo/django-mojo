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
MALFORMED_ISSUE = "The bucket policy has a statement this check cannot read safely."

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
    }
    for label, condition in not_recognised.items():
        result = _audit([PUBLIC_ALLOW, dict(transport, Condition=condition)])
        _assert_unknown_for(label, result, DENY_ISSUE)

    not_a_condition = {
        "nested list": {"Bool": {"aws:SecureTransport": [["false"]]}},
        "empty list": {"Bool": {"aws:SecureTransport": []}},
        "empty condition": {},
        "condition that is not an object": "aws:SecureTransport=false",
    }
    for label, condition in not_a_condition.items():
        result = _audit([PUBLIC_ALLOW, dict(transport, Condition=condition)])
        _assert_unknown_for(f"a deny with {label}", result, MALFORMED_ISSUE)


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


@th.django_unit_test("S3 audit: valid JSON that is not a valid policy statement is unknown")
def test_malformed_statement_is_unknown(opts):
    transport = _transport_deny()
    resource = PUBLIC_ALLOW["Resource"]
    no_action = {key: value for key, value in PUBLIC_ALLOW.items() if key != "Action"}
    no_resource = {key: value for key, value in PUBLIC_ALLOW.items() if key != "Resource"}
    malformed = {
        "an allow whose Condition is a list": dict(PUBLIC_ALLOW, Condition=[]),
        "an allow whose Condition is a string": dict(PUBLIC_ALLOW, Condition="none"),
        "a statement that is a number": 42,
        "a statement that is a list": [PUBLIC_ALLOW],
        "a deny with no Action": dict(no_action, Effect="Deny"),
        "a deny whose Action is a number": dict(PUBLIC_ALLOW, Effect="Deny", Action=42),
        "a deny whose Action is an empty list": dict(PUBLIC_ALLOW, Effect="Deny", Action=[]),
        "a deny with no Resource": dict(no_resource, Effect="Deny"),
        "a deny whose Resource is a number": dict(PUBLIC_ALLOW, Effect="Deny", Resource=42),
        "a statement with both Action and NotAction": dict(PUBLIC_ALLOW, NotAction="s3:PutObject"),
        "a statement with both Resource and NotResource": dict(PUBLIC_ALLOW, NotResource=resource),
        "a statement with no Effect": {key: value for key, value in PUBLIC_ALLOW.items() if key != "Effect"},
        "a statement with a lower-case effect": dict(PUBLIC_ALLOW, Effect="allow"),
        "an allow with an empty Condition object": dict(PUBLIC_ALLOW, Condition={}),
        "an allow whose Condition operator is a number": dict(PUBLIC_ALLOW, Condition={"Bool": 42}),
        "an allow whose Condition operator is empty": dict(PUBLIC_ALLOW, Condition={"Bool": {}}),
        "an allow whose Condition value is an object": dict(
            PUBLIC_ALLOW, Condition={"Bool": {"aws:SecureTransport": {"is": "false"}}}),
        "an allow whose Condition value is null": dict(
            PUBLIC_ALLOW, Condition={"Bool": {"aws:SecureTransport": None}}),
        "a principal list with a number in it": dict(PUBLIC_ALLOW, Principal={"AWS": ["*", 42]}),
        "a principal with a malformed sibling": dict(PUBLIC_ALLOW, Principal={"AWS": "*", "Service": 42}),
        "a principal of an unknown kind": dict(PUBLIC_ALLOW, Principal={"Everyone": "*"}),
        "a principal that is an empty object": dict(PUBLIC_ALLOW, Principal={}),
        "a principal string that is not the wildcard": dict(PUBLIC_ALLOW, Principal="everyone"),
        "a principal that is a list": dict(PUBLIC_ALLOW, Principal=["*"]),
        "a statement with no principal": {
            key: value for key, value in PUBLIC_ALLOW.items() if key != "Principal"},
        "a statement with both Principal and NotPrincipal": dict(
            PUBLIC_ALLOW, NotPrincipal={"AWS": "arn:aws:iam::123456789012:role/app"}),
        "a statement with an element the grammar does not have": dict(PUBLIC_ALLOW, Principals="*"),
        "a statement whose Sid is a number": dict(PUBLIC_ALLOW, Sid=7),
    }
    for label, statement in malformed.items():
        # Beside the transport-only deny: the case the exemption must not open.
        _assert_unknown_for(f"{label} beside a public allow and the transport-only deny",
                            _audit([PUBLIC_ALLOW, statement, transport]), MALFORMED_ISSUE)
        _assert_unknown_for(f"{label} beside the transport-only deny",
                            _audit([statement, transport]), MALFORMED_ISSUE)
        _assert_unknown_for(f"{label} beside a public allow",
                            _audit([PUBLIC_ALLOW, statement]), MALFORMED_ISSUE)

    backend = _backend([])
    backend._client.get_bucket_policy = lambda **kwargs: {
        "Policy": json.dumps({"Version": "2012-10-17", "Statement": 42})}
    _assert_unknown_for("a Statement that is a number",
                        backend.check_public_access_for_prefix(), MALFORMED_ISSUE)

    well_formed = {
        "a single statement object": PUBLIC_ALLOW,
        "an action list": [dict(PUBLIC_ALLOW, Action=["s3:GetObject", "s3:GetObjectVersion"]), transport],
        "a principal written as an AWS list": [dict(PUBLIC_ALLOW, Principal={"AWS": ["*"]}), transport],
        "a statement with a Sid": [dict(PUBLIC_ALLOW, Sid="PublicRead"), transport],
        "a conditional allow beside the public allow": [
            PUBLIC_ALLOW,
            dict(PUBLIC_ALLOW, Condition={"StringEquals": {"aws:PrincipalOrgID": "o-example"}}),
            transport],
        "a role allow with a numeric and a list condition beside the public allow": [
            PUBLIC_ALLOW,
            dict(PUBLIC_ALLOW, Principal={"AWS": "arn:aws:iam::123456789012:role/app", "Service": [
                "cloudfront.amazonaws.com"]}, Condition={
                    "NumericLessThan": {"s3:max-keys": 10},
                    "IpAddress": {"aws:SourceIp": ["203.0.113.0/24", "198.51.100.0/24"]}}),
            transport],
    }
    for label, statements in well_formed.items():
        backend = _backend([])
        backend._client.get_bucket_policy = lambda statements=statements, **kwargs: {
            "Policy": json.dumps({"Version": "2012-10-17", "Statement": statements})}
        ok, issues, details = backend.check_public_access_for_prefix()
        assert_true(ok and details["status"] == "public",
                    f"{label} is a valid policy form and stays public; got {details['status']}, issues={issues}")
