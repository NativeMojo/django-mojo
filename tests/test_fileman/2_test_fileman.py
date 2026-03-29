"""
Tests for the fileman File and FileManager models and REST endpoints.
"""
import os
import tempfile
from testit import helpers as th
from testit.helpers import assert_true, assert_eq

TEST_USER = "fileman_test_user"
TEST_PWORD = "fileman##mojo99"


def _write_dummy_file(tmpdir, storage_file_path):
    """Write a dummy file so backend.exists() returns True for mark_as_completed."""
    full_path = os.path.join(tmpdir, storage_file_path.lstrip('/'))
    os.makedirs(os.path.dirname(full_path), exist_ok=True)
    with open(full_path, 'w') as fh:
        fh.write("test")


@th.django_unit_setup()
def setup_fileman(opts):
    from mojo.apps.account.models import User
    from mojo.apps.fileman.models import FileManager, File
    from mojo.decorators.limits import clear_rate_limits
    clear_rate_limits(ip="127.0.0.1")

    user = User.objects.filter(username=TEST_USER).last()
    if user is None:
        user = User(username=TEST_USER, email=f"{TEST_USER}@example.com")
        user.save()
    user.is_email_verified = True
    user.save_password(TEST_PWORD)
    user.add_permission(["view_fileman", "manage_files"])
    user.save()
    opts.user = user

    # temp dir for local-backend FileManager
    tmpdir = tempfile.mkdtemp(prefix="mojo_fileman_test_")
    opts.tmpdir = tmpdir

    fm = FileManager.objects.filter(name="test_fileman_fm", user=user).first()
    if fm is None:
        fm = FileManager(
            name="test_fileman_fm",
            backend_type="file",
            # empty root_path so storage_file_path = just the filename
            backend_url="file://",
            user=user,
            is_active=True,
            is_default=True,
        )
        fm.save()
    else:
        fm.backend_url = "file://"
        fm.is_active = True
        fm.save()
    # Tell the filesystem backend to use our tmpdir
    fm.set_setting("base_path", tmpdir)
    fm.save(update_fields=["mojo_secrets", "modified"])
    opts.fm_id = fm.pk

    File.objects.filter(user=user).delete()


# ---------------------------------------------------------------------------
# Unit: File status transitions
# ---------------------------------------------------------------------------

@th.django_unit_test("File: pending → uploading status transition")
def test_file_status_uploading(opts):
    from mojo.apps.fileman.models import FileManager, File

    fm = FileManager.objects.get(pk=opts.fm_id)
    f = File(filename="test.txt", content_type="text/plain", file_size=100, file_manager=fm, user=opts.user)
    f.save()
    assert_eq(f.upload_status, File.PENDING, "new file should be PENDING")

    f.mark_as_uploading()
    assert_eq(f.upload_status, File.UPLOADING, "should be UPLOADING after mark_as_uploading")
    opts.file_id = f.pk


@th.django_unit_test("File: uploading → completed when file exists on backend")
def test_file_status_completed(opts):
    from mojo.apps.fileman.models import FileManager, File

    fm = FileManager.objects.get(pk=opts.fm_id)
    f = File(filename="complete.txt", content_type="text/plain", file_size=4, file_manager=fm, user=opts.user)
    f.generate_storage_filename()
    f.save()

    # Write a real file so backend.exists() returns True
    _write_dummy_file(opts.tmpdir, f.storage_file_path)

    f.mark_as_completed(commit=True)
    f.refresh_from_db()
    assert_eq(f.upload_status, File.COMPLETED, "should be COMPLETED when file exists on backend")
    assert_true(f.is_completed, "is_completed should be True")


@th.django_unit_test("File: mark_as_failed sets status")
def test_file_mark_failed(opts):
    from mojo.apps.fileman.models import FileManager, File

    fm = FileManager.objects.get(pk=opts.fm_id)
    f = File(filename="fail.txt", content_type="text/plain", file_size=10, file_manager=fm, user=opts.user)
    f.save()
    f.mark_as_failed(error_message="test error", commit=True)
    f.refresh_from_db()
    assert_eq(f.upload_status, File.FAILED, "should be FAILED")
    assert_true(f.is_failed, "is_failed should be True")


@th.django_unit_test("File: mark_as_completed sets FAILED when file missing from backend")
def test_file_completed_no_file(opts):
    from mojo.apps.fileman.models import FileManager, File

    fm = FileManager.objects.get(pk=opts.fm_id)
    f = File(filename="ghost.txt", content_type="text/plain", file_size=10, file_manager=fm, user=opts.user)
    f.save()
    f.mark_as_completed(commit=True)
    assert_eq(f.upload_status, File.FAILED, "should FAIL if no actual file on backend")


@th.django_unit_test("File: generate_upload_token produces 32-char hex string")
def test_file_upload_token(opts):
    from mojo.apps.fileman.models import FileManager, File

    fm = FileManager.objects.get(pk=opts.fm_id)
    f = File(filename="tok.txt", content_type="text/plain", file_size=10, file_manager=fm, user=opts.user)
    f.generate_upload_token()
    assert_true(f.upload_token is not None, "upload_token should be set")
    assert_eq(len(f.upload_token), 32, f"upload_token should be 32 chars, got {len(f.upload_token)}")


@th.django_unit_test("File: metadata get/set")
def test_file_metadata(opts):
    from mojo.apps.fileman.models import FileManager, File

    fm = FileManager.objects.get(pk=opts.fm_id)
    f = File(filename="meta.txt", content_type="text/plain", file_size=10, file_manager=fm, user=opts.user)
    f.save()
    f.set_metadata("source", "test")
    f.set_metadata("width", 1920)
    assert_eq(f.get_metadata("source"), "test", "metadata source should be test")
    assert_eq(f.get_metadata("width"), 1920, "metadata width should be 1920")
    assert_eq(f.get_metadata("missing", "default"), "default", "missing key should return default")


