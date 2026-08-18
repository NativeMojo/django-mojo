"""Curated Admin Settings catalog, writer, and feature contracts."""

from pathlib import Path
from unittest import mock

from testit import helpers as th


ROOT = Path(__file__).resolve().parents[2]
CATALOG_KEYS = (
    "ALLOW_EMAIL_CHANGE", "ALLOW_PHONE_CHANGE", "ALLOW_USERNAME_CHANGE",
    "ALLOW_SELF_DEACTIVATION", "WEBAPP_BASE_URL",
)
FLEET_PROVIDER_KEYS = (
    "GEOIP_PRIMARY_PROVIDER", "GEOIP_FALLBACK_PROVIDER",
    "GEOIP_ADDITIONAL_PROVIDERS", "GEOIP_MOJO_PROVIDER_URL",
    "GEOIP_MOJO_SYNC_ENABLED", "GEOIP_API_KEY_MOJO",
)


@th.django_unit_setup()
def setup_admin_settings(opts):
    from mojo.apps.account.models import Group, Setting, User
    Setting.objects.filter(key__in=(*CATALOG_KEYS, *FLEET_PROVIDER_KEYS)).delete()
    Group.objects.filter(name="admin-settings-group").delete()
    User.objects.filter(username__in=("settings-admin", "settings-denied")).delete()
    admin = User.objects.create_user(
        email="settings-admin@test.com", username="settings-admin", password="example")
    admin.is_active = True
    admin.is_email_verified = True
    admin.requires_mfa = False
    admin.permissions = {"manage_settings": True}
    admin.save()
    denied = User.objects.create_user(
        email="settings-denied@test.com", username="settings-denied", password="example")
    denied.is_active = True
    denied.save()
    group = Group.objects.create(name="admin-settings-group", kind="organization")
    opts.settings_admin = admin.pk
    opts.settings_denied = denied.pk
    opts.settings_group = group.pk


@th.django_unit_test("manage_settings can enter Admin and receives catalog capability")
def test_manage_settings_admin_admission(opts):
    assert opts.client.login("settings-admin@test.com", "example"), \
        "manage_settings user could not establish an interactive session"
    source = opts.client.post("/api/account/admin/session", json={})
    assert source.status_code == 200, \
        f"manage_settings was refused by the Admin source gate: {source.response!r}"
    bootstrap = opts.client.get("/api/account/admin/bootstrap")
    data = bootstrap.json.get("data") or {}
    assert data.get("capabilities", {}).get("catalog_write") is True, \
        f"manage_settings did not receive catalog write capability: {data!r}"
    assert data.get("features", {}).get("settings", {}).get("enabled") is True, \
        f"manage_settings did not receive the Settings feature: {data!r}"
    assert opts.client.get("/api/account/admin/dashboard").status_code == 200, \
        "a manage_settings-only operator lands on an unusable Dashboard"
    catalog = opts.client.get("/api/account/admin/settings")
    assert catalog.status_code == 200, \
        f"manage_settings could not read the curated catalog: {catalog.response!r}"
    rows = {row["key"] for row in (catalog.json.get("data") or {}).get("entries", [])}
    assert "ALLOW_EMAIL_CHANGE" in rows and "AUTH_CONFIG" not in rows, \
        f"catalog-write and owner-display authority were not separated: {rows!r}"


