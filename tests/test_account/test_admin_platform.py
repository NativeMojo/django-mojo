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
    system_settings.set_value(root, system_settings.AUTH_CONFIG, {})


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
    system_settings.set_value(root, system_settings.AUTH_CONFIG, {})


@th.django_unit_test("fleet collector uses the exact bounded edge registry")
def test_fleet_exact_bounded_registry(opts):
    from mojo.apps.account.services import admin_platform
    with mock.patch("mojo.apps.jobs.get_runners_bounded", return_value=[]) as roster:
        result = admin_platform._fleet()
    roster.assert_called_once_with("edge", limit=100, timeout=1.0)
    assert result["runners"] == []
    assert result["truncated"] is False, \
        "the exact registry was incorrectly described as a partial SCAN page"


@th.django_unit_test("fleet collector preserves the exact live edge roster")
def test_fleet_roster_projection(opts):
    from mojo.apps.account.services import admin_platform
    rows = [
        {"runner_id": "mv1-engine", "channels": ["edge", "default"],
         "last_heartbeat": "2026-08-11T14:55:22+00:00"},
        {"runner_id": "mv2-engine", "channels": ["edge"],
         "last_heartbeat": "2026-08-11T14:55:25+00:00"},
    ]
    with mock.patch("mojo.apps.jobs.get_runners_bounded", return_value=rows):
        result = admin_platform._fleet()
    assert [row["runner"] for row in result["runners"]] == [
        "mv1-engine", "mv2-engine"], \
        "the Admin fleet rollup dropped a live edge runner"
    assert result["_collector_status"] == "healthy"


@th.django_unit_test("ignored incidents are excluded from every open rollup")
def test_open_incident_predicate(opts):
    from mojo.apps.account.services import admin_platform
    from mojo.apps.incident.models import Incident
    category = "admin-platform-open-contract"
    Incident.objects.filter(category=category).delete()
    for status in ("new", "investigating", "ignored", "resolved", "closed"):
        Incident.objects.create(category=category, status=status)
    statuses = set(admin_platform._open_incidents().filter(
        category=category).values_list("status", flat=True))
    Incident.objects.filter(category=category).delete()
    assert statuses == {"new", "investigating"}, \
        f"terminal incidents leaked into the open predicate: {statuses!r}"


@th.django_unit_test("WebApp current health is independent from onboarding history")
def test_webapp_current_health_axes(opts):
    from mojo.apps.account.services import admin_platform
    healthy_not_started = [{
        "onboarding": {"status": "not_started"},
        "deployment_key": {"active": False},
        "current_health": {"status": "healthy"},
    }]
    unknown_succeeded = [{
        "onboarding": {"status": "succeeded"},
        "deployment_key": {"active": True},
        "current_health": {"status": "unknown"},
    }]
    assert admin_platform._webapp_collector_status(healthy_not_started) == "healthy", \
        "historical onboarding incorrectly degraded current public health"
    assert admin_platform._webapp_collector_status(unknown_succeeded) == "degraded", \
        "historical success incorrectly made unknown current health green"
    assert admin_platform._webapp_collector_status(
        healthy_not_started, truncated=True) == "degraded", \
        "a capped WebApp collector can still report unprobed rows as green"


@th.django_unit_test("WebApp probe uses the pinned no-redirect public collector")
def test_webapp_current_probe_contract(opts):
    from mojo.apps.account.services import admin_platform
    from mojo.apps.edge.services import public_probe
    summary = {"address": {"https_origin": "https://app.example.com"}}
    with mock.patch.object(public_probe, "probe_https_root", return_value={
            "ok": True, "status": 200, "redirected": False}) as probe:
        result = admin_platform._probe_webapp(summary)
    probe.assert_called_once_with(
        "https://app.example.com", timeout=1.5, max_body=65536)
    assert result["status"] == "healthy" and result["http_status"] == 200, \
        f"current WebApp proof was not freshness-bearing health: {result!r}"
    assert result.get("observed_at") and result.get("stale_after"), \
        f"current WebApp proof omitted freshness bounds: {result!r}"


@th.django_unit_test("security evidence names each disabled secure control")
def test_security_disabled_posture(opts):
    from mojo.apps.account.services import admin_platform
    values = {
        "SECURE_SSL_REDIRECT": False, "SESSION_COOKIE_SECURE": True,
        "CSRF_COOKIE_SECURE": False, "SECURE_HSTS_SECONDS": 0,
    }
    with mock.patch.object(admin_platform.settings, "get_static",
                           side_effect=lambda key, default=None: values.get(key, default)):
        posture = admin_platform._secure_posture()
    assert posture["disabled"] == ["https_redirect", "csrf_cookie_secure", "hsts"], \
        f"disabled security controls were not explicit: {posture!r}"
    assert set(posture["controls"].values()) <= {True, False}, \
        f"security posture exposed values beyond booleans: {posture!r}"


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


