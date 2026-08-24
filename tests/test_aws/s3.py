
TESTIT_TIER = "admin"
import datetime
import importlib
import json
import time
from unittest import mock

from botocore.exceptions import ClientError, EndpointConnectionError
from testit import helpers as th


REGION_HEADERS = {
    "ResponseMetadata": {
        "HTTPStatusCode": 200,
        "HTTPHeaders": {"x-amz-bucket-region": "us-east-1"},
    }
}


def _client_error(code, status, operation="operation", region=None,
                  message="RAW PROVIDER SENTINEL", request_id="RAW REQUEST ID"):
    headers = {"x-amz-request-id": request_id}
    if region:
        headers["x-amz-bucket-region"] = region
    return ClientError({
        "Error": {"Code": code, "Message": message},
        "ResponseMetadata": {"HTTPStatusCode": status, "HTTPHeaders": headers},
    }, operation)


class ScriptedPaginator:
    def __init__(self, pages):
        self.pages = list(pages)
        self.calls = []

    def paginate(self, **kwargs):
        self.calls.append(kwargs)
        return iter(self.pages)


class ScriptedClient:
    def __init__(self, **scripts):
        self.scripts = {name: list(values) for name, values in scripts.items()}
        self.calls = []
        self.paginators = {}

    def get_paginator(self, name):
        self.calls.append(("get_paginator", {"name": name}))
        if name not in self.paginators:
            raise AssertionError("unexpected paginator %s" % name)
        return self.paginators[name]

    def __getattr__(self, name):
        if name.startswith("_"):
            raise AttributeError(name)

        def invoke(**kwargs):
            self.calls.append((name, kwargs))
            queue = self.scripts.get(name)
            if not queue:
                raise AssertionError("unexpected %s call" % name)
            value = queue.pop(0)
            if isinstance(value, Exception):
                raise value
            return value

        return invoke


class FakeClientFactory:
    def __init__(self, clients):
        self.clients = clients
        self.calls = []

    def __call__(self, service, access_key=None, secret_key=None, region=None):
        self.calls.append((service, region))
        key = (service, region)
        if key not in self.clients:
            raise AssertionError("unexpected client %r" % (key,))
        return self.clients[key]


def _factory_for(s3_client, s3control=None):
    sts = ScriptedClient(get_caller_identity=[{"Account": "123456789012"}])
    return FakeClientFactory({
        ("s3", "us-east-1"): s3_client,
        ("sts", "us-east-1"): sts,
        ("s3control", "us-east-1"): s3control or ScriptedClient(),
    })


def _success_heads(count=2):
    return [dict(REGION_HEADERS) for unused in range(count)]


@th.django_unit_test()
def test_inventory_is_paginated_and_preserves_provider_order(opts):
    from mojo.helpers.aws import s3

    created = datetime.datetime(2026, 8, 6, tzinfo=datetime.timezone.utc)
    paginator = ScriptedPaginator([
        {"Buckets": [{"Name": "second", "CreationDate": created}], "ContinuationToken": "next"},
        {"Buckets": [{"Name": "first", "CreationDate": created}]},
    ])
    client = ScriptedClient()
    client.paginators["list_buckets"] = paginator
    # Injected via list_all_buckets' client seam: assigning s3.S3._client
    # mutates the shared module-level S3Config under the parallel runner (#2558).
    rows = s3.S3.list_all_buckets(client=client)
    assert [row["name"] for row in rows] == ["second", "first"], \
        f"inventory must preserve provider order, got {rows}"
    assert paginator.calls == [{"MaxBuckets": 1000}], \
        f"inventory must request bounded pages, got {paginator.calls}"
    assert set(rows[0]) == {"id", "name", "created"}, \
        f"inventory row shape changed: {rows[0]}"


