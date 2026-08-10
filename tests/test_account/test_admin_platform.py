"""Admin Platform/Advanced permission, settings, and packaging boundaries."""

from pathlib import Path
from unittest import mock

from testit import helpers as th


ROOT = Path(__file__).resolve().parents[2]


@th.django_unit_setup()
def setup_admin_platform(opts):
    from mojo.apps.account.models import User
    from mojo.apps.account.services import system_settings
    User.objects.filter(username__in=("platform-root", "platform-user")).delete()
    root = User.objects.create_user(
        email="platform-root@test.com", username="platform-root", password="example")
    root.is_active = True
    root.is_superuser = True
    root.save()
    system_settings.set_value(root, system_settings.AUTH_CONFIG, {})
    user = User.objects.create_user(
        email="platform-user@test.com", username="platform-user", password="example")
    user.is_active = True
    user.save()
    opts.platform_root = root.pk
    opts.platform_user = user.pk


@th.django_unit_test("AUTH_CONFIG is protected from generic save, rename and delete")
def test_auth_config_generic_protection(opts):
    from mojo import errors as me
    from mojo.apps.account.models import Setting, User
    from mojo.apps.account.services import system_settings
    root = User.objects.get(pk=opts.platform_root)
    system_settings.set_auth_safe_fields(root, {"theme.app_title": "Control"})
    row = Setting.objects.get(key="AUTH_CONFIG")
    row.value = "{}"
    with th.assert_raises(me.PermissionDeniedException):
        row.save()
    with th.assert_raises(me.PermissionDeniedException):
        row.delete()
    generic = Setting.objects.create(key="PLATFORM_TEST_GENERIC", value="ok")
    generic.key = "AUTH_CONFIG"
    with th.assert_raises(me.PermissionDeniedException):
        generic.save()


@th.django_unit_test("safe auth writer supports methods and preserves unknown keys")
def test_auth_safe_merge(opts):
    from mojo import errors as me
    from mojo.apps.account.models import Setting, User
    from mojo.apps.account.services import system_settings
    root = User.objects.get(pk=opts.platform_root)
    # Seed through the private dedicated service primitive to represent a
    # future AUTH_CONFIG key this version does not understand.
    system_settings.set_value(root, system_settings.AUTH_CONFIG, {
        "future": {"retained": True}, "login": {"methods": ["password", "passkey"]}})
    result = system_settings.set_auth_safe_fields(
        root, {
            "theme.app_title": "Platform", "theme.accent_color": "#112233",
            "login.methods": ["password", "passkey"],
            "registration.enabled": True,
            "registration.methods": ["password", "github"],
            "registration.passkey_prompt": "optional",
        })
    assert result["future"] == {"retained": True}
    assert result["login"]["methods"] == ["password", "passkey"]
    assert result["registration"] == {
        "enabled": True, "methods": ["password", "github"],
        "passkey_prompt": "optional",
    }
    with th.assert_raises(me.ValueException):
        system_settings.set_auth_safe_fields(root, {"login.methods": ["passkey"]})
    with th.assert_raises(me.ValueException):
        system_settings.set_auth_safe_fields(root, {
            "registration.enabled": True, "registration.methods": []})
    with th.assert_raises(me.ValueException):
        system_settings.set_auth_safe_fields(root, {
            "login.methods": ["password", "telepathy"]})
    with th.assert_raises(me.ValueException):
        system_settings.set_auth_safe_fields(root, {
            "registration.enabled": "yes"})
    disabled = system_settings.set_auth_safe_fields(root, {
        "registration.enabled": False, "registration.methods": []})
    assert disabled["registration"]["methods"] == []
    for path in (
            "theme.api_base", "theme.success_redirect",
            "theme.back_to_website_url", "theme.terms_url",
            "theme.custom_css_url"):
        with th.assert_raises(me.ValueException):
            system_settings.set_auth_safe_fields(root, {path: "https://unsafe.test"})
    assert Setting.objects.get(key="AUTH_CONFIG").is_secret is False


