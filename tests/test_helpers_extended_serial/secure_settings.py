"""Secure-settings tests moved out of tests/test_helpers/secure_settings.py.

test_settings_helper_db_override assigns an attribute on django.conf.settings
and test_rest_create_setting writes a global Setting through the live server —
both visible to every parallel module — so they run only in the opt-in serial
tier (maestro item #1839).
"""
from testit import helpers as th


# ===========================================================================
# Setup
# ===========================================================================

TEST_USER = "settings_admin"
TEST_PWORD = "settings##mojo99"
TEST_EMAIL = "settings_admin@example.com"
OWNED_SETTING_KEYS = ("MY_FAKE_VAR", "REST_TEST")


@th.django_unit_setup()
def setup_secure_settings_serial(opts):
    from mojo.apps.account.models import User
    from mojo.apps.account.models.setting import Setting
    from mojo.decorators.limits import clear_rate_limits
    clear_rate_limits(ip="127.0.0.1")

    # Admin user with manage_settings permission
    user = User.objects.filter(email=TEST_EMAIL).last()
    if user is None:
        user = User(username=TEST_USER, email=TEST_EMAIL)
        user.save()
    user.is_active = True
    user.is_email_verified = True
    user.requires_mfa = False
    user.save_password(TEST_PWORD)
    user.add_permission("manage_settings")
    user.save()
    opts.user_id = user.pk

    # Clean up only this module's fixtures.
    Setting.objects.filter(key__in=OWNED_SETTING_KEYS, group=None).delete()
    r = Setting._redis()
    if r:
        r.hdel(Setting._redis_key(None), *OWNED_SETTING_KEYS)


# ===========================================================================
# SettingsHelper integration
# ===========================================================================

@th.django_unit_test()
def test_settings_helper_db_override(opts):
    """DB setting overrides django.conf.settings value."""
    from mojo.helpers.settings import settings
    from mojo.apps.account.models.setting import Setting
    from django.conf import settings as django_settings

    django_settings.MY_FAKE_VAR = True
    # DEBUG is True in django.conf.settings for testproject
    original = settings.get("MY_FAKE_VAR")
    assert original is True, f"Precondition: DEBUG should be True, got {original}"

    # Override via DB
    Setting.set("MY_FAKE_VAR", False)
    val = settings.get("MY_FAKE_VAR", kind="bool")
    assert val == False, f"DB setting should override, got {val}"

    Setting.set("MY_FAKE_VAR", True)
    val = settings.get("MY_FAKE_VAR", kind="bool")
    assert val == True, f"DB setting should override, got {val}"

    # Clean up — django.conf.settings should come back
    Setting.remove("MY_FAKE_VAR")
    val = settings.get("MY_FAKE_VAR")
    assert val is True, f"After removal, should fall back to django.conf, got {val}"


# ===========================================================================
# REST API tests
# ===========================================================================

@th.django_unit_test()
def test_rest_create_setting(opts):
    """POST /api/settings creates a setting."""
    from mojo.decorators.limits import clear_rate_limits
    clear_rate_limits(ip="127.0.0.1")

    opts.client.login(TEST_USER, TEST_PWORD)
    resp = opts.client.post("/api/settings", {
        "key": "REST_TEST",
        "value": "rest_value",
        "is_secret": False,
    })
    opts.client.logout()
    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}"
    data = resp.json
    assert data.get("status") is True, f"Expected status=true, got {data}"

    from mojo.apps.account.models.setting import Setting
    s = Setting.objects.filter(key="REST_TEST", group=None).first()
    assert s is not None, "Setting should exist in DB"
    assert s.get_value() == "rest_value", f"Expected 'rest_value', got {s.get_value()}"

    Setting.remove("REST_TEST")
