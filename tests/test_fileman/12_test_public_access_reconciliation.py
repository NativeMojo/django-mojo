"""Regression coverage for truthful user-scoped S3 public URL behavior."""
import io
import json
from unittest.mock import patch

from botocore.exceptions import ClientError

from testit import helpers as th
from testit.helpers import assert_eq, assert_true


class _PrivateS3Backend:
    def __init__(self):
        self.audit_calls = []
        self.policy_mutations = []

    def test_connection(self):
        return True

    def check_public_access_for_prefix(self, file_path=None):
        self.audit_calls.append(file_path)
        return False, ["anonymous HeadObject returned 403"], {
            "status": "private",
            "method": "object" if file_path else "policy",
        }

    def get_url(self, file_path, expires_in=None):
        mode = "signed" if expires_in is not None else "unsigned"
        return f"https://example.test/{mode}/{file_path}"

    def make_path_public(self):
        self.policy_mutations.append("public")

    def make_path_private(self):
        self.policy_mutations.append("private")


class _UnknownS3Backend(_PrivateS3Backend):
    def check_public_access_for_prefix(self, file_path=None):
        self.audit_calls.append(file_path)
        return False, ["policy inspection unavailable"], {
            "status": "unknown",
            "method": "object" if file_path else "policy",
        }


class _PublicS3Backend(_PrivateS3Backend):
    def check_public_access_for_prefix(self, file_path=None):
        self.audit_calls.append(file_path)
        return True, [], {
            "status": "public",
            "method": "object" if file_path else "policy",
        }


class _PolicyClient:
    def __init__(self, statements=None, bucket_pab=None, policy_error=None, head_error=None):
        self.statements = statements or []
        self.bucket_pab = bucket_pab or {}
        self.policy_error = policy_error
        self.head_error = head_error

    def get_bucket_policy(self, **kwargs):
        if self.policy_error:
            raise self.policy_error
        return {"Policy": json.dumps({"Version": "2012-10-17", "Statement": self.statements})}

    def get_public_access_block(self, **kwargs):
        return {"PublicAccessBlockConfiguration": self.bucket_pab}

    def head_object(self, **kwargs):
        if self.head_error:
            raise self.head_error
        return {"ContentLength": 10}


def _client_error(code):
    return ClientError({"Error": {"Code": code, "Message": code}}, "test")


def _policy_backend(statements, bucket_pab=None, policy_error=None):
    from mojo.apps.fileman.backends.s3 import S3StorageBackend

    backend = object.__new__(S3StorageBackend)
    backend.bucket_name = "audit-test-bucket"
    backend.folder_path = "fileman/user-123"
    backend.endpoint_url = "https://s3.us-east-1.amazonaws.com"
    backend.addressing_style = "auto"
    backend._client = _PolicyClient(
        statements=statements,
        bucket_pab=bucket_pab,
        policy_error=policy_error,
    )
    return backend


@th.django_unit_test("FileManager: a new user S3 manager inherits and reconciles public access")
def test_new_user_manager_reconciles_public_access(opts):
    from mojo.apps.account.models import User
    from mojo.apps.fileman.models import FileManager

    username = "fm_public_audit_new_user"
    User.objects.filter(username=username).delete()
    user = User.objects.create(username=username, email=f"{username}@example.com")
    # get_for_user() uses QuerySet.first(); a parallel package may create its
    # own system default before this test. A dedicated negative fixture id keeps
    # this row deterministic without deleting or mutating another test's row.
    FileManager.objects.filter(pk=-1202).delete()
    system_manager = FileManager.objects.create(
        pk=-1202,
        name="fm_public_audit_system",
        backend_type="s3",
        backend_url="s3://audit-test-bucket/fileman",
        is_active=True,
        is_default=True,
        is_public=False,
    )
    backend = _PrivateS3Backend()

    try:
        with patch("mojo.apps.fileman.backends.get_backend", return_value=backend):
            manager = FileManager.get_for_user(user)

        assert_true(manager is not None, "get_for_user should provision a child manager")
        assert_eq(manager.is_public, False,
                  "a user manager derived from a private prefix must not keep the model's public default")
        assert_true(len(backend.audit_calls) == 1,
                    f"new user manager should be audited once, got {backend.audit_calls}")
    finally:
        FileManager.objects.filter(user=user).delete()
        system_manager.delete()
        user.delete()


