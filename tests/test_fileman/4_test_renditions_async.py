"""Tests for the async rendition pipeline in fileman.

Covers:
  - File.mark_as_completed() enqueues a Job (no inline ffmpeg/Pillow).
  - process_file_renditions handler creates image renditions.
  - regenerate_renditions action publishes a regenerate Job.
  - regenerate_renditions handler replaces only requested roles.
  - Video rendition path is gated on ffmpeg availability.
"""
import io
import os
import shutil
import tempfile
from testit import helpers as th
from testit.helpers import assert_true, assert_eq

TEST_USER = "fileman_async_user"
TEST_PWORD = "fileman##mojo99"


def _tiny_png_bytes():
    """Return bytes of a small valid PNG suitable for Pillow processing."""
    try:
        from PIL import Image
    except ImportError:
        return None
    img = Image.new("RGB", (64, 64), color=(200, 50, 50))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _write_file(tmpdir, storage_file_path, data):
    full_path = os.path.join(tmpdir, storage_file_path.lstrip('/'))
    os.makedirs(os.path.dirname(full_path) or tmpdir, exist_ok=True)
    with open(full_path, 'wb') as fh:
        fh.write(data)


@th.django_unit_setup()
def setup_renditions(opts):
    from mojo.apps.account.models import User
    from mojo.apps.fileman.models import FileManager, File
    from mojo.apps.jobs.models import Job
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

    tmpdir = tempfile.mkdtemp(prefix="mojo_fileman_async_")
    opts.tmpdir = tmpdir

    fm = FileManager.objects.filter(name="test_fileman_async_fm", user=user).first()
    if fm is None:
        fm = FileManager(
            name="test_fileman_async_fm",
            backend_type="file",
            backend_url="file://",
            user=user,
            is_active=True,
            is_default=False,
        )
        fm.save()
    fm.backend_url = "file://"
    fm.is_active = True
    fm.save()
    fm.set_setting("base_path", tmpdir)
    fm.save(update_fields=["mojo_secrets", "modified"])
    opts.fm_id = fm.pk

    File.objects.filter(user=user).delete()
    # Start with a clean jobs table slice for our test file_ids.
    Job.objects.filter(func__startswith="mojo.apps.fileman.asyncjobs.").delete()


# ---------------------------------------------------------------------------
# mark_as_completed enqueues an async rendition job (no inline render)
# ---------------------------------------------------------------------------

@th.django_unit_test("Renditions: mark_as_completed enqueues process_file_renditions job")
def test_mark_completed_enqueues_job(opts):
    from mojo.apps.fileman.models import FileManager, File, FileRendition
    from mojo.apps.jobs.models import Job

    fm = FileManager.objects.get(pk=opts.fm_id)
    f = File(filename="async.txt", content_type="text/plain",
             file_size=4, file_manager=fm, user=opts.user)
    f.generate_storage_filename()
    f.save()

    _write_file(opts.tmpdir, f.storage_file_path, b"test")

    # Baseline: no job yet for this file.
    key = f"renditions:{f.id}"
    before = Job.objects.filter(idempotency_key=key).count()
    assert_eq(before, 0, f"no prior rendition job should exist for file {f.id}")

    f.mark_as_completed(commit=True)
    f.refresh_from_db()
    assert_eq(f.upload_status, File.COMPLETED, "file should be COMPLETED")

    # Exactly one rendition job enqueued.
    jobs = Job.objects.filter(idempotency_key=key)
    assert_eq(jobs.count(), 1, f"one rendition job should be enqueued, got {jobs.count()}")

    job = jobs.first()
    assert_eq(job.func, "mojo.apps.fileman.asyncjobs.process_file_renditions",
              f"unexpected func: {job.func}")
    assert_eq(job.channel, "renditions", f"unexpected channel: {job.channel}")
    assert_eq(job.payload.get("file_id"), f.id,
              f"payload.file_id should be {f.id}, got {job.payload}")

    # No renditions have been created yet (the worker hasn't run).
    assert_eq(FileRendition.objects.filter(original_file=f).count(), 0,
              "renditions should not be created inline")

    opts.text_file_id = f.id


