"""Curated Admin Settings catalog, writer, and feature contracts."""

from contextlib import ExitStack
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
    "ADMIN_PROVIDER_SETUP_REVISION", "ADMIN_PROVIDER_VERIFY_STATE",
)
FLEET_STATIC = {
    "ADMIN_FLEET_CONFIG_BUCKET": "config-bucket",
    "ADMIN_FLEET_CONFIG_PREFIX": "config/prod",
    "ADMIN_FLEET_CONFIG_FILENAME": "django.override.json",
    "ADMIN_FLEET_CONFIG_KMS_KEY_ID": "alias/config",
    "ADMIN_FLEET_CONFIG_RESTART_ENABLED": True,
}
SETTINGS_ASSETS = (
    "mojo/apps/account/admin_portal/assets/features/settings/page.js",
    "mojo/apps/account/admin_portal/assets/features/settings/language.js",
    "mojo/apps/account/admin_portal/assets/features/settings/panels.js",
)


def _fleet_static(provider_setup):
    """Deployment settings under which fleet publishing is fully available."""
    return {**FLEET_STATIC,
            "ADMIN_FLEET_CONFIG_ALLOWED_KEYS": list(provider_setup.FLEET_KEYS)}


def _geoip_payload(provider_setup, **overrides):
    values = dict(provider_setup.config_override.DEFAULTS)
    return {"geoip": {**values, "GEOIP_API_KEY_MOJO": None, **overrides}}


def _sms_payload(**overrides):
    return {"sms": {"remote_url": "https://sms.example.com", "api_key": None,
                    **overrides}}


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
    # The browser cannot derive either of these from a value, so the app owns
    # them: what an integer counts, and what absence actually means.
    assert hold.unset_meaning, \
        "an unset framework hold does not say what the fleet installs instead"
    assert rows["DNSMAN_CERT_RENEW_DAYS"].unit == "days", \
        f"a renewal window is a bare number without its unit: {rows['DNSMAN_CERT_RENEW_DAYS']!r}"
    assert rows["DNSMAN_ACME_CONTACT_EMAIL"].unset_meaning, \
        "an absent ACME contact does not say what nobody is being warned about"
    for key in ("INCIDENT_EMAIL_FROM", "AWS_MONITORING_NAME",
                "AWS_CLOUDWATCH_ALARM_TOPIC_ARNS", "SYSTEM_SETUP_LOCAL_API_URL"):
        assert rows[key].unset_meaning, \
            f"{key} leaves an operator to guess what unset does"
    assert not rows["KMS_KEY_ID"].unset_meaning, \
        "a missing encryption key must not be described reassuringly"
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
    Setting.objects.bulk_create([
        Setting(key="GEOIP_API_KEY_MOJO", value="", is_secret=True)])
    secret = Setting.objects.get(key="GEOIP_API_KEY_MOJO", group=None)
    with th.assert_raises(me.PermissionDeniedException):
        secret.save()
    Setting.objects.filter(key="GEOIP_API_KEY_MOJO").delete()


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
    assert rows["GEOIP_API_KEY_MOJO"]["effective_value"] in (
        {"configured": True}, {"configured": False}), \
        "the provider secret descriptor exposed more than configured state"
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
    source = (ROOT / "mojo/apps/account/rest/admin_settings.py").read_text()
    assert "system_setup.request_origin(request)" in source and \
        "system_setup.require_request_admin(request)" in source, \
        "provider secret/fleet mutations lack the literal-superuser same-Origin gate"


