"""Tests for the settings-free scalar text redactor."""

from testit import helpers as th


@th.tier("core")
@th.django_unit_test()
def test_safe_text_redacts_secret_shapes_and_url_queries(opts):
    from mojo.helpers.safe_text import REDACTED, sanitize_scalar

    opaque = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/"
    sensitive = (
        "credential=my-password",
        "AKIAIOSFODNN7EXAMPLE",
        "eyJhbGciOiJIUzI1NiJ9.eyJ1c2VyIjoiMSJ9.abcdefghijklmnop",
        "Bearer abcdefghijklmnopqrstuvwxyz012345",
        "-----BEGIN PRIVATE KEY-----",
        opaque,
    )
    for value in sensitive:
        th.assert_eq(sanitize_scalar(value), REDACTED,
                     f"the scalar helper did not redact secret-shaped input: {value!r}")

    sanitized_url = sanitize_scalar(
        "https://user:pass@example.com:443/rpm?X-Amz-Signature=private#fragment")
    th.assert_eq(sanitized_url, "https://example.com/rpm?redacted",
                 "URL sanitization must remove userinfo, query values, and fragments")


@th.tier("core")
@th.django_unit_test()
def test_safe_text_applies_exact_utf8_input_and_output_bounds(opts):
    from mojo.helpers.safe_text import TRUNCATED, sanitize_scalar

    bounded = sanitize_scalar("\u754c" * 100, max_input_characters=100, max_bytes=40)
    th.assert_true(len(bounded.encode("utf-8")) <= 40,
                   f"the scalar helper exceeded its exact UTF-8 cap: {bounded!r}")
    th.assert_true(bounded.endswith(TRUNCATED),
                   f"a bounded scalar omitted its truncation marker: {bounded!r}")
    th.assert_eq(sanitize_scalar("x" * 101, max_input_characters=100), TRUNCATED,
                 "input beyond the pre-regex character cap must truncate immediately")
