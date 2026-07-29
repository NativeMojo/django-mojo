"""Tests for SVG rasterization in the fileman rendition pipeline.

Covers:
  - Happy path: an uploaded SVG produces PNG renditions, original untouched.
  - Role parity with raster images.
  - Security: XXE reads nothing and leaks no path, no network fetch, no JS.
  - Caps: entity bomb, canvas bomb, oversize input, embedded-raster bomb.
  - Degradation: every refusal leaves no rows and raises nothing.
  - The rasterizer runs once per file, not once per role.
  - regenerate_renditions dedupes duplicate roles.
"""
import base64
import io
import os
import socketserver
import http.server
import tempfile
import threading
from testit import helpers as th
from testit.helpers import assert_true, assert_eq

TEST_USER = "fileman_svg_user"
TEST_PWORD = "fileman##mojo99"

# A real SVG: 200x100, blue field with a yellow circle.
GOOD_SVG = (
    '<svg xmlns="http://www.w3.org/2000/svg" width="200" height="100">'
    '<rect width="200" height="100" fill="#3366ff"/>'
    '<circle cx="100" cy="50" r="30" fill="#ffcc00"/>'
    '</svg>'
).encode()

XXE_MARKER = "TOPSECRET_XXE_MARKER_VALUE"


def _tiny_png_bytes(size=(64, 64), color=(200, 50, 50)):
    from PIL import Image
    img = Image.new("RGB", size, color=color)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _write_file(tmpdir, storage_file_path, data):
    full_path = os.path.join(tmpdir, storage_file_path.lstrip('/'))
    os.makedirs(os.path.dirname(full_path) or tmpdir, exist_ok=True)
    with open(full_path, 'wb') as fh:
        fh.write(data)


def _read_rendition(opts, rendition):
    """Read a rendition's stored bytes off the local file backend."""
    full_path = os.path.join(opts.tmpdir, rendition.storage_path.lstrip('/'))
    with open(full_path, 'rb') as fh:
        return fh.read()


def _make_svg_file(opts, filename, data, content_type="image/svg+xml"):
    """Create a completed File row backed by `data` on the local backend."""
    from mojo.apps.fileman.models import FileManager, File

    fm = FileManager.objects.get(pk=opts.fm_id)
    f = File(filename=filename, content_type=content_type, category="image",
             file_size=len(data), file_manager=fm, user=opts.user)
    f.generate_storage_filename()
    f.save()
    _write_file(opts.tmpdir, f.storage_file_path, data)
    f.mark_as_completed(commit=True)
    return f


def _run_renditions(f):
    """Run the async handler for a file exactly as the worker would."""
    from mojo.apps.fileman import asyncjobs
    from mojo.apps.jobs.models import Job

    job = Job.objects.filter(idempotency_key=f"renditions:{f.id}").first()
    assert_true(job is not None, f"a rendition job should exist for file {f.id}")
    return asyncjobs.process_file_renditions(job)


