"""
Test QR code helper behaviour.
"""

from testit import helpers as th
from testit.helpers import assert_eq, assert_true

from mojo.helpers.qrcode import QRCodeError, build_vcard, generate_qrcode


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
        assert_true(payload.width > 0 and payload.height > 0, f"Size {size} should produce positive dimensions")
        # Larger target sizes should produce larger images
        if size >= 256:
            assert_true(payload.width >= 100, f"Size {size} should produce reasonably sized image")


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


@th.unit_test("build_vcard minimal returns valid vCard 3.0")
def test_build_vcard_minimal(opts):
    result = build_vcard({"name": "Jane Doe"})
    assert_true(result.startswith("BEGIN:VCARD\r\nVERSION:3.0\r\n"), f"vCard should start with BEGIN/VERSION, got: {result[:60]!r}")
    assert_true(result.endswith("END:VCARD"), f"vCard should end with END:VCARD, got: {result[-40:]!r}")
    assert_true("FN:Jane Doe" in result, f"vCard should include FN line, got: {result!r}")
    assert_true("N:Doe;Jane;;;" in result, f"vCard should include structured N line, got: {result!r}")


@th.unit_test("build_vcard full field set with arrays emits repeated TEL/EMAIL")
def test_build_vcard_full(opts):
    result = build_vcard({
        "name": "Jane Q Doe",
        "org": "Acme Inc",
        "title": "Engineer",
        "phone": ["+15551234567", "+15557654321"],
        "email": "jane@acme.com",
        "url": "https://acme.com",
        "address": "123 Main St",
        "note": "VIP contact",
    })
    assert_true("FN:Jane Q Doe" in result, "vCard should include FN with full name")
    assert_true("N:Doe;Jane Q;;;" in result, f"vCard should split given/family on last space, got: {result!r}")
    assert_true("ORG:Acme Inc" in result, "vCard should include ORG")
    assert_true("TITLE:Engineer" in result, "vCard should include TITLE")
    assert_true("TEL:+15551234567" in result, "vCard should include first TEL")
    assert_true("TEL:+15557654321" in result, "vCard should include second TEL")
    assert_true("EMAIL:jane@acme.com" in result, "vCard should include EMAIL")
    assert_true("URL:https://acme.com" in result, "vCard should include URL")
    assert_true("ADR:;;123 Main St;;;;" in result, "vCard should include ADR with street in component 3")
    assert_true("NOTE:VIP contact" in result, "vCard should include NOTE")


@th.unit_test("build_vcard escapes RFC 6350 special characters")
def test_build_vcard_escaping(opts):
    result = build_vcard({
        "name": "Doe, Jane",
        "org": "Acme; Inc",
        "note": "line1\nline2",
        "title": "back\\slash",
    })
    assert_true("FN:Doe\\, Jane" in result, f"comma should be escaped, got: {result!r}")
    assert_true("ORG:Acme\; Inc" in result, f"semicolon should be escaped, got: {result!r}")
    assert_true("NOTE:line1\\nline2" in result, f"newline should become literal \\n, got: {result!r}")
    assert_true("TITLE:back\\\\slash" in result, f"backslash should be escaped, got: {result!r}")


@th.unit_test("build_vcard mecard variant produces MECARD string")
def test_build_vcard_mecard(opts):
    result = build_vcard({
        "name": "Jane Doe",
        "phone": "+15551234567",
        "email": "jane@acme.com",
    }, fmt="mecard")
    assert_true(result.startswith("MECARD:"), f"mecard should start with MECARD:, got: {result[:40]!r}")
    assert_true(result.endswith(";;"), f"mecard should end with terminator ;;, got: {result[-10:]!r}")
    assert_true("N:Jane Doe" in result, "mecard should include N field")
    assert_true("TEL:+15551234567" in result, "mecard should include TEL field")
    assert_true("EMAIL:jane@acme.com" in result, "mecard should include EMAIL field")


@th.unit_test("build_vcard missing name raises QRCodeError")
def test_build_vcard_missing_name(opts):
    try:
        build_vcard({"phone": "+15551234567"})
    except QRCodeError as exc:
        assert_true("name" in str(exc).lower(), f"error should mention name requirement, got: {exc}")
    else:
        assert_true(False, "missing name should raise QRCodeError")