@th.django_unit_test()
def test_inventory_failure_is_not_an_empty_account(opts):
    from mojo.helpers.aws import s3

    client = ScriptedClient()
    client.paginators["list_buckets"] = ScriptedPaginator([
        _client_error("AccessDenied", 403, "ListBuckets")
    ])
    # A paginator raises while it is iterated, so use a tiny raising paginator.
    client.paginators["list_buckets"].paginate = mock.Mock(
        side_effect=_client_error("AccessDenied", 403, "ListBuckets")
    )
    try:
        s3.S3.list_all_buckets(client=client)
    except s3.S3OperationError as error:
        assert error.status == 403, f"IAM denial must remain 403, got {error.status}"
        assert error.provider_code == "AccessDenied", \
            f"safe provider code should survive, got {error.provider_code}"
    else:
        assert False, "inventory IAM denial must raise instead of returning []"


@th.django_unit_test()
def test_operation_errors_redact_raw_provider_detail(opts):
    from mojo.helpers.aws import s3

    raw = _client_error("UnboundedSecretCode", 500, message="secret-object-key",
                        request_id="request-secret")
    error = s3._map_provider_error(raw, "head_bucket")
    rendered = json.dumps({"failure": error.failure(), "text": str(error), "repr": repr(error)})
    assert error.provider_code == "provider_error", \
        f"unknown provider codes must collapse, got {error.provider_code}"
    for sentinel in ("secret-object-key", "request-secret", "UnboundedSecretCode"):
        assert sentinel not in rendered, f"raw provider sentinel leaked through structured error: {rendered}"


@th.django_unit_test()
def test_head_uses_region_hint_and_never_turns_access_denial_into_missing(opts):
    from mojo.helpers.aws import s3

    east = ScriptedClient(head_bucket=[
        _client_error("PermanentRedirect", 301, "HeadBucket", region="us-west-2"),
    ])
    west = ScriptedClient(head_bucket=[{
        "ResponseMetadata": {
            "HTTPStatusCode": 200,
            "HTTPHeaders": {"x-amz-bucket-region": "us-west-2"},
        }
    }])
    factory = FakeClientFactory({("s3", "us-east-1"): east, ("s3", "us-west-2"): west})
    bucket = s3.S3Bucket("cross-region", client_factory=factory)
    assert bucket.exists is True and bucket.region == "us-west-2", \
        f"cross-region head should freeze the advertised region, got {bucket.region}"
    assert factory.calls == [("s3", "us-east-1"), ("s3", "us-west-2")], \
        f"region resolution should construct exactly the initial and regional clients: {factory.calls}"

    denied = ScriptedClient(head_bucket=[_client_error("AccessDenied", 403, "HeadBucket")])
    try:
        s3.S3Bucket("denied", client_factory=FakeClientFactory({("s3", "us-east-1"): denied}))
    except s3.S3OperationError as error:
        assert error.status == 403, f"head access denial must be 403, got {error.status}"
    else:
        assert False, "head access denial must never become exists=False"


@th.django_unit_test()
def test_private_create_verifies_lock_without_cors_or_delete(opts):
    from mojo.helpers.aws import s3

    client = ScriptedClient(
        head_bucket=[
            _client_error("NoSuchBucket", 404, "HeadBucket"),
            dict(REGION_HEADERS),
        ],
        create_bucket=[{}],
        put_public_access_block=[{}],
        get_public_access_block=[{"PublicAccessBlockConfiguration": dict(s3.PRIVATE_PUBLIC_ACCESS_BLOCK)}],
    )
    bucket = s3.S3Bucket("new-private", client_factory=_factory_for(client))
    result = bucket.create_private()
    assert result == {"id": "new-private", "name": "new-private", "created_new": True}, \
        f"new private result is not exact: {result}"
    operations = [name for name, unused in client.calls]
    assert "put_bucket_cors" not in operations, "private create must not install wildcard CORS"
    assert "delete_bucket" not in operations, "private create must never roll back with DeleteBucket"
    owner_head = [kwargs for name, kwargs in client.calls
                  if name == "head_bucket" and "ExpectedBucketOwner" in kwargs]
    assert owner_head and owner_head[0]["ExpectedBucketOwner"] == "123456789012", \
        f"post-create PAB must follow an owner-bound head, got {client.calls}"


