"""Maestro item 50 — AUTH_PHONE_VERIFY_DEV_BYPASS_CODE is FILE-ONLY.

The key arms a full phone-verification bypass: when set, verify_code() accepts
that fixed code in addition to the real generated one. It was read with
settings.get, which is DB/Redis-first, so a single global Setting row —
writable through the generic settings REST surface or a direct Redis HSET —
could arm an authentication bypass at runtime on a production deployment.

Worse, the startup warning in mojo/apps/account/apps.py reads the same key via
settings.get_static, so a DB-armed bypass produced no boot warning at all: the
one signal an operator had was blind to the very configuration that mattered.

The fix reads it with settings.get_static. These tests write a REAL global
Setting row and assert it is ignored, then assert the file-backed value still
works so dev ergonomics are preserved. Style mirrors
tests/test_geofence/test_override_file_only.py (the DM-031 precedent for the
same class of bug); django.conf overrides use direct setattr + try/finally,
NOT th.server_settings — these are in-process reads.
"""
from testit import helpers as th
from testit.helpers import assert_true, assert_eq

KEY = "AUTH_PHONE_VERIFY_DEV_BYPASS_CODE"
DB_CODE = "111111"      # planted in the DB/Redis settings plane — must be ignored
FILE_CODE = "222222"    # planted in django.conf — must still work
TEST_PHONE = "+15555550150"
_SENTINEL = object()


def _save_conf(name):
    from django.conf import settings as dj_settings
    return getattr(dj_settings, name, _SENTINEL)


def _restore_conf(name, orig):
    from django.conf import settings as dj_settings
    if orig is _SENTINEL:
        if hasattr(dj_settings, name):
            delattr(dj_settings, name)
    else:
        setattr(dj_settings, name, orig)


@th.django_unit_setup()
def setup_bypass_file_only(opts):
    """Clear leftovers so the DB/file distinction is unambiguous."""
    from mojo.apps.account.models.setting import Setting
    Setting.remove(KEY)


@th.django_unit_test("a DB Setting row cannot arm the phone-verify bypass (THE regression)")
def test_db_setting_does_not_arm_bypass(opts):
    from mojo.apps.account.models.setting import Setting
    from mojo.apps.account.services import phone_register
    import mojo.errors as merrors

    orig = _save_conf(KEY)
    try:
        # Nothing in the file, everything in the DB/Redis plane.
        _restore_conf(KEY, _SENTINEL)
        Setting.set(KEY, DB_CODE)

        assert_true(
            phone_register._dev_bypass_code() is None,
            "a DB/Redis Setting row must NOT arm the bypass — it is file-only config",
        )

        session_token, real_code, _ttl = phone_register.start(TEST_PHONE)
        try:
            phone_register.verify_code(session_token, DB_CODE)
            raise AssertionError(
                "verify_code accepted a DB-planted bypass code — a Setting row "
                "can arm a full phone-verification bypass"
            )
        except merrors.ValueException:
            pass  # correct: the DB value is not a bypass

        # The genuine code must still verify on the same session (the wrong-code
        # attempt above must not have consumed it).
        _tok, phone, _ttl2 = phone_register.verify_code(session_token, real_code)
        assert_eq(phone, TEST_PHONE, f"the real code must still verify, got phone={phone!r}")
    finally:
        Setting.remove(KEY)
        _restore_conf(KEY, orig)


@th.django_unit_test("a file-configured bypass code still works (dev ergonomics preserved)")
def test_file_setting_still_arms_bypass(opts):
    from mojo.apps.account.models.setting import Setting
    from mojo.apps.account.services import phone_register

    orig = _save_conf(KEY)
    try:
        Setting.remove(KEY)
        _restore_conf(KEY, FILE_CODE)

        assert_eq(
            phone_register._dev_bypass_code(), FILE_CODE,
            "a file-configured bypass code must still arm the bypass",
        )

        session_token, _real_code, _ttl = phone_register.start(TEST_PHONE)
        _tok, phone, _ttl2 = phone_register.verify_code(session_token, FILE_CODE)
        assert_eq(phone, TEST_PHONE, f"the file bypass code must verify, got phone={phone!r}")
    finally:
        _restore_conf(KEY, orig)


@th.django_unit_test("the file value wins even when a DB row also exists")
def test_file_wins_over_db_row(opts):
    from mojo.apps.account.models.setting import Setting
    from mojo.apps.account.services import phone_register
    import mojo.errors as merrors

    orig = _save_conf(KEY)
    try:
        _restore_conf(KEY, FILE_CODE)
        Setting.set(KEY, DB_CODE)

        assert_eq(
            phone_register._dev_bypass_code(), FILE_CODE,
            "the file value must win — the DB plane is not consulted at all",
        )

        session_token, _real_code, _ttl = phone_register.start(TEST_PHONE)
        try:
            phone_register.verify_code(session_token, DB_CODE)
            raise AssertionError(
                "the DB-planted code was accepted even with a file value present"
            )
        except merrors.ValueException:
            pass

        _tok, phone, _ttl2 = phone_register.verify_code(session_token, FILE_CODE)
        assert_eq(phone, TEST_PHONE, f"the file bypass code must verify, got phone={phone!r}")
    finally:
        Setting.remove(KEY)
        _restore_conf(KEY, orig)
