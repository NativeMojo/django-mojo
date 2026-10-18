from testit import helpers as th


FORMATS = (("image/png", "PNG"), ("image/jpeg", "JPEG"),
           ("image/gif", "GIF"), ("image/webp", "WEBP"))


def _image(fmt="PNG", size=(4, 3)):
    import io
    from PIL import Image

    out = io.BytesIO()
    Image.new("RGB", size, (200, 30, 30)).save(out, format=fmt)
    return out.getvalue()


def _refused(code, run):
    from mojo.helpers.llm_images import ImageInputError

    try:
        run()
        assert False, f"the image input should have been refused with {code}"
    except ImageInputError as err:
        assert err.code == code, f"expected image refusal {code}, got {err.code}"
        return err


@th.django_unit_test()
def test_image_block_is_canonical_for_each_format(opts):
    import base64
    from mojo.helpers.llm_images import decode_image_block, image_block

    for media_type, fmt in FORMATS:
        raw = _image(fmt)
        block = image_block(raw, media_type)
        assert block == {"type": "image", "source": {
            "type": "base64", "media_type": media_type,
            "data": base64.b64encode(raw).decode("ascii")}}, \
            f"{media_type} must produce the canonical base64 block, got {block['source']['media_type']}"
        decoded = decode_image_block(block)
        assert decoded == (media_type.split("/")[1], raw), \
            f"{media_type} block must decode back to its format and exact bytes"


@th.django_unit_test()
def test_malformed_blocks_are_image_invalid(opts):
    from mojo.helpers.llm_images import decode_image_block, image_block

    good = image_block(_image(), "image/png")
    source = good["source"]
    bad = [
        None, "image", {"type": "image"}, dict(good, extra=1),
        {"type": "text", "source": source},
        {"type": "image", "source": "unsupported"},
        {"type": "image", "source": dict(source, url="https://example.com/a.png")},
        {"type": "image", "source": {"type": "url", "media_type": "image/png",
                                     "data": source["data"]}},
        {"type": "image", "source": dict(source, media_type="image/svg+xml")},
        {"type": "image", "source": dict(source, media_type="image/bmp")},
        {"type": "image", "source": dict(source, data="not base64!")},
        {"type": "image", "source": dict(source, data="")},
        {"type": "image", "source": dict(source, data=123)},
    ]
    for block in bad:
        _refused("image_invalid", lambda: decode_image_block(block))
    for data, media_type in ((b"", "image/png"), ("text", "image/png"),
                             (_image(), "image/svg+xml"), (_image(), "image/bmp"),
                             (_image("BMP"), "image/png"), (_image(), None)):
        _refused("image_invalid", lambda: image_block(data, media_type))


@th.django_unit_test()
def test_media_type_must_match_magic_bytes_and_parsed_format(opts):
    from mojo.helpers.llm_images import image_block

    png = _image("PNG")
    for media_type in ("image/jpeg", "image/gif", "image/webp"):
        _refused("image_invalid", lambda: image_block(png, media_type))
    truncated = png[:16]
    _refused("image_invalid", lambda: image_block(truncated, "image/png"))
    forged = b"\x89PNG\r\n\x1a\n" + b"\x00" * 64
    _refused("image_invalid", lambda: image_block(forged, "image/png"))


@th.django_unit_test()
def test_size_and_side_limits_are_input_too_large(opts):
    import base64
    from mojo.helpers import llm_images

    wide = _image("PNG", size=(llm_images.MAX_IMAGE_SIDE + 1, 1))
    _refused("input_too_large", lambda: llm_images.image_block(wide, "image/png"))
    edge = _image("PNG", size=(llm_images.MAX_IMAGE_SIDE, 1))
    assert llm_images.image_block(edge, "image/png")["type"] == "image", \
        "an image exactly at the side limit must be accepted"
    big = b"\x89PNG\r\n\x1a\n" + b"\x00" * llm_images.MAX_IMAGE_BYTES
    _refused("input_too_large", lambda: llm_images.image_block(big, "image/png"))
    block = {"type": "image", "source": {
        "type": "base64", "media_type": "image/png",
        "data": base64.b64encode(big).decode("ascii")}}
    _refused("input_too_large", lambda: llm_images.decode_image_block(block))