@th.django_unit_setup()
def setup_svg_renditions(opts):
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

    tmpdir = tempfile.mkdtemp(prefix="mojo_fileman_svg_")
    opts.tmpdir = tmpdir

    fm = FileManager.objects.filter(name="test_fileman_svg_fm", user=user).first()
    if fm is None:
        fm = FileManager(
            name="test_fileman_svg_fm",
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

    # Long-lived database: clear our own rows before creating them.
    File.objects.filter(user=user).delete()
    Job.objects.filter(func__startswith="mojo.apps.fileman.asyncjobs.").delete()


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------

@th.django_unit_test("SVG: upload produces a PNG thumbnail, original preserved")
def test_svg_produces_png_thumbnail(opts):
    from PIL import Image
    from mojo.apps.fileman.models import File, FileRendition

    f = _make_svg_file(opts, "logo.svg", GOOD_SVG)
    result = _run_renditions(f)
    assert_true(result is not None and result.startswith("completed:"),
                f"handler should return a completed:* sentinel, got {result}")

    thumb = FileRendition.objects.filter(original_file=f, role="thumbnail").first()
    assert_true(thumb is not None, "SVG should produce a thumbnail rendition")
    assert_eq(thumb.content_type, "image/png",
              f"SVG thumbnail should be PNG, got {thumb.content_type}")
    assert_eq(thumb.category, "image",
              f"SVG thumbnail category should be image, got {thumb.category}")

    img = Image.open(io.BytesIO(_read_rendition(opts, thumb)))
    assert_eq(img.format, "PNG", f"stored rendition should decode as PNG, got {img.format}")
    assert_true(img.width <= 150 and img.height <= 150,
                f"thumbnail should fit in 150x150, got {img.size}")
    # 200x100 source -> 2:1 aspect preserved.
    assert_eq(img.width, 150, f"200x100 svg should scale to width 150, got {img.size}")
    assert_eq(img.height, 75, f"200x100 svg should scale to height 75, got {img.size}")

    # The original row and its bytes are untouched.
    f.refresh_from_db()
    assert_eq(f.content_type, "image/svg+xml",
              f"original content_type should stay svg, got {f.content_type}")
    original_path = os.path.join(opts.tmpdir, f.storage_file_path.lstrip('/'))
    with open(original_path, 'rb') as fh:
        assert_eq(fh.read(), GOOD_SVG, "the stored original SVG should be byte-identical")


@th.django_unit_test("SVG: rendition roles match what a raster image produces")
def test_svg_role_parity_with_raster(opts):
    from mojo.apps.fileman.models import FileRendition

    svg_file = _make_svg_file(opts, "parity.svg", GOOD_SVG)
    _run_renditions(svg_file)
    svg_roles = set(FileRendition.objects.filter(
        original_file=svg_file).values_list("role", flat=True))

    png_file = _make_svg_file(opts, "parity.png", _tiny_png_bytes(size=(200, 100)),
                              content_type="image/png")
    _run_renditions(png_file)
    png_roles = set(FileRendition.objects.filter(
        original_file=png_file).values_list("role", flat=True))

    assert_true(len(svg_roles) > 0, "SVG should produce at least one rendition role")
    assert_eq(svg_roles, png_roles,
              f"SVG roles {sorted(svg_roles)} should match raster roles {sorted(png_roles)}")


# ---------------------------------------------------------------------------
# Security
# ---------------------------------------------------------------------------

@th.django_unit_test("SVG: XXE reads nothing and leaks no path")
def test_svg_xxe_is_refused(opts):
    from mojo.apps.fileman.models import FileRendition
    from mojo.apps.fileman.renderer import svg_raster

    secret = tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False)
    secret.write(XXE_MARKER)
    secret.close()
    try:
        payload = (
            '<?xml version="1.0"?>'
            f'<!DOCTYPE svg [<!ENTITY xxe SYSTEM "file://{secret.name}">]>'
            '<svg xmlns="http://www.w3.org/2000/svg" width="300" height="60">'
            '<text x="5" y="30">&xxe;</text></svg>'
        ).encode()

        # The rasterizer refuses outright.
        raised = None
        try:
            svg_raster.rasterize(payload)
        except svg_raster.SvgRasterError as e:
            raised = e
        assert_true(raised is not None, "an external entity reference must be refused")
        assert_true(XXE_MARKER not in str(raised),
                    f"the refusal must not leak the entity contents: {raised}")
        assert_true(secret.name not in str(raised),
                    f"the refusal must not leak the target path: {raised}")
        assert_true(raised.not_svg is False,
                    "an XXE refusal is terminal and must not fall back to the raster path")

        # And through the pipeline: no rows, no exception.
        f = _make_svg_file(opts, "xxe.svg", payload)
        result = _run_renditions(f)
        assert_true(result is not None and result.startswith("completed:"),
                    f"handler should complete cleanly on a refused SVG, got {result}")
        assert_eq(FileRendition.objects.filter(original_file=f).count(), 0,
                  "an XXE SVG must produce no renditions")
    finally:
        os.unlink(secret.name)


