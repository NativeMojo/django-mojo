"""Admin-configurable rendition options (maestro #7728).

Three JSON Setting keys override the renderers' class defaults; the merge
happens in BaseRenderer's instance accessors; write-time validators refuse
anything the renderer would not honor; a read-only endpoint describes roles,
defaults, caps and choices for an admin UI.

Parallel-safety: this package is scanned in strict isolation mode. Every
Setting write here is a GROUP-scoped row on a test-owned group with a literal
key, never a global row and never a /api/settings write; validator
rejections are proven in-process through the same save() hook REST runs.
"""
import json
import os
import tempfile

from testit import helpers as th
from testit.helpers import assert_eq, assert_true

ADMIN_USER = "fileman_rcfg_admin"
PLAIN_USER = "fileman_rcfg_noperm"
PASSWORD = "rcfg##mojo99"
GROUP_NAME = "fileman_rcfg_group"


def _clear_group_rows(group):
    from mojo.apps.account.models import Setting
    Setting.remove("FILEMAN_RENDITIONS_IMAGE", group=group)
    Setting.remove("FILEMAN_RENDITIONS_VIDEO", group=group)
    Setting.remove("FILEMAN_RENDITIONS_DOCUMENT", group=group)


def _user(username, perms):
    from mojo.apps.account.models import User
    user = User.objects.filter(username=username).first()
    if user is None:
        user = User(username=username, email="%s@example.com" % username)
        user.save()
    user.is_email_verified = True
    user.permissions = {}
    user.save_password(PASSWORD)
    if perms:
        user.add_permission(perms)
    user.save()
    return user


@th.django_unit_setup()
def setup_rendition_config(opts):
    from mojo.apps.account.models import Group
    from mojo.apps.fileman.models import File, FileManager
    from mojo.decorators.limits import clear_rate_limits
    clear_rate_limits(ip="127.0.0.1")

    # Long-lived database: remove leftovers before creating anything.
    for group in Group.objects.filter(name=GROUP_NAME):
        _clear_group_rows(group)
    File.objects.filter(filename__startswith="rcfg-").delete()
    FileManager.objects.filter(name__startswith="rcfg_").delete()
    Group.objects.filter(name=GROUP_NAME).delete()

    opts.admin = _user(ADMIN_USER, ["manage_settings"])
    opts.plain = _user(PLAIN_USER, None)
    opts.group = Group.objects.create(name=GROUP_NAME)
    opts.tmpdir = tempfile.mkdtemp(prefix="rcfg-")

    grouped = FileManager.objects.create(
        name="rcfg_grouped", backend_type="file",
        backend_url="file://" + opts.tmpdir, group=opts.group, is_active=True)
    ungrouped = FileManager.objects.create(
        name="rcfg_ungrouped", backend_type="file",
        backend_url="file://" + opts.tmpdir, is_active=True)

    def make_file(manager, group, name, content_type, category):
        # No bytes on disk: a renderer's download fails before any processing,
        # which is exactly what these tests want (options only, no rendering).
        return File.objects.create(
            file_manager=manager, group=group, filename=name,
            storage_filename=name,
            storage_file_path=os.path.join(opts.tmpdir, name),
            content_type=content_type, category=category,
            upload_token="rcfg-" + name, upload_status=File.COMPLETED)

    opts.grouped_image_id = make_file(grouped, opts.group, "rcfg-grouped.png", "image/png", "image").pk
    opts.plain_image_id = make_file(ungrouped, None, "rcfg-plain.png", "image/png", "image").pk


# ---------------------------------------------------------------------------
# Merge semantics (pure)
# ---------------------------------------------------------------------------

@th.unit_test("Rendition config: merge keeps defaults unless a role overrides them")
def test_merge_semantics(opts):
    from mojo.apps.fileman.renderer import config

    defaults = {"thumbnail": {"width": 150, "height": 150, "mode": "contain"},
                "square_sm": {"width": 100, "height": 100, "mode": "crop"}}
    automatic_default = ("thumbnail", "square_sm")

    options, automatic = config.merge(defaults, automatic_default, {})
    assert_eq(options, defaults, "an empty override must reproduce the class defaults")
    assert_eq(automatic, automatic_default, "an empty override must keep the automatic list")

    options, automatic = config.merge(
        defaults, automatic_default, {"thumbnail": {"width": 99}, "_automatic": ["square_sm"]})
    assert_eq(options["thumbnail"], {"width": 99, "height": 150, "mode": "contain"},
              "a partial override must replace only the keys it names")
    assert_eq(options["square_sm"], defaults["square_sm"],
              "an untouched role must keep its defaults")
    assert_eq(automatic, ("square_sm",), "_automatic must replace the automatic list")
    assert_true(defaults["thumbnail"]["width"] == 150,
                "merge must never mutate the class defaults it was given")

    options, automatic = config.merge(defaults, automatic_default, {"_automatic": []})
    assert_eq(automatic, (), "an empty _automatic list means nothing runs on upload")