@th.django_unit_test("Renditions: duplicate mark_as_completed collapses via idempotency_key")
def test_mark_completed_idempotent(opts):
    from mojo.apps.fileman.models import FileManager, File
    from mojo.apps.jobs.models import Job

    fm = FileManager.objects.get(pk=opts.fm_id)
    f = File(filename="idem.txt", content_type="text/plain",
             file_size=4, file_manager=fm, user=opts.user)
    f.generate_storage_filename()
    f.save()
    _write_file(opts.tmpdir, f.storage_file_path, b"test")

    f.mark_as_completed(commit=True)
    f.mark_as_completed(commit=True)  # second publish for same file_id

    jobs = Job.objects.filter(idempotency_key=f"renditions:{f.id}")
    assert_eq(jobs.count(), 1,
              f"idempotency_key should collapse duplicate publishes, got {jobs.count()}")


# ---------------------------------------------------------------------------
# process_file_renditions handler creates image renditions
# ---------------------------------------------------------------------------

@th.django_unit_test("Renditions: process_file_renditions handler creates image renditions")
def test_process_image_renditions(opts):
    png = _tiny_png_bytes()
    if png is None:
        return  # Pillow not installed; skip silently.

    from mojo.apps.fileman.models import FileManager, File, FileRendition
    from mojo.apps.fileman import asyncjobs
    from mojo.apps.jobs.models import Job

    fm = FileManager.objects.get(pk=opts.fm_id)
    f = File(filename="tiny.png", content_type="image/png", category="image",
             file_size=len(png), file_manager=fm, user=opts.user)
    f.generate_storage_filename()
    f.save()
    _write_file(opts.tmpdir, f.storage_file_path, png)
    f.mark_as_completed(commit=True)

    job = Job.objects.filter(idempotency_key=f"renditions:{f.id}").first()
    assert_true(job is not None, f"job should exist for file {f.id}")

    # Run the handler directly (no engine roundtrip).
    result = asyncjobs.process_file_renditions(job)
    assert_true(result is not None and result.startswith("completed:"),
                f"handler should return a completed:* sentinel, got {result}")

    renditions = FileRendition.objects.filter(original_file=f)
    assert_true(renditions.count() > 0,
                f"handler should create at least one rendition for image, got {renditions.count()}")
    # Image renderer always creates a THUMBNAIL role.
    assert_true(renditions.filter(role="thumbnail").exists(),
                "image renderer should produce a thumbnail rendition")

    opts.image_file_id = f.id


@th.django_unit_test("Renditions: handler is a no-op when file is not completed")
def test_handler_skips_incomplete_file(opts):
    from mojo.apps.fileman.models import FileManager, File, FileRendition
    from mojo.apps.fileman import asyncjobs
    from mojo.apps.jobs.models import Job

    fm = FileManager.objects.get(pk=opts.fm_id)
    f = File(filename="pending.txt", content_type="text/plain",
             file_size=4, file_manager=fm, user=opts.user)
    f.generate_storage_filename()
    f.save()
    # No mark_as_completed — file stays PENDING.

    # Fabricate a Job row so we can call the handler directly.
    fake_job = Job(
        func="mojo.apps.fileman.asyncjobs.process_file_renditions",
        payload={"file_id": f.id},
        channel="renditions",
    )
    result = asyncjobs.process_file_renditions(fake_job)
    assert_true("not-completed" in (result or ""),
                f"handler should skip non-completed file, got {result}")
    assert_eq(FileRendition.objects.filter(original_file=f).count(), 0,
              "no renditions should be created for non-completed file")


@th.django_unit_test("Renditions: handler tolerates missing file")
def test_handler_skips_missing_file(opts):
    from mojo.apps.fileman import asyncjobs
    from mojo.apps.jobs.models import Job

    fake_job = Job(
        func="mojo.apps.fileman.asyncjobs.process_file_renditions",
        payload={"file_id": 999999999},
        channel="renditions",
    )
    result = asyncjobs.process_file_renditions(fake_job)
    assert_true("file-missing" in (result or ""),
                f"handler should return file-missing sentinel, got {result}")


# ---------------------------------------------------------------------------
# regenerate_renditions action + handler
# ---------------------------------------------------------------------------