@th.django_unit_test("SVG: external references are never fetched")
def test_svg_makes_no_network_calls(opts):
    from mojo.apps.fileman.renderer import svg_raster

    hits = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            hits.append(self.path)
            self.send_response(404)
            self.end_headers()

        def log_message(self, *args):
            pass

    server = socketserver.TCPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        payload = (
            '<svg xmlns="http://www.w3.org/2000/svg" '
            'xmlns:xlink="http://www.w3.org/1999/xlink" width="100" height="100">'
            f'<image href="http://127.0.0.1:{port}/probe.png" '
            f'xlink:href="http://127.0.0.1:{port}/probe.png" width="100" height="100"/>'
            '</svg>'
        ).encode()

        png = svg_raster.rasterize(payload)
        assert_true(len(png) > 0, "an SVG with an external image should still rasterize")
        assert_eq(len(hits), 0,
                  f"rasterizing must not fetch external references, saw {hits}")
    finally:
        server.shutdown()
        server.server_close()


@th.django_unit_test("SVG: script elements and event handlers never execute")
def test_svg_runs_no_javascript(opts):
    from mojo.apps.fileman.renderer import svg_raster

    hits = []

    class Handler(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            hits.append(self.path)
            self.send_response(404)
            self.end_headers()

        def log_message(self, *args):
            pass

    server = socketserver.TCPServer(("127.0.0.1", 0), Handler)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        payload = (
            '<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100" '
            'onload="fetch(\'http://127.0.0.1:%d/onload\')">'
            '<script>fetch("http://127.0.0.1:%d/script")</script>'
            '<rect width="100" height="100" fill="green"/></svg>' % (port, port)
        ).encode()

        png = svg_raster.rasterize(payload)
        assert_true(len(png) > 0, "an SVG carrying a script should still rasterize")
        assert_eq(len(hits), 0, f"no JavaScript may execute during render, saw {hits}")
    finally:
        server.shutdown()
        server.server_close()


# ---------------------------------------------------------------------------
# Caps
# ---------------------------------------------------------------------------

@th.django_unit_test("SVG: billion-laughs entity expansion is refused")
def test_svg_entity_bomb_is_refused(opts):
    from mojo.apps.fileman.models import FileRendition

    payload = (
        '<?xml version="1.0"?>'
        '<!DOCTYPE svg ['
        '<!ENTITY a "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaa">'
        '<!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;&a;&a;">'
        '<!ENTITY c "&b;&b;&b;&b;&b;&b;&b;&b;&b;&b;">'
        '<!ENTITY d "&c;&c;&c;&c;&c;&c;&c;&c;&c;&c;">'
        '<!ENTITY e "&d;&d;&d;&d;&d;&d;&d;&d;&d;&d;">'
        ']>'
        '<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100">'
        '<text x="0" y="20">&e;</text></svg>'
    ).encode()

    f = _make_svg_file(opts, "bomb.svg", payload)
    result = _run_renditions(f)
    assert_true(result is not None and result.startswith("completed:"),
                f"handler should complete cleanly on an entity bomb, got {result}")
    assert_eq(FileRendition.objects.filter(original_file=f).count(), 0,
              "an entity bomb must produce no renditions")


@th.django_unit_test("SVG: an absurd canvas is bounded by the raster box")
def test_svg_canvas_bomb_is_bounded(opts):
    import time
    from PIL import Image
    from mojo.apps.fileman.renderer import svg_raster

    payload = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="100000" height="100000">'
        '<rect width="100000" height="100000" fill="red"/></svg>'
    ).encode()

    started = time.time()
    png = svg_raster.rasterize(payload)
    elapsed = time.time() - started

    img = Image.open(io.BytesIO(png))
    box = svg_raster.DEFAULT_RASTER_BOX
    assert_true(img.width <= box and img.height <= box,
                f"a 100000px canvas must be capped at {box}px, got {img.size}")
    # Unbounded this input renders for roughly two minutes. If someone stops
    # passing BOTH width and height to resvg, this is what catches it.
    assert_true(elapsed < 30,
                f"a bounded canvas render should be fast, took {elapsed:.1f}s")