@th.django_unit_test("catalog registry is immutable, idempotent, and opt-in")
def test_settings_registry_contract(opts):
    from mojo.apps.account.services import admin_settings
    rows = {row.key: row for row in admin_settings.descriptors()}
    for key in (*CATALOG_KEYS, "BASE_URL", "AUTH_CONFIG", "EDGE_EXPECTED_TOPOLOGY",
                "EDGE_FRAMEWORK_VERSION"):
        assert key in rows, f"the curated catalog omitted {key}"
    hold = rows["EDGE_FRAMEWORK_VERSION"]
    assert hold.resolver == "protected" and hold.writable == "owner", \
        f"the framework hold must stay owner-written and protected: {hold!r}"
    assert "hold" in hold.constraints, \
        f"the catalog must tell an operator what it accepts: {hold.constraints!r}"
    descriptor = rows["ALLOW_EMAIL_CHANGE"]
    assert admin_settings.register_descriptor(descriptor) == descriptor, \
        "an identical app-owned registration was not idempotent"
    conflicting = admin_settings.Descriptor(
        descriptor.key, "Conflict", descriptor.section, descriptor.description,
        descriptor.value_type)
    with th.assert_raises(RuntimeError):
        admin_settings.register_descriptor(conflicting)
    without_optional_apps = tuple(
        row for row in rows.values()
        if row.section not in {"Domains & DNS", "Edge & Web Apps"})
    sections = admin_settings._section_names(without_optional_apps)
    assert "Domains & DNS" not in sections and "Edge & Web Apps" not in sections, \
        "categories from absent optional applications remained advertised"


@th.django_unit_test("catalog protection is global-scope-aware across moves")
def test_settings_global_protection(opts):
    from mojo import errors as me
    from mojo.apps.account.models import Group, Setting
    key = "WEBAPP_BASE_URL"
    Setting.objects.filter(key=key).delete()
    global_row = Setting(key=key, value='"https://global.example.com"')
    Setting.objects.bulk_create([global_row])
    global_row = Setting.objects.get(key=key, group=None)
    global_row.value = '"https://changed.example.com"'
    with th.assert_raises(me.PermissionDeniedException):
        global_row.save()
    group = Group.objects.get(pk=opts.settings_group)
    group_row = Setting.objects.create(
        key=key, group=group, value='"https://group.example.com"')
    group_row.value = '"https://group-two.example.com"'
    group_row.save()
    assert Setting.objects.get(pk=group_row.pk).group_id == group.pk, \
        "a documented group-scoped setting row was blocked"
    group_row.group = None
    with th.assert_raises(me.PermissionDeniedException):
        group_row.save()
    global_row.group = group
    with th.assert_raises(me.PermissionDeniedException):
        global_row.save()
    Setting.objects.filter(key=key).delete()


@th.django_unit_test("static fleet provider keys cannot become ignored DB shadows")
def test_static_provider_settings_are_protected(opts):
    from mojo import errors as me
    from mojo.apps.account.models import Setting

    key = "GEOIP_PRIMARY_PROVIDER"
    Setting.objects.filter(key=key).delete()
    Setting.objects.bulk_create([Setting(key=key, value='"mojo"')])
    row = Setting.objects.get(key=key, group=None)
    row.value = '"ipinfo"'
    with th.assert_raises(me.PermissionDeniedException):
        row.save()
    Setting.objects.filter(key=key).delete()


@th.django_unit_test("dedicated writer rejects ambiguity and clears every duplicate")
def test_settings_duplicate_set_clear(opts):
    from mojo import errors as me
    from mojo.apps.account.models import Setting, User
    from mojo.apps.account.services import admin_settings
    key = "ALLOW_PHONE_CHANGE"
    Setting.objects.filter(key=key).delete()
    Setting.objects.bulk_create([
        Setting(key=key, value="true"), Setting(key=key, value="false")])
    actor = User.objects.get(pk=opts.settings_admin)
    with mock.patch.object(admin_settings, "_audit"):
        with th.assert_raises(me.ValueException):
            admin_settings.set_override(actor, key, True)
        removed = admin_settings.clear_override(actor, key)
    assert removed == 2, f"clear removed {removed} duplicate rows instead of every row"
    assert not Setting.objects.filter(key=key, group=None).exists(), \
        "duplicate global overrides survived atomic clear"


@th.django_unit_test("catalog cache publication waits for the outer transaction commit")
def test_settings_cache_rollback(opts):
    from django.db import transaction
    from mojo.apps.account.models import Setting, User
    from mojo.apps.account.services import admin_settings
    key = "ALLOW_SELF_DEACTIVATION"
    Setting.objects.filter(key=key).delete()
    Setting.objects.bulk_create([Setting(key=key, value="false")])
    actor = User.objects.get(pk=opts.settings_admin)
    with mock.patch.object(admin_settings, "_cache_delete") as cache_delete, \
            mock.patch.object(admin_settings, "_audit"):
        with th.assert_raises(RuntimeError):
            with transaction.atomic():
                admin_settings.clear_override(actor, key)
                raise RuntimeError("force outer rollback")
    assert Setting.objects.filter(key=key, group=None).exists(), \
        "a rolled-back catalog clear changed the database"
    assert not cache_delete.called, \
        "a rolled-back catalog clear invalidated Redis before commit"
    Setting.objects.filter(key=key).delete()