@th.django_unit_test("Settings owns one first-class Admin feature and typed owner home")
def test_settings_feature_assets(opts):
    from mojo.apps.account.services import admin_assets, admin_features
    assets = admin_assets.load_manifest()
    required = {
        "assets/features/settings/feature.js", "assets/features/settings/page.js",
        "assets/features/settings/panels.js", "assets/features/settings/language.js",
        "assets/features/settings/styles.css",
    }
    assert required <= set(assets), "the private Settings feature package is incomplete"
    assert "settings" in admin_features.FEATURE_NAMES, \
        "the server fixed feature roster omitted Settings"
    registry = (ROOT / "mojo/apps/account/admin_portal/assets/features/registry.js").read_text()
    page = (ROOT / "mojo/apps/account/admin_portal/assets/features/settings/page.js").read_text()
    panels = (ROOT / "mojo/apps/account/admin_portal/assets/features/settings/panels.js").read_text()
    advanced = (ROOT / "mojo/apps/account/admin_portal/assets/features/advanced/page.js").read_text()
    assert "[dashboard, webapps, advanced, people, activity, platform, settings, sms, email]" in registry, \
        "the sidebar feature order is not Dashboard/Web Apps/Domains/People/Activity/Platform/Settings/SMS/Email"
    for contract in ("Search settings", "openBusy", "apiOnce", "configure_providers",
                     "rowSection", "statusRow", "How this platform is configured.",
                     "focus: 'geoip'"):
        assert contract in page, f"the Settings list omitted {contract}"
    for contract in ("Technical details", "settings-token-editor", "topic",
                     "expected_revision"):
        assert contract in panels, f"the Settings panels omitted {contract}"
    language = (ROOT / "mojo/apps/account/admin_portal/assets/features/settings/language.js").read_text()
    for contract in ("VERIFY_TONE_MAX_AGE_MS", "export function verifyIsCurrent"):
        assert contract in language, \
            f"the Settings language layer omitted the verify staleness cap: {contract}"
    # A verification is a point-in-time fact: past the cap it must stop driving
    # tone (no red dot, no Fix) and read as a dated "last checked" note.
    for contract in ("verifyIsCurrent(verify)", "last ${ago}"):
        assert contract in page, \
            f"the Settings list omitted the verify staleness cap: {contract}"
    # The one combined provider modal is what this feature replaced; if the
    # string comes back, so has the "fix SMS by reading GeoIP" page.
    for gone in ("Mojo GeoIP and SMS", "openModal"):
        assert gone not in page and gone not in panels, \
            f"Settings still carries the combined provider modal: {gone}"
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
    static = _fleet_static(provider_setup)
    payload = _geoip_payload(provider_setup)

    s3.put_object.return_value = {"VersionId": "version-1"}
    verified = ({"geoip": {"success": True}}, False)
    with mock.patch.object(provider_setup, "_s3_client", return_value=s3), \
            mock.patch.object(provider_setup, "_published", return_value=None), \
            mock.patch.object(provider_setup, "_test_provider_credentials", return_value=verified), \
            mock.patch.object(provider_setup, "_save_geoip_secret") as save_secret, \
            mock.patch.object(provider_setup, "_save_sms_config") as save_sms, \
            mock.patch.object(provider_setup, "_configuration_revision", return_value=None), \
            mock.patch.object(provider_setup, "_write_configuration_revision") as write_revision, \
            mock.patch.object(provider_setup, "_write_verify_state") as write_verify, \
            mock.patch.object(provider_setup, "_audit"), \
            mock.patch.object(provider_setup, "_static", side_effect=lambda key, default=None: static.get(key, default)):
        result = provider_setup.apply(actor, "geoip", payload)

    assert result["published"] is True and result["pending_restart"] is True, \
        f"provider setup did not report a pending fleet publish: {result!r}"
    assert result["topic"] == "geoip", \
        f"a provider result must name the topic it wrote: {result!r}"
    kwargs = s3.put_object.call_args.kwargs
    assert kwargs["Bucket"] == "config-bucket" and kwargs["Key"] == "config/prod/django.override.json", \
        f"provider setup published to the wrong canonical object: {kwargs!r}"
    assert kwargs["ServerSideEncryption"] == "aws:kms" and kwargs["SSEKMSKeyId"] == "alias/config", \
        "provider setup did not require KMS server-side encryption"
    assert kwargs["IfNoneMatch"] == "*", \
        "the first Admin publication can overwrite a concurrent creator"
    body = kwargs["Body"].decode("utf-8")
    assert "GEOIP_API_KEY_MOJO" not in body and "sms.example.com" not in body, \
        "a database credential or SMS configuration leaked into the fleet object"
    assert save_secret.call_count == 1, "validated database settings were not saved"
    assert not save_sms.called, \
        "a GeoIP publication rewrote the unrelated SMS provider row"
    assert write_revision.call_count == 1, "the provider edit revision was not advanced"
    assert write_verify.call_args.args[0] == "geoip", \
        "a successful publish did not record the stored GeoIP verification"


