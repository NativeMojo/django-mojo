"""SVG -> PNG rasterization, isolated in a subprocess.

An uploaded SVG is an XML document written by whoever uploaded it, and a
rasterizer is an interpreter for it. This module is the boundary between the
jobs worker and that interpreter.

Two halves live here:

  * The PARENT half (`validate_svg_bytes`, `rasterize`) runs inside the worker
    with Django available. It applies the input caps and spawns the child.
  * The CHILD half (`_child_main`) runs in a bare interpreter with no Django,
    no ORM and no credentials. It reads SVG from stdin and writes PNG to
    stdout, and nothing else.

The child is launched by ABSOLUTE PATH, never `python -m ...`: `-m` imports the
parent packages first, and `mojo/__init__.py` reaches a module-scope settings
read in `mojo/helpers/request.py`, which raises ImproperlyConfigured in a bare
interpreter (and AppRegistryNotReady once DJANGO_SETTINGS_MODULE is set). That
is also why every `mojo.*` import in this file sits inside a function body:
running the file directly executes the module body.

Engine: resvg (via resvg-py). It has no JavaScript engine, no network stack,
and resolves no external XML entities, so scripts, SSRF and XXE are absent by
construction rather than disabled by flag. Local file loading is off because
`resources_dir` is never passed.

Five independent caps bound the work, each covering a bomb the others miss:

  1. max input bytes          - large `data:` payloads
  2. max embedded raster px   - a 1.6 MB SVG embedding a 20000x20000 PNG slips
                                under the byte cap AND the timeout, and decodes
                                to multiple GB. This is the only portable
                                control against it.
  3. fixed output box         - an absurd `viewBox`/`width` (100000x100000
                                renders for two minutes when unbounded)
  4. wall-clock timeout       - filter bombs: `feMorphology radius=800` is 326
                                bytes and runs for over 40 seconds
  5. RLIMIT_AS in the child   - Linux-only backstop; macOS cannot set it at all

Two rules keep cap 2 honest, and both are load-bearing. resvg parses `data:`
URIs with a full WHATWG-grammar parser and expands internal DTD entities inside
attribute values, so a scan looking only for a literal `data:image/...;base64,`
run of text is trivially evaded — by a media-type parameter
(`;charset=x;base64,`), by a percent-encoded body (`data:image/png,%89PNG...`),
or by splitting the URI across an entity. Therefore:

  * a `data:image/` URI in any form other than the canonical `;base64,` is
    REFUSED rather than skipped, and
  * a document declaring any internal entity is REFUSED outright.

Both checks run before the not-SVG sniff, so a hostile document can never be
demoted to "probably a raster image" and handed a second pass.

See docs/django_developer/fileman/renditions.md.
"""

import base64
import io
import os
import re
import sys


# Content types that route to this rasterizer.
SVG_CONTENT_TYPES = ("image/svg+xml", "image/svg")

# Defaults for the five caps. Each is overridable by the matching setting.
DEFAULT_MAX_BYTES = 2097152          # 2 MB
DEFAULT_MAX_EMBEDDED_PIXELS = 40000000   # 40 Mpx summed across data: images
DEFAULT_RASTER_BOX = 1024            # px, applied to BOTH width and height
DEFAULT_TIMEOUT = 15                 # seconds of wall clock for the child
DEFAULT_MEMORY_MB = 512              # RLIMIT_AS ceiling inside the child

# How much of the head we decode when sniffing.
SNIFF_BYTES = 4096

# Formats the embedded-raster scan will read a header from. Mirrors the
# allowlist ImageRenderer uses — this scan runs in the WORKER, outside the
# subprocess boundary, so it must not hand arbitrary bytes to every Pillow
# decoder's _open().
_EMBEDDED_IMAGE_FORMATS = ["JPEG", "PNG", "WEBP", "GIF", "BMP", "TIFF"]

# Any `data:image/...` URI, however it is spelled. Group 1 is everything
# between the media type and the comma, so a non-canonical form can be
# recognised and refused rather than silently skipped.
_DATA_URI_RE = re.compile(r"data:image/[a-z0-9.+-]+([^,\"'>\s]*),",
                          re.IGNORECASE)

# Base64 payload following a canonical `;base64,` introducer.
_B64_BLOB_RE = re.compile(r"[A-Za-z0-9+/=\s]*")

# An internal DTD entity declaration. Refused outright: entities let an author
# split a `data:` URI across the document so no single run of text matches the
# scan below, and roxmltree expands them inside attribute values. They are also
# the billion-laughs vector. Nothing legitimate needs them in a thumbnail.
_ENTITY_DECL_RE = re.compile(r"<!ENTITY\b", re.IGNORECASE)