@th.django_unit_test()
def test_private_access_sets_all_four_flags_and_never_writes_policy(opts):
    from mojo.helpers.aws import s3

    client = ScriptedClient(
        head_bucket=_success_heads(3),
        put_public_access_block=[{}],
        get_public_access_block=[{"PublicAccessBlockConfiguration": dict(s3.PRIVATE_PUBLIC_ACCESS_BLOCK)}],
    )
    bucket = s3.S3Bucket("private", client_factory=_factory_for(client))
    result = bucket.set_public(False)
    assert result["is_public"] is False and result["complete"] is True, \
        f"private access result should be verified complete, got {result}"
    operations = [name for name, unused in client.calls]
    assert "put_bucket_policy" not in operations, "private mode must leave policy text untouched"
    put = next(kwargs for name, kwargs in client.calls if name == "put_public_access_block")
    assert put["PublicAccessBlockConfiguration"] == s3.PRIVATE_PUBLIC_ACCESS_BLOCK, \
        f"private mode must set all four flags, got {put}"
    assert put["ExpectedBucketOwner"] == "123456789012", \
        "private mutation must bind ExpectedBucketOwner"


@th.django_unit_test()
def test_public_access_preserves_unrelated_policy_and_blocks_acls(opts):
    from mojo.helpers.aws import s3

    unrelated = {
        "Sid": "KeepOperatorRule",
        "Effect": "Deny",
        "Principal": "*",
        "Action": "s3:DeleteObject",
        "Resource": "arn:aws:s3:::public-test/*",
    }
    old_policy = {"Version": "2012-10-17", "Id": "keep-id", "Statement": unrelated}
    managed = {
        "Sid": s3.MANAGED_PUBLIC_READ_SID,
        "Effect": "Allow", "Principal": "*", "Action": "s3:GetObject",
        "Resource": "arn:aws:s3:::public-test/*",
    }
    final_policy = {"Version": "2012-10-17", "Id": "keep-id", "Statement": [unrelated, managed]}
    client = ScriptedClient(
        head_bucket=_success_heads(3),
        get_public_access_block=[
            {"PublicAccessBlockConfiguration": dict(s3.PRIVATE_PUBLIC_ACCESS_BLOCK)},
            {"PublicAccessBlockConfiguration": dict(s3.PUBLIC_POLICY_ACCESS_BLOCK)},
            {"PublicAccessBlockConfiguration": dict(s3.PUBLIC_POLICY_ACCESS_BLOCK)},
        ],
        get_bucket_policy=[
            {"Policy": json.dumps(old_policy)},
            {"Policy": json.dumps(old_policy)},
            {"Policy": json.dumps(final_policy)},
        ],
        get_bucket_policy_status=[
            {"PolicyStatus": {"IsPublic": False}},
            {"PolicyStatus": {"IsPublic": True}},
        ],
        put_public_access_block=[{}],
        put_bucket_policy=[{}],
    )
    control = ScriptedClient(get_public_access_block=[
        {"PublicAccessBlockConfiguration": {key: False for key in s3.PRIVATE_PUBLIC_ACCESS_BLOCK}},
        {"PublicAccessBlockConfiguration": {key: False for key in s3.PRIVATE_PUBLIC_ACCESS_BLOCK}},
    ])
    bucket = s3.S3Bucket("public-test", client_factory=_factory_for(client, control))
    result = bucket.set_public(True)
    assert result["is_public"] is True and result["complete"] is True, \
        f"public result should be verified complete, got {result}"
    put_pab = next(kwargs for name, kwargs in client.calls if name == "put_public_access_block")
    assert put_pab["PublicAccessBlockConfiguration"] == s3.PUBLIC_POLICY_ACCESS_BLOCK, \
        f"public mode must keep ACL flags blocked, got {put_pab}"
    put_policy = next(kwargs for name, kwargs in client.calls if name == "put_bucket_policy")
    decoded = json.loads(put_policy["Policy"])
    assert decoded["Id"] == "keep-id" and unrelated in decoded["Statement"], \
        f"unrelated policy content must survive, got {decoded}"