@th.django_unit_test("dedicated writer uses strict types, canonical origins, and exact authority")
def test_settings_writer_validation(opts):
    from mojo import errors as me
    from mojo.apps.account.models import Setting, User
    from mojo.apps.account.services import admin_settings
    from mojo.helpers.settings import settings as runtime_settings
    actor = User.objects.get(pk=opts.settings_admin)
    denied = User.objects.get(pk=opts.settings_denied)
    with mock.patch.object(admin_settings, "_audit"):
        with th.assert_raises(me.PermissionDeniedException):
            admin_settings.set_override(denied, "ALLOW_EMAIL_CHANGE", False)
        with th.assert_raises(me.ValueException):
            admin_settings.set_override(actor, "ALLOW_EMAIL_CHANGE", "false")
        refused = (
            "http://localhost:8000", "https://localhost", "https://admin.localhost",
            "https://127.0.0.1", "https://127.1", "https://0177.0.0.1",
            "https://0x7f.0.0.1", "https://service.local",
            "https://service.internal", "https://service.test",
            "https://service.invalid", "https://service.onion",
            "https://home.arpa", "https://service.example",
            "https://sub.example.com",
        )
        for origin in refused:
            with th.assert_raises(me.ValueException):
                admin_settings.set_override(actor, "WEBAPP_BASE_URL", origin)
        saved = admin_settings.set_override(
            actor, "WEBAPP_BASE_URL", "https://Apps.MojoVerify.COM/")
        stored = Setting.objects.get(key="WEBAPP_BASE_URL", group=None)
        assert stored.value == saved and not stored.is_secret, \
            "the catalog writer JSON-encoded a string override"
        assert runtime_settings.get("WEBAPP_BASE_URL") == saved, \
            "SettingsHelper did not return the normalized submitted origin exactly"
        admin_settings.clear_override(actor, "WEBAPP_BASE_URL")
    assert saved == "https://apps.mojoverify.com", \
        f"the WebApp origin was not canonicalized: {saved!r}"
    assert not Setting.objects.filter(key="WEBAPP_BASE_URL", group=None).exists(), \
        "the writer regression did not clean up its global override"


