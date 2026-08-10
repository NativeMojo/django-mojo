"""Private Admin source delivery and bootstrap boundary."""

import uuid
from unittest import mock

from testit import helpers as th


ADMIN_EMAIL = "admin_portal@test.com"
ADMIN_PASSWORD = "Admin_portal_pw_99"
USER_EMAIL = "admin_portal_regular@test.com"
USER_PASSWORD = "Admin_portal_regular_pw_99"


@th.django_unit_setup()
def setup_admin_portal(opts):
    from django.core.cache import cache
    from mojo.apps.account.models import User

    cache.clear()
    User.objects.filter(email__in=[ADMIN_EMAIL, USER_EMAIL]).delete()
    user = User.objects.create_user(
        username=ADMIN_EMAIL, email=ADMIN_EMAIL, password=ADMIN_PASSWORD)
    user.display_name = "Portal Admin"
    user.is_active = True
    user.is_email_verified = True
    user.requires_mfa = False
    user.is_superuser = True
    user.save()
    opts.portal_user_id = user.pk

    regular = User.objects.create_user(
        username=USER_EMAIL, email=USER_EMAIL, password=USER_PASSWORD)
    regular.is_active = True
    regular.is_email_verified = True
    regular.requires_mfa = False
    regular.save()


@th.django_unit_test("anonymous Admin delivery contains only the Bouncer handoff gate")
def test_anonymous_delivery_is_gate_only(opts):
    from testit.client import RestClient

    client = RestClient(opts.client.host)
    bare = client.get("/admin", allow_redirects=False)
    assert bare.status_code in (301, 302, 307, 308), \
        "slashless Admin path did not redirect to the asset-safe canonical URL"
    root = client.get("/admin/")
    assert root.status_code == 200, f"Admin gate failed: {root.status_code}"
    assert "Checking your secure session" in root.text, \
        "anonymous root did not receive the tiny session gate"
    assert "Round-one control center" not in root.text, \
        "anonymous root disclosed private portal markup"
    asset = client.get("/admin/assets/app.js")
    assert asset.status_code == 404, \
        f"anonymous caller received private JavaScript ({asset.status_code})"


@th.django_unit_test("interactive JWT creates a path-scoped private source session")
def test_authenticated_admin_delivery(opts):
    assert opts.client.login(ADMIN_EMAIL, ADMIN_PASSWORD), "Admin login failed"
    issued = opts.client.post("/api/account/admin/session", json={})
    assert issued.status_code == 200, f"source session failed: {issued.body}"
    cookie = next((item for item in opts.client.session.cookies
                   if item.name == "mojo_admin"), None)
    assert cookie is not None, "source session response did not set its cookie"
    assert cookie.path == "/admin", \
        f"source cookie was not scoped to /admin: {cookie.path}"
    assert "HttpOnly" in cookie._rest and cookie._rest.get("SameSite") == "Strict", \
        f"source cookie missed hardening attributes: {cookie._rest}"

    bootstrap = opts.client.get("/api/account/admin/bootstrap")
    capabilities = (bootstrap.json.get("data") or {}).get("capabilities") or {}
    assert bootstrap.status_code == 200 and capabilities.get("network") is True, \
        "superuser bootstrap did not advertise the permanent network controls"
    assert capabilities.get("manage_network") is True, \
        "superuser bootstrap did not advertise network mutation access"

    opts.client.logout()  # Browser navigation carries the cookie, not Authorization.
    root = opts.client.get("/admin/")
    assert root.status_code == 200 and "<title>MOJO Admin</title>" in root.text, \
        "valid source session did not receive the private shell"
    asset = opts.client.get("/admin/assets/app.js")
    assert asset.status_code == 200 and "admin/bootstrap" in asset.text, \
        "valid source session did not receive the private JavaScript"
    setup_asset = opts.client.get("/admin/assets/setup.js")
    assert setup_asset.status_code == 200 and "Run all checks" in setup_asset.text, \
        "valid source session did not receive the private System Setup module"
    network_asset = opts.client.get("/admin/assets/network.js")
    assert network_asset.status_code == 200 and "MutationCoordinator" in network_asset.text, \
        "valid source session did not receive the private network module"
    cache_control = next((value for key, value in
                          opts.client.last_response.headers.items()
                          if key.lower() == "cache-control"), "")
    assert "no-store" in cache_control, \
        f"private source was cacheable: {cache_control}"


@th.django_unit_test("auth-key rotation invalidates the Admin source session")
def test_auth_key_rotation_revokes_source(opts):
    from mojo.apps.account.models import User

    assert opts.client.login(ADMIN_EMAIL, ADMIN_PASSWORD), "Admin login failed"
    assert opts.client.post("/api/account/admin/session", json={}).status_code == 200
    user = User.objects.get(pk=opts.portal_user_id)
    user.auth_key = "rotated-admin-auth-key"
    user.save(update_fields=["auth_key", "modified"])

    opts.client.logout()  # Match a document navigation: source cookie only.
    root = opts.client.get("/admin/")
    assert root.status_code == 200 and "Checking your secure session" in root.text, \
        "auth-key rotation did not return the browser to the public gate"


@th.django_unit_test("forced Bouncer reauth suppresses silent refresh loops")
def test_force_reauth_context(opts):
    from django.test import RequestFactory
    from mojo.apps.account.rest.bouncer.views import _auth_context

    request = RequestFactory().get(
        "/auth", {"redirect": "/admin/", "force_reauth": "1"})
    request.DATA = request.GET
    context = _auth_context(request)
    assert context["skip_session_check"] is True, \
        "forced Admin reauth would silently refresh and loop"
    assert "force_reauth=1" in context["register_url"], \
        "auth-method switch lost the forced-reauth intent"