# ---------------------------------------------------------------------------
# Unit: FileManager
# ---------------------------------------------------------------------------

@th.django_unit_test("FileManager: get_from_request returns manager")
def test_fm_get_from_request(opts):
    from mojo.apps.fileman.models import FileManager
    from testit.helpers import get_mock_request

    request = get_mock_request()
    request.user = opts.user
    request.DATA = type("D", (), {"get": lambda s, k, d=None: None})()

    fm = FileManager.get_from_request(request)
    assert_true(fm is not None, "should return a FileManager")
    assert_eq(fm.pk, opts.fm_id, "should return the test FileManager")


@th.django_unit_test("FileManager: can_upload_file checks size limit")
def test_fm_can_upload_file(opts):
    from mojo.apps.fileman.models import FileManager

    fm = FileManager.objects.get(pk=opts.fm_id)
    ok, _ = fm.can_upload_file("test.txt", 100)
    assert_true(ok, "small file should be allowed")

    ok, msg = fm.can_upload_file("huge.bin", fm.max_file_size + 1)
    assert_true(not ok, f"file over max_file_size should be rejected: {msg}")


@th.django_unit_test("FileManager: is_file_system property")
def test_fm_backend_type_property(opts):
    from mojo.apps.fileman.models import FileManager

    fm = FileManager.objects.get(pk=opts.fm_id)
    assert_true(fm.is_file_system, "local backend should report is_file_system=True")
    assert_true(not fm.is_s3, "local backend should report is_s3=False")


# ---------------------------------------------------------------------------
# REST: upload initiate
# ---------------------------------------------------------------------------

@th.django_unit_test("REST: upload initiate returns upload_url")
def test_rest_upload_initiate(opts):
    opts.client.login(TEST_USER, TEST_PWORD)
    assert_true(opts.client.is_authenticated, "login should succeed")

    resp = opts.client.post("/api/fileman/upload/initiate", {
        "filename": "test_upload.txt",
        "content_type": "text/plain",
        "file_size": 4,
        "file_manager": opts.fm_id,
    })
    assert_eq(resp.status_code, 200, f"initiate should return 200, got {resp.status_code} {resp.response}")
    data = resp.response.data
    assert_true(data.id is not None, "should return file id")
    assert_true(data.upload_url is not None, "should return upload_url")
    opts.initiated_file_id = data.id


@th.django_unit_test("REST: mark_as_completed action on file with real content")
def test_rest_mark_completed(opts):
    from mojo.apps.fileman.models import File

    f = File.objects.get(pk=opts.initiated_file_id)
    _write_dummy_file(opts.tmpdir, f.storage_file_path)

    opts.client.login(TEST_USER, TEST_PWORD)
    resp = opts.client.post(f"/api/fileman/file/{opts.initiated_file_id}", {"action": "mark_as_completed"})
    assert_eq(resp.status_code, 200, f"mark_as_completed should return 200, got {resp.status_code}")

    f.refresh_from_db()
    assert_eq(f.upload_status, File.COMPLETED, "file should be COMPLETED after action")


# ---------------------------------------------------------------------------
# REST: file list and get
# ---------------------------------------------------------------------------

@th.django_unit_test("REST: authenticated user can list own files")
def test_rest_file_list(opts):
    opts.client.login(TEST_USER, TEST_PWORD)
    resp = opts.client.get("/api/fileman/file")
    assert_eq(resp.status_code, 200, f"file list should return 200, got {resp.status_code}")


@th.django_unit_test("REST: get file by id returns correct graph")
def test_rest_file_get(opts):
    opts.client.login(TEST_USER, TEST_PWORD)
    resp = opts.client.get(f"/api/fileman/file/{opts.initiated_file_id}")
    assert_eq(resp.status_code, 200, f"file get should return 200, got {resp.status_code}")
    data = resp.response.data
    assert_eq(data.id, opts.initiated_file_id, "should return correct file id")
    assert_true(data.filename is not None, "should include filename")


@th.django_unit_test("REST: unauthenticated cannot list files")
def test_rest_file_list_unauth(opts):
    from mojo.models.rest import MOJO_REST_LIST_PERM_DENY
    opts.client.logout()
    resp = opts.client.get("/api/fileman/file")
    if MOJO_REST_LIST_PERM_DENY:
        assert_true(resp.status_code in [401, 403], f"unauthenticated should be denied, got {resp.status_code}")
    else:
        assert_true(resp.status_code in [200, 401, 403], f"expected empty or denied, got {resp.status_code}")
        if resp.status_code == 200:
            assert_eq(resp.response.count, 0, "unauthenticated should get empty list")


# ---------------------------------------------------------------------------
# REST: file delete
# ---------------------------------------------------------------------------

@th.django_unit_test("REST: delete file removes DB record")
def test_rest_file_delete(opts):
    from mojo.apps.fileman.models import File

    opts.client.login(TEST_USER, TEST_PWORD)
    resp = opts.client.delete(f"/api/fileman/file/{opts.initiated_file_id}")
    assert_eq(resp.status_code, 200, f"delete should return 200, got {resp.status_code}")
    assert_true(not File.objects.filter(pk=opts.initiated_file_id).exists(), "file row should be deleted")


# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------

@th.django_unit_setup()
def cleanup_fileman(opts):
    import shutil
    from mojo.apps.fileman.models import FileManager, File

    File.objects.filter(user=opts.user).delete()
    FileManager.objects.filter(pk=opts.fm_id).delete()

    if os.path.exists(opts.tmpdir):
        shutil.rmtree(opts.tmpdir, ignore_errors=True)