@th.django_unit_test("File: an existing misclassified user S3 manager self-repairs and presigns")
def test_existing_user_manager_repairs_before_direct_url(opts):
    from mojo.apps.account.models import User
    from mojo.apps.fileman.models import FileManager, File

    username = "fm_public_audit_existing_user"
    User.objects.filter(username=username).delete()
    user = User.objects.create(username=username, email=f"{username}@example.com")
    manager = FileManager.objects.create(
        name="fm_public_audit_existing",
        backend_type="s3",
        backend_url="s3://audit-test-bucket/fileman/existing",
        user=user,
        is_active=True,
        is_public=True,
    )
    backend = _PrivateS3Backend()
    manager._backend = backend
    file_obj = File.objects.create(
        filename="avatar.png",
        storage_file_path="fileman/existing/avatar.png",
        content_type="image/png",
        file_size=10,
        file_manager=manager,
        user=user,
        download_url="https://example.test/unsigned/fileman/existing/avatar.png",
    )

    try:
        url = file_obj.get_direct_download_url()
        manager.refresh_from_db()

        assert_eq(manager.is_public, False,
                  "a confirmed anonymous 403 should repair the stored manager classification")
        assert_true("/signed/" in url,
                    f"a repaired private manager must return a fresh presigned URL, got {url}")
        assert_eq(backend.audit_calls, [file_obj.storage_file_path],
                  f"the audit should probe the exact existing object once, got {backend.audit_calls}")
    finally:
        file_obj.delete()
        manager.delete()
        user.delete()


@th.django_unit_test("File: genuine public object evidence preserves an unsigned URL")
def test_public_object_keeps_unsigned_url(opts):
    from mojo.apps.account.models import User
    from mojo.apps.fileman.models import FileManager, File

    username = "fm_public_audit_public_user"
    User.objects.filter(username=username).delete()
    user = User.objects.create(username=username, email=f"{username}@example.com")
    manager = FileManager.objects.create(
        name="fm_public_audit_public",
        backend_type="s3",
        backend_url="s3://audit-test-bucket/fileman/public",
        user=user,
        is_active=True,
        is_public=True,
    )
    backend = _PublicS3Backend()
    manager._backend = backend
    file_obj = File.objects.create(
        filename="public.png",
        storage_file_path="fileman/public/public.png",
        content_type="image/png",
        file_size=10,
        file_manager=manager,
        user=user,
    )

    try:
        url = file_obj.get_direct_download_url()
        manager.refresh_from_db()

        assert_true("/unsigned/" in url,
                    f"conclusive public evidence should preserve an unsigned URL, got {url}")
        assert_eq(manager.is_public, True, "public evidence should preserve is_public=True")
        assert_eq(manager.public_access_audit["status"], "public",
                  f"public evidence should be persisted, got {manager.public_access_audit}")
    finally:
        file_obj.delete()
        manager.delete()
        user.delete()


@th.django_unit_test("FileRendition: a misclassified manager repairs before direct URL resolution")
def test_rendition_repairs_and_presigns(opts):
    from mojo.apps.account.models import User
    from mojo.apps.fileman.models import FileManager, File, FileRendition

    username = "fm_public_audit_rendition_user"
    User.objects.filter(username=username).delete()
    user = User.objects.create(username=username, email=f"{username}@example.com")
    manager = FileManager.objects.create(
        name="fm_public_audit_rendition",
        backend_type="s3",
        backend_url="s3://audit-test-bucket/fileman/rendition",
        user=user,
        is_active=True,
        is_public=True,
    )
    backend = _PrivateS3Backend()
    manager._backend = backend
    file_obj = File.objects.create(
        filename="source.png",
        storage_file_path="fileman/rendition/source.png",
        content_type="image/png",
        file_size=10,
        file_manager=manager,
        user=user,
    )
    rendition = FileRendition.objects.create(
        original_file=file_obj,
        role="thumbnail",
        filename="thumb.png",
        storage_path="fileman/rendition/thumb.png",
        content_type="image/png",
        category="image",
        upload_status=FileRendition.COMPLETED,
    )

    try:
        url = rendition.get_direct_download_url()
        manager.refresh_from_db()

        assert_true("/signed/" in url,
                    f"a private rendition must resolve to a presigned URL, got {url}")
        assert_eq(manager.is_public, False,
                  "rendition resolution should repair a conclusive private manager")
        assert_eq(backend.audit_calls, [rendition.storage_path],
                  f"rendition resolution should probe its exact key, got {backend.audit_calls}")
    finally:
        rendition.delete()
        file_obj.delete()
        manager.delete()
        user.delete()


