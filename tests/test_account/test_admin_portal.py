"""Private Admin delivery, feature bootstrap, and asset-boundary regressions."""

from contextlib import contextmanager
from unittest import mock

from testit import helpers as th


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


ADMIN_EMAIL = "admin_portal@test.com"
ADMIN_PASSWORD = "Admin_portal_pw_99"
USER_EMAIL = "admin_portal_regular@test.com"
USER_PASSWORD = "Admin_portal_regular_pw_99"


@th.django_unit_setup()
def setup_admin_portal_foundation(opts):
    from django.core.cache import cache
    from mojo.apps.account.models import User

    cache.clear()
    User.objects.filter(email__in=[ADMIN_EMAIL, USER_EMAIL]).delete()
    user = User.objects.create_user(username=ADMIN_EMAIL, email=ADMIN_EMAIL,
                                    password=ADMIN_PASSWORD)
    user.display_name = "Portal Admin"
    user.is_active = True
    user.is_email_verified = True
    user.requires_mfa = False
    user.is_superuser = True
    user.save()
    opts.portal_user_id = user.pk
    regular = User.objects.create_user(username=USER_EMAIL, email=USER_EMAIL,
                                       password=USER_PASSWORD)
    regular.is_active = True
    regular.is_email_verified = True
    regular.requires_mfa = False
    regular.save()


@th.django_unit_test("anonymous Admin delivery contains only the Bouncer handoff gate")
def test_anonymous_delivery_is_gate_only(opts):
    from testit.client import RestClient

    client = RestClient(opts.client.host)
    bare = client.get("/admin", allow_redirects=False)
    assert bare.status_code in (301, 302, 307, 308)
    root = client.get("/admin/")
    assert root.status_code == 200 and "Checking your secure session" in root.text
    assert client.get("/admin/assets/app.js").status_code == 404
    assert client.get("/admin/assets/features/people/feature.js").status_code == 404
    assert client.get("/admin/assets/features/advanced/page.js").status_code == 404
    assert client.get("/admin/assets/features/platform/page.js").status_code == 404
    assert client.get("/admin/assets/features/activity/page.js").status_code == 404
    assert client.get("/admin/assets/features/settings/page.js").status_code == 404


@th.django_unit_test("interactive JWT creates a path-scoped modular Admin source session")
def test_authenticated_admin_delivery(opts):
    assert opts.client.login(ADMIN_EMAIL, ADMIN_PASSWORD), "Admin login failed"
    issued = opts.client.post("/api/account/admin/session", json={})
    assert issued.status_code == 200, issued.response
    cookie = next((item for item in opts.client.session.cookies
                   if item.name == "mojo_admin"), None)
    assert cookie is not None and cookie.path == "/admin", cookie
    assert "HttpOnly" in cookie._rest and cookie._rest.get("SameSite") == "Strict", cookie._rest

    bootstrap = opts.client.get("/api/account/admin/bootstrap")
    data = bootstrap.json.get("data") or {}
    assert data.get("user", {}).get("username") == ADMIN_EMAIL, \
        "Admin bootstrap omitted the signed-in username required for inline recent authentication"
    assert data.get("capabilities", {}).get("manage_network") is True, data
    assert data.get("edge") == {
        "available": True, "http_enabled": True,
        "dnsman_issuance": "dns-01"}, \
        f"Admin bootstrap omitted the edge certificate posture: {data.get('edge')}"
    assert tuple(data.get("features", {})) == (
        "dashboard", "people", "webapps", "activity", "platform", "advanced",
        "settings", "sms", "email"), data.get("features")
    assert data["features"]["activity"] == {
        "id": "activity", "enabled": True,
        "capabilities": {
            "view_logs": True, "view_security": True,
            "manage_security": True,
        },
    }, data["features"]["activity"]
    assert data["features"]["platform"]["capabilities"]["setup"] is True, data["features"]["platform"]
    assert data["features"]["advanced"]["capabilities"]["manage"] is True, data["features"]["advanced"]
    assert data["features"]["settings"]["capabilities"]["owner_edit"] is True, data["features"]["settings"]

    opts.client.logout()
    assert "<title>MOJO Admin</title>" in opts.client.get("/admin/").text, "Admin shell unavailable"
    for asset in ("assets/app.js", "assets/features/registry.js",
                  "assets/features/advanced/page.js",
                  "assets/features/platform/page.js",
                  "assets/features/activity/page.js",
                  "assets/features/settings/page.js",
                  "assets/components/relationship.js"):
        response = opts.client.get(f"/admin/{asset}")
        assert response.status_code == 200, f"private asset unavailable: {asset}"
        assert "no-store" in opts.client.last_response.headers.get("cache-control", ""), \
            opts.client.last_response.headers


