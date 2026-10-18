"""Validated, bounded image blocks for guarded LLM calls.

Bytes only: nothing here fetches a URL, reads a file or touches the network.
Errors carry a safe code and never any image data.
"""

import base64
import binascii
import io
import warnings


IMAGE_MEDIA_TYPES = {"image/png": "png", "image/jpeg": "jpeg",
                     "image/gif": "gif", "image/webp": "webp"}
MAX_IMAGES = 5
MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_TOTAL_IMAGE_BYTES = 20 * 1024 * 1024
MAX_IMAGE_SIDE = 8000
IMAGE_RESERVED_TOKENS = 1600

_PILLOW_FORMATS = {"png": "PNG", "jpeg": "JPEG", "gif": "GIF", "webp": "WEBP"}


class ImageInputError(Exception):
    def __init__(self, code):
        self.code = code if code in ("image_invalid", "input_too_large") \
            else "image_invalid"
        super().__init__(self.code)


def _magic_matches(fmt, raw):
    if fmt == "png":
        return raw.startswith(b"\x89PNG\r\n\x1a\n")
    if fmt == "jpeg":
        return raw.startswith(b"\xff\xd8\xff")
    if fmt == "gif":
        return raw.startswith((b"GIF87a", b"GIF89a"))
    return raw[:4] == b"RIFF" and raw[8:12] == b"WEBP"


def _check_image(fmt, raw):
    if not raw:
        raise ImageInputError("image_invalid")
    if len(raw) > MAX_IMAGE_BYTES:
        raise ImageInputError("input_too_large")
    if not _magic_matches(fmt, raw):
        raise ImageInputError("image_invalid")
    from PIL import Image
    expected = _PILLOW_FORMATS[fmt]
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            # Header parse only; pixel data is never decoded here.
            with Image.open(io.BytesIO(raw), formats=[expected]) as image:
                actual = image.format
                width, height = image.size
    except Exception:
        raise ImageInputError("image_invalid") from None
    if actual != expected or width <= 0 or height <= 0:
        raise ImageInputError("image_invalid")
    if width > MAX_IMAGE_SIDE or height > MAX_IMAGE_SIDE:
        raise ImageInputError("input_too_large")


def image_block(data, media_type):
    """Return the canonical base64 image block for llm.call()."""
    if not isinstance(data, (bytes, bytearray)) or not isinstance(media_type, str) \
            or media_type not in IMAGE_MEDIA_TYPES:
        raise ImageInputError("image_invalid")
    raw = bytes(data)
    _check_image(IMAGE_MEDIA_TYPES[media_type], raw)
    return {"type": "image", "source": {
        "type": "base64", "media_type": media_type,
        "data": base64.b64encode(raw).decode("ascii")}}


def decode_image_block(block):
    """Validate one canonical image block; return (format, raw_bytes)."""
    if not isinstance(block, dict) or set(block) != {"type", "source"} \
            or block.get("type") != "image":
        raise ImageInputError("image_invalid")
    source = block["source"]
    if not isinstance(source, dict) or set(source) != {"type", "media_type", "data"} \
            or source.get("type") != "base64" \
            or not isinstance(source.get("media_type"), str) \
            or source["media_type"] not in IMAGE_MEDIA_TYPES \
            or not isinstance(source.get("data"), str):
        raise ImageInputError("image_invalid")
    # Reject before decoding anything far over the raw cap (base64 is 4/3).
    if len(source["data"]) > (MAX_IMAGE_BYTES + 2) // 3 * 4:
        raise ImageInputError("input_too_large")
    try:
        raw = base64.b64decode(source["data"], validate=True)
    except (binascii.Error, ValueError):
        raise ImageInputError("image_invalid") from None
    fmt = IMAGE_MEDIA_TYPES[source["media_type"]]
    _check_image(fmt, raw)
    return fmt, raw


def _is_image(value):
    return isinstance(value, dict) and value.get("type") == "image"


def _contains_image(value):
    if isinstance(value, dict):
        if value.get("type") == "image":
            return True
        return any(_contains_image(item) for item in value.values())
    if isinstance(value, (list, tuple)):
        return any(_contains_image(item) for item in value)
    return False


def collect_images(messages, system=None, tools=None):
    """Validate every image in a request.

    Images are allowed only as top-level blocks of a user message's content
    list. Returns (count, sizing_messages): a copy of messages with each
    image's data blanked, for byte-size and token-reservation checks.
    """
    if _contains_image(system) or _contains_image(tools) \
            or not isinstance(messages, (list, tuple)):
        raise ImageInputError("image_invalid")
    count = 0
    total = 0
    sizing = []
    for message in messages:
        content = message.get("content") if isinstance(message, dict) else None
        if not isinstance(content, list):
            if _contains_image(message):
                raise ImageInputError("image_invalid")
            sizing.append(message)
            continue
        blocks = []
        for block in content:
            if _is_image(block):
                if message.get("role") != "user":
                    raise ImageInputError("image_invalid")
                count += 1
                if count > MAX_IMAGES:
                    raise ImageInputError("input_too_large")
                _fmt, raw = decode_image_block(block)
                total += len(raw)
                if total > MAX_TOTAL_IMAGE_BYTES:
                    raise ImageInputError("input_too_large")
                source = dict(block["source"], data="")
                blocks.append({"type": "image", "source": source})
            else:
                if _contains_image(block):
                    raise ImageInputError("image_invalid")
                blocks.append(block)
        sizing.append(dict(message, content=blocks))
    return count, sizing