@th.django_unit_test("FileManager: unknown evidence presigns, preserves is_public, and is cached")
def test_unknown_evidence_is_fail_closed_and_cached(opts):
    from mojo.apps.account.models import User
    from mojo.apps.fileman.models import FileManager, File

    username = "fm_public_audit_unknown_user"
    User.objects.filter(username=username).delete()
    user = User.objects.create(username=username, email=f"{username}@example.com")
    manager = FileManager.objects.create(
        name="fm_public_audit_unknown",
        backend_type="s3",
        backend_url="s3://audit-test-bucket/fileman/unknown",
        user=user,
        is_active=True,
        is_public=True,
    )
    backend = _UnknownS3Backend()
    manager._backend = backend
    file_obj = File.objects.create(
        filename="unknown.png",
        storage_file_path="fileman/unknown/unknown.png",
        content_type="image/png",
        file_size=10,
        file_manager=manager,
        user=user,
    )

    try:
        first = file_obj.get_direct_download_url()
        second = file_obj.get_direct_download_url()
        manager.refresh_from_db()

        assert_true("/signed/" in first and "/signed/" in second,
                    f"unknown access must fail closed to presigned URLs, got {first} and {second}")
        assert_eq(manager.is_public, True,
                  "unknown evidence must preserve the operator's stored is_public value")
        assert_eq(manager.public_access_audit["status"], "unknown",
                  f"unknown evidence should be persisted, got {manager.public_access_audit}")
        assert_eq(backend.audit_calls, [file_obj.storage_file_path],
                  f"current unknown evidence should suppress repeat AWS calls, got {backend.audit_calls}")
    finally:
        file_obj.delete()
        manager.delete()
        user.delete()


@th.django_unit_test("FileManager: a backend configuration change invalidates cached evidence")
def test_config_fingerprint_invalidates_cached_audit(opts):
    from mojo.apps.account.models import User
    from mojo.apps.fileman.models import FileManager

    username = "fm_public_audit_fingerprint_user"
    User.objects.filter(username=username).delete()
    user = User.objects.create(username=username, email=f"{username}@example.com")
    manager = FileManager.objects.create(
        name="fm_public_audit_fingerprint",
        backend_type="s3",
        backend_url="s3://audit-test-bucket/fileman/one",
        user=user,
        is_active=True,
        is_public=True,
    )
    first_backend = _PrivateS3Backend()
    second_backend = _PrivateS3Backend()

    try:
        with patch("mojo.apps.fileman.backends.get_backend",
                   side_effect=[first_backend, second_backend]) as backend_factory:
            manager.ensure_public_access_audited(file_path="fileman/one/a.png")
            manager.ensure_public_access_audited(file_path="fileman/one/a.png")
            manager.backend_url = "s3://audit-test-bucket/fileman/two"
            manager.save(update_fields=["backend_url", "modified"])
            manager.ensure_public_access_audited(file_path="fileman/two/a.png")

        assert_eq(first_backend.audit_calls, ["fileman/one/a.png"],
                  f"the original backend should only audit its original prefix, got {first_backend.audit_calls}")
        assert_eq(second_backend.audit_calls, ["fileman/two/a.png"],
                  f"the changed URL must rebuild the backend before auditing, got {second_backend.audit_calls}")
        assert_eq(backend_factory.call_count, 2,
                  f"the backend should be constructed once per configuration, got {backend_factory.call_count}")
    finally:
        manager.delete()
        user.delete()


