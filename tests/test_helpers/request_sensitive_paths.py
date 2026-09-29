"""Maestro item #5960 — MOJO_SENSITIVE_BODY_PATHS validation and matching.

Pure functions with explicit values: no settings are changed here. The
settings-driven checks live in
tests/test_account_admin_extended_serial/test_sensitive_body_paths.py.
"""
from testit import helpers as th
from testit.helpers import assert_true, assert_eq

BASE = "/api/test_host_sensitive"


@th.django_unit_test("host_sensitive_paths accepts valid entries and strips a trailing slash")
def test_valid_entries(opts):
    from mojo.helpers.request import host_sensitive_paths

    assert_eq(host_sensitive_paths([]), (), "an empty list means no host paths")
    assert_eq(host_sensitive_paths(None), (), "None means no host paths")
    assert_eq(host_sensitive_paths(["/api/payments/webhooks/", "/hooks.v1/a_b~c-d"]),
              ("/api/payments/webhooks", "/hooks.v1/a_b~c-d"),
              "valid entries are returned with the trailing slash stripped")
    assert_eq(host_sensitive_paths(("/",)), ("",), "the literal '/' means every path")


@th.django_unit_test("host_sensitive_paths rejects every bad shape with ImproperlyConfigured")
def test_rejected_entries(opts):
    from django.core.exceptions import ImproperlyConfigured
    from mojo.helpers.request import host_sensitive_paths

    bad_values = [
        "/api/payments",            # bare string, not a list
        [42],                       # non-string entry
        ["api/payments"],           # no leading slash
        [""],                       # empty
        ["//"],                     # empty segments
        ["///"],                    # must not silently mean "all paths"
        ["/api//payments"],
        ["/api/./payments"],
        ["/api/../payments"],
        ["/api/pay*"],
        ["/api/pay?x=1"],
        ["/api/pay#frag"],
        ["/api/pay ments"],
        ["/api/payments\n"],        # trailing newline
        ["/api/pay\x00"],
        ["/" + "a" * 256],          # 257 characters
    ]
    for value in bad_values:
        try:
            host_sensitive_paths(value)
        except ImproperlyConfigured as err:
            assert_true("MOJO_SENSITIVE_BODY_PATHS" in str(err),
                        f"the error for {value!r} must name the setting, got {err}")
        else:
            raise AssertionError(f"{value!r} must be rejected")


@th.django_unit_test("host_label matches by whole segment, and '/' matches every path")
def test_host_label(opts):
    from mojo.helpers.request import host_label

    prefixes = (BASE,)
    assert_eq(host_label(BASE, prefixes), "host_sensitive", "the prefix itself matches")
    assert_eq(host_label((BASE + "/").rstrip("/"), prefixes), "host_sensitive",
              "a trailing slash (after rstrip) matches")
    assert_eq(host_label(BASE + "/cb", prefixes), "host_sensitive", "a sub-path matches")
    assert_eq(host_label(BASE + "_x", prefixes), None, "a longer segment must not match")
    assert_eq(host_label("/api/other", prefixes), None, "an unrelated path must not match")
    assert_eq(host_label("/anything/at/all", ("",)), "host_sensitive", "'/' matches every path")
    assert_eq(host_label(BASE, ()), None, "no prefixes means no match")
