"""
Test QR code helper behaviour.
"""

from testit import helpers as th
from testit.helpers import assert_eq, assert_true

from mojo.helpers.qrcode import QRCodeError, generate_qrcode


@th.unit_test("png output returns binary payload with dimensions")
def test_generate_png(opts):
    payload = generate_qrcode(data="https://example.com", fmt="png", size=256)

    assert_eq(payload.format, "png", "PNG generation should report png format")
    assert_eq(payload.content_type, "image/png", "PNG content type should be image/png")
    assert_true(isinstance(payload.content, (bytes, bytearray)), "PNG payload should be bytes")
    assert_true(payload.width and payload.height, "PNG payload should include dimensions")


@th.unit_test("svg output returns svg bytes")
def test_generate_svg(opts):
    payload = generate_qrcode(data="https://mojo.dev", fmt="svg")

    assert_eq(payload.format, "svg", "SVG generation should report svg format")
    assert_eq(payload.content_type, "image/svg+xml", "SVG content type should be image/svg+xml")
    assert_true(isinstance(payload.content, (bytes, bytearray)), "SVG payload should be bytes")
    snippet = payload.content[:100]
    assert_true(b"<svg" in snippet, "SVG payload should contain <svg tag near start")


@th.unit_test("base64 output returns encoded string")
def test_generate_base64(opts):
    payload = generate_qrcode(data="MOJO QR", fmt="base64")

    assert_eq(payload.format, "png", "Base64 default should encode PNG content")
    assert_eq(payload.content_type, "image/png", "Base64 PNG content type should be image/png")
    assert_true(isinstance(payload.content, str), "Base64 payload should be str")
    assert_true(len(payload.content) > 0, "Base64 data should not be empty")


@th.unit_test("base64 svg output returns encoded string")
def test_generate_base64_svg(opts):
    payload = generate_qrcode(data="MOJO SVG", fmt="base64", base64_format="svg")

    assert_eq(payload.format, "svg", "Base64 SVG should report svg format")
    assert_eq(payload.content_type, "image/svg+xml", "Base64 SVG content type should be image/svg+xml")
    assert_true(isinstance(payload.content, str), "Base64 SVG payload should be str")


@th.unit_test("invalid color raises error")
def test_invalid_color(opts):
    try:
        generate_qrcode(data="bad color", color="red")
    except QRCodeError as exc:
        assert_true("hex" in str(exc).lower(), "QR helper should report hex color requirement")
    else:  # pragma: no cover - guard clause
        assert_true(False, "Invalid color should raise QRCodeError")


@th.unit_test("invalid error correction raises error")
def test_invalid_error_level(opts):
    try:
        generate_qrcode(data="bad ecc", error_correction="z")
    except QRCodeError as exc:
        assert_true("invalid error correction" in str(exc).lower(), "Helper should reject unknown error level")
    else:  # pragma: no cover
        assert_true(False, "Invalid error correction should raise QRCodeError")


@th.unit_test("different sizes produce valid images")
def test_various_sizes(opts):
    for size in [48, 128, 512, 1024, 2048]:
        payload = generate_qrcode(data="test", fmt="png", size=size)
        assert_true(payload.width and payload.height, f"Size {size} should produce valid dimensions")
        assert_true(payload.width >= 48, f"Width should meet minimum for size {size}")


@th.unit_test("short hex color format works")
def test_short_hex_color(opts):
    payload = generate_qrcode(data="color test", color="#F0F", background="#ABC")
    assert_true(len(payload.content) > 0, "Short hex colors should work")


@th.unit_test("all error correction levels work")
def test_error_correction_levels(opts):
    for level in ["L", "M", "Q", "H", "l", "m", "q", "h"]:
        payload = generate_qrcode(data="ecc test", error_correction=level)
        assert_true(len(payload.content) > 0, f"Error correction level {level} should work")


@th.unit_test("invalid format raises error")
def test_invalid_format(opts):
    try:
        generate_qrcode(data="test", fmt="jpeg")
    except QRCodeError as exc:
        assert_true("unsupported format" in str(exc).lower(), "Should reject invalid format")
    else:  # pragma: no cover
        assert_true(False, "Invalid format should raise QRCodeError")


@th.unit_test("invalid base64_format raises error")
def test_invalid_base64_format(opts):
    try:
        generate_qrcode(data="test", fmt="base64", base64_format="jpeg")
    except QRCodeError as exc:
        assert_true("base64_format" in str(exc).lower(), "Should reject invalid base64_format")
    else:  # pragma: no cover
        assert_true(False, "Invalid base64_format should raise QRCodeError")


@th.unit_test("invalid background color raises error")
def test_invalid_background(opts):
    try:
        generate_qrcode(data="test", background="blue")
    except QRCodeError as exc:
        assert_true("hex" in str(exc).lower(), "Should reject invalid background color")
    else:  # pragma: no cover
        assert_true(False, "Invalid background should raise QRCodeError")


@th.unit_test("border clamping works")
def test_border_values(opts):
    # Should not raise errors - values should be clamped
    payload = generate_qrcode(data="test", border=0)
    assert_true(len(payload.content) > 0, "Border 0 should work")

    payload = generate_qrcode(data="test", border=32)
    assert_true(len(payload.content) > 0, "Border 32 should work")


@th.unit_test("unicode data encodes correctly")
def test_unicode_data(opts):
    payload = generate_qrcode(data="Hello 世界 🌍", fmt="png")
    assert_true(len(payload.content) > 0, "Unicode data should encode successfully")


@th.unit_test("empty data generates minimal qr code")
def test_empty_data(opts):
    payload = generate_qrcode(data="", fmt="png")
    assert_true(len(payload.content) > 0, "Empty data should still generate a QR code")
    assert_true(payload.width and payload.height, "Empty data QR should have dimensions")


@th.unit_test("size clamping works at boundaries")
def test_size_clamping(opts):
    # Test that extreme values get clamped properly
    payload_small = generate_qrcode(data="test", size=10)  # Below min of 48
    assert_true(len(payload_small.content) > 0, "Size below minimum should be clamped")

    payload_large = generate_qrcode(data="test", size=5000)  # Above max of 2048
    assert_true(len(payload_large.content) > 0, "Size above maximum should be clamped")