@th.django_unit_test("S3 audit: whole-prefix policy is public; partial, conditional, and deny are not")
def test_s3_policy_evidence_is_conservative(opts):
    parent_allow = {
        "Effect": "Allow",
        "Principal": "*",
        "Action": "s3:GetObject",
        "Resource": "arn:aws:s3:::audit-test-bucket/fileman/*",
    }
    backend = _policy_backend([parent_allow])
    with patch.object(backend, "_get_account_public_access_block", return_value={}):
        ok, issues, details = backend.check_public_access_for_prefix()
    assert_true(ok, f"an unconditional parent-prefix allow should be public: {issues}")
    assert_eq(details["status"], "public", f"expected public details, got {details}")

    sibling = dict(parent_allow, Resource="arn:aws:s3:::audit-test-bucket/site_media/*")
    backend = _policy_backend([sibling])
    with patch.object(backend, "_get_account_public_access_block", return_value={}):
        ok, _, details = backend.check_public_access_for_prefix()
    assert_true(not ok and details["status"] == "private",
                f"a sibling prefix must not establish public access, got {details}")

    partial = dict(parent_allow, Resource="arn:aws:s3:::audit-test-bucket/fileman/user-123/avatars/*")
    backend = _policy_backend([partial])
    with patch.object(backend, "_get_account_public_access_block", return_value={}):
        ok, _, details = backend.check_public_access_for_prefix()
    assert_true(not ok and details["status"] == "unknown",
                f"a public sub-prefix cannot establish whole-prefix access, got {details}")

    conditional = dict(parent_allow, Condition={"StringEquals": {"aws:Referer": "example"}})
    backend = _policy_backend([conditional])
    with patch.object(backend, "_get_account_public_access_block", return_value={}):
        ok, _, details = backend.check_public_access_for_prefix()
    assert_true(not ok and details["status"] == "unknown",
                f"a conditional allow must remain unknown, got {details}")

    deny = dict(parent_allow, Effect="Deny")
    backend = _policy_backend([parent_allow, deny])
    with patch.object(backend, "_get_account_public_access_block", return_value={}):
        ok, _, details = backend.check_public_access_for_prefix()
    assert_true(not ok and details["status"] == "unknown",
                f"a potentially overriding deny must prevent public classification, got {details}")

    not_action_deny = {
        "Effect": "Deny",
        "Principal": "*",
        "NotAction": "s3:DeleteObject",
        "Resource": "arn:aws:s3:::audit-test-bucket/fileman/*",
    }
    backend = _policy_backend([parent_allow, not_action_deny])
    with patch.object(backend, "_get_account_public_access_block", return_value={}):
        ok, _, details = backend.check_public_access_for_prefix()
    assert_true(not ok and details["status"] == "unknown",
                f"a NotAction deny that includes GetObject must prevent public classification, got {details}")

    wildcard_deny = dict(parent_allow, Effect="Deny", Action="S3:gEt*")
    backend = _policy_backend([parent_allow, wildcard_deny])
    with patch.object(backend, "_get_account_public_access_block", return_value={}):
        ok, _, details = backend.check_public_access_for_prefix()
    assert_true(not ok and details["status"] == "unknown",
                f"a mixed-case wildcard deny must prevent public classification, got {details}")


@th.django_unit_test("S3 audit: restrictive or unreadable controls never classify public")
def test_s3_access_blocks_and_unreadable_policy_are_safe(opts):
    allow = {
        "Effect": "Allow",
        "Principal": "*",
        "Action": "s3:GetObject",
        "Resource": "arn:aws:s3:::audit-test-bucket/fileman/*",
    }
    backend = _policy_backend([allow], bucket_pab={"RestrictPublicBuckets": True})
    with patch.object(backend, "_get_account_public_access_block", return_value={}):
        ok, _, details = backend.check_public_access_for_prefix()
    assert_true(not ok and details["status"] == "private",
                f"restrictive bucket PAB must be effectively private, got {details}")

    backend = _policy_backend([allow], bucket_pab={"BlockPublicPolicy": True})
    with patch.object(backend, "_get_account_public_access_block", return_value={}):
        ok, _, details = backend.check_public_access_for_prefix()
    assert_true(ok and details["status"] == "public",
                f"BlockPublicPolicy alone does not disable an existing public policy, got {details}")

    backend = _policy_backend([allow])
    with patch.object(backend, "_get_account_public_access_block",
                      return_value={"BlockPublicPolicy": True}):
        ok, _, details = backend.check_public_access_for_prefix()
    assert_true(ok and details["status"] == "public",
                f"account BlockPublicPolicy alone must not rewrite current access, got {details}")

    backend = _policy_backend([allow])
    with patch.object(backend, "_get_account_public_access_block",
                      side_effect=_client_error("AccessDenied")):
        ok, _, details = backend.check_public_access_for_prefix()
    assert_true(not ok and details["status"] == "unknown",
                f"unreadable account PAB must remain unknown, got {details}")

    backend = _policy_backend([], policy_error=_client_error("AccessDenied"))
    ok, _, details = backend.check_public_access_for_prefix()
    assert_true(not ok and details["status"] == "unknown",
                f"unreadable bucket policy must remain unknown, got {details}")


