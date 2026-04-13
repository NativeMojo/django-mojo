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
