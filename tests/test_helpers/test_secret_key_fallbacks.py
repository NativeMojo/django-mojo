"""
secret_keys() accessor — SECRET_KEY + SECRET_KEY_FALLBACKS normalization.

Only pure/no-patch tests live here: this package runs in parallel with every
other module in ONE process, so patching shared key state from here would
race them. Rotation behavior is exercised in tests/test_secret_rotation/
(bouncer, serial) and tests/test_filevault/5_test_key_rotation.py (filevault).
"""

from testit import helpers as th
from testit.helpers import assert_eq, assert_true


@th.unit_test("build_key_list: no fallbacks -> [primary]")
def test_no_fallbacks(opts):
    from mojo.helpers.crypto.keys import build_key_list

    assert_eq(build_key_list("primary", []), ["primary"],
              "empty fallback list must yield just the primary")
    assert_eq(build_key_list("primary", None), ["primary"],
              "None fallbacks must yield just the primary")


@th.unit_test("build_key_list: fallbacks follow the primary in order")
def test_fallback_order(opts):
    from mojo.helpers.crypto.keys import build_key_list

    assert_eq(build_key_list("primary", ["old1", "old2"]),
              ["primary", "old1", "old2"],
              "candidates must be primary first, then fallbacks in listed order")
    assert_eq(build_key_list("primary", ("old1", "old2")),
              ["primary", "old1", "old2"],
              "a tuple of fallbacks must work the same as a list")


@th.unit_test("build_key_list: duplicates are collapsed, order preserved")
def test_dedupe(opts):
    from mojo.helpers.crypto.keys import build_key_list

    assert_eq(build_key_list("primary", ["primary", "old1", "old1"]),
              ["primary", "old1"],
              "the primary repeated in fallbacks and duplicate fallbacks must be de-duped")


@th.unit_test("build_key_list: empty and non-string fallback entries are dropped")
def test_drops_garbage(opts):
    from mojo.helpers.crypto.keys import build_key_list

    assert_eq(build_key_list("primary", ["", None, 123, "old1"]),
              ["primary", "old1"],
              "empty, None, and non-string entries must be dropped, valid ones kept")


@th.unit_test("build_key_list: a bare string is ONE key, never char-iterated")
def test_bare_string_footgun(opts):
    from mojo.helpers.crypto.keys import build_key_list

    result = build_key_list("primary", "oldkeystring")
    assert_eq(result, ["primary", "oldkeystring"],
              f"a bare-string fallback value must become one candidate, got {result!r}")
    assert_true("o" not in result[1:] or result[1] == "oldkeystring",
                "the string must never be iterated character by character")


@th.unit_test("build_key_list: the primary keeps index 0 even when empty")
def test_empty_primary_kept(opts):
    from mojo.helpers.crypto.keys import build_key_list

    assert_eq(build_key_list("", ["old1"]), ["", "old1"],
              "an empty primary stays at index 0 — sign/wrap callers read [0] as-is")
    assert_eq(build_key_list(None, None), [""],
              "a missing primary normalizes to empty string at index 0")


@th.django_unit_test("secret_keys() live: primary is the file-based SECRET_KEY")
def test_live_secret_keys(opts):
    from mojo.helpers.crypto import keys as crypto_keys
    from mojo.helpers.settings import settings

    result = crypto_keys.secret_keys()
    assert_true(len(result) >= 1,
                f"secret_keys() must never be empty, got {result!r}")
    assert_eq(result[0], settings.SECRET_KEY,
              "index 0 must be exactly settings.SECRET_KEY (the static, file-based read)")
    expected = crypto_keys.build_key_list(
        settings.get_static("SECRET_KEY", ""),
        settings.get_static("SECRET_KEY_FALLBACKS", []))
    assert_eq(result, expected,
              "secret_keys() must be the normalized view of the static settings pair")


@th.unit_test("secret_keys is exported from mojo.helpers.crypto")
def test_package_export(opts):
    from mojo.helpers.crypto import secret_keys
    from mojo.helpers.crypto import keys as crypto_keys

    assert_true(secret_keys is crypto_keys.secret_keys,
                "the package-level export must be the keys module function")