@th.unit_test("Rendition config: video automatic roles default to #4909's opt-in set")
def test_video_automatic_default_unchanged(opts):
    from mojo.apps.fileman.renderer.base import RenditionRole
    from mojo.apps.fileman.renderer.video import VideoRenderer

    assert_eq(
        set(VideoRenderer.default_automatic_roles()),
        {RenditionRole.VIDEO_THUMBNAIL, RenditionRole.THUMBNAIL, RenditionRole.VIDEO_PREVIEW},
        "full transcodes (mp4/webm/hevc) must stay opt-in by default",
    )
    assert_eq(VideoRenderer.default_renditions[RenditionRole.VIDEO_HEVC]["codec"], "h265",
              "video_hevc must be declared as an H.265 role")


# ---------------------------------------------------------------------------
# Instance resolution through a group-scoped row
# ---------------------------------------------------------------------------

@th.django_unit_test("Rendition config: a group row overrides options for that group's files only")
def test_group_override_resolution(opts):
    from mojo.apps.account.models import Setting
    from mojo.apps.fileman.models import File
    from mojo.apps.fileman.renderer.image import ImageRenderer

    grouped = File.objects.get(pk=opts.grouped_image_id)
    plain = File.objects.get(pk=opts.plain_image_id)
    try:
        Setting.set("FILEMAN_RENDITIONS_IMAGE",
                    {"thumbnail": {"width": 99}, "_automatic": ["thumbnail"]},
                    group=opts.group)

        renderer = ImageRenderer(grouped)
        options = renderer.get_rendition_options("thumbnail")
        assert_eq(options["width"], 99, "the group row must override the thumbnail width")
        assert_eq(options["height"], 150, "keys the row does not name must keep their default")
        assert_eq(renderer.get_automatic_rendition_roles(), ("thumbnail",),
                  "the group row must decide which roles run on upload")

        untouched = ImageRenderer(plain)
        assert_eq(untouched.get_rendition_options("thumbnail")["width"], 150,
                  "a file outside the group must not see the group's override")
        assert_eq(set(untouched.get_automatic_rendition_roles()),
                  set(ImageRenderer.default_automatic_roles()),
                  "a file outside the group must keep the default automatic roles")

        raised = False
        try:
            renderer.get_rendition_options("video_mp4")
        except ValueError:
            raised = True
        assert_true(raised, "an undeclared role must still raise from the accessor")
    finally:
        Setting.remove("FILEMAN_RENDITIONS_IMAGE", group=opts.group)


@th.django_unit_test("Rendition config: create_rendition never mutates the class defaults")
def test_create_rendition_does_not_mutate_class_defaults(opts):
    from mojo.apps.fileman.models import File
    from mojo.apps.fileman.renderer.image import ImageRenderer

    before = dict(ImageRenderer.default_renditions["thumbnail"])
    renderer = ImageRenderer(File.objects.get(pk=opts.plain_image_id))
    # No bytes on disk: the download fails and the call returns None, but the
    # option merge has already happened by then.
    result = renderer.create_rendition("thumbnail", {"width": 1})
    assert_eq(result, None, "a file with no bytes must not produce a rendition")
    assert_eq(ImageRenderer.default_renditions["thumbnail"], before,
              "caller options must merge into a copy, never into the class dict")
    assert_eq(renderer.create_rendition("video_mp4"), None,
              "an undeclared role must return None rather than raise")


# ---------------------------------------------------------------------------
# Write-time validation (same hook REST runs: Setting.save -> _validate_value)
# ---------------------------------------------------------------------------

def _refused(key, value, group):
    from mojo import errors as merrors
    from mojo.apps.account.models import Setting
    try:
        Setting(key=key, value=json.dumps(value), group=group).save()
    except merrors.ValueException as exc:
        return str(exc)
    return None