@th.django_unit_test("provider publication is conditional and skips an unchanged override")
def test_provider_setup_concurrency_and_noop(opts):
    from mojo.apps.account.models import Setting, User
    from mojo.apps.account.services import provider_setup

    actor = User.objects.get(pk=opts.settings_admin)
    actor.is_superuser = True
    actor.save(update_fields=["is_superuser"])
    Setting.objects.filter(
        key=provider_setup.SETUP_REVISION_KEY, group=None).delete()
    static = _fleet_static(provider_setup)
    values = dict(provider_setup.config_override.DEFAULTS)
    payload = {"expected_revision": "c" * 32,
               **_geoip_payload(provider_setup)}
    current = {"document": {"settings": values, "revision": "c" * 32},
               "etag": '"etag-current"', "version_id": "version-current"}
    s3 = mock.Mock()
    verified = ({"geoip": {"success": True}}, False)

    with mock.patch.object(provider_setup, "_static", side_effect=lambda key, default=None: static.get(key, default)), \
            mock.patch.object(provider_setup, "_s3_client", return_value=s3), \
            mock.patch.object(provider_setup, "_published", return_value=current), \
            mock.patch.object(provider_setup, "_test_provider_credentials", return_value=verified), \
            mock.patch.object(provider_setup, "_save_geoip_secret") as save_database, \
            mock.patch.object(provider_setup, "_write_verify_state"), \
            mock.patch.object(provider_setup, "_audit"):
        result = provider_setup.apply(actor, "geoip", payload)

    assert result["unchanged"] is True and not s3.put_object.called, \
        "an unchanged provider patch uploaded a new revision"
    assert result["revision"] != payload["expected_revision"], \
        "a database-only provider mutation did not advance its edit revision"
    assert save_database.call_count == 1, \
        "an unchanged static patch skipped the requested DB credential/model writes"
    from mojo import errors as me
    with mock.patch.object(provider_setup, "_static", side_effect=lambda key, default=None: static.get(key, default)), \
            mock.patch.object(provider_setup, "_s3_client", return_value=s3), \
            mock.patch.object(provider_setup, "_published", return_value=current), \
            mock.patch.object(provider_setup, "_test_provider_credentials", return_value=verified), \
            mock.patch.object(provider_setup, "_save_geoip_secret") as stale_save, \
            mock.patch.object(provider_setup, "_write_verify_state"), \
            mock.patch.object(provider_setup, "_audit"), \
            th.assert_raises(me.ValueException):
        provider_setup.apply(actor, "geoip", payload)
    assert not stale_save.called, "a stale database-only provider form was applied"
    payload["expected_revision"] = result["revision"]
    external = {**current, "document": {
        **current["document"], "revision": "d" * 32}}
    with mock.patch.object(provider_setup, "_static", side_effect=lambda key, default=None: static.get(key, default)), \
            mock.patch.object(provider_setup, "_s3_client", return_value=s3), \
            mock.patch.object(provider_setup, "_published", return_value=external), \
            mock.patch.object(provider_setup, "_test_provider_credentials", return_value=verified), \
            mock.patch.object(provider_setup, "_save_geoip_secret") as external_save, \
            mock.patch.object(provider_setup, "_write_verify_state"), \
            mock.patch.object(provider_setup, "_audit"), \
            th.assert_raises(me.ValueException):
        provider_setup.apply(actor, "geoip", payload)
    assert not external_save.called, "a stale form overwrote an external S3 revision"
    changed = {**current, "document": {
        **current["document"],
        "settings": {**values, "GEOIP_PRIMARY_PROVIDER": "ipinfo"},
    }}
    s3.reset_mock()
    s3.put_object.return_value = {"VersionId": "version-next"}
    with mock.patch.object(provider_setup, "_static", side_effect=lambda key, default=None: static.get(key, default)), \
            mock.patch.object(provider_setup, "_s3_client", return_value=s3), \
            mock.patch.object(provider_setup, "_published", return_value=changed), \
            mock.patch.object(provider_setup, "_test_provider_credentials", return_value=verified), \
            mock.patch.object(provider_setup, "_save_geoip_secret"), \
            mock.patch.object(provider_setup, "_write_verify_state"), \
            mock.patch.object(provider_setup, "_audit"):
        updated = provider_setup.apply(actor, "geoip", payload)
    assert updated["published"] is True and \
        s3.put_object.call_args.kwargs["IfMatch"] == '"etag-current"', \
        "an existing override was not protected by its current ETag"