@th.django_unit_test("S3 audit: a confirmed existing object's anonymous 403 is private")
def test_s3_existing_object_403_is_private(opts):
    backend = _policy_backend([])
    response = type("Response", (), {"status_code": 403})()
    with patch("mojo.apps.fileman.backends.s3.requests.head", return_value=response):
        ok, _, details = backend.check_public_access_for_prefix("fileman/user-123/avatar.png")
    assert_true(not ok and details["status"] == "private",
                f"authenticated existence plus anonymous 403 should be private, got {details}")

    backend._client.head_error = _client_error("NoSuchKey")
    ok, _, details = backend.check_public_access_for_prefix("fileman/user-123/missing.png")
    assert_true(not ok and details["status"] == "unknown",
                f"a missing/stale object must remain unknown, got {details}")


@th.django_unit_test("S3 audit: public object requires whole-prefix policy evidence")
def test_s3_public_object_requires_prefix_policy(opts):
    allow = {
        "Effect": "Allow",
        "Principal": "*",
        "Action": "s3:GetObject",
        "Resource": "arn:aws:s3:::audit-test-bucket/fileman/*",
    }
    response = type("Response", (), {"status_code": 200})()
    backend = _policy_backend([allow])
    with patch("mojo.apps.fileman.backends.s3.requests.head", return_value=response), \
            patch.object(backend, "_get_account_public_access_block", return_value={}):
        ok, _, details = backend.check_public_access_for_prefix("fileman/user-123/avatar.png")
    assert_true(ok and details["status"] == "public",
                f"object and whole-prefix evidence together should be public, got {details}")

    partial = dict(allow, Resource="arn:aws:s3:::audit-test-bucket/fileman/user-123/avatar.png")
    backend = _policy_backend([partial])
    with patch("mojo.apps.fileman.backends.s3.requests.head", return_value=response), \
            patch.object(backend, "_get_account_public_access_block", return_value={}):
        ok, _, details = backend.check_public_access_for_prefix("fileman/user-123/avatar.png")
    assert_true(not ok and details["status"] == "unknown",
                f"one public object must not establish manager-wide public access, got {details}")


@th.django_unit_test("S3 URLs: anonymous probes use the configured endpoint and addressing style")
def test_s3_public_url_uses_configured_endpoint(opts):
    backend = _policy_backend([])
    backend.endpoint_url = "https://minio.example.test:9000/storage"
    backend.addressing_style = "path"

    url = backend.get_url("fileman/user-123/My avatar.png")

    assert_eq(
        url,
        "https://minio.example.test:9000/storage/audit-test-bucket/fileman/user-123/My%20avatar.png",
        f"custom S3 public URLs must use the configured endpoint, got {url}",
    )


