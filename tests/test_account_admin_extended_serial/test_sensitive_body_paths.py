"""Maestro item #5960 — MOJO_SENSITIVE_BODY_PATHS read from django.conf.settings.

These override django.conf.settings in-process, so they live in this serial
package, not the default test_helpers / test_models ones.
"""

from contextlib import contextmanager

from testit import helpers as th
from testit.helpers import assert_true, assert_eq

BASE = "/api/test_host_sensitive"


@contextmanager
def _override_setting(name, value):
    """In-process Django settings override (th.server_settings only affects the
    separate server process; override_settings is banned by testing rules)."""
    import django.conf
    sentinel = object()
    original = getattr(django.conf.settings, name, sentinel)
    setattr(django.conf.settings, name, value)
    try:
        yield
    finally:
        if original is sentinel:
            delattr(django.conf.settings, name)
        else:
            setattr(django.conf.settings, name, original)


def _request(path, method="POST"):
    import objict
    req = objict.objict()
    req.path = path
    req.method = method
    return req


@th.django_unit_test("sensitive_body_label and is_host_sensitive honour MOJO_SENSITIVE_BODY_PATHS")
def test_setting_drives_label_and_host_check(opts):
    from mojo.helpers.request import sensitive_body_label, is_host_sensitive, API_ROOT

    auth_path = f"{API_ROOT}/auth/x"
    with _override_setting("MOJO_SENSITIVE_BODY_PATHS", [BASE]):
        assert_eq(sensitive_body_label(_request(BASE + "/cb")), "host_sensitive",
                  "a request under a listed host path must get the host_sensitive label")
        assert_eq(sensitive_body_label(_request("/api/test_not_listed")), None,
                  "a request outside every listed path must get no label")
        assert_eq(sensitive_body_label(_request(auth_path)), "account_auth",
                  "a framework label must win over the host rule")
        assert_true(is_host_sensitive(_request(BASE + "/cb")) is True,
                    "is_host_sensitive must be true under a listed host path")
        assert_true(is_host_sensitive(_request(auth_path)) is False,
                    "a framework label alone must not make a request host-sensitive")

    with _override_setting("MOJO_SENSITIVE_BODY_PATHS", [auth_path]):
        assert_eq(sensitive_body_label(_request(auth_path)), "account_auth",
                  "a host prefix must not rename a framework label")
        assert_true(is_host_sensitive(_request(auth_path)) is True,
                    "a listed host prefix under a framework-labelled path must still be host-sensitive")


@th.django_unit_test("a bad MOJO_SENSITIVE_BODY_PATHS fails the startup check and fails closed per request")
def test_bad_setting_stops_startup_and_fails_closed(opts):
    from django.apps import apps
    from django.core.exceptions import ImproperlyConfigured
    from mojo.helpers.request import sensitive_body_label, is_host_sensitive

    for bad_value in (["no-slash"], None):
        with _override_setting("MOJO_SENSITIVE_BODY_PATHS", bad_value):
            try:
                apps.get_app_config("account")._check_sensitive_body_paths()
            except ImproperlyConfigured as err:
                assert_true("MOJO_SENSITIVE_BODY_PATHS" in str(err),
                            f"the startup error for {bad_value!r} must name the setting, got {err}")
            else:
                raise AssertionError(f"MOJO_SENSITIVE_BODY_PATHS={bad_value!r} must stop startup")
            assert_eq(sensitive_body_label(_request("/api/test_not_listed")), "host_sensitive",
                      f"at call time {bad_value!r} must mask every path, not raise")
            assert_true(is_host_sensitive(_request("/api/test_not_listed")) is True,
                        f"at call time {bad_value!r} must make every request host-sensitive")