@th.django_unit_test("SVG: input over the byte cap is refused before rasterizing")
def test_svg_oversize_input_is_refused(opts):
    from mojo.apps.fileman.renderer import svg_raster

    padding = b"x" * (svg_raster.DEFAULT_MAX_BYTES + 1024)
    payload = (b'<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10">'
               b'<!--' + padding + b'--></svg>')

    raised = None
    try:
        svg_raster.rasterize(payload)
    except svg_raster.SvgRasterError as e:
        raised = e
    assert_true(raised is not None,
                f"an SVG over {svg_raster.DEFAULT_MAX_BYTES} bytes must be refused")
    assert_true(raised.not_svg is False,
                "an oversize refusal is terminal and must not fall back to the raster path")


@th.django_unit_test("SVG: an embedded raster bomb under the byte cap is refused")
def test_svg_embedded_raster_bomb_is_refused(opts):
    import time
    from PIL import Image
    from mojo.apps.fileman.models import FileRendition
    from mojo.apps.fileman.renderer import svg_raster

    # A solid 20000x20000 PNG compresses to ~1.2 MB, so the whole SVG lands
    # UNDER the 2 MB byte cap and renders in ~4s (under the timeout) at a 1024
    # output box — while decoding to multiple GB of pixels. The embedded-pixel
    # budget is the only cap that stops it, and it is the only one that works on
    # macOS, where RLIMIT_AS cannot be set at all.
    buf = io.BytesIO()
    Image.new("RGB", (20000, 20000), (10, 200, 30)).save(buf, format="PNG",
                                                         compress_level=9)
    encoded = base64.b64encode(buf.getvalue()).decode()
    payload = (
        '<svg xmlns="http://www.w3.org/2000/svg" width="200" height="200">'
        f'<image width="200" height="200" href="data:image/png;base64,{encoded}"/>'
        '</svg>'
    ).encode()
    assert_true(len(payload) < svg_raster.DEFAULT_MAX_BYTES,
                f"this bomb must sit under the byte cap to be meaningful, "
                f"got {len(payload)} bytes")

    started = time.time()
    raised = None
    try:
        svg_raster.rasterize(payload)
    except svg_raster.SvgRasterError as e:
        raised = e
    elapsed = time.time() - started

    assert_true(raised is not None,
                "an SVG embedding a 400-megapixel raster must be refused")
    assert_true(elapsed < 10,
                f"the refusal should come from the header scan, not a render, "
                f"took {elapsed:.1f}s")

    f = _make_svg_file(opts, "embedbomb.svg", payload)
    result = _run_renditions(f)
    assert_true(result is not None and result.startswith("completed:"),
                f"handler should complete cleanly on an embedded bomb, got {result}")
    assert_eq(FileRendition.objects.filter(original_file=f).count(), 0,
              "an embedded raster bomb must produce no renditions")


@th.django_unit_test("SVG: gzip-compressed .svgz is refused")
def test_svgz_is_refused(opts):
    import gzip
    from mojo.apps.fileman.renderer import svg_raster

    payload = gzip.compress(GOOD_SVG)
    raised = None
    try:
        svg_raster.rasterize(payload)
    except svg_raster.SvgRasterError as e:
        raised = e
    assert_true(raised is not None, "gzip-compressed svg must be refused")
    assert_true(raised.not_svg is False,
                "an svgz refusal is terminal and must not fall back to the raster path")


# ---------------------------------------------------------------------------
# Content sniffing and fallback
# ---------------------------------------------------------------------------

@th.django_unit_test("SVG: HTML declared as image/svg+xml produces no renditions")
def test_non_svg_declared_as_svg(opts):
    from mojo.apps.fileman.models import FileRendition

    payload = b"<html><body><h1>not an svg</h1></body></html>"
    f = _make_svg_file(opts, "fake.svg", payload)
    result = _run_renditions(f)
    assert_true(result is not None and result.startswith("completed:"),
                f"handler should complete cleanly on a non-SVG payload, got {result}")
    assert_eq(FileRendition.objects.filter(original_file=f).count(), 0,
              "an HTML file declared as SVG must produce no renditions")


