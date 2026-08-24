"""Curated Admin Settings catalog and feature contracts (default tier).

Read-only half of the original module: catalog and descriptor-registry
reads, decorator-attribute assertions, and asset/source contracts. The
mutation matrices — the dedicated writer, provider setup, and catalog
redaction — moved to
tests/test_account_admin_extended_serial/test_admin_settings.py (maestro
item #1839) because they patch production module attributes and write
protected Setting rows, which is unsafe under the parallel default tier.
"""

from pathlib import Path
from unittest import mock

from testit import helpers as th

TESTIT_TIER = "admin"


ROOT = Path(__file__).resolve().parents[2]


CATALOG_KEYS = (
    "ALLOW_EMAIL_CHANGE", "ALLOW_PHONE_CHANGE", "ALLOW_USERNAME_CHANGE",
    "ALLOW_SELF_DEACTIVATION", "WEBAPP_BASE_URL",
)


SETTINGS_ASSETS = (
    "mojo/apps/account/admin_portal/assets/features/settings/page.js",
    "mojo/apps/account/admin_portal/assets/features/settings/language.js",
    "mojo/apps/account/admin_portal/assets/features/settings/panels.js",
)


@th.django_unit_setup()
def setup_admin_settings(opts):
    from mojo.apps.account.models import User
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
    opts.settings_admin = admin.pk
    opts.settings_denied = denied.pk


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
    # `busy: {title:` rather than `openBusy`: every save still runs behind the
    # scrim, but it is opened by the shared runAction wrapper now instead of by
    # hand — see docs/django_developer/account/admin_portal/responsiveness.md.
    for contract in ("Search settings", "busy: {title:", "apiOnce", "configure_providers",
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