@th.django_unit_test("the framework hold is written by the typed owner writer, and read back at once")
def test_framework_pin_write_path(opts):
    from mojo import errors as me
    from mojo.apps.account.models import Setting, User
    from mojo.apps.account.services import admin_platform, system_settings
    from mojo.apps.edge.settings_validators import FRAMEWORK_VERSION_KEY

    root = User.objects.get(pk=opts.platform_root)
    Setting.objects.filter(key=FRAMEWORK_VERSION_KEY).delete()
    try:
        assert system_settings.set_value(root, FRAMEWORK_VERSION_KEY, "1.11.6") == "1.11.6", \
            "an explicit published version must be stored verbatim"
        assert admin_platform._deployments()["framework_pin"] == {
            "configured": True, "value": "1.11.6", "mode": "pinned",
            "resolved": "1.11.6"}, \
            "the Platform overview did not reflect the pin that was just written"

        assert system_settings.set_value(root, FRAMEWORK_VERSION_KEY, "HOLD") == "hold", \
            "the hold sentinel must be casefolded on the way in"
        held = admin_platform._deployments()["framework_pin"]
        assert held["mode"] == "hold" and held["value"] == "hold", \
            f"a hold must be reported as such: {held!r}"
        assert "resolved" in held, \
            f"a hold must report what it currently resolves to: {held!r}"

        assert system_settings.set_value(root, FRAMEWORK_VERSION_KEY, "latest") == "", \
            "'latest' is the unset synonym — it must not be stored literally"
        assert admin_platform._deployments()["framework_pin"] == {
            "configured": False, "value": None, "mode": "latest", "resolved": None}, \
            "an unset hold must read back as the newest-release default"

        for bad in ("stable", "v1.2.3", "1.0; rm -rf /"):
            try:
                system_settings.set_value(root, FRAMEWORK_VERSION_KEY, bad)
                raise AssertionError(f"{bad!r} is not a version and must be refused")
            except me.ValueException as err:
                assert "'hold'" in str(err), \
                    f"the refusal must name the accepted forms: {err}"

        # The generic Setting writer must not be able to reach it at all.
        generic = Setting(key=FRAMEWORK_VERSION_KEY, value="9.9.9")
        with th.assert_raises(me.PermissionDeniedException):
            generic.save()
    finally:
        Setting.objects.filter(key=FRAMEWORK_VERSION_KEY).delete()


@th.django_unit_test("the framework hold endpoint demands a fresh interactive superuser")
def test_framework_pin_endpoint_authority(opts):
    from mojo.apps.account.models import Setting, User
    from mojo.apps.edge.settings_validators import FRAMEWORK_VERSION_KEY

    Setting.objects.filter(key=FRAMEWORK_VERSION_KEY).delete()
    user = User.objects.get(pk=opts.platform_user)
    user.add_permission("manage_advanced")
    user.save()
    try:
        assert opts.client.login("platform-user@test.com", "example"), \
            "the non-superuser fixture could not establish a session"
        response = opts.client.post(
            "/api/account/admin/advanced/settings", json={"framework_pin": "1.11.6"})
        assert response.status_code in (401, 403), \
            f"a non-superuser wrote the fleet's framework version: {response.response!r}"
        assert not Setting.objects.filter(key=FRAMEWORK_VERSION_KEY).exists(), \
            "a refused write still landed a protected row"
    finally:
        opts.client.logout()
        Setting.objects.filter(key=FRAMEWORK_VERSION_KEY).delete()


def _tail_deployment():
    """One durable attempt whose evidence carries a deploy stderr tail."""
    import uuid
    from mojo.apps.edge.models import PlatformDeployment
    PlatformDeployment.objects.all().delete()
    return PlatformDeployment.objects.create(
        sha="c" * 40, actor="test", source="test",
        request_key=str(uuid.uuid4()), frozen_roster=["edge-a-engine"],
        status="failed", transitions=[],
        node_evidence=[{
            "runner": "edge-a-engine", "state": "failed",
            "detail": {"phase": "update_script", "exit": 23,
                       "stderr_tail": ["psql: postgres://deploy:hunter2@db/app"]}}])


def _viewer(*granted):
    """A request whose user holds exactly the named global permissions."""
    granted = set(granted)
    user = mock.Mock(is_superuser=False)
    user.has_permission.side_effect = lambda perms: bool(granted & set(perms))
    return mock.Mock(user=user)


@th.django_unit_test("read-only platform viewers never receive the deploy stderr tail")
def test_stderr_tail_hidden_from_read_only_viewer(opts):
    """The tail can survive redaction with a credential in it, so it belongs to
    the security tier — while the rest of node_evidence stays visible to the
    read-only role that is meant to see deploy state."""
    from mojo.apps.account.services import admin_platform

    _tail_deployment()
    request = _viewer("view_platform")

    overview = admin_platform.platform_overview(request)
    section = overview["sections"]["deployments"]
    # The section status tracks the deployment's own health (this fixture is a
    # failed attempt); what matters here is that it rendered at all.
    th.assert_true(section["status"] != "unauthorized",
                   "a view_platform viewer must still get the deployments section")
    assert "stderr_tail" not in str(section), \
        "view_platform alone must not reveal the deploy stderr tail"
    detail = section["data"]["items"][0]["node_evidence"][0]["detail"]
    th.assert_eq(detail["phase"], "update_script",
                 "the benign evidence detail must survive for read-only viewers")

    dashboard = admin_platform.dashboard_overview(request)
    assert "stderr_tail" not in str(dashboard["sources"]["last_deployment"]), \
        "the dashboard's last_deployment must be gated the same way"


@th.django_unit_test("security and deploy authority still read the stderr tail")
def test_stderr_tail_visible_to_privileged_tiers(opts):
    from mojo.apps.account.services import admin_platform

    _tail_deployment()
    for granted in (("view_platform", "view_platform_security"),
                    ("view_platform", "manage_platform")):
        request = _viewer(*granted)
        overview = admin_platform.platform_overview(request)
        section = overview["sections"]["deployments"]
        assert "hunter2" in str(section), \
            f"{granted} must still read the full diagnostic tail"
        dashboard = admin_platform.dashboard_overview(request)
        assert "hunter2" in str(dashboard["sources"]["last_deployment"]), \
            f"{granted} must read the tail on the dashboard too"
