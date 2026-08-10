"""Private Admin delivery, feature bootstrap, and asset-boundary regressions."""

from unittest import mock

from testit import helpers as th


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


@th.django_unit_test("interactive JWT creates a path-scoped modular Admin source session")
def test_authenticated_admin_delivery(opts):
    assert opts.client.login(ADMIN_EMAIL, ADMIN_PASSWORD), "Admin login failed"
    issued = opts.client.post("/api/account/admin/session", json={})
    assert issued.status_code == 200
    cookie = next((item for item in opts.client.session.cookies
                   if item.name == "mojo_admin"), None)
    assert cookie is not None and cookie.path == "/admin"
    assert "HttpOnly" in cookie._rest and cookie._rest.get("SameSite") == "Strict"

    bootstrap = opts.client.get("/api/account/admin/bootstrap")
    data = bootstrap.json.get("data") or {}
    assert data.get("capabilities", {}).get("manage_network") is True
    assert tuple(data.get("features", {})) == (
        "dashboard", "people", "webapps", "platform", "activity", "advanced")
    assert data["features"]["activity"] == {
        "id": "activity", "enabled": True,
        "capabilities": {
            "view_logs": True, "view_security": True,
            "manage_security": True,
        },
    }
    assert data["features"]["platform"]["capabilities"]["setup"] is True
    assert data["features"]["advanced"]["capabilities"]["manage"] is True

    opts.client.logout()
    assert "<title>MOJO Admin</title>" in opts.client.get("/admin/").text
    for asset in ("assets/app.js", "assets/features/registry.js",
                  "assets/features/advanced/page.js",
                  "assets/features/platform/page.js",
                  "assets/features/activity/page.js",
                  "assets/components/relationship.js"):
        response = opts.client.get(f"/admin/{asset}")
        assert response.status_code == 200, f"private asset unavailable: {asset}"
        assert "no-store" in opts.client.last_response.headers.get("Cache-Control", "")


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
