from testit import helpers as th


# ===========================================================================
# Setup
# ===========================================================================

TEST_USER = "settings_admin"
TEST_PWORD = "settings##mojo99"
TEST_EMAIL = "settings_admin@example.com"


@th.django_unit_setup()
def setup_secure_settings(opts):
    from mojo.apps.account.models import User, Group
    from mojo.apps.account.models.setting import Setting
    from mojo.decorators.limits import clear_rate_limits
    clear_rate_limits(ip="127.0.0.1")

    # Admin user with manage_settings permission
    user = User.objects.filter(email=TEST_EMAIL).last()
    if user is None:
        user = User(username=TEST_USER, email=TEST_EMAIL)
        user.save()
    user.is_active = True
    user.requires_mfa = False
    user.save_password(TEST_PWORD)
    user.add_permission("manage_settings")
    user.save()
    opts.user_id = user.pk

    # Parent group -> child group hierarchy for scoped tests
    parent = Group.objects.filter(name="settings_parent").last()
    if parent is None:
        parent = Group(name="settings_parent", kind="organization")
        parent.save()
    opts.parent_group_id = parent.pk

    child = Group.objects.filter(name="settings_child").last()
    if child is None:
        child = Group(name="settings_child", kind="team", parent=parent)
        child.save()
    else:
        child.parent = parent
        child.save(update_fields=["parent"])
    opts.child_group_id = child.pk

    # Clean up any leftover settings
    Setting.objects.all().delete()
    r = Setting._redis()
    if r:
        for key in r.scan_iter("settings:*"):
            r.delete(key)


# ===========================================================================
# Model tests
# ===========================================================================

@th.django_unit_test()
def test_setting_create_plain(opts):
    """Plain (non-secret) setting stored in value field."""
    from mojo.apps.account.models.setting import Setting

    s = Setting.set("SITE_NAME", "My App")
    assert s.pk is not None, "Setting must be saved"
    assert s.value == "My App", f"Expected 'My App', got {s.value}"
    assert s.is_secret is False, "Should not be secret"
    assert s.get_value() == "My App", f"get_value() should return 'My App'"

    # Clean up
    Setting.remove("SITE_NAME")


@th.django_unit_test()
def test_setting_create_secret(opts):
    """Secret setting stored encrypted in mojo_secrets, value field empty."""
    from mojo.apps.account.models.setting import Setting

    s = Setting.set("API_KEY", "sk-abc123secret", is_secret=True)
    assert s.is_secret is True, "Should be secret"
    assert s.value == "", "Plain value field must be empty for secrets"
    assert s.get_value() == "sk-abc123secret", f"Decrypted value must match"

    # Verify raw DB field is encrypted (not plaintext)
    raw = Setting.objects.filter(pk=s.pk).values_list("mojo_secrets", flat=True).first()
    assert raw is not None, "mojo_secrets must be set"
    assert "sk-abc123secret" not in (raw or ""), "Secret must not appear in plaintext"

    # display_value must be masked
    assert s.display_value == "******", f"display_value must mask secret, got {s.display_value}"

    Setting.remove("API_KEY")


@th.django_unit_test()
def test_setting_update(opts):
    """Updating a setting replaces the value and refreshes cache."""
    from mojo.apps.account.models.setting import Setting

    Setting.set("COLOR", "blue")
    s = Setting.set("COLOR", "red")
    assert s.get_value() == "red", f"Expected 'red', got {s.get_value()}"

    # Cache should also reflect the update
    val, found = Setting.get_cached("COLOR")
    assert found, "Should be in cache"
    assert val == "red", f"Cache should be 'red', got {val}"

    Setting.remove("COLOR")


@th.django_unit_test()
def test_setting_delete(opts):
    """Deleting removes from DB and Redis cache."""
    from mojo.apps.account.models.setting import Setting

    Setting.set("TEMP", "gone_soon")
    removed = Setting.remove("TEMP")
    assert removed is True, "remove() should return True"

    val, found = Setting.get_cached("TEMP")
    assert not found, "Should not be in cache after delete"

    assert Setting.objects.filter(key="TEMP", group=None).count() == 0, "Should not be in DB"


# ===========================================================================
# Redis cache tests
# ===========================================================================

@th.django_unit_test()
def test_setting_push_to_redis(opts):
    """Setting.set() pushes value to Redis immediately."""
    from mojo.apps.account.models.setting import Setting

    Setting.set("CACHED_KEY", "cached_value")
    val, found = Setting.get_cached("CACHED_KEY")
    assert found, "Must be in Redis"
    assert val == "cached_value", f"Expected 'cached_value', got {val}"

    Setting.remove("CACHED_KEY")


@th.django_unit_test()
def test_setting_warm_cache(opts):
    """warm_cache loads all settings for a scope into Redis."""
    from mojo.apps.account.models.setting import Setting

    Setting.set("W1", "one")
    Setting.set("W2", "two")

    # Clear Redis, verify gone
    r = Setting._redis()
    r.delete(Setting._redis_key())
    _, found = Setting.get_cached("W1")
    assert not found, "Should be gone from cache"

    # Warm and verify
    Setting.warm_cache()
    val, found = Setting.get_cached("W1")
    assert found and val == "one", f"Expected 'one', got {val}"
    val, found = Setting.get_cached("W2")
    assert found and val == "two", f"Expected 'two', got {val}"

    Setting.remove("W1")
    Setting.remove("W2")


# ===========================================================================
# Group scoping + parent chain tests
# ===========================================================================

