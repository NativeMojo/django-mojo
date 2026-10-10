"""Tests for the expired file cleanup job."""
from testit import helpers as th


@th.django_unit_setup()
@th.requires_app("mojo.apps.fileman")
def setup_cleanup(opts):
    from datetime import timedelta
    from django.utils import timezone
    from mojo.apps.account.models import User
    from mojo.apps.fileman.models import FileManager, File

    # Clean up test data
    User.objects.filter(email="cleanuptest@test.com").delete()
    opts.user = User.objects.create_user(
        username="cleanuptest@test.com", email="cleanuptest@test.com", password="pass123",
    )

    FileManager.objects.filter(name="cleanuptest_fm").delete()
    opts.fm = FileManager.objects.create(
        name="cleanuptest_fm",
        backend_type="file",
        backend_url="filesystem:///tmp/cleanuptest_files",
        is_default=True,
        is_active=True,
        user=opts.user,
    )

    # Clean up previous test files
    File.objects.filter(filename__startswith="cleanuptest_").delete()

    now = timezone.now()

    # Create an expired file (expires_at in the past)
    opts.expired_file = File.objects.create(
        filename="cleanuptest_expired.csv",
        file_manager=opts.fm,
        user=opts.user,
        content_type="text/csv",
        category="csv",
        file_size=100,
        upload_status="completed",
        storage_file_path="/tmp/cleanuptest_files/cleanuptest_expired.csv",
        storage_filename="cleanuptest_expired.csv",
        upload_token="tok_expired",
        metadata={
            "source": "assistant_export",
            "expires_at": (now - timedelta(days=1)).isoformat(),
        },
    )

    # Create a non-expired file (expires_at in the future)
    opts.active_file = File.objects.create(
        filename="cleanuptest_active.csv",
        file_manager=opts.fm,
        user=opts.user,
        content_type="text/csv",
        category="csv",
        file_size=200,
        upload_status="completed",
        storage_file_path="/tmp/cleanuptest_files/cleanuptest_active.csv",
        storage_filename="cleanuptest_active.csv",
        upload_token="tok_active",
        metadata={
            "source": "assistant_export",
            "expires_at": (now + timedelta(days=14)).isoformat(),
        },
    )

    # Create a file with no expires_at (should not be touched)
    opts.no_expiry_file = File.objects.create(
        filename="cleanuptest_noexpiry.csv",
        file_manager=opts.fm,
        user=opts.user,
        content_type="text/csv",
        category="csv",
        file_size=300,
        upload_status="completed",
        storage_file_path="/tmp/cleanuptest_files/cleanuptest_noexpiry.csv",
        storage_filename="cleanuptest_noexpiry.csv",
        upload_token="tok_noexpiry",
        metadata={"source": "manual_upload"},
    )


@th.django_unit_test()
def test_cleanup_deletes_expired_files(opts):
    from mojo.apps.fileman.models import File

    # Run the cleanup job function directly with a mock job
    from mojo.apps.fileman.asyncjobs import cleanup_expired_files
    from objict import objict
    mock_job = objict(payload={})

    result = cleanup_expired_files(mock_job)
    assert "deleted=1" in result, f"Expected 1 deletion, got: {result}"

    # Expired file should be gone
    assert not File.objects.filter(pk=opts.expired_file.pk).exists(), \
        "Expired file should have been deleted"


@th.django_unit_test()
def test_cleanup_preserves_active_files(opts):
    from mojo.apps.fileman.models import File

    # Active file should still exist
    assert File.objects.filter(pk=opts.active_file.pk).exists(), \
        "Active (non-expired) file should still exist"


@th.django_unit_test()
def test_cleanup_preserves_no_expiry_files(opts):
    from mojo.apps.fileman.models import File

    # File with no expires_at should still exist
    assert File.objects.filter(pk=opts.no_expiry_file.pk).exists(), \
        "File with no expires_at should still exist"


def _make_file(opts, name, expires_at):
    """A completed file with the given raw metadata.expires_at value (#7387)."""
    from mojo.apps.fileman.models import File

    return File.objects.create(
        filename=f"cleanuptest_{name}.csv",
        file_manager=opts.fm,
        user=opts.user,
        content_type="text/csv",
        category="csv",
        file_size=100,
        upload_status="completed",
        storage_file_path=f"/tmp/cleanuptest_files/cleanuptest_{name}.csv",
        storage_filename=f"cleanuptest_{name}.csv",
        upload_token=f"tok_{name}",
        metadata={"source": "cleanuptest", "expires_at": expires_at},
    )


def _run_cleanup():
    from mojo.apps.fileman.asyncjobs import cleanup_expired_files
    from objict import objict

    return cleanup_expired_files(objict(payload={}))


def _naive_iso(days):
    """An ISO timestamp with no timezone, `days` from now in UTC."""
    from datetime import timedelta
    from django.utils import timezone

    return (timezone.now() + timedelta(days=days)).replace(tzinfo=None).isoformat()


@th.django_unit_test()
def test_cleanup_deletes_naive_past_expiry(opts):
    """#7387: an expiry with no timezone is read as UTC; in the past, the file goes."""
    from mojo.apps.fileman.models import File

    f = _make_file(opts, "naive_past", _naive_iso(-1))
    try:
        _run_cleanup()
        assert not File.objects.filter(pk=f.pk).exists(), \
            "A file whose timezone-less expiry is in the past should have been deleted"
    finally:
        File.objects.filter(pk=f.pk).delete()