@th.django_unit_test("auth-key rotation invalidates the Admin source session")
def test_auth_key_rotation_revokes_source(opts):
    from mojo.apps.account.models import User

    assert opts.client.login(ADMIN_EMAIL, ADMIN_PASSWORD)
    assert opts.client.post("/api/account/admin/session", json={}).status_code == 200
    user = User.objects.get(pk=opts.portal_user_id)
    user.auth_key = "rotated-admin-auth-key"
    user.save(update_fields=["auth_key", "modified"])
    opts.client.logout()
    assert "Checking your secure session" in opts.client.get("/admin/").text


@th.django_unit_test("ordinary authenticated users cannot mint an Admin source session")
def test_non_admin_source_session_denied(opts):
    assert opts.client.login(USER_EMAIL, USER_PASSWORD)
    assert opts.client.post("/api/account/admin/session", json={}).status_code == 403


@th.django_unit_test("forced Bouncer reauth suppresses silent refresh loops")
def test_force_reauth_context(opts):
    from django.test import RequestFactory
    from mojo.apps.account.rest.bouncer.views import _auth_context

    request = RequestFactory().get("/auth", {"redirect": "/admin/", "force_reauth": "1"})
    request.DATA = request.GET
    context = _auth_context(request)
    assert context["skip_session_check"] is True
    assert "force_reauth=1" in context["register_url"]


@th.django_unit_test("a malformed feature provider fails closed without hiding its siblings")
def test_malformed_feature_provider_fails_closed(opts):
    from mojo.apps.account.services import admin_features

    with mock.patch(
            "mojo.apps.account.services.admin_features.people.describe",
            return_value={"id": "people", "enabled": "yes", "capabilities": {}}):
        features = admin_features.bootstrap_features(None, {})
    assert features["people"] == {"id": "people", "enabled": False,
                                   "capabilities": {}}
    assert features["dashboard"]["enabled"] is True


@th.django_unit_test("the exact Admin manifest rejects unknown and traversal paths")
def test_private_asset_manifest_is_exact(opts):
    from mojo.apps.account.services import admin_assets

    assert admin_assets.load_manifest() == admin_assets.PRIVATE_ASSETS
    assert "assets/features/people/feature.js" in admin_assets.PRIVATE_ASSETS
    assert admin_assets.asset_path("assets/features/people/feature.js").is_file()
    for value in ("assets/pages.js", "manifest.json", "../memory.md",
                  "assets/./app.js", "assets//app.js",
                  "assets/features/people/../platform/page.js", "/etc/passwd"):
        assert admin_assets.asset_path(value) is None, value


# ── INFRASTRUCTURE_MODE ─────────────────────────────────────────────────────

def _bootstrap(opts):
    import inspect
    from mojo.apps.account.models import User
    from mojo.apps.account.rest import admin_portal as views

    user = User.objects.get(pk=opts.portal_user_id)
    return inspect.unwrap(views.on_admin_bootstrap)(mock.Mock(user=user))