@th.django_unit_test("provider connection test checks GeoIP and SMS permissions separately")
def test_provider_setup_connection_permissions(opts):
    from mojo.apps.account.services import provider_setup
    from mojo.apps.phonehub.services import mojo_provider

    responses = [
        mock.Mock(ok=True, permissions={"geoip_sync": True}),
        mock.Mock(ok=True, permissions={"send_sms": False, "comms": False}),
        mock.Mock(ok=True, permissions={}),
    ]
    with mock.patch.object(mojo_provider, "verify_credentials", side_effect=responses):
        geo = provider_setup._credential_result(
            "https://geo.example.com", "secret", ("geoip_sync",))
        sms = provider_setup._credential_result(
            "https://sms.example.com", "secret", ("send_sms", "comms"))
        lookup = provider_setup._credential_result(
            "https://geo.example.com", "secret", ())
    assert geo["success"] is True, f"a valid GeoIP key failed testing: {geo!r}"
    assert sms["success"] is False and sms["code"] == "insufficient_permission", \
        f"an SMS key without send authority passed testing: {sms!r}"
    assert lookup["success"] is True, \
        f"lookup-only GeoIP unnecessarily required write authority: {lookup!r}"
    with mock.patch.object(
            provider_setup, "_static", return_value="https://loaded.example.com"):
        assert provider_setup._stored_geoip_key("https://attacker.example.com") == "", \
            "a stored GeoIP secret can be replayed to a newly submitted origin"


@th.django_unit_test("dedicated provider writer encrypts and explicitly clears its protected key")
def test_provider_setup_secret_writer(opts):
    from mojo.apps.account.models import Setting
    from mojo.apps.account.services import provider_setup

    key = "GEOIP_API_KEY_MOJO"
    Setting.objects.filter(key=key, group=None).delete()
    with mock.patch.object(Setting, "_redis", return_value=None):
        provider_setup._write_secret(key, "rotation-secret", False)
    row = Setting.objects.get(key=key, group=None)
    assert row.is_secret and row.value == "" and row.get_value() == "rotation-secret", \
        "the dedicated provider writer did not encrypt the GeoIP credential"
    with mock.patch.object(Setting, "_redis", return_value=None):
        provider_setup._write_secret(key, None, True)
    assert not Setting.objects.filter(key=key, group=None).exists(), \
        "the explicit provider credential clear left a protected row behind"


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

    # The split writer owns one topic and nothing else: no secret write is
    # mocked out here because the SMS path never reaches one.
    provider_setup._save_sms_config(sms)

    active.refresh_from_db()
    inactive.refresh_from_db()
    assert active.provider == "mojo" and active.name == "Mojo Remote SMS", \
        f"the effective system row was not converted to Mojo: {active.provider!r}"
    assert active.mojo_remote_url == "https://sms.example.com" and active.test_mode, \
        "the system Mojo configuration did not receive the submitted values"
    assert active.is_active and not inactive.is_active, \
        "provider setup left an ambiguous active system default"
    PhoneConfig.objects.filter(pk__in=(active.pk, inactive.pk)).delete()