@th.django_unit_test("Rendition config: validators refuse what the renderer would not honor")
def test_validators_refuse_bad_values(opts):
    group = opts.group
    cases = [
        ("FILEMAN_RENDITIONS_IMAGE", {"nope": {"width": 10}}, "nope: unknown role"),
        ("FILEMAN_RENDITIONS_IMAGE", {"thumbnail": {"widht": 10}}, "thumbnail.widht: unknown option"),
        ("FILEMAN_RENDITIONS_IMAGE", {"thumbnail": {"width": -5}}, "thumbnail.width: must be between"),
        ("FILEMAN_RENDITIONS_IMAGE", {"thumbnail": {"width": 8192}}, "thumbnail.width: must be between"),
        ("FILEMAN_RENDITIONS_IMAGE", {"thumbnail": {"mode": "fill"}}, "thumbnail.mode: must be one of"),
        ("FILEMAN_RENDITIONS_IMAGE", {"_automatic": ["thumbnail", "ghost"]}, "_automatic: unknown role ghost"),
        ("FILEMAN_RENDITIONS_IMAGE", ["thumbnail"], "must be a JSON object"),
        ("FILEMAN_RENDITIONS_VIDEO", {"video_webm": {"codec": "h265"}}, "video_webm.codec: only valid when format is mp4"),
        ("FILEMAN_RENDITIONS_VIDEO", {"video_hevc": {"crf": 99}}, "video_hevc.crf: must be between 18 and 51"),
        ("FILEMAN_RENDITIONS_VIDEO", {"video_hevc": {"preset": "veryslow"}}, "video_hevc.preset: must be one of"),
        ("FILEMAN_RENDITIONS_VIDEO", {"video_mp4": {"bitrate": "fast"}}, "video_mp4.bitrate: must look like"),
        ("FILEMAN_RENDITIONS_VIDEO", {"thumbnail": {"time_offset": "3s"}}, "thumbnail.time_offset: must look like"),
        ("FILEMAN_RENDITIONS_VIDEO", {"thumbnail": {"codec": "h265"}}, "thumbnail.codec: unknown option"),
        ("FILEMAN_RENDITIONS_DOCUMENT", {"document_preview": {"max_pages": 0}}, "document_preview.max_pages: must be between"),
        ("FILEMAN_RENDITIONS_DOCUMENT", {"document_preview": {"quality": 90}}, "document_preview.quality: must be one of"),
    ]
    for key, value, expected in cases:
        message = _refused(key, value, group)
        assert_true(message is not None, "%s %r must be refused" % (key, value))
        assert_true(expected in message,
                    "%s %r: refusal must name the field (%r), got %r" % (key, value, expected, message))


@th.django_unit_test("Rendition config: format wins over codec, and valid rows persist")
def test_validators_accept_valid_rows(opts):
    from mojo.apps.account.models import Setting
    try:
        # video_mp4 carries codec: h264 in its class default; switching the
        # format to webm must still be accepted (the codec is simply ignored).
        assert_eq(_refused("FILEMAN_RENDITIONS_VIDEO", {"video_mp4": {"format": "webm"}}, opts.group),
                  None, "changing a role's format to webm must not be refused for its default codec")
        # The accepted row persisted; clear it before the next valid write.
        Setting.remove("FILEMAN_RENDITIONS_VIDEO", group=opts.group)
        assert_eq(_refused("FILEMAN_RENDITIONS_VIDEO",
                           {"video_mp4": {"codec": "h265", "crf": 24, "preset": "fast"},
                            "_automatic": ["thumbnail", "video_hevc"]}, opts.group),
                  None, "a complete, in-range h265 override must be accepted")
        assert_eq(_refused("FILEMAN_RENDITIONS_IMAGE", {}, opts.group), None,
                  "an empty object means defaults and must be accepted")
    finally:
        _clear_group_rows(opts.group)


@th.django_unit_test("Rendition config: the video engine key accepts only ffmpeg")
def test_engine_validator(opts):
    from mojo import errors as merrors
    from mojo.apps.account.models import Setting
    refused = False
    try:
        # Global-only key: validated before any row is written, so nothing
        # persists and no global state is touched.
        Setting(key="FILEMAN_VIDEO_ENGINE", value=json.dumps("mediaconvert")).save()
    except merrors.ValueException as exc:
        refused = True
        assert_true("ffmpeg" in str(exc), "the refusal must name the accepted engine")
    assert_true(refused, "an unknown video engine must be refused")


# ---------------------------------------------------------------------------
# ffmpeg argv (pure builders)
# ---------------------------------------------------------------------------

@th.unit_test("Rendition config: h265 transcodes are CRF-driven and tagged for Apple players")
def test_h265_transcode_args(opts):
    from mojo.apps.fileman.renderer import video

    hevc = video.build_transcode_args("in.mov", "out.mp4", {
        "width": 1280, "height": 720, "format": "mp4", "codec": "h265",
        "crf": 28, "preset": "medium", "audio": True})
    assert_true("libx265" in hevc, "h265 must encode with libx265")
    assert_true("hvc1" in hevc, "h265 in mp4 must carry the hvc1 tag")
    assert_true("-crf" in hevc and "28" in hevc, "h265 must be CRF-driven")
    assert_true("-b:v" not in hevc, "h265 must not mix a bitrate target with CRF")
    assert_true("aac" in hevc, "mp4 audio must stay AAC")

    h264 = video.build_transcode_args("in.mov", "out.mp4", {
        "width": 1280, "height": 720, "bitrate": "2000k", "format": "mp4",
        "codec": "h264", "audio": True})
    assert_true("libx264" in h264 and "-b:v" in h264 and "2000k" in h264,
                "h264 must keep today's bitrate-driven argv")

    webm = video.build_transcode_args("in.mov", "out.webm", {
        "width": 1280, "height": 720, "bitrate": "2000k", "format": "webm",
        "codec": "h265", "audio": False})
    assert_true("libvpx" in webm and "libx265" not in webm,
                "format wins: webm is VP8 regardless of codec")
    assert_true("-an" in webm, "audio=false must drop the audio track")