class SvgRasterError(Exception):
    """A rasterization was refused or failed.

    `not_svg` marks the one refusal the caller is allowed to recover from: the
    bytes are not an SVG document at all, so the file may still be a raster
    image that PIL can handle. Every other refusal (too big, bomb, timeout) is
    terminal — it must NOT be retried down another path.
    """

    def __init__(self, message, not_svg=False):
        super().__init__(message)
        self.not_svg = not_svg


def _setting(name, default):
    """Read a cap from settings, falling back to the module default.

    Imported here rather than at module scope because the child executes this
    file with no Django configured.
    """
    try:
        from mojo.helpers.settings import settings
        value = settings.get(name, default)
    except Exception:
        return default
    try:
        value = int(value)
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def _embedded_pixel_count(text):
    """Total decoded pixels across every `data:` image in the document.

    Only image headers are parsed — PIL reads the dimensions without decoding
    pixel data, so this stays sub-millisecond even for a megabyte of base64.

    Fail-closed in both directions: an embedded image we cannot measure is one
    we cannot bound, and so is a `data:` URI written in any form other than the
    canonical `;base64,`. resvg parses data URIs with a full WHATWG-grammar
    parser, so a scan that only recognises the canonical spelling would let
    `data:image/png;charset=x;base64,...` and `data:image/png,<percent-encoded>`
    through unmeasured. Refusing the odd spellings is the only way a byte-level
    scan can keep up with a real URL parser.
    """
    from PIL import Image

    total = 0
    for match in _DATA_URI_RE.finditer(text):
        meta = match.group(1)
        if meta.lower() != ";base64":
            raise SvgRasterError(
                "embedded data: image must use the canonical ;base64, form")

        blob_match = _B64_BLOB_RE.match(text, match.end())
        blob = "".join(blob_match.group(0).split()) if blob_match else ""
        if not blob:
            continue
        try:
            raw = base64.b64decode(blob, validate=False)
        except Exception:
            raise SvgRasterError("embedded image is not decodable base64")
        if not raw:
            continue
        try:
            with Image.open(io.BytesIO(raw), formats=_EMBEDDED_IMAGE_FORMATS) as img:
                width, height = img.size
        except Exception:
            raise SvgRasterError("embedded image header is unreadable")
        total += int(width) * int(height)
    return total


def max_input_bytes():
    """The input cap, for callers that must check a size before reading bytes."""
    return _setting("FILEMAN_SVG_MAX_BYTES", DEFAULT_MAX_BYTES)


def validate_svg_bytes(data):
    """Fail-closed input checks. Raises SvgRasterError; returns None on success."""
    if not data:
        raise SvgRasterError("empty file")

    max_bytes = max_input_bytes()
    if len(data) > max_bytes:
        raise SvgRasterError(
            "svg is %d bytes, over the %d byte limit" % (len(data), max_bytes))

    # .svgz is deliberately unsupported: a compressed input is a decompression
    # bomb vector, and refusing on the magic bytes beats an opaque decode error.
    if data[:2] == b"\x1f\x8b":
        raise SvgRasterError("gzip-compressed svg (.svgz) is not supported")

    # The bomb checks run BEFORE the sniff, so that a hostile document can never
    # be classified as merely "not svg" and handed a second pass down the raster
    # path. Only a document that survives them may be called not-SVG.
    text = data.decode("utf-8", errors="ignore")

    if _ENTITY_DECL_RE.search(text):
        raise SvgRasterError("internal entity declarations are not allowed")

    max_pixels = _setting("FILEMAN_SVG_MAX_EMBEDDED_PIXELS",
                          DEFAULT_MAX_EMBEDDED_PIXELS)
    embedded = _embedded_pixel_count(text)
    if embedded > max_pixels:
        raise SvgRasterError(
            "embedded rasters total %d pixels, over the %d pixel limit"
            % (embedded, max_pixels))

    head = text[:SNIFF_BYTES].lstrip("﻿").lstrip()
    if not head.startswith("<"):
        raise SvgRasterError("content does not start with an xml/svg element",
                             not_svg=True)
    if "<svg" not in head.lower():
        raise SvgRasterError("no <svg> element in the first %d bytes" % SNIFF_BYTES,
                             not_svg=True)


