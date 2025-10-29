"""
Reusable QR code generation helpers.
"""

from __future__ import annotations

import base64
import binascii
import re
from dataclasses import dataclass
from io import BytesIO
from typing import Optional

from mojo.helpers import logit

try:
    import qrcode
    from qrcode.constants import (
        ERROR_CORRECT_H,
        ERROR_CORRECT_L,
        ERROR_CORRECT_M,
        ERROR_CORRECT_Q,
    )
    from qrcode.exceptions import DataOverflowError
    from qrcode.image.svg import SvgFillImage
except ImportError:  # pragma: no cover - dependency missing at runtime
    qrcode = None
    ERROR_CORRECT_H = ERROR_CORRECT_L = ERROR_CORRECT_M = ERROR_CORRECT_Q = None
    DataOverflowError = RuntimeError
    SvgFillImage = None

HEX_COLOR_RE = re.compile(r"^#(?:[0-9a-fA-F]{3}){1,2}$")
SUPPORTED_FORMATS = {"png", "svg", "base64"}
SUPPORTED_ERROR_LEVELS = {"l", "m", "q", "h"}
ERROR_CORRECTION_MAP = {
    "l": ERROR_CORRECT_L,
    "m": ERROR_CORRECT_M,
    "q": ERROR_CORRECT_Q,
    "h": ERROR_CORRECT_H,
}


class QRCodeError(Exception):
    """Raised when QR code generation fails or receives invalid input."""

    def __init__(self, message: str, *, status: int = 400):
        super().__init__(message)
        self.status = status


@dataclass
class QRCodePayload:
    """Represents rendered QR code data."""

    format: str
    content: bytes | str
    content_type: str
    width: Optional[int] = None
    height: Optional[int] = None


def generate_qrcode(
    *,
    data: str,
    fmt: str = "png",
    size: int = 256,
    border: int = 4,
    error_correction: str = "m",
    color: str = "#000000",
    background: str = "#FFFFFF",
    base64_format: str = "png",
    logo: Optional[str] = None,
    logo_scale: float = 0.2,
) -> QRCodePayload:
    """
    Generate a QR code using the provided configuration.

    :param data: Payload encoded into the QR code.
    :param fmt: Output format - png, svg, or base64.
    :param size: Target image size in pixels.
    :param border: QR module border width.
    :param error_correction: L, M, Q, or H.
    :param color: Foreground color (#RGB or #RRGGBB).
    :param background: Background color (#RGB or #RRGGBB).
    :param base64_format: When fmt == "base64", the underlying format (png|svg).
    :param logo: Optional base64-encoded logo overlay.
    :param logo_scale: Fraction of QR size reserved for the logo (0.05-0.35).
    """
    if qrcode is None:
        logit.error("mojo.helpers.qrcode", "qrcode library is not installed")
        raise QRCodeError("QR code support is not available.", status=500)

    fmt = (fmt or "png").lower()
    if fmt not in SUPPORTED_FORMATS:
        raise QRCodeError("Unsupported format. Choose png, svg, or base64.")

    error_correction = (error_correction or "m").lower()
    if error_correction not in SUPPORTED_ERROR_LEVELS:
        raise QRCodeError("Invalid error correction level. Use L, M, Q, or H.")

    border = _clamp_int(border, default=4, minimum=0, maximum=32)
    size = _clamp_int(size, default=256, minimum=48, maximum=2048)

    fill_color = _normalize_color(color or "#000000")
    back_color = _normalize_color(background or "#FFFFFF")
    if fill_color is None or back_color is None:
        raise QRCodeError("Colors must be hex values in #RGB or #RRGGBB form.")

    base64_format = (base64_format or "png").lower()
    if fmt == "base64" and base64_format not in {"png", "svg"}:
        raise QRCodeError("base64_format must be png or svg.")

    logo_bytes = None
    if logo:
        logo_bytes = _decode_base64(logo)
    logo_ratio = _clamp_float(logo_scale, default=0.2, minimum=0.05, maximum=0.35)

    # Build QR code with a temporary box_size to compute module count
    temp_qr = _build_qrcode(data, ERROR_CORRECTION_MAP[error_correction], border, box_size=1)
    box_size = _compute_box_size(temp_qr, size, border)

    # Rebuild with the computed box_size
    qr = _build_qrcode(data, ERROR_CORRECTION_MAP[error_correction], border, box_size)

    if fmt == "svg":
        svg_bytes = _render_svg(qr, fill_color, back_color)
        return QRCodePayload(
            format="svg",
            content=svg_bytes,
            content_type="image/svg+xml",
        )

    if fmt == "base64" and base64_format == "svg":
        svg_bytes = _render_svg(qr, fill_color, back_color)
        encoded = base64.b64encode(svg_bytes).decode("ascii")
        return QRCodePayload(
            format="svg",
            content=encoded,
            content_type="image/svg+xml",
        )

    png_bytes, width, height = _render_png(
        qr,
        fill_color,
        back_color,
        logo_bytes,
        logo_ratio,
    )

    if fmt == "base64":
        encoded = base64.b64encode(png_bytes).decode("ascii")
        return QRCodePayload(
            format="png",
            content=encoded,
            content_type="image/png",
            width=width,
            height=height,
        )

    return QRCodePayload(
        format="png",
        content=png_bytes,
        content_type="image/png",
        width=width,
        height=height,
    )