@th.django_unit_test()
def test_account_public_block_refuses_before_any_mutation(opts):
    from mojo.helpers.aws import s3

    client = ScriptedClient(
        head_bucket=_success_heads(3),
        get_public_access_block=[{"PublicAccessBlockConfiguration": dict(s3.PRIVATE_PUBLIC_ACCESS_BLOCK)}],
        get_bucket_policy=[_client_error("NoSuchBucketPolicy", 404, "GetBucketPolicy")],
        get_bucket_policy_status=[{"PolicyStatus": {"IsPublic": False}}],
    )
    control = ScriptedClient(get_public_access_block=[{"PublicAccessBlockConfiguration": {
        "BlockPublicAcls": False, "IgnorePublicAcls": False,
        "BlockPublicPolicy": True, "RestrictPublicBuckets": False,
    }}])
    bucket = s3.S3Bucket("blocked-public", client_factory=_factory_for(client, control))
    try:
        bucket.set_public(True)
    except s3.S3OperationError as error:
        assert error.mutation_state == "none", \
            f"account-PAB refusal must be pre-mutation, got {error.mutation_state}"
    else:
        assert False, "account public block must reject public enablement"
    operations = [name for name, unused in client.calls]
    assert not any(name.startswith("put_") for name in operations), \
        f"account-PAB refusal must make zero mutations, got {operations}"


@th.django_unit_test()
def test_empty_unversioned_counts_only_delete_acknowledgements(opts):
    from mojo.helpers.aws import s3

    client = ScriptedClient(
        head_bucket=_success_heads(3),
        get_bucket_versioning=[{}, {}, {}, {}, {}],
        list_object_versions=[
            {"Versions": [], "DeleteMarkers": []},
            {"Versions": [], "DeleteMarkers": []},
            {"Versions": [], "DeleteMarkers": []},
        ],
        list_objects_v2=[
            {"Contents": [{"Key": "secret-key"}]},
            {"Contents": [{"Key": "secret-key"}]},
            {"Contents": []},
            {"Contents": []},
        ],
        list_multipart_uploads=[{"Uploads": []}, {"Uploads": []}, {"Uploads": []}],
        delete_objects=[{"Deleted": [{"Key": "secret-key"}], "Errors": []}],
    )
    bucket = s3.S3Bucket("empty-test", client_factory=_factory_for(client))
    result = bucket.empty()
    assert result["complete"] is True and result["deleted_objects"] == 1, \
        f"empty must count the one exact acknowledgement, got {result}"
    delete_call = next(kwargs for name, kwargs in client.calls if name == "delete_objects")
    assert delete_call["Delete"]["Quiet"] is False, "DeleteObjects must request verbose acknowledgements"
    assert delete_call["ExpectedBucketOwner"] == "123456789012", \
        "empty mutations must bind ExpectedBucketOwner"
    assert "delete_bucket" not in [name for name, unused in client.calls], \
        "empty must never call DeleteBucket"


@th.django_unit_test()
def test_delete_errors_stop_without_fabricating_counts_or_identifiers(opts):
    from mojo.helpers.aws import s3

    client = ScriptedClient(
        head_bucket=_success_heads(3),
        get_bucket_versioning=[{}, {}, {}],
        list_object_versions=[
            {"Versions": [], "DeleteMarkers": []},
            {"Versions": [], "DeleteMarkers": []},
        ],
        list_objects_v2=[
            {"Contents": [{"Key": "do-not-leak"}]},
            {"Contents": [{"Key": "do-not-leak"}]},
        ],
        list_multipart_uploads=[{"Uploads": []}, {"Uploads": []}],
        delete_objects=[{
            "Deleted": [],
            "Errors": [{"Key": "do-not-leak", "Code": "AccessDenied", "Message": "raw-secret"}],
        }],
    )
    bucket = s3.S3Bucket("partial-test", client_factory=_factory_for(client))
    try:
        bucket.empty()
    except s3.S3OperationError as error:
        wire = json.dumps(error.data)
        assert error.data["counts"]["deleted_objects"] == 0, \
            f"unacknowledged delete must not increment count, got {error.data}"
        assert error.data["failed"]["objects"] == 1, \
            f"known per-key failure must aggregate safely, got {error.data}"
        assert "do-not-leak" not in wire and "raw-secret" not in wire, \
            f"identifiers/provider message leaked in error data: {wire}"
    else:
        assert False, "per-item DeleteObjects error must reject empty"