@th.django_unit_test("one provider topic saves and fails without touching the other")
def test_provider_setup_topic_isolation(opts):
    from mojo import errors as me
    from mojo.apps.account.models import User
    from mojo.apps.account.services import provider_setup

    actor = User.objects.get(pk=opts.settings_admin)
    actor.is_superuser = True
    actor.save(update_fields=["is_superuser"])
    static = _fleet_static(provider_setup)
    s3 = mock.Mock()

    def credentials(topic, section):
        if topic == "geoip":
            return {"geoip": {"success": False, "code": "http_401",
                              "message": "api.mojoverify.com rejected the API key"}}, False
        return {"sms": {"success": True, "code": None,
                        "message": "Connection verified"}}, False

    def fleet_patches(stack):
        for patcher in (
                mock.patch.object(provider_setup, "_static", side_effect=lambda key, default=None: static.get(key, default)),
                mock.patch.object(provider_setup, "_s3_client", return_value=s3),
                mock.patch.object(provider_setup, "_published", return_value=None),
                mock.patch.object(provider_setup, "_test_provider_credentials", side_effect=credentials),
                mock.patch.object(provider_setup, "_configuration_revision", return_value=None),
                mock.patch.object(provider_setup, "_write_configuration_revision"),
                mock.patch.object(provider_setup, "_write_verify_state"),
                mock.patch.object(provider_setup, "_audit")):
            stack.enter_context(patcher)

    # A rejected GeoIP key is the exact situation the old combined form turned
    # into "you cannot save SMS either".
    with ExitStack() as stack:
        geo_write = stack.enter_context(
            mock.patch.object(provider_setup, "_save_geoip_secret"))
        sms_write = stack.enter_context(
            mock.patch.object(provider_setup, "_save_sms_config"))
        fleet_patches(stack)
        with th.assert_raises(me.ValueException):
            provider_setup.apply(actor, "geoip", _geoip_payload(provider_setup))
        assert not geo_write.called and not sms_write.called, \
            "a rejected GeoIP credential still wrote provider configuration"
        result = provider_setup.apply(actor, "sms", _sms_payload())
        assert result["topic"] == "sms", \
            f"an SMS save was blocked by the failing GeoIP credential: {result!r}"
        assert sms_write.call_count == 1 and not geo_write.called, \
            "the SMS save wrote the wrong topic's storage"
        assert not s3.put_object.called, \
            "an SMS save published the fleet configuration document"
        # A payload carrying the untouched topic is a rejection, not an ignored
        # extra: the browser must not believe it saved something this skipped.
        with th.assert_raises(me.ValueException):
            provider_setup.apply(
                actor, "sms", {**_sms_payload(), **_geoip_payload(provider_setup)})

    # An installation with no fleet publishing plane at all still saves SMS,
    # and never even builds an S3 client to do it.
    bare = {}
    with mock.patch.object(provider_setup, "_static", side_effect=lambda key, default=None: bare.get(key, default)), \
            mock.patch.object(provider_setup, "_s3_client") as client, \
            mock.patch.object(provider_setup, "_test_provider_credentials", side_effect=credentials), \
            mock.patch.object(provider_setup, "_configuration_revision", return_value=None), \
            mock.patch.object(provider_setup, "_write_configuration_revision"), \
            mock.patch.object(provider_setup, "_write_verify_state"), \
            mock.patch.object(provider_setup, "_save_sms_config") as offline_write, \
            mock.patch.object(provider_setup, "_audit"):
        offline = provider_setup.apply(actor, "sms", _sms_payload())
        with th.assert_raises(me.ValueException):
            provider_setup.apply(actor, "geoip", _geoip_payload(provider_setup))
    assert offline["topic"] == "sms" and offline_write.call_count == 1, \
        f"SMS could not be saved without a fleet publishing plane: {offline!r}"
    assert not client.called, \
        "an SMS save built an S3 client on an installation with no AWS config"


@th.django_unit_test("a stored key is identified by four characters and never returned")
def test_provider_setup_key_hint_contract(opts):
    import json
    from mojo.apps.account.models import Setting
    from mojo.apps.account.services import provider_setup

    for value in ("", None, 12345678, "short", "1234567"):
        assert provider_setup._key_hint(value) == "", \
            f"a value too short to hint safely produced one: {value!r}"
    assert provider_setup._key_hint("12345678") == "5678", \
        "an eight-character key did not produce its last four characters"
    secret = "mojo_live_key_0123456789abcdef"
    assert provider_setup._key_hint(secret) == secret[-4:] and \
        len(provider_setup._key_hint(secret)) == 4, \
        "the hint is not exactly the last four characters"
    assert not secret.startswith(provider_setup._key_hint(secret)), \
        "the hint is a prefix, which is the half of a key conventions publish"

    Setting.objects.filter(key="GEOIP_API_KEY_MOJO", group=None).delete()
    with mock.patch.object(Setting, "_redis", return_value=None):
        provider_setup._write_secret("GEOIP_API_KEY_MOJO", secret, False)
        report = provider_setup.state(include_remote=False)
    encoded = json.dumps(report)
    assert secret not in encoded, "the provider state returned a stored credential"
    assert report["geoip"]["GEOIP_API_KEY_MOJO_HINT"] == secret[-4:], \
        f"the stored GeoIP key produced no hint: {report['geoip']!r}"
    assert report["geoip"]["GEOIP_API_KEY_MOJO_CONFIGURED"] is True, \
        "a stored GeoIP key stopped reporting that it exists"
    assert "api_key_hint" in report["sms"], \
        f"the SMS section lost its key hint: {report['sms']!r}"
    assert report["geoip_providers"], \
        "the provider picker has no known providers to offer"
    Setting.objects.filter(key="GEOIP_API_KEY_MOJO", group=None).delete()