@th.django_unit_test("fleet collector performs one bounded Redis scan page")
def test_fleet_single_scan_page(opts):
    from mojo.apps.account.services import admin_platform
    redis = mock.Mock()
    redis.scan.return_value = (42, [])
    redis.pipeline.return_value.execute.return_value = []
    with mock.patch.object(admin_platform, "_bounded_redis", return_value=redis):
        result = admin_platform._fleet()
    redis.scan.assert_called_once()
    assert result["truncated"] is True
    assert not redis.scan_iter.called


@th.django_unit_test("fleet collector pipelines bounded heartbeat reads")
def test_fleet_pipeline(opts):
    from mojo.apps.account.services import admin_platform
    redis = mock.Mock()
    redis.scan.return_value = (0, ["jobs:runner:a", "jobs:runner:b"])
    pipe = redis.pipeline.return_value
    pipe.execute.return_value = [
        '{"runner_id":"a","channels":["edge"],"last_heartbeat":"now"}',
        '{"runner_id":"b","channels":["default"],"last_heartbeat":"now"}',
    ]
    with mock.patch.object(admin_platform, "_bounded_redis", return_value=redis):
        result = admin_platform._fleet()
    assert [row["runner"] for row in result["runners"]] == ["a"]
    assert pipe.get.call_count == 2
    assert not redis.get.called, "fleet heartbeat reads were sequential"


@th.django_unit_test("safe settings writer requires a live literal superuser")
def test_auth_writer_literal_superuser(opts):
    from mojo import errors as me
    from mojo.apps.account.models import User
    from mojo.apps.account.services import system_settings
    user = User.objects.get(pk=opts.platform_user)
    user.add_permission("manage_advanced")
    with th.assert_raises(me.PermissionDeniedException):
        system_settings.set_auth_safe_fields(user, {"theme.app_title": "Nope"})


@th.django_unit_test("denied sections do not execute their collector")
def test_denied_collector_not_run(opts):
    from mojo.apps.account.models import User
    from mojo.apps.account.services import admin_platform
    user = User.objects.get(pk=opts.platform_user)
    request = mock.Mock(user=user)
    collector = mock.Mock(side_effect=AssertionError("collector ran"))
    result = admin_platform._guarded(request, ("view_platform",), collector)
    assert result["status"] == "unauthorized"
    assert not collector.called


@th.django_unit_test("collector failures expose a stable reason, never provider text")
def test_collector_error_sanitized(opts):
    from mojo.apps.account.services import admin_platform
    result = admin_platform._collect(
        lambda: (_ for _ in ()).throw(RuntimeError("token=fixture-secret")))
    assert result["status"] == "unavailable"
    assert result["reason"] == "collector_unavailable"
    assert "fixture-secret" not in str(result)


@th.django_unit_test("Platform and Advanced assets remain feature-owned and previewed")
def test_platform_feature_package(opts):
    from mojo.apps.account.services import admin_assets
    assets = admin_assets.load_manifest()
    required = {
        "assets/features/platform/feature.js", "assets/features/platform/page.js",
        "assets/features/platform/styles.css", "assets/features/advanced/feature.js",
        "assets/features/advanced/page.js", "assets/features/advanced/styles.css",
    }
    assert required <= set(assets)
    platform = (ROOT / "mojo/apps/account/admin_portal/assets/features/platform/feature.js").read_text()
    advanced = (ROOT / "mojo/apps/account/admin_portal/assets/features/advanced/feature.js").read_text()
    preview = (ROOT / "bin/admin_preview_support/server.py").read_text()
    assert "deployments" in platform and "platformPage" in platform
    assert "advancedControlPage" in advanced and "domains" in advanced
    assert "features import activity, advanced, platform" in preview


@th.django_unit_test("all platform writes declare key denial and fixed fresh auth")
def test_platform_write_decorators(opts):
    from mojo.apps.account.rest import admin_platform as views
    funcs = (
        views.on_admin_platform_retry, views.on_admin_platform_verify,
        views.on_admin_platform_converge, views.on_admin_advanced_settings)
    for func in funcs:
        assert getattr(func, "_mojo_denies_key_backed_session", False), func.__name__
        assert getattr(func, "_mojo_requires_fresh_auth", False), func.__name__
        assert getattr(func, "_mojo_fresh_auth_seconds", None) == 600, func.__name__