@th.django_unit_test()
def test_empty_versioned_bucket_deletes_null_versions_versions_and_markers(opts):
    from mojo.helpers.aws import s3

    listed = {
        "Versions": [
            {"Key": "null-object", "VersionId": "null"},
            {"Key": "versioned-object", "VersionId": "version-1"},
        ],
        "DeleteMarkers": [{"Key": "marked-object", "VersionId": "marker-1"}],
    }
    client = ScriptedClient(
        head_bucket=_success_heads(3),
        get_bucket_versioning=[{"Status": "Enabled"}, {"Status": "Enabled"}, {"Status": "Enabled"}],
        list_object_versions=[listed, listed, {"Versions": [], "DeleteMarkers": []},
                              {"Versions": [], "DeleteMarkers": []}],
        list_objects_v2=[{"Contents": []}, {"Contents": []}],
        list_multipart_uploads=[{"Uploads": []}, {"Uploads": []}, {"Uploads": []}],
        delete_objects=[{"Deleted": [
            {"Key": "null-object", "VersionId": "null"},
            {"Key": "versioned-object", "VersionId": "version-1"},
            {"Key": "marked-object", "VersionId": "marker-1"},
        ], "Errors": []}],
    )
    result = s3.S3Bucket("versioned-empty", client_factory=_factory_for(client)).empty()
    assert result["deleted_objects"] == 1, f"null version must count as object, got {result}"
    assert result["deleted_versions"] == 1, f"non-null version count is wrong: {result}"
    assert result["deleted_markers"] == 1, f"delete-marker count is wrong: {result}"
    request = next(kwargs for name, kwargs in client.calls if name == "delete_objects")
    identifiers = request["Delete"]["Objects"]
    assert all("VersionId" in item for item in identifiers), \
        f"versioned cleanup must qualify every delete, got {identifiers}"
    assert not any(name == "delete_objects" and any("VersionId" not in item for item in kwargs["Delete"]["Objects"])
                   for name, kwargs in client.calls), \
        "enabled versioning must never issue a key-only delete"


def _multipart_empty_client(abort_response, parts_response):
    return ScriptedClient(
        head_bucket=_success_heads(3),
        get_bucket_versioning=[{}, {}, {}, {}],
        list_object_versions=[
            {"Versions": [], "DeleteMarkers": []},
            {"Versions": [], "DeleteMarkers": []},
            {"Versions": [], "DeleteMarkers": []},
        ],
        list_objects_v2=[{"Contents": []}, {"Contents": []}, {"Contents": []}],
        list_multipart_uploads=[
            {"Uploads": [{"Key": "upload-key", "UploadId": "upload-id"}]},
            {"Uploads": [{"Key": "upload-key", "UploadId": "upload-id"}]},
            {"Uploads": []},
            {"Uploads": []},
        ],
        abort_multipart_upload=[abort_response],
        list_parts=[parts_response],
    )


@th.django_unit_test()
def test_empty_aborts_and_verifies_multipart_upload(opts):
    from mojo.helpers.aws import s3

    client = _multipart_empty_client(
        {},
        _client_error("NoSuchUpload", 404, "ListParts"),
    )
    result = s3.S3Bucket("multipart-empty", client_factory=_factory_for(client)).empty()
    assert result["aborted_uploads"] == 1 and result["complete"] is True, \
        f"acknowledged abort plus terminal ListParts must complete, got {result}"
    abort = next(kwargs for name, kwargs in client.calls if name == "abort_multipart_upload")
    parts = next(kwargs for name, kwargs in client.calls if name == "list_parts")
    assert abort["ExpectedBucketOwner"] == "123456789012", \
        f"abort must carry ExpectedBucketOwner, got {abort}"
    assert parts["MaxParts"] == 1 and parts["UploadId"] == "upload-id", \
        f"abort must be verified by a bounded ListParts call, got {parts}"


