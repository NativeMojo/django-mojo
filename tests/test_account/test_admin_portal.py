"""Private Admin source delivery and bootstrap boundary."""

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

    opts.client.logout()  # Browser navigation carries the cookie, not Authorization.
    root = opts.client.get("/admin/")
    assert root.status_code == 200 and "<title>MOJO Admin</title>" in root.text, \
        "valid source session did not receive the private shell"
    asset = opts.client.get("/admin/assets/app.js")
    assert asset.status_code == 200 and "admin/bootstrap" in asset.text, \
        "valid source session did not receive the private JavaScript"
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