@th.unit_test("build_vcard unknown format raises QRCodeError")
def test_build_vcard_unknown_format(opts):
    try:
        build_vcard({"name": "x"}, fmt="ical")
    except QRCodeError as exc:
        assert_true("vcard_format" in str(exc).lower() or "vcard" in str(exc).lower(), f"error should mention format, got: {exc}")
    else:
        assert_true(False, "unknown format should raise QRCodeError")


@th.unit_test("oversized logo rejected with QRCodeError")
def test_oversized_logo(opts):
    import base64 as _b64
    # 600 KB of data exceeds 512 KB cap
    oversized = _b64.b64encode(b"x" * (600 * 1024)).decode("ascii")
    try:
        generate_qrcode(data="test", logo=oversized)
    except QRCodeError as exc:
        assert_true("512" in str(exc) or "limit" in str(exc).lower(), f"error should mention the size limit, got: {exc}")
    else:
        assert_true(False, "oversized logo should raise QRCodeError")


@th.unit_test("build_vcard empty phone array emits no TEL lines")
def test_build_vcard_empty_array(opts):
    result = build_vcard({"name": "Jane Doe", "phone": []})
    assert_true("TEL:" not in result, f"empty phone array should not emit TEL, got: {result!r}")


@th.django_unit_test("REST: qrcode/vcard generates PNG for minimal vcard")
def test_rest_vcard_minimal(opts):
    resp = opts.client.post("/api/qrcode/vcard", {"vcard": {"name": "Jane Doe"}})
    assert_eq(resp.status_code, 200, f"vcard endpoint should return 200, got {resp.status_code}")
    headers = opts.client.last_response.headers
    content_type = headers.get("Content-Type") or headers.get("content-type") or ""
    assert_true(content_type.startswith("image/png"), f"default format should be PNG, got content-type: {content_type!r}, all headers: {dict(headers)}")


@th.django_unit_test("REST: qrcode/vcard missing vcard param returns error")
def test_rest_vcard_missing(opts):
    resp = opts.client.post("/api/qrcode/vcard", {})
    assert_true(resp.status_code >= 400, f"missing vcard should fail, got {resp.status_code}")


@th.django_unit_test("REST: qrcode/vcard missing name inside vcard returns 400")
def test_rest_vcard_missing_name(opts):
    resp = opts.client.post("/api/qrcode/vcard", {"vcard": {"phone": "+15551234567"}})
    assert_eq(resp.status_code, 400, f"missing vcard.name should return 400, got {resp.status_code}")


@th.django_unit_test("REST: qrcode/vcard base64 returns JSON with PNG content")
def test_rest_vcard_base64(opts):
    resp = opts.client.post("/api/qrcode/vcard", {
        "vcard": {"name": "Jane Doe", "email": "jane@acme.com"},
        "format": "base64",
    })
    assert_eq(resp.status_code, 200, f"vcard base64 should return 200, got {resp.status_code}")
    data = resp.response
    assert_eq(data.get("format"), "png", f"base64 default should be png, got: {data.get('format')}")
    assert_true(data.get("data") and len(data["data"]) > 0, "base64 data should be non-empty")


@th.django_unit_test("REST: qrcode/builder renders HTML page")
def test_rest_qrcode_builder(opts):
    resp = opts.client.get("/qrcode/builder")
    assert_eq(resp.status_code, 200, f"builder page should return 200, got {resp.status_code}")
    body = opts.client.last_response.body
    body_text = body if isinstance(body, str) else str(body)
    assert_true("QR Code Builder" in body_text, "builder page should contain title")
    assert_true("/api/qrcode/vcard" in body_text, "builder page should reference the vcard endpoint")


@th.django_unit_test("REST: qrcode/vcard mecard format generates successfully")
def test_rest_vcard_mecard(opts):
    resp = opts.client.post("/api/qrcode/vcard", {
        "vcard": {"name": "Jane Doe", "phone": "+15551234567"},
        "vcard_format": "mecard",
        "format": "base64",
    })
    assert_eq(resp.status_code, 200, f"mecard vcard should return 200, got {resp.status_code}")
    assert_true(len(resp.response.get("data", "")) > 0, "mecard base64 data should be non-empty")