@th.django_unit_test()
def test_empty_handles_no_such_upload_race_without_inventing_count(opts):
    from mojo.helpers.aws import s3

    client = _multipart_empty_client(
        _client_error("NoSuchUpload", 404, "AbortMultipartUpload"),
        _client_error("NoSuchUpload", 404, "ListParts"),
    )
    result = s3.S3Bucket("multipart-race", client_factory=_factory_for(client)).empty()
    assert result["aborted_uploads"] == 0 and result["complete"] is True, \
        f"concurrent NoSuchUpload proves terminal state without attribution, got {result}"


@th.django_unit_test()
def test_multipart_retryable_failure_is_unknown_and_preserves_zero_count(opts):
    from mojo.helpers.aws import s3

    client = ScriptedClient(
        head_bucket=_success_heads(3),
        get_bucket_versioning=[{}],
        list_object_versions=[{"Versions": [], "DeleteMarkers": []}],
        list_objects_v2=[{"Contents": []}],
        list_multipart_uploads=[
            {"Uploads": [{"Key": "upload-key", "UploadId": "upload-id"}]},
            {"Uploads": [{"Key": "upload-key", "UploadId": "upload-id"}]},
        ],
        abort_multipart_upload=[_client_error("SlowDown", 503, "AbortMultipartUpload")],
    )
    try:
        s3.S3Bucket("multipart-unknown", client_factory=_factory_for(client)).empty()
    except s3.S3OperationError as error:
        assert error.mutation_state == "unknown", \
            f"first retryable abort outcome must remain unknown, got {error.mutation_state}"
        assert error.data["counts"]["aborted_uploads"] == 0, \
            f"ambiguous abort must not invent an acknowledgement, got {error.data}"
        assert error.data["failed"]["uploads"] == 1, \
            f"failed upload must be aggregated once, got {error.data}"
    else:
        assert False, "retryable AbortMultipartUpload failure must reject empty"


@th.django_unit_test()
def test_first_retryable_service_error_is_unknown_for_every_mutating_call(opts):
    from mojo.helpers.aws import s3

    operations = [
        "create_bucket",
        "put_bucket_policy",
        "put_public_access_block",
        "abort_multipart_upload",
        "delete_objects",
    ]
    for operation in operations:
        heads = [_client_error("NoSuchBucket", 404, "HeadBucket")] if operation == "create_bucket" else _success_heads(2)
        client = ScriptedClient(head_bucket=heads, **{
            operation: [_client_error("SlowDown", 503, operation)],
        })
        bucket = s3.S3Bucket("unknown-%s" % operation, client_factory=FakeClientFactory({
            ("s3", "us-east-1"): client,
        }))
        try:
            bucket._call(operation, getattr(client, operation), mutation=True)
        except s3.S3OperationError as error:
            assert error.mutation_state == "unknown", \
                f"first retryable {operation} must be unknown, got {error.mutation_state}"
            assert error.status == 409 and error.error_code == "s3_operation_incomplete", \
                f"ambiguous {operation} must use incomplete 409, got {error.status}/{error.error_code}"
        else:
            assert False, f"retryable {operation} must raise a structured operation error"


@th.django_unit_test()
def test_rest_action_parser_is_a_strict_union(opts):
    rest_s3 = importlib.import_module("mojo.apps.aws.rest.s3")

    assert rest_s3._parse_post_action({"set_public": False}, "bucket") == ("set_public", False), \
        "set_public:false must dispatch by key presence"
    assert rest_s3._parse_post_action({"empty": {"confirm_name": "bucket"}}, "bucket")[0] == "empty", \
        "exact empty confirmation must dispatch"
    bad = [
        {"set_public": 0},
        {"set_public": True, "empty": {"confirm_name": "bucket"}},
        {"empty": {"confirm_name": "other"}},
        {"empty": {"confirm_name": "bucket", "extra": True}},
        {"typo": True},
    ]
    for value in bad:
        try:
            rest_s3._parse_post_action(value, "bucket")
        except ValueError:
            continue
        assert False, f"malformed action must reject before provider construction: {value}"