@th.django_unit_test("FileManager command: dry-run is non-mutating and reports every manager")
def test_reconcile_command_dry_run(opts):
    from django.core.management import call_command
    from mojo.apps.account.models import User
    from mojo.apps.fileman.models import FileManager

    username = "fm_public_audit_command_user"
    User.objects.filter(username=username).delete()
    user = User.objects.create(username=username, email=f"{username}@example.com")
    manager = FileManager.objects.create(
        name="fm_public_audit_command",
        backend_type="s3",
        backend_url="s3://audit-test-bucket/fileman/command",
        user=user,
        is_active=True,
        is_public=True,
    )
    backend = _UnknownS3Backend()
    output = io.StringIO()

    try:
        with patch("mojo.apps.fileman.backends.get_backend", return_value=backend):
            call_command("reconcile_fileman_public_access", "--dry-run", stdout=output)
        manager.refresh_from_db()

        assert_true(f"FileManager {manager.pk} ({manager.name}): unknown" in output.getvalue(),
                    f"dry-run should report this manager as unknown, got {output.getvalue()}")
        assert_true(manager.public_access_audit is None,
                    f"dry-run must not persist audit metadata, got {manager.public_access_audit}")
        assert_eq(manager.is_public, True, "dry-run must not mutate is_public")

        persisted_output = io.StringIO()
        with patch("mojo.apps.fileman.backends.get_backend", return_value=_PrivateS3Backend()):
            call_command("reconcile_fileman_public_access", stdout=persisted_output)
        manager.refresh_from_db()
        assert_eq(manager.public_access_audit["status"], "private",
                  f"forced reconciliation should retry and persist private evidence, got {manager.public_access_audit}")
        assert_eq(manager.is_public, False,
                  "forced reconciliation should repair a conclusive private manager")
    finally:
        manager.delete()
        user.delete()


@th.django_unit_test("FileManager command: one manager failure does not abort later managers")
def test_reconcile_command_isolates_failures(opts):
    from django.core.management import call_command
    from mojo.apps.account.models import User
    from mojo.apps.fileman.models import FileManager

    users = []
    managers = []
    for suffix in ("bad", "good"):
        username = f"fm_public_audit_command_{suffix}_user"
        User.objects.filter(username=username).delete()
        user = User.objects.create(username=username, email=f"{username}@example.com")
        users.append(user)
        managers.append(FileManager.objects.create(
            name=f"fm_public_audit_command_{suffix}",
            backend_type="s3",
            backend_url=f"s3://audit-test-bucket/fileman/{suffix}",
            user=user,
            is_active=True,
            is_public=True,
        ))

    original_audit = FileManager.audit_is_public

    def _audit_with_one_failure(manager, *args, **kwargs):
        if manager.name.endswith("_bad"):
            raise RuntimeError("simulated manager failure")
        return original_audit(manager, *args, **kwargs)

    output = io.StringIO()
    errors = io.StringIO()
    try:
        with patch.object(FileManager, "audit_is_public", autospec=True,
                          side_effect=_audit_with_one_failure), \
                patch("mojo.apps.fileman.backends.get_backend", return_value=_PrivateS3Backend()):
            call_command("reconcile_fileman_public_access", stdout=output, stderr=errors)

        assert_true("simulated manager failure" in errors.getvalue(),
                    f"the failing manager should be reported, got {errors.getvalue()}")
        assert_true(f"FileManager {managers[1].pk} ({managers[1].name}): private" in output.getvalue(),
                    f"the command should continue to the good manager, got {output.getvalue()}")
    finally:
        for manager in managers:
            manager.delete()
        for user in users:
            user.delete()


@th.django_unit_test("FileManager: a policy mutation immediately re-audits S3 access")
def test_policy_mutation_reaudits(opts):
    from mojo.apps.account.models import User
    from mojo.apps.fileman.models import FileManager

    username = "fm_public_audit_mutation_user"
    User.objects.filter(username=username).delete()
    user = User.objects.create(username=username, email=f"{username}@example.com")
    manager = FileManager.objects.create(
        name="fm_public_audit_mutation",
        backend_type="s3",
        backend_url="s3://audit-test-bucket/fileman/mutation",
        user=user,
        is_active=True,
        is_public=False,
    )
    backend = _PublicS3Backend()
    manager._backend = backend

    try:
        manager.is_public = True
        manager.on_rest_saved(["is_public"], created=False)
        manager.refresh_from_db()

        assert_eq(backend.policy_mutations, ["public"],
                  f"the public policy mutation should be applied once, got {backend.policy_mutations}")
        assert_eq(backend.audit_calls, [None],
                  f"the policy mutation should force one immediate policy audit, got {backend.audit_calls}")
        assert_eq(manager.public_access_audit["status"], "public",
                  f"the refreshed public evidence should be persisted, got {manager.public_access_audit}")
    finally:
        manager.delete()
        user.delete()