@th.unit_test("Rendition config: video scaling keeps aspect ratio and never upscales")
def test_scale_filter(opts):
    from mojo.apps.fileman.renderer import video

    args = video.build_transcode_args("in.mov", "out.mp4", {"width": 640, "height": 360})
    vf = args[args.index("-vf") + 1]
    assert_true("force_original_aspect_ratio=decrease" in vf,
                "the transcode filter must fit a bounding box, not squash")
    assert_true("min(iw,640)" in vf and "min(ih,360)" in vf,
                "the transcode filter must never upscale a smaller source")
    assert_true("trunc(iw/2)*2" in vf, "x264/x265 need even dimensions")

    thumb = video.build_thumbnail_args("in.mov", "out.jpg", {
        "width": 300, "height": 169, "time_offset": "00:00:03"})
    assert_true("-s" not in thumb, "thumbnails must not use the squashing -s WxH form")
    assert_true("force_original_aspect_ratio=decrease" in thumb[thumb.index("-vf") + 1],
                "thumbnails must keep aspect ratio")
    assert_true("-vframes" in thumb and "00:00:03" in thumb,
                "thumbnails must still extract one frame at the offset")


# ---------------------------------------------------------------------------
# Options endpoint
# ---------------------------------------------------------------------------

@th.django_unit_test("Rendition config: describe() reports DB overrides only, defaults always")
def test_describe_shape(opts):
    from mojo.apps.fileman.renderer import config

    report = config.describe()
    video = report["categories"]["video"]
    assert_eq(video["key"], "FILEMAN_RENDITIONS_VIDEO", "each category must name its setting key")
    assert_eq(video["defaults"]["video_hevc"]["codec"], "h265",
              "defaults must come from the renderer's class dict")
    assert_true("video_hevc" not in video["automatic_default"],
                "automatic_default must mirror the renderer")
    assert_eq(video["role_kinds"]["thumbnail"], "thumbnail", "roles must be classified by kind")
    assert_eq(video["choices"]["transcode"]["codec"], ["h264", "h265"],
              "enum choices must be published per role kind")
    assert_true("codec" not in video["choices"]["thumbnail"],
                "thumbnail roles must not advertise transcode options")
    assert_eq(video["limits"]["crf"], [18, 51], "numeric caps must be published")
    assert_eq(report["engine"]["effective"], "ffmpeg", "ffmpeg is the only engine today")
    image = report["categories"]["image"]
    assert_eq(image["effective"]["roles"]["thumbnail"]["width"], 150,
              "effective options must equal the defaults when nothing is set")


@th.django_unit_test("Rendition config: the options endpoint is readable by settings admins only")
def test_options_endpoint(opts):
    opts.client.login(ADMIN_USER, PASSWORD)
    resp = opts.client.get("/api/fileman/renditions/options")
    assert_eq(resp.status_code, 200, "manage_settings must read the options: %r" % resp.response)
    data = resp.json.get("data") or {}
    assert_eq(data["categories"]["video"]["defaults"]["video_hevc"]["codec"], "h265",
              "the endpoint must expose the renderer defaults")
    assert_true(set(data["categories"]) == {"image", "video", "document"},
                "all three categories must be described")
    opts.client.logout()

    opts.client.login(PLAIN_USER, PASSWORD)
    resp = opts.client.get("/api/fileman/renditions/options")
    assert_eq(resp.status_code, 403, "a user without any files/settings permission must be refused")
    opts.client.logout()


# ---------------------------------------------------------------------------
# Cleanup
# ---------------------------------------------------------------------------

@th.django_unit_setup()
def cleanup_rendition_config(opts):
    import shutil
    from mojo.apps.account.models import Group, User
    from mojo.apps.fileman.models import File, FileManager

    _clear_group_rows(opts.group)
    File.objects.filter(filename__startswith="rcfg-").delete()
    FileManager.objects.filter(name__startswith="rcfg_").delete()
    Group.objects.filter(name=GROUP_NAME).delete()
    User.objects.filter(username__in=[ADMIN_USER, PLAIN_USER]).delete()
    shutil.rmtree(opts.tmpdir, ignore_errors=True)