@th.django_unit_test()
def test_routed_validation_permissions_freshness_and_delete_need_no_provider(opts):
    from mojo.apps.account.models import User
    from mojo.apps.account.utils.jwtoken import JWToken
    from tests.test_global_perms._helpers import make_group_member, make_user, login

    allowed, email, password = make_user(perms=["manage_aws"])
    login(opts, email, password)
    try:
        invalid = opts.client.post("/api/aws/s3/bucket/example", {"typo": True})
        assert invalid.status_code == 400, \
            f"authorized caller must reach provider-free validation, got {invalid.status_code}: {invalid.json}"
        grouped = opts.client.post("/api/aws/s3/bucket/example", {"group": 1, "typo": True})
        assert grouped.status_code == 400, \
            f"global caller's group parameter must be rejected, got {grouped.status_code}: {grouped.json}"
        deleted = opts.client.delete("/api/aws/s3/bucket/example")
        assert deleted.status_code == 405, \
            f"bucket DELETE must remain unsupported, got {deleted.status_code}: {deleted.json}"

        stale = JWToken(allowed.get_auth_key()).create_access_token(
            uid=allowed.pk, auth_time=int(time.time()) - 600,
        )
        opts.client.access_token = stale
        opts.client.is_authenticated = True
        opts.client.bearer = "bearer"
        stale_response = opts.client.post(
            "/api/aws/s3/bucket/example",
            {"empty": {"confirm_name": "example"}},
        )
        assert stale_response.status_code == 440, \
            f"valid confirmed empty must require fresh auth before AWS, got {stale_response.status_code}: {stale_response.json}"
    finally:
        opts.client.logout()

    unused, denied_email, denied_password = make_user(perms=["manage_files"])
    login(opts, denied_email, denied_password)
    try:
        denied = opts.client.post("/api/aws/s3/bucket/example", {"typo": True})
        assert denied.status_code == 403, \
            f"manage_files must not open the global S3 route, got {denied.status_code}: {denied.json}"
    finally:
        opts.client.logout()

    unused, member_email, member_password, group = make_group_member(["manage_aws", "files"])
    login(opts, member_email, member_password)
    try:
        member_denied = opts.client.post(
            "/api/aws/s3/bucket/example",
            {"group": group.pk, "typo": True},
        )
        assert member_denied.status_code == 403, \
            f"member-only AWS/files grants must stop at the global gate, got {member_denied.status_code}: {member_denied.json}"
    finally:
        opts.client.logout()
    User.objects.filter(pk=allowed.pk).delete()


@th.django_unit_test()
def test_work_budget_is_finite_and_safe(opts):
    from mojo.helpers.aws import s3

    clock = mock.Mock(side_effect=[0, 26])
    budget = s3.S3OperationBudget(clock=clock)
    try:
        budget.before_call("list_objects_v2")
    except s3.S3OperationError as error:
        assert error.provider_code == "work_limit" and error.status == 409, \
            f"deadline exhaustion must be retryable incomplete work_limit, got {error.failure()}"
        assert error.retryable is True, "work_limit must be retryable"
    else:
        assert False, "expired operation budget must reject before a provider call"


@th.django_unit_test()
def test_transport_failure_after_mutation_is_unknown(opts):
    from mojo.helpers.aws import s3

    client = ScriptedClient(
        head_bucket=[_client_error("NoSuchBucket", 404, "HeadBucket")],
        create_bucket=[EndpointConnectionError(endpoint_url="https://s3.invalid")],
    )
    bucket = s3.S3Bucket("ambiguous-create", client_factory=_factory_for(client))
    try:
        bucket.create_private()
    except s3.S3OperationError as error:
        assert error.mutation_state == "unknown", \
            f"ambiguous create transport must be unknown, got {error.mutation_state}"
    else:
        assert False, "unreconciled ambiguous create must reject"