@th.django_unit_test()
def test_collect_images_bounds_count_total_and_placement(opts):
    from mojo.helpers import llm_images

    block = llm_images.image_block(_image(), "image/png")
    text = {"type": "text", "text": "Describe this screenshot."}
    count, sizing = llm_images.collect_images(
        [{"role": "user", "content": [block, block, text]},
         {"role": "assistant", "content": "Earlier answer"}])
    assert count == 2, f"two user images must be counted, got {count}"
    assert sizing[0]["content"][0]["source"]["data"] == "" \
        and sizing[0]["content"][2] == text and sizing[1]["content"] == "Earlier answer", \
        "sizing copy must blank only image data and keep every other block"
    assert block["source"]["data"], "collect_images must not mutate the caller's blocks"

    many = [{"role": "user", "content": [block] * (llm_images.MAX_IMAGES + 1)}]
    _refused("input_too_large", lambda: llm_images.collect_images(many))

    placements = [
        [{"role": "assistant", "content": [block]}],
        [{"role": "user", "content": [{"type": "tool_result", "content": [block]}]}],
        [{"role": "user", "content": {"type": "image"}}],
    ]
    for messages in placements:
        _refused("image_invalid", lambda: llm_images.collect_images(messages))
    plain = [{"role": "user", "content": "hello"}]
    _refused("image_invalid", lambda: llm_images.collect_images(plain, system=[block]))
    _refused("image_invalid", lambda: llm_images.collect_images(
        plain, tools=[{"name": "t", "input_schema": block}]))


@th.django_unit_test()
def test_collect_images_enforces_the_total_byte_cap(opts):
    import io
    import os
    from PIL import Image
    from mojo.helpers import llm_images

    # Random pixels do not compress, so this PNG stays just under the
    # per-image cap while five copies exceed the 20 MiB total.
    width, height = 1300, 1200
    out = io.BytesIO()
    Image.frombytes("RGB", (width, height), os.urandom(width * height * 3)).save(
        out, format="PNG", compress_level=0)
    raw = out.getvalue()
    assert llm_images.MAX_TOTAL_IMAGE_BYTES < len(raw) * llm_images.MAX_IMAGES \
        and len(raw) <= llm_images.MAX_IMAGE_BYTES, \
        f"fixture must fit one image but overflow the total, got {len(raw)} bytes"
    block = llm_images.image_block(raw, "image/png")
    under = [{"role": "user", "content": [block] * 4}]
    assert llm_images.collect_images(under)[0] == 4, \
        "four large images under the total cap must be accepted"
    over = [{"role": "user", "content": [block] * llm_images.MAX_IMAGES}]
    _refused("input_too_large", lambda: llm_images.collect_images(over))


@th.django_unit_test()
def test_errors_never_carry_image_data(opts):
    import base64
    from mojo.helpers.llm_images import decode_image_block

    secret = b"\x89PNG\r\n\x1a\nPRIVATE-SCREENSHOT-BYTES"
    data = base64.b64encode(secret).decode("ascii")
    err = _refused("image_invalid", lambda: decode_image_block(
        {"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                     "data": data}}))
    text = f"{err!r} {err} {err.args}"
    assert data not in text and "PRIVATE" not in text, \
        "an image refusal must carry only its safe code, never the data"


@th.django_unit_test()
def test_decompression_bomb_header_is_input_too_large(opts):
    import struct
    import zlib
    from mojo.helpers.llm_images import image_block

    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(
            ">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    # Header-only PNG declaring 20000 x 20000; no pixels are allocated.
    raw = b"\x89PNG\r\n\x1a\n" + chunk(
        b"IHDR", struct.pack(">IIBBBBB", 20000, 20000, 8, 2, 0, 0, 0)) \
        + chunk(b"IDAT", b"") + chunk(b"IEND", b"")
    assert len(raw) < 100, "the bomb fixture must stay a tiny header-only file"
    _refused("input_too_large", lambda: image_block(raw, "image/png"))