@th.django_unit_test("bootstrap publishes the infrastructure mode as a fact and a capability")
def test_bootstrap_publishes_infrastructure_mode(opts):
    managed = _bootstrap(opts)
    with _override_setting("INFRASTRUCTURE_MODE", "external"):
        external = _bootstrap(opts)

    assert managed["infrastructure"] == {"mode": "managed", "managed": True}, \
        f"a default installation is not published as managed: {managed['infrastructure']}"
    assert managed["capabilities"]["infrastructure_managed"] is True, \
        "the managed capability is missing from a default installation"
    assert external["infrastructure"] == {"mode": "external", "managed": False}, \
        f"external mode is not published: {external['infrastructure']}"
    assert external["capabilities"]["infrastructure_managed"] is False, \
        "the capability did not flip with the mode"

    # The feature validator accepts named booleans only, so the mode STRING
    # must never ride inside a feature's capabilities — it would disable the
    # whole lane rather than describe it.
    for payload in (managed, external):
        for name, feature in payload["features"].items():
            for key, value in feature["capabilities"].items():
                assert isinstance(value, bool), \
                    f"features.{name}.capabilities.{key} is not a bool: {value!r}"


@th.django_unit_test("bootstrap publishes the file-only edge HTTP posture")
def test_bootstrap_publishes_edge_http_posture(opts):
    from mojo.apps.account.models.setting import Setting

    Setting.set("EDGE_HTTP_ENABLED", True, group=None)
    try:
        with _override_setting("EDGE_HTTP_ENABLED", "false"):
            disabled = _bootstrap(opts)
        with _override_setting("EDGE_HTTP_ENABLED", "true"):
            enabled = _bootstrap(opts)
    finally:
        Setting.remove("EDGE_HTTP_ENABLED", group=None)

    assert disabled["edge"] == {
        "available": True, "http_enabled": False,
        "dnsman_issuance": "dns-01"}, \
        f"file-disabled edge posture is wrong: {disabled['edge']}"
    assert enabled["edge"]["http_enabled"] is True, \
        f"file-enabled edge posture is wrong: {enabled['edge']}"

    from mojo.apps.account.rest import admin_portal as views
    with mock.patch.object(views.apps, "is_installed", return_value=False):
        absent = _bootstrap(opts)
    assert absent["edge"] == {
        "available": False, "http_enabled": None,
        "dnsman_issuance": None}, \
        f"an installation without optional Edge did not degrade safely: {absent['edge']}"


@th.django_unit_test("the Platform lane never opens on the infrastructure flag alone")
def test_platform_feature_enabled_ignores_infrastructure_flag(opts):
    from mojo.apps.account.services.admin_features import platform, webapps

    lane = platform.describe(None, {"infrastructure_managed": True})
    assert lane["enabled"] is False, \
        ("a caller with no platform grant was given the Platform lane by a flag "
         f"that is true on every managed install: {lane}")
    assert lane["capabilities"]["infrastructure_managed"] is True, \
        "the flag was dropped instead of merely being kept out of `enabled`"

    merged = webapps.describe(None, {"infrastructure_managed": True})
    assert merged["enabled"] is False, \
        f"the Deployments lane opened on the infrastructure flag alone: {merged}"
    assert merged["capabilities"]["infrastructure_managed"] is True, \
        "the Deployments lane dropped the flag"


@th.django_unit_test("the infrastructure capability survives feature validation as a bool")
def test_infrastructure_capability_survives_validation(opts):
    from mojo.apps.account.services import admin_features

    for managed in (True, False):
        features = admin_features.bootstrap_features(
            None, {"infrastructure_managed": managed})
        for name in ("platform", "webapps"):
            capabilities = features[name]["capabilities"]
            assert "infrastructure_managed" in capabilities, \
                (f"{name} lost the infrastructure flag in validation — the "
                 f"provider was rejected and failed closed: {features[name]}")
            assert capabilities["infrastructure_managed"] is managed, \
                (f"{name} published {capabilities['infrastructure_managed']!r} "
                 f"for a mode of managed={managed}")