@th.django_unit_test("verify state records only the stored provider configuration")
def test_provider_setup_verify_state_persistence(opts):
    from mojo import errors as me
    from mojo.apps.account.models import Setting, User
    from mojo.apps.account.services import admin_settings, provider_setup

    key = provider_setup.VERIFY_STATE_KEY
    assert admin_settings.is_catalog_protected(key) is True, \
        "the verification record is writable through the generic settings API"
    Setting.objects.filter(key=key, group=None).delete()

    actor = User.objects.get(pk=opts.settings_admin)
    actor.is_superuser = True
    actor.save(update_fields=["is_superuser"])
    ok = {"success": True, "code": None, "message": "Connection verified"}
    bad = {"success": False, "code": "http_401",
           "message": "api.mojoverify.com rejected the API key"}

    provider_setup._write_verify_state("geoip", ok)
    provider_setup._write_verify_state("sms", ok)
    seeded = provider_setup._read_verify_state()
    assert seeded["geoip"]["ok"] is True and seeded["sms"]["ok"] is True, \
        f"the verification record did not persist both topics: {seeded!r}"

    row = Setting.objects.get(key=key, group=None)
    row.value = "{}"
    with th.assert_raises(me.PermissionDeniedException):
        row.save()

    # A draft nobody saved is not an observation of the installation.
    with mock.patch.object(provider_setup, "_test_provider_credentials",
                           return_value=({"sms": bad}, False)):
        provider_setup.test(actor, "sms", _sms_payload(api_key="typed-replacement"))
    assert provider_setup._read_verify_state()["sms"]["ok"] is True, \
        "a draft credential test overwrote the stored configuration's state"

    # Testing what is already stored is.
    with mock.patch.object(provider_setup, "_test_provider_credentials",
                           return_value=({"sms": bad}, True)):
        provider_setup.test(actor, "sms", _sms_payload())
    after = provider_setup._read_verify_state()
    assert after["sms"]["ok"] is False and after["sms"]["code"] == "http_401", \
        f"testing the stored SMS credential did not record its failure: {after!r}"
    assert after["geoip"]["ok"] is True, \
        "an SMS observation rewrote the unrelated GeoIP topic"

    static = _fleet_static(provider_setup)
    # A rejected candidate was never stored, so nothing about it is recorded.
    with mock.patch.object(provider_setup, "_static", side_effect=lambda name, default=None: static.get(name, default)), \
            mock.patch.object(provider_setup, "_s3_client", return_value=mock.Mock()), \
            mock.patch.object(provider_setup, "_published", return_value=None), \
            mock.patch.object(provider_setup, "_test_provider_credentials",
                              return_value=({"geoip": bad}, False)), \
            mock.patch.object(provider_setup, "_audit"), \
            th.assert_raises(me.ValueException):
        provider_setup.apply(actor, "geoip", _geoip_payload(provider_setup))
    assert provider_setup._read_verify_state()["geoip"]["ok"] is True, \
        "a failed publish recorded a configuration the installation never stored"

    with mock.patch.object(provider_setup, "_static", side_effect=lambda name, default=None: static.get(name, default)), \
            mock.patch.object(provider_setup, "_test_provider_credentials",
                              return_value=({"sms": ok}, False)), \
            mock.patch.object(provider_setup, "_published", return_value=None), \
            mock.patch.object(provider_setup, "_configuration_revision", return_value=None), \
            mock.patch.object(provider_setup, "_write_configuration_revision"), \
            mock.patch.object(provider_setup, "_save_sms_config"), \
            mock.patch.object(provider_setup, "_audit"):
        provider_setup.apply(actor, "sms", _sms_payload())
    saved = provider_setup._read_verify_state()
    assert saved["sms"]["ok"] is True, \
        f"a successful SMS save did not record its verification: {saved!r}"
    stored = Setting.objects.get(key=key, group=None)
    assert not stored.is_secret and "typed-replacement" not in str(stored.value), \
        f"the verification record retained credential material: {stored.value!r}"
    Setting.objects.filter(key=key, group=None).delete()