@th.django_unit_test("catalog reports duplicate state without exposing sensitive values")
def test_settings_catalog_redaction(opts):
    from mojo.apps.account.models import Setting
    from mojo.apps.account.services import admin_settings
    key = "WEBAPP_BASE_URL"
    Setting.objects.filter(key=key).delete()
    Setting.objects.bulk_create([
        Setting(key=key, value='"https://one.example.com"'),
        Setting(key=key, value='"https://two.example.com"'),
    ])
    report = admin_settings.catalog(capabilities={
        "catalog_write": True, "owner_display": True, "owner_edit": False})
    rows = {row["key"]: row for row in report["entries"]}
    assert rows[key]["source"] == "duplicate_override" and not rows[key]["can_write"], \
        "duplicate global overrides did not fail closed"
    assert rows[key]["effective_value"] is None, \
        "an ambiguous duplicate override exposed a cached or arbitrary effective value"
    assert rows["KMS_KEY_ID"]["effective_value"] in (
        {"configured": True}, {"configured": False}), \
        "a sensitive deployment setting exposed more than configured state"
    sensitive = admin_settings.Descriptor(
        "TEST_SECRET", "Test secret", "Security & operations", "Test only.",
        "configured", sensitivity="configured_only")
    with mock.patch.object(admin_settings.settings, "get_static", return_value=None):
        value, source, ignored = admin_settings._static_state(sensitive, [])
    assert value == {"configured": False} and source == "default" and not ignored, \
        "an absent sensitive setting reported false deployment provenance"
    with mock.patch.object(admin_settings.settings, "get_static", return_value="secret"):
        value, source, ignored = admin_settings._static_state(sensitive, [])
    assert value == {"configured": True} and source == "deployment" and not ignored, \
        "a configured sensitive setting exposed its value or lost provenance"
    assert "raw_value" not in rows[key], "the catalog exposed raw ignored override material"
    Setting.objects.filter(key=key).delete()
    Setting.objects.bulk_create([
        Setting(key=key, value="must-not-be-read", is_secret=True),
        Setting(key="BASE_URL", value="also-must-not-be-read", is_secret=True),
    ])
    original_get_value = Setting.get_value

    def guarded_get_value(row):
        assert not row.is_secret, "the catalog attempted to decrypt a legacy secret row"
        return original_get_value(row)

    requested_cache_keys = []

    def cache_values(redis_key, keys):
        requested_cache_keys.extend(keys)
        return [None] * len(keys)

    redis = mock.Mock()
    redis.hmget.side_effect = cache_values
    with mock.patch.object(Setting, "_redis", return_value=redis), \
            mock.patch.object(Setting, "get_value", guarded_get_value):
        report = admin_settings.catalog(capabilities={
            "catalog_write": True, "owner_display": True, "owner_edit": False})
    row = {item["key"]: item for item in report["entries"]}[key]
    assert row["effective_value"] == {"configured": True} and \
        row["source"] == "secret_override", \
        "a legacy secret row returned anything beyond configured metadata"
    assert key not in requested_cache_keys, \
        "the catalog requested a cached plaintext value for a legacy secret row"
    assert report["setup_incomplete"] is True, \
        "a legacy secret BASE_URL row was treated as valid Setup configuration"
    Setting.objects.filter(key__in=(key, "BASE_URL")).delete()


@th.django_unit_test("Settings mutations require fresh human global authority")
def test_settings_rest_decorators(opts):
    from mojo.apps.account.rest import admin_settings as views
    func = views.on_admin_settings_mutate
    assert getattr(func, "_mojo_denies_key_backed_session", False), \
        "Settings mutation accepts a key-backed session"
    assert getattr(func, "_mojo_requires_fresh_auth", False), \
        "Settings mutation lacks recent interactive authentication"
    assert getattr(func, "_mojo_fresh_auth_seconds", None) == 600, \
        "Settings recent-authentication window is not 600 seconds"


@th.django_unit_test("Settings owns one first-class Admin feature and typed owner home")
def test_settings_feature_assets(opts):
    from mojo.apps.account.services import admin_assets, admin_features
    assets = admin_assets.load_manifest()
    required = {
        "assets/features/settings/feature.js", "assets/features/settings/page.js",
        "assets/features/settings/styles.css",
    }
    assert required <= set(assets), "the private Settings feature package is incomplete"
    assert admin_features.FEATURE_NAMES[-1] == "settings", \
        "the server fixed feature roster omitted Settings"
    registry = (ROOT / "mojo/apps/account/admin_portal/assets/features/registry.js").read_text()
    page = (ROOT / "mojo/apps/account/admin_portal/assets/features/settings/page.js").read_text()
    advanced = (ROOT / "mojo/apps/account/admin_portal/assets/features/advanced/page.js").read_text()
    assert "[dashboard, webapps, advanced, people, activity, platform, settings]" in registry, \
        "the sidebar feature order is not Dashboard/Web Apps/Domains/People/Activity/Platform/Settings"
    for contract in ("Technical details", "Search settings", "openBusy", "apiOnce", "Clear database override", "settings-token-editor", "Mojo GeoIP and SMS", "configure_providers"):
        assert contract in page, f"Settings UX omitted {contract}"
    assert "Expected edge nodes (comma-separated)" not in advanced and "Typed settings" not in advanced, \
        "Advanced still owns the duplicated settings form"


@th.django_unit_test("Settings request bodies are classified sensitive")
def test_settings_request_redaction(opts):
    from mojo.helpers import request as request_helpers
    request = mock.Mock(path="/api/account/admin/settings", method="POST")
    assert request_helpers.sensitive_body_label(request) == "admin_settings", \
        "the Settings mutation body can enter generic request logs"