def rasterize(svg_bytes):
    """Rasterize SVG bytes to PNG bytes in an isolated child process.

    Raises SvgRasterError on any refusal, timeout or child failure.
    """
    import subprocess

    validate_svg_bytes(svg_bytes)

    from importlib import util as importlib_util
    if importlib_util.find_spec("resvg_py") is None:
        raise SvgRasterError(
            "svg rasterization requires resvg-py — install django-mojo[svg]")

    box = _setting("FILEMAN_SVG_RASTER_BOX", DEFAULT_RASTER_BOX)
    timeout = _setting("FILEMAN_SVG_TIMEOUT", DEFAULT_TIMEOUT)
    memory_mb = _setting("FILEMAN_SVG_MEMORY_MB", DEFAULT_MEMORY_MB)

    # A minimal environment on purpose. The child needs none of the worker's
    # database or cloud credentials, and the interpreter resolves its venv from
    # sys.executable rather than from the environment.
    child_env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": os.environ.get("HOME", "/tmp"),
        "LANG": os.environ.get("LANG", "C.UTF-8"),
    }

    try:
        proc = subprocess.run(
            [sys.executable, os.path.abspath(__file__),
             str(box), str(memory_mb), str(timeout)],
            input=svg_bytes,
            capture_output=True,
            timeout=timeout,
            start_new_session=True,
            env=child_env,
        )
    except subprocess.TimeoutExpired:
        raise SvgRasterError("rasterization exceeded %ds and was killed" % timeout)
    except Exception as e:
        raise SvgRasterError("failed to spawn rasterizer: %s" % str(e)[:200])

    if proc.returncode != 0 or not proc.stdout:
        # Only the child's own diagnostic is surfaced, truncated. The SVG source
        # never reaches the log, so a crafted document cannot use it as a channel.
        detail = (proc.stderr or b"").decode("utf-8", errors="replace").strip()[-200:]
        raise SvgRasterError("rasterizer failed (rc=%s): %s" % (proc.returncode, detail))

    return proc.stdout


def _child_main():
    """Bare-interpreter entry point: SVG on stdin, PNG on stdout."""
    # Running by absolute path puts this file's directory on sys.path. The child
    # imports nothing local, so drop it rather than let siblings shadow anything.
    if sys.path and sys.path[0] == os.path.dirname(os.path.abspath(__file__)):
        sys.path.pop(0)

    box = int(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_RASTER_BOX
    memory_mb = int(sys.argv[2]) if len(sys.argv) > 2 else DEFAULT_MEMORY_MB
    cpu_seconds = int(sys.argv[3]) if len(sys.argv) > 3 else DEFAULT_TIMEOUT

    # Best-effort resource ceilings, applied BEFORE the engine is imported.
    # Linux enforces RLIMIT_AS; macOS refuses to set it at all, which is why the
    # embedded-pixel cap in the parent is the portable memory control and this
    # is only a backstop. RLIMIT_CPU catches a spinning child if the parent's
    # wall clock somehow does not fire.
    try:
        import resource

        def _limit(what, target, label):
            # Keep the INHERITED hard limit. Passing RLIM_INFINITY as the hard
            # limit fails for an unprivileged process whenever the inherited
            # hard limit is finite — exactly the hardened deployments (systemd
            # LimitAS=, container memory limits) where the ceiling matters most.
            try:
                soft, hard = resource.getrlimit(what)
                if hard != resource.RLIM_INFINITY:
                    target = min(target, hard)
                resource.setrlimit(what, (target, hard))
            except Exception as e:
                # Say so rather than swallowing it: a silently absent ceiling
                # reads identically to an enforced one.
                sys.stderr.write("warning: could not set %s: %s\n" % (label, e))

        _limit(resource.RLIMIT_AS, memory_mb * 1024 * 1024, "RLIMIT_AS")
        _limit(resource.RLIMIT_CPU, cpu_seconds, "RLIMIT_CPU")
    except ImportError:
        pass

    raw = sys.stdin.buffer.read()
    try:
        svg = raw.decode("utf-8")
    except UnicodeDecodeError:
        sys.stderr.write("svg is not valid utf-8\n")
        return 1

    import resvg_py

    # BOTH width and height, always. `width` alone preserves aspect ratio without
    # bounding the other axis — a 40x4000 SVG at width=150 renders 150x15000.
    # With both, resvg fits inside the box and the canvas bomb cannot happen.
    png = resvg_py.svg_to_bytes(svg_string=svg, width=box, height=box)
    data = bytes(png)
    if not data:
        sys.stderr.write("rasterizer produced no output\n")
        return 1

    sys.stdout.buffer.write(data)
    sys.stdout.buffer.flush()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(_child_main())
    except Exception as e:
        # Message only — never the document, and never a traceback that could
        # echo attacker-controlled content back into the worker's log.
        sys.stderr.write("%s: %s\n" % (type(e).__name__, str(e)[:200]))
        sys.exit(1)
