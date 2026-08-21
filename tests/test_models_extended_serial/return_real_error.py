"""Maestro item 51 — LOGIT_RETURN_REAL_ERROR is read file-only.

Moved out of tests/test_models/return_real_error.py (maestro item #1839): this
test mutates process-global django.conf.settings via setattr/delattr, which is
unsafe under the parallel default tier. The rest of the dispatcher 500-handler
coverage stays in the source module.
"""
from testit import helpers as th
from testit.helpers import assert_true


@th.django_unit_test("a DB Setting row cannot re-enable error leakage")
def test_db_setting_cannot_flip_the_flag(opts):
    """The flag is read with get_static, so the DB/Redis settings plane is not
    consulted — a Setting row must not be able to turn leakage back on for a
    deployment that disabled it in its settings file."""
    from django.conf import settings as dj_settings
    from mojo.apps.account.models.setting import Setting
    from mojo.decorators import http as http_decorators

    sentinel = object()
    orig = getattr(dj_settings, "LOGIT_RETURN_REAL_ERROR", sentinel)
    try:
        setattr(dj_settings, "LOGIT_RETURN_REAL_ERROR", False)
        Setting.set("LOGIT_RETURN_REAL_ERROR", "1")

        assert_true(http_decorators._return_real_error() is False,
                    "a DB Setting row must not override the file's False — the flag is "
                    "read file-only so leakage cannot be re-enabled at runtime")
    finally:
        Setting.remove("LOGIT_RETURN_REAL_ERROR")
        if orig is sentinel:
            if hasattr(dj_settings, "LOGIT_RETURN_REAL_ERROR"):
                delattr(dj_settings, "LOGIT_RETURN_REAL_ERROR")
        else:
            setattr(dj_settings, "LOGIT_RETURN_REAL_ERROR", orig)