@th.django_unit_test("a failed credential names the host, never the key")
def test_provider_setup_diagnoses_host(opts):
    from mojo.apps.account.services import provider_setup
    from mojo.apps.phonehub.services import mojo_provider

    secret = "mojo_live_key_0123456789abcdef"
    responses = [
        mock.Mock(ok=False, code="http_401", permissions=None),
        mock.Mock(ok=False, code="timeout", permissions=None),
        mock.Mock(ok=True, permissions={}),
    ]
    with mock.patch.object(mojo_provider, "verify_credentials", side_effect=responses):
        rejected = provider_setup._credential_result(
            "https://api.mojoverify.com", secret, ())
        timed_out = provider_setup._credential_result(
            "https://api.mojoverify.com", secret, ())
        unpermitted = provider_setup._credential_result(
            "https://sms.example.com", secret, ("send_sms",))
    assert "api.mojoverify.com" in rejected["message"] and \
        "rejected" in rejected["message"], \
        f"a rejected key does not say who rejected it: {rejected!r}"
    assert "api.mojoverify.com" in timed_out["message"], \
        f"a timeout does not name the host that did not answer: {timed_out!r}"
    assert "sms.example.com" in unpermitted["message"] and \
        "send_sms" in unpermitted["message"], \
        f"a permission failure does not say what is missing: {unpermitted!r}"
    for result in (rejected, timed_out, unpermitted):
        assert secret not in result["message"] and secret[-4:] not in result["message"], \
            f"a diagnosis leaked credential material: {result!r}"


@th.django_unit_test("provider mutations name their topic behind an unchanged gate")
def test_settings_rest_topic_contract(opts):
    from mojo import errors as me
    from mojo.apps.account.models import User
    from mojo.apps.account.rest import admin_settings as views
    from mojo.apps.account.services import provider_setup

    func = views.on_admin_settings_mutate
    assert getattr(func, "_mojo_denies_key_backed_session", False), \
        "Settings mutation accepts a key-backed session"
    assert getattr(func, "_mojo_requires_fresh_auth", False) is True, \
        "Settings mutation lacks recent interactive authentication"
    assert getattr(func, "_mojo_fresh_auth_seconds", None) == 600, \
        "Settings recent-authentication window is not 600 seconds"
    source = (ROOT / "mojo/apps/account/rest/admin_settings.py").read_text()
    assert '{"action", "topic", "providers"}' in source, \
        "the provider branch does not require an explicit topic"
    assert "provider_setup.TOPICS" in source, \
        "the REST boundary invents its own topic list"
    assert "system_setup.request_origin(request)" in source and \
        "system_setup.require_request_admin(request)" in source, \
        "provider secret/fleet mutations lack the literal-superuser same-Origin gate"

    actor = User.objects.get(pk=opts.settings_admin)
    actor.is_superuser = True
    actor.save(update_fields=["is_superuser"])
    for topic in (None, "", "GEOIP", "both", "providers"):
        with th.assert_raises(me.ValueException):
            provider_setup.test(actor, topic, _sms_payload())
        with th.assert_raises(me.ValueException):
            provider_setup.apply(actor, topic, _sms_payload())

    # phonehub is optional and SMS is addressable on its own, so an absent app
    # must produce a sentence rather than an import failure inside a writer.
    from django.apps import apps as django_apps
    with mock.patch.object(django_apps, "is_installed", return_value=False), \
            th.assert_raises(me.ValueException):
        provider_setup.test(actor, "sms", _sms_payload())