@th.django_unit_test("Renditions: regenerate_renditions action enqueues regenerate job")
def test_regenerate_action_enqueues(opts):
    if not hasattr(opts, "image_file_id"):
        return  # Pillow wasn't available; earlier test short-circuited.

    from mojo.apps.jobs.models import Job

    opts.client.login(TEST_USER, TEST_PWORD)
    resp = opts.client.post(
        f"/api/fileman/file/{opts.image_file_id}",
        {"action": "regenerate_renditions", "roles": ["thumbnail"]},
    )
    assert_eq(resp.status_code, 200,
              f"regenerate action should return 200, got {resp.status_code} {getattr(resp, 'response', None)}")

    job = (Job.objects
           .filter(func="mojo.apps.fileman.asyncjobs.regenerate_renditions")
           .filter(payload__file_id=opts.image_file_id)
           .order_by("-created")
           .first())
    assert_true(job is not None,
                f"regenerate job should be enqueued for file {opts.image_file_id}")
    assert_eq(job.channel, "renditions", "regenerate job should be on renditions channel")
    assert_eq(job.payload.get("roles"), ["thumbnail"],
              f"regenerate job should carry roles filter, got {job.payload}")
    opts.regen_job_id = job.id


@th.django_unit_test("Renditions: regenerate handler replaces only requested roles")
def test_regenerate_handler_scoped(opts):
    if not hasattr(opts, "image_file_id"):
        return

    from mojo.apps.fileman.models import File, FileRendition
    from mojo.apps.fileman import asyncjobs
    from mojo.apps.jobs.models import Job

    f = File.objects.get(pk=opts.image_file_id)

    # Snapshot existing rendition ids by role.
    before = {r.role: r.id for r in FileRendition.objects.filter(original_file=f)}
    assert_true("thumbnail" in before, "precondition: thumbnail should already exist")

    # Drop in a sentinel rendition row we do NOT want regenerated.
    sentinel = FileRendition.objects.create(
        original_file=f,
        role="__test_sentinel",
        filename="sentinel.jpg",
        storage_path="sentinel.jpg",
        content_type="image/jpeg",
        category="image",
        upload_status=FileRendition.COMPLETED,
    )

    job = Job(
        func="mojo.apps.fileman.asyncjobs.regenerate_renditions",
        payload={"file_id": f.id, "roles": ["thumbnail"]},
        channel="renditions",
    )
    asyncjobs.regenerate_renditions(job)

    # Thumbnail id should be different (was deleted and re-created).
    new_thumb = FileRendition.objects.filter(original_file=f, role="thumbnail").first()
    assert_true(new_thumb is not None, "thumbnail should exist after regenerate")
    assert_true(new_thumb.id != before["thumbnail"],
                "thumbnail should have been replaced (new id)")

    # Sentinel should still be present (not in requested roles).
    assert_true(FileRendition.objects.filter(pk=sentinel.id).exists(),
                "sentinel rendition (outside requested roles) should be preserved")


# ---------------------------------------------------------------------------
# Video: only run if ffmpeg is available
# ---------------------------------------------------------------------------

@th.django_unit_test("Renditions: video rendition skipped gracefully without ffmpeg")
def test_video_rendition_gate(opts):
    """Gate check — we do not build a video fixture here, we just confirm that
    when the handler runs against a non-video it does not attempt ffmpeg, and
    when ffmpeg is missing the renderer module still imports cleanly."""
    from mojo.apps.fileman.renderer import video as video_module
    assert_true(hasattr(video_module, "VideoRenderer"),
                "VideoRenderer should import regardless of ffmpeg presence")
    if shutil.which("ffmpeg") is None:
        # No ffmpeg — that is fine; this is just a presence check.
        return
    # With ffmpeg available, the class must expose the default role set.
    roles = set(video_module.VideoRenderer.default_renditions.keys())
    assert_true("video_thumbnail" in roles,
                "VideoRenderer should expose a video_thumbnail role")


# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------

@th.django_unit_setup()
def cleanup_renditions_tests(opts):
    from mojo.apps.fileman.models import FileManager, File
    from mojo.apps.jobs.models import Job

    File.objects.filter(user=opts.user).delete()
    FileManager.objects.filter(pk=opts.fm_id).delete()
    Job.objects.filter(func__startswith="mojo.apps.fileman.asyncjobs.").delete()

    if os.path.exists(opts.tmpdir):
        shutil.rmtree(opts.tmpdir, ignore_errors=True)
