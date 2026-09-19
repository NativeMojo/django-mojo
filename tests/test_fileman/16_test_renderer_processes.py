"""Regression coverage for bounded fileman renderer processes."""

import os
import sys
import tempfile
import time
from unittest import mock

from testit import helpers as th
from testit.helpers import assert_eq, assert_true


@th.django_unit_setup()
def setup_renderer_processes(opts):
    from mojo.apps.fileman.models import File, FileManager

    File.objects.filter(filename="renderer-process-regression.mp4").delete()
    FileManager.objects.filter(name="renderer_process_regression").delete()

    opts.tmpdir = tempfile.mkdtemp(prefix="renderer-process-")
    manager = FileManager.objects.create(
        name="renderer_process_regression",
        backend_type="file",
        backend_url="file://" + opts.tmpdir,
        is_active=True,
    )
    source = os.path.join(opts.tmpdir, "renderer-process-regression.mp4")
    with open(source, "wb") as fh:
        fh.write(b"not-a-real-video")
    original = File.objects.create(
        file_manager=manager,
        filename="renderer-process-regression.mp4",
        storage_filename="renderer-process-regression.mp4",
        storage_file_path=source,
        content_type="video/mp4",
        category="video",
        upload_token="renderer-process-regression",
        upload_status=File.COMPLETED,
    )
    opts.renderer_file_id = original.pk


@th.unit_test("Video uploads automatically build only thumbnails and the short preview")
def test_video_automatic_roles_exclude_full_transcodes(opts):
    from mojo.apps.fileman.renderer.base import RenditionRole
    from mojo.apps.fileman.renderer.video import VideoRenderer

    automatic = set(VideoRenderer.get_automatic_rendition_roles())
    assert_eq(
        automatic,
        {RenditionRole.VIDEO_THUMBNAIL, RenditionRole.THUMBNAIL,
         RenditionRole.VIDEO_PREVIEW},
        "automatic video renditions must keep thumbnails and the short preview only",
    )
    assert_true(
        RenditionRole.VIDEO_MP4 in VideoRenderer.default_renditions,
        "MP4 must remain available through an explicit role request",
    )
    assert_true(
        RenditionRole.VIDEO_WEBM in VideoRenderer.default_renditions,
        "WebM must remain available through an explicit role request",
    )


@th.unit_test("Renderer timeout kills and reaps converter descendants")
def test_renderer_timeout_reaps_process_group(opts):
    from mojo.apps.fileman.renderer.process import RendererProcessTimeout, run

    fd, pid_path = tempfile.mkstemp(prefix="renderer-child-", suffix=".pid")
    os.close(fd)
    os.unlink(pid_path)
    script = (
        "import pathlib, subprocess, sys, time; "
        "child=subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); "
        "pathlib.Path(sys.argv[1]).write_text(str(child.pid)); "
        "time.sleep(60)"
    )
    try:
        timed_out = False
        try:
            run([sys.executable, "-c", script, pid_path], timeout=0.25)
        except RendererProcessTimeout as exc:
            timed_out = True
            assert_true("python" in str(exc).lower(),
                        "timeout diagnostic must name the converter without its arguments")
            assert_true(pid_path not in str(exc),
                        "timeout diagnostic must not expose input or output paths")
        assert_true(timed_out, "the renderer runner must enforce its deadline")
        assert_true(os.path.exists(pid_path), "the child must publish its pid before timeout")
        child_pid = int(open(pid_path).read())
        deadline = time.time() + 2
        alive = True
        while time.time() < deadline:
            try:
                os.kill(child_pid, 0)
            except ProcessLookupError:
                alive = False
                break
            time.sleep(0.02)
        assert_true(not alive, "the timed-out converter descendant must be reaped")
    finally:
        if os.path.exists(pid_path):
            os.unlink(pid_path)

    fd, pid_path = tempfile.mkstemp(prefix="renderer-detached-child-", suffix=".pid")
    os.close(fd)
    os.unlink(pid_path)
    script = (
        "import pathlib, subprocess, sys; "
        "child=subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); "
        "pathlib.Path(sys.argv[1]).write_text(str(child.pid))"
    )
    try:
        started = time.time()
        run([sys.executable, "-c", script, pid_path], timeout=1)
        assert_true(time.time() - started < 1,
                    "a converter that exits must not leave the runner waiting on descendant pipes")
        child_pid = int(open(pid_path).read())
        deadline = time.time() + 2
        while time.time() < deadline:
            try:
                os.kill(child_pid, 0)
            except ProcessLookupError:
                break
            time.sleep(0.02)
        else:
            assert_true(False, "a descendant must not survive its converter leader")
    finally:
        if os.path.exists(pid_path):
            os.unlink(pid_path)


@th.django_unit_test("Renderer process failure persists a bounded failed row and fails the job")
def test_renderer_failure_is_persisted_and_propagated(opts):
    from mojo.apps.fileman import asyncjobs
    from mojo.apps.fileman.models import File, FileRendition
    from mojo.apps.fileman.renderer.base import BaseRenderer
    from mojo.apps.fileman.renderer.process import RendererProcessFailed

    original = File.objects.get(pk=opts.renderer_file_id)
    FileRendition.objects.filter(original_file=original, role="video_preview").delete()

    class FailingRenderer(BaseRenderer):
        default_renditions = {"video_preview": {}}

        def create_rendition(self, role, options=None):
            self.record_failure(
                role,
                RendererProcessFailed("ffmpeg failed with exit status 9"),
            )
            return None

    renderer = FailingRenderer(original)
    job = type("Job", (), {"payload": {"file_id": original.pk}})()

    failed = False
    with mock.patch(
        "mojo.apps.fileman.renderer.get_renderer_for_file", return_value=renderer
    ):
        try:
            asyncjobs.process_file_renditions(job)
        except Exception as exc:
            failed = True
            assert_true("video_preview" in str(exc),
                        "job failure must identify the failed rendition role")
    assert_true(failed, "a converter failure must not report a completed rendition job")

    row = FileRendition.objects.get(original_file=original, role="video_preview")
    assert_eq(row.upload_status, FileRendition.FAILED,
              "the refused rendition must have a failed row")
    assert_true("ffmpeg failed" in row.error_message,
                "the failed row must carry a bounded operator diagnostic")
    assert_true(len(row.error_message) <= 200,
                "the persisted rendition diagnostic must remain bounded")
    assert_eq(row.url, None, "failed renditions must not expose a download URL")
    assert_true(
        "error_message" in FileRendition.RestMeta.NO_SAVE_FIELDS,
        "renderer failure diagnostics must not be client-writable",
    )
    refused_share = False
    try:
        row.on_action_share(True)
    except ValueError:
        refused_share = True
    assert_true(refused_share, "failed renditions must not mint share links")


@th.unit_test("Media renderers do not bypass the bounded process runner")
def test_media_renderers_have_no_raw_subprocess_run(opts):
    import inspect
    from mojo.apps.fileman.renderer import audio, document, utils, video

    for module in (audio, document, utils, video):
        source = inspect.getsource(module)
        assert_true(
            "subprocess.run(" not in source,
            "%s must use renderer.process.run instead of raw subprocess.run"
            % module.__name__,
        )


@th.django_unit_setup()
def cleanup_renderer_processes(opts):
    import shutil
    from mojo.apps.fileman.models import File, FileManager

    File.objects.filter(filename="renderer-process-regression.mp4").delete()
    FileManager.objects.filter(name="renderer_process_regression").delete()
    shutil.rmtree(opts.tmpdir, ignore_errors=True)