@th.django_unit_test("SVG: a real PNG named .svg still gets a thumbnail")
def test_mislabeled_raster_still_renders(opts):
    from mojo.apps.fileman.models import FileRendition

    # Browsers set content_type from the extension, so a PNG saved as logo.svg
    # arrives declared as image/svg+xml. PIL sniffs content and thumbnails it
    # today; routing SVG by content type must not regress that.
    png = _tiny_png_bytes(size=(120, 80), color=(30, 90, 200))
    f = _make_svg_file(opts, "mislabeled.svg", png)
    result = _run_renditions(f)
    assert_true(result is not None and result.startswith("completed:"),
                f"handler should complete cleanly, got {result}")

    thumb = FileRendition.objects.filter(original_file=f, role="thumbnail").first()
    assert_true(thumb is not None,
                "a real PNG mislabeled as .svg must still produce a thumbnail")


# ---------------------------------------------------------------------------
# Cost control
# ---------------------------------------------------------------------------

@th.django_unit_test("SVG: the rasterizer runs once per file, not once per role")
def test_rasterize_runs_once_per_file(opts):
    from mojo.apps.fileman.models import FileRendition
    from mojo.apps.fileman.renderer import svg_raster
    from mojo.apps.fileman.renderer.vector import VectorRenderer

    f = _make_svg_file(opts, "once.svg", GOOD_SVG)

    calls = []
    original = svg_raster.rasterize

    def counting_rasterize(svg_bytes):
        calls.append(len(svg_bytes))
        return original(svg_bytes)

    svg_raster.rasterize = counting_rasterize
    try:
        renderer = VectorRenderer(f)
        created = renderer.create_all_renditions()
    finally:
        svg_raster.rasterize = original

    assert_true(len(created) > 1,
                f"this test needs several roles to be meaningful, got {len(created)}")
    assert_eq(len(calls), 1,
              f"rasterization must happen once for {len(created)} roles, ran {len(calls)} times")
    assert_eq(FileRendition.objects.filter(original_file=f, role="thumbnail").count(), 1,
              "the memoized raster should still produce a thumbnail")


@th.django_unit_test("Renditions: duplicate roles collapse before the worker sees them")
def test_regenerate_renditions_dedupes_roles(opts):
    from mojo.apps.jobs.models import Job

    f = _make_svg_file(opts, "dedupe.svg", GOOD_SVG)
    Job.objects.filter(func="mojo.apps.fileman.asyncjobs.regenerate_renditions",
                       payload__file_id=f.id).delete()

    f.publish_regenerate_renditions(roles=["thumbnail"] * 20)

    job = Job.objects.filter(
        func="mojo.apps.fileman.asyncjobs.regenerate_renditions",
        payload__file_id=f.id).order_by("-id").first()
    assert_true(job is not None, f"a regenerate job should be published for file {f.id}")
    assert_eq(job.payload.get("roles"), ["thumbnail"],
              f"20 duplicate roles should collapse to one, got {job.payload.get('roles')}")


@th.django_unit_test("SVG: renderer selection routes SVG away from the PIL path")
def test_renderer_selection_is_explicit(opts):
    from mojo.apps.fileman import renderer as renderer_mod
    from mojo.apps.fileman.renderer.vector import VectorRenderer
    from mojo.apps.fileman.renderer.image import ImageRenderer

    svg_file = _make_svg_file(opts, "selection.svg", GOOD_SVG)
    chosen = renderer_mod.get_renderer_for_file(svg_file)
    assert_true(isinstance(chosen, VectorRenderer),
                f"an SVG must route to VectorRenderer, got {type(chosen).__name__}")

    png_file = _make_svg_file(opts, "selection.png", _tiny_png_bytes(),
                              content_type="image/png")
    chosen = renderer_mod.get_renderer_for_file(png_file)
    assert_true(isinstance(chosen, ImageRenderer)
                and not isinstance(chosen, VectorRenderer),
                f"a PNG must still route to ImageRenderer, got {type(chosen).__name__}")