@th.django_unit_test("Settings speaks plainly and guards before it formats")
def test_settings_plain_language_contract(opts):
    sources = {name: (ROOT / name).read_text() for name in SETTINGS_ASSETS}
    for name, source in sources.items():
        for banned in ("Configured", "Not configured", "None configured", "Open owner"):
            assert banned not in source, \
                f"{name} still reports that a value exists instead of what it does: {banned}"
    page = sources[SETTINGS_ASSETS[0]]
    assert "badge(" not in page, \
        "a settings row still spends a badge on provenance"
    assert "settings-grid" not in page and "settings-card" not in page, \
        "the settings card grid survived the row rewrite"

    language = sources[SETTINGS_ASSETS[1]]
    dispatch = language.index("SENTENCE[row.key]")
    for guard in ("Conflicting values saved", "Could not be read", "row.unset_meaning"):
        assert 0 < language.index(guard) < dispatch, \
            f"the {guard!r} guard runs after a formatter could narrate the value"
    for phrase in ("Not set — ", " before expiry", "registration open",
                   "HTTPS redirect ", "templates ready", "No fleet expected yet",
                   "DNSMAN_CERT_RENEW_DAYS", "EMAIL_DELIVERY_POSTURE"):
        assert phrase in language, f"the plain-language registry lost {phrase!r}"


def _owner_principal(username, superuser=False, permissions=None):
    """Build one throwaway principal; callers delete the name before and after."""
    from mojo.apps.account.models import User
    user = User.objects.create_user(
        email=f"{username}@test.com", username=username, password="example")
    user.is_active = True
    user.is_superuser = superuser
    user.permissions = dict(permissions or {})
    user.save()
    return user


@th.django_unit_test("advertised owner_edit matches the writer that actually decides")
def test_owner_edit_matches_writer_authority(opts):
    from mojo import errors as me
    from mojo.apps.account.models import User
    from mojo.apps.account.rest import admin_settings as views
    from mojo.apps.account.services import system_settings

    # These principals are this test's own: the module fixture users are
    # permanently promoted by earlier tests and cannot state a truth table.
    names = ("owner-edit-super", "owner-edit-advanced", "owner-edit-admin",
             "owner-edit-settings")
    User.objects.filter(username__in=names).delete()
    try:
        # A superuser holding no permission keys at all is the case the
        # collapsed conjunction could never tell apart from a real grant check.
        root = _owner_principal("owner-edit-super", superuser=True)
        assert views._capabilities(root)["owner_edit"] is True, \
            "a literal superuser was not offered the owner-tier settings editor"
        # The patch is deliberately empty: it clears require_system_admin and is
        # then refused by validation, so authority is proven without persisting a
        # global AUTH_CONFIG row that every later reader would inherit.
        with th.assert_raises(me.ValueException):
            system_settings.set_auth_safe_fields(root, {})

        for username, permission in (
                ("owner-edit-advanced", "manage_advanced"),
                ("owner-edit-admin", "admin"),
                ("owner-edit-settings", "manage_settings")):
            actor = _owner_principal(username, permissions={permission: True})
            assert views._capabilities(actor)["owner_edit"] is False, \
                f"{permission} was advertised an owner editor the writer refuses"
            with th.assert_raises(me.PermissionDeniedException):
                system_settings.set_auth_safe_fields(actor, {})
    finally:
        User.objects.filter(username__in=names).delete()


@th.django_unit_test("owner_edit has one authority, quoted by both advertisers")
def test_owner_edit_has_one_authority(opts):
    from mojo.apps.account.models import User
    from mojo.apps.account.services import system_settings

    for name in ("mojo/apps/account/rest/admin_settings.py",
                 "mojo/apps/account/rest/admin_portal.py"):
        source = (ROOT / name).read_text()
        assert "can_system_admin" in source, \
            f"{name} advertises owner-tier edit without asking the writer's predicate"

    names = ("owner-authority-super", "owner-authority-advanced")
    User.objects.filter(username__in=names).delete()
    try:
        root = _owner_principal("owner-authority-super", superuser=True)
        granted = _owner_principal(
            "owner-authority-advanced", permissions={"manage_advanced": True})
        assert system_settings.can_system_admin(root) is True, \
            "the advisory predicate refused a literal superuser the writer accepts"
        assert system_settings.can_system_admin(granted) is False, \
            "the advisory predicate granted manage_advanced what require_system_admin denies"
    finally:
        User.objects.filter(username__in=names).delete()