@th.django_unit_test()
def test_setting_group_scoped(opts):
    """Group-scoped setting is separate from global."""
    from mojo.apps.account.models import Group
    from mojo.apps.account.models.setting import Setting

    group = Group.objects.get(pk=opts.parent_group_id)

    Setting.set("FEATURE_FLAG", "global_val")
    Setting.set("FEATURE_FLAG", "group_val", group=group)

    # Resolve with group should return group value
    val = Setting.resolve("FEATURE_FLAG", group=group)
    assert val == "group_val", f"Expected 'group_val', got {val}"

    # Resolve without group should return global
    val = Setting.resolve("FEATURE_FLAG")
    assert val == "global_val", f"Expected 'global_val', got {val}"

    Setting.remove("FEATURE_FLAG")
    Setting.remove("FEATURE_FLAG", group=group)


@th.django_unit_test()
def test_setting_parent_chain_fallback(opts):
    """Child group falls back to parent group, then to global."""
    from mojo.apps.account.models import Group
    from mojo.apps.account.models.setting import Setting

    parent = Group.objects.get(pk=opts.parent_group_id)
    child = Group.objects.get(pk=opts.child_group_id)

    # Only set on parent
    Setting.set("PARENT_ONLY", "from_parent", group=parent)

    # Child should inherit from parent
    val = Setting.resolve("PARENT_ONLY", group=child)
    assert val == "from_parent", f"Child should inherit from parent, got {val}"

    # Child override should win
    Setting.set("PARENT_ONLY", "from_child", group=child)
    val = Setting.resolve("PARENT_ONLY", group=child)
    assert val == "from_child", f"Child override should win, got {val}"

    Setting.remove("PARENT_ONLY", group=parent)
    Setting.remove("PARENT_ONLY", group=child)


@th.django_unit_test()
def test_setting_global_fallback(opts):
    """Group with no setting falls back to global."""
    from mojo.apps.account.models import Group
    from mojo.apps.account.models.setting import Setting

    child = Group.objects.get(pk=opts.child_group_id)

    Setting.set("GLOBAL_ONLY", "from_global")

    val = Setting.resolve("GLOBAL_ONLY", group=child)
    assert val == "from_global", f"Should fall back to global, got {val}"

    Setting.remove("GLOBAL_ONLY")


# ===========================================================================
# SettingsHelper integration
# ===========================================================================

@th.django_unit_test()
def test_settings_helper_db_override(opts):
    """DB setting overrides django.conf.settings value."""
    from mojo.helpers.settings import settings
    from mojo.apps.account.models.setting import Setting

    # DEBUG is True in django.conf.settings for testproject
    original = settings.get("DEBUG")
    assert original is True, f"Precondition: DEBUG should be True, got {original}"

    # Override via DB
    Setting.set("DEBUG", "False")
    val = settings.get("DEBUG")
    assert val == "False", f"DB setting should override, got {val}"

    # Clean up — django.conf.settings should come back
    Setting.remove("DEBUG")
    val = settings.get("DEBUG")
    assert val is True, f"After removal, should fall back to django.conf, got {val}"


@th.django_unit_test()
def test_settings_helper_with_group(opts):
    """settings.get(name, group=group) resolves via group chain."""
    from mojo.helpers.settings import settings
    from mojo.apps.account.models import Group
    from mojo.apps.account.models.setting import Setting

    group = Group.objects.get(pk=opts.parent_group_id)

    Setting.set("CUSTOM_FLAG", "group_value", group=group)
    val = settings.get("CUSTOM_FLAG", group=group)
    assert val == "group_value", f"Expected 'group_value', got {val}"

    # Without group, not found in DB, falls through to django.conf (not set there either)
    val = settings.get("CUSTOM_FLAG", default="nope")
    assert val == "nope", f"Expected 'nope' (default), got {val}"

    Setting.remove("CUSTOM_FLAG", group=group)


@th.django_unit_test()
def test_settings_helper_secret_transparent(opts):
    """Secret settings are decrypted transparently via settings.get()."""
    from mojo.helpers.settings import settings
    from mojo.apps.account.models.setting import Setting

    Setting.set("SECRET_TOKEN", "super-secret-value", is_secret=True)
    val = settings.get("SECRET_TOKEN")
    assert val == "super-secret-value", f"Expected 'super-secret-value', got {val}"

    Setting.remove("SECRET_TOKEN")


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


@th.django_unit_test()
def test_rest_list_settings(opts):
    """GET /api/settings returns settings list with masked secrets."""
    from mojo.apps.account.models.setting import Setting
    from mojo.decorators.limits import clear_rate_limits
    clear_rate_limits(ip="127.0.0.1")

    Setting.set("LIST_PLAIN", "visible")
    Setting.set("LIST_SECRET", "hidden_val", is_secret=True)

    opts.client.login(TEST_USER, TEST_PWORD)
    resp = opts.client.get("/api/settings")
    opts.client.logout()
    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}"

    items = resp.json.get("data", [])
    plain = next((i for i in items if i.get("key") == "LIST_PLAIN"), None)
    secret = next((i for i in items if i.get("key") == "LIST_SECRET"), None)

    assert plain is not None, "LIST_PLAIN should be in response"
    assert secret is not None, "LIST_SECRET should be in response"
    assert secret.get("display_value") == "******", f"Secret display_value must be masked, got {secret.get('display_value')}"
    assert "hidden_val" not in str(resp.json), "Secret value must not appear anywhere in response"

    Setting.remove("LIST_PLAIN")
    Setting.remove("LIST_SECRET")


@th.django_unit_test()
def test_rest_requires_permission(opts):
    """Unauthenticated and unprivileged users cannot access settings."""
    from mojo.decorators.limits import clear_rate_limits
    clear_rate_limits(ip="127.0.0.1")

    # Unauthenticated
    opts.client.logout()
    resp = opts.client.get("/api/settings")
    assert resp.status_code in (401, 403), f"Unauthenticated should be blocked, got {resp.status_code}"