@th.django_unit_test("provider setup publishes only typed delegated values with KMS")
def test_provider_setup_publisher_contract(opts):
    from mojo.apps.account.models import User
    from mojo.apps.account.services import provider_setup

    actor = User.objects.get(pk=opts.settings_admin)
    actor.is_superuser = True
    actor.save(update_fields=["is_superuser"])
    s3 = mock.Mock()
    static = {
        "ADMIN_FLEET_CONFIG_BUCKET": "config-bucket",
        "ADMIN_FLEET_CONFIG_PREFIX": "config/prod",
        "ADMIN_FLEET_CONFIG_FILENAME": "django.override.json",
        "ADMIN_FLEET_CONFIG_KMS_KEY_ID": "alias/config",
        "ADMIN_FLEET_CONFIG_ALLOWED_KEYS": list(provider_setup.FLEET_KEYS),
    }
    payload = {"geoip": {
        "GEOIP_PRIMARY_PROVIDER": "mojo",
        "GEOIP_FALLBACK_PROVIDER": "ipinfo",
        "GEOIP_ADDITIONAL_PROVIDERS": [],
        "GEOIP_MOJO_PROVIDER_URL": "https://api.mojoverify.com",
        "GEOIP_MOJO_SYNC_ENABLED": False,
        "GEOIP_API_KEY_MOJO": None,
    }, "sms": {"remote_url": "https://sms.example.com", "api_key": None}}

    with mock.patch.object(provider_setup, "_s3_client", return_value=s3), \
            mock.patch.object(provider_setup, "_save_database") as save_database, \
            mock.patch.object(provider_setup, "_static", side_effect=lambda key, default=None: static.get(key, default)):
        result = provider_setup.apply(actor, payload)

    assert result["published"] is True and result["pending_restart"] is True, \
        f"provider setup did not report a pending fleet publish: {result!r}"
    kwargs = s3.put_object.call_args.kwargs
    assert kwargs["Bucket"] == "config-bucket" and kwargs["Key"] == "config/prod/django.override.json", \
        f"provider setup published to the wrong canonical object: {kwargs!r}"
    assert kwargs["ServerSideEncryption"] == "aws:kms" and kwargs["SSEKMSKeyId"] == "alias/config", \
        "provider setup did not require KMS server-side encryption"
    body = kwargs["Body"].decode("utf-8")
    assert "GEOIP_API_KEY_MOJO" not in body and "sms.example.com" not in body, \
        "a database credential or SMS configuration leaked into the fleet object"
    assert save_database.call_count == 1, "validated database settings were not saved"


@th.django_unit_test("provider setup converts the effective system SMS row to Mojo")
def test_provider_setup_selects_system_sms(opts):
    from mojo.apps.account.services import provider_setup
    from mojo.apps.phonehub.models import PhoneConfig

    PhoneConfig.objects.filter(group=None, name__startswith="provider-setup-test").delete()
    active = PhoneConfig.objects.create(
        group=None, name="provider-setup-test-active", provider="twilio",
        is_active=True)
    inactive = PhoneConfig.objects.create(
        group=None, name="provider-setup-test-inactive", provider="mojo",
        is_active=False)
    sms = {
        "remote_url": "https://sms.example.com", "api_key": None,
        "clear_api_key": False, "test_mode": True,
    }

    with mock.patch.object(provider_setup, "_write_secret"):
        provider_setup._save_database(None, False, sms)

    active.refresh_from_db()
    inactive.refresh_from_db()
    assert active.provider == "mojo" and active.name == "Mojo Remote SMS", \
        f"the effective system row was not converted to Mojo: {active.provider!r}"
    assert active.mojo_remote_url == "https://sms.example.com" and active.test_mode, \
        "the system Mojo configuration did not receive the submitted values"
    assert active.is_active and not inactive.is_active, \
        "provider setup left an ambiguous active system default"
    PhoneConfig.objects.filter(pk__in=(active.pk, inactive.pk)).delete()