@th.django_unit_test()
def test_cleanup_keeps_naive_future_expiry(opts):
    """#7387: an expiry with no timezone in the future is kept, and the run completes."""
    from mojo.apps.fileman.models import File

    f = _make_file(opts, "naive_future", _naive_iso(14))
    try:
        result = _run_cleanup()
        assert result.startswith("completed:"), f"Expected a completed run, got: {result}"
        assert File.objects.filter(pk=f.pk).exists(), \
            "A file whose timezone-less expiry is in the future should still exist"
    finally:
        File.objects.filter(pk=f.pk).delete()


@th.django_unit_test()
def test_cleanup_deletes_date_only_past_expiry(opts):
    """#7387: a date alone is read as UTC midnight; in the past, the file goes."""
    from mojo.apps.fileman.models import File

    f = _make_file(opts, "date_past", "2020-01-01")
    try:
        _run_cleanup()
        assert not File.objects.filter(pk=f.pk).exists(), \
            "A file whose date-only expiry is in the past should have been deleted"
    finally:
        File.objects.filter(pk=f.pk).delete()


@th.django_unit_test()
def test_cleanup_keeps_date_only_future_expiry(opts):
    """#7387: a date alone in the future is kept, and the run completes."""
    from mojo.apps.fileman.models import File

    f = _make_file(opts, "date_future", "2999-01-01")
    try:
        result = _run_cleanup()
        assert result.startswith("completed:"), f"Expected a completed run, got: {result}"
        assert File.objects.filter(pk=f.pk).exists(), \
            "A file whose date-only expiry is in the future should still exist"
    finally:
        File.objects.filter(pk=f.pk).delete()


@th.django_unit_test()
def test_cleanup_continues_past_naive_file(opts):
    """#7387: a timezone-less expiry does not end the run; the files after it are handled."""
    from datetime import timedelta
    from django.utils import timezone
    from mojo.apps.fileman.models import File

    naive = _make_file(opts, "mixed_naive", _naive_iso(-1))
    aware = _make_file(opts, "mixed_aware", (timezone.now() - timedelta(days=1)).isoformat())
    try:
        _run_cleanup()
        assert not File.objects.filter(pk=naive.pk).exists(), \
            "The file with a timezone-less past expiry should have been deleted"
        assert not File.objects.filter(pk=aware.pk).exists(), \
            "The properly expired file in the same run should have been deleted"
    finally:
        File.objects.filter(pk__in=[naive.pk, aware.pk]).delete()


@th.django_unit_test()
def test_cleanup_continues_when_one_file_raises(opts):
    """#7387: one file that raises is counted and reported; the other is still deleted."""
    from datetime import timedelta
    from django.utils import timezone
    from mojo.apps.fileman.models import FileManager, File

    # A manager whose backend type does not exist: deleting one of its files raises.
    FileManager.objects.filter(name="cleanuptest_broken_fm").delete()
    broken_fm = FileManager.objects.create(
        name="cleanuptest_broken_fm",
        backend_type="cleanuptest_nosuch",
        backend_url="cleanuptest_nosuch:///nowhere",
        is_active=True,
        user=opts.user,
    )

    past = (timezone.now() - timedelta(days=1)).isoformat()
    broken = _make_file(opts, "raises", past)
    File.objects.filter(pk=broken.pk).update(file_manager=broken_fm)
    good = _make_file(opts, "raises_other", past)

    try:
        result = _run_cleanup()
        assert File.objects.filter(pk=broken.pk).exists(), \
            "The file that raised should have been kept"
        assert not File.objects.filter(pk=good.pk).exists(), \
            "The other expired file should have been deleted"
        assert "failed=1" in result, f"Expected the result to report failed=1, got: {result}"
    finally:
        File.objects.filter(pk__in=[broken.pk, good.pk]).delete()
        FileManager.objects.filter(pk=broken_fm.pk).delete()


@th.django_unit_test()
def test_cleanup_skips_unreadable_expiry(opts):
    """#7387: free text and a number are skipped, and the run continues past them."""
    from datetime import timedelta
    from django.utils import timezone
    from mojo.apps.fileman.models import File

    text = _make_file(opts, "unreadable_text", "next friday")
    number = _make_file(opts, "unreadable_number", 12345)
    expired = _make_file(opts, "unreadable_other", (timezone.now() - timedelta(days=1)).isoformat())
    try:
        result = _run_cleanup()
        assert result.startswith("completed:"), f"Expected a completed run, got: {result}"
        assert "failed" not in result, f"An unreadable expiry is skipped, not a failure: {result}"
        assert File.objects.filter(pk=text.pk).exists(), \
            "A file with a free-text expiry should be skipped"
        assert File.objects.filter(pk=number.pk).exists(), \
            "A file with a numeric expiry should be skipped"
        assert not File.objects.filter(pk=expired.pk).exists(), \
            "The expired file in the same run should have been deleted"
    finally:
        File.objects.filter(pk__in=[text.pk, number.pk, expired.pk]).delete()