def _build_qrcode(data, error_correction, border, box_size=10):
    try:
        qr = qrcode.QRCode(
            version=None,
            error_correction=error_correction,
            box_size=box_size,
            border=border,
        )
        qr.add_data(data)
        qr.make(fit=True)
    except DataOverflowError as exc:
        raise QRCodeError("Data payload is too large for a QR code.") from exc
    return qr


def _render_png(qr, fill_color, back_color, logo_bytes, logo_ratio):
    from PIL import Image

    image = qr.make_image(
        fill_color=fill_color,
        back_color=back_color,
    )
    if hasattr(image, "get_image"):
        image = image.get_image()
    image = image.convert("RGBA")

    if logo_bytes:
        image = _overlay_logo(image, logo_bytes, logo_ratio)

    buffer = BytesIO()
    image.save(buffer, format="PNG")
    return buffer.getvalue(), image.width, image.height


def _render_svg(qr, fill_color, back_color):
    if SvgFillImage is None:
        raise QRCodeError("SVG rendering is not available.", status=500)
    buffer = BytesIO()
    image = qr.make_image(
        image_factory=SvgFillImage,
        fill_color=fill_color,
        back_color=back_color,
    )
    image.save(buffer)
    return buffer.getvalue()


def _overlay_logo(image, logo_bytes, logo_ratio):
    from PIL import Image

    try:
        logo = Image.open(BytesIO(logo_bytes)).convert("RGBA")
    except Exception as exc:
        raise QRCodeError("Unable to read logo data.") from exc

    max_size = int(min(image.size) * logo_ratio)
    if max_size <= 0:
        return image

    scale = max_size / max(logo.width, logo.height)
    new_size = (
        max(1, int(logo.width * scale)),
        max(1, int(logo.height * scale)),
    )

    try:
        resample = Image.Resampling.LANCZOS
    except AttributeError:  # pragma: no cover - Pillow < 10
        resample = Image.LANCZOS

    logo = logo.resize(new_size, resample=resample)
    x = (image.width - logo.width) // 2
    y = (image.height - logo.height) // 2
    image.paste(logo, (x, y), mask=logo)
    return image


def _compute_box_size(qr, target_size, border):
    modules = qr.modules_count + (border * 2)
    if modules <= 0:
        return 10
    return max(1, target_size // modules)


def _normalize_color(value):
    normalized = (value or "").strip()
    if not HEX_COLOR_RE.match(normalized):
        return None
    if len(normalized) == 4:
        normalized = "#" + "".join(ch * 2 for ch in normalized[1:])
    return normalized.upper()


def _clamp_int(value, default, minimum=None, maximum=None):
    try:
        value = int(value)
    except (TypeError, ValueError):
        value = default
    if minimum is not None:
        value = max(minimum, value)
    if maximum is not None:
        value = min(maximum, value)
    return value


def _clamp_float(value, default, minimum=None, maximum=None):
    try:
        value = float(value)
    except (TypeError, ValueError):
        value = default
    if minimum is not None:
        value = max(minimum, value)
    if maximum is not None:
        value = min(maximum, value)
    return value


def _decode_base64(value: str) -> bytes:
    if not isinstance(value, str):
        raise QRCodeError("Logo must be a base64 encoded string.")
    _, _, payload = value.partition(",")
    candidate = payload or value
    try:
        return base64.b64decode(candidate, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise QRCodeError("Invalid base64 logo data.") from exc