@th.django_unit_test("ordinary authenticated users cannot mint an Admin source session")
def test_non_admin_source_session_denied(opts):
    assert opts.client.login(USER_EMAIL, USER_PASSWORD), "Regular-user login failed"
    response = opts.client.post("/api/account/admin/session", json={})
    assert response.status_code == 403, \
        f"regular user received an Admin source session ({response.status_code})"


@th.django_unit_test("anonymous callers cannot download the System Setup module")
def test_setup_source_is_private(opts):
    from testit.client import RestClient
    client = RestClient(opts.client.host)
    response = client.get("/admin/assets/setup.js")
    assert response.status_code == 404, \
        f"anonymous caller received System Setup source ({response.status_code})"

    network = client.get("/admin/assets/network.js")
    assert network.status_code == 404, \
        f"anonymous caller received network source ({network.status_code})"


@th.django_unit_test("portal assets encode provider-safe hosting workflows")
def test_network_asset_contract(opts):
    assert opts.client.login(ADMIN_EMAIL, ADMIN_PASSWORD), "Admin login failed"
    assert opts.client.post("/api/account/admin/session", json={}).status_code == 200
    opts.client.logout()

    setup = opts.client.get("/admin/assets/setup.js").text
    network = opts.client.get("/admin/assets/network.js").text
    assert "result.token" not in setup and "MOJO_DEPLOY_KEY" not in setup, \
        "System Setup learned how to read or render a deployment secret"
    assert "apiOnce" in network and "refresh-required" in network, \
        "DNS mutation safety or the authoritative-refresh latch disappeared"
    for shape in ("api", "site", "site_api", "redirect"):
        assert f"'{shape}'" in network, f"Vhost shape {shape} disappeared"
    for endpoint in (
            "/api/dnsman/registrar/purchase", "/api/dnsman/credential/link",
            "/api/dnsman/dns", "/api/dnsman/certificate/request",
            "/api/edge/upstream/declare", "/api/edge/vhost", "/api/edge/route"):
        assert endpoint in network, f"permanent control is missing {endpoint}"


@th.django_unit_test("authenticated portal covers missing active rotated and revoked deploy keys")
def test_webapp_key_portal_smoke(opts):
    from mojo.apps.account.models import ApiKey, Group
    from mojo.apps.edge.models import WebApp

    group_name = "admin_portal_key_lifecycle"
    Group.objects.filter(name=group_name).delete()
    group = Group.objects.create(name=group_name, kind="organization")
    site = WebApp(group=group, slug="portal-key-smoke", bucket="portal-test",
                  prefix="pending")
    try:
        with mock.patch("mojo.apps.edge.validators.validate_web_app"):
            site.save()
            site.prefix = site.storage_prefix()
            site.save()

            assert opts.client.login(ADMIN_EMAIL, ADMIN_PASSWORD), "Admin login failed"
            missing = opts.client.get(
                f"/api/edge/webapp/key_status?webapp={site.pk}")
            missing_data = (missing.json.get("data") or {}).get("status") or {}
            assert missing.status_code == 200 and missing_data.get("linked") is False, \
                "a fresh WebApp did not report a missing deployment key"
            assert "token" not in missing_data, "metadata status exposed a token field"

            minted = opts.client.post("/api/edge/webapp/link_key", json={
                "webapp": site.pk, "action": "mint",
                "operation_id": str(uuid.uuid4()),
            })
            minted_data = minted.json.get("data") or {}
            assert minted.status_code == 200 and minted_data.get("token"), \
                "the reveal-once create response did not contain the deployment key"
            active = opts.client.get(
                f"/api/edge/webapp/key_status?webapp={site.pk}")
            active_data = (active.json.get("data") or {}).get("status") or {}
            assert active_data.get("linked") is True and active_data.get("active") is True, \
                "the created deployment key did not become active"
            assert "token" not in active_data, "active status read the secret back"

            first_key = active_data.get("api_key")
            rotated = opts.client.post("/api/edge/webapp/link_key", json={
                "webapp": site.pk, "action": "rotate",
                "operation_id": str(uuid.uuid4()),
            })
            assert rotated.status_code == 200, "deployment-key rotation failed"
            rotated_status = ((opts.client.get(
                f"/api/edge/webapp/key_status?webapp={site.pk}").json.get("data")
                               or {}).get("status") or {})
            assert rotated_status.get("last_action") == "rotate", \
                "rotation metadata was not visible to the portal"
            assert rotated_status.get("api_key") != first_key, \
                "rotation kept the previous deployment key"
            assert "token" not in rotated_status, "rotated status read the secret back"

            revoked = opts.client.post("/api/edge/webapp/revoke_key", json={
                "webapp": site.pk, "operation_id": str(uuid.uuid4()),
            })
            assert revoked.status_code == 200, "deployment-key revoke failed"
            revoked_status = ((opts.client.get(
                f"/api/edge/webapp/key_status?webapp={site.pk}").json.get("data")
                               or {}).get("status") or {})
            assert revoked_status.get("linked") is False and \
                revoked_status.get("last_action") == "revoke", \
                "revoked deployment key was not distinguishable from never configured"
            assert "token" not in revoked_status, "revoked status exposed a token field"
    finally:
        with mock.patch("mojo.apps.edge.validators.validate_web_app"):
            WebApp.objects.filter(pk=site.pk).delete()
        ApiKey.objects.filter(name="webapp:portal-key-smoke").delete()
        Group.objects.filter(pk=group.pk).delete()
