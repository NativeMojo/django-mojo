"""Admin Sign-in page: system login look and feel + OAuth provider setup.

Serial/extended: writes the protected AUTH_CONFIG row and global OAuth
credential Setting rows.
"""

from testit import helpers as th

KEYS = ("APPLE_TEAM_ID", "APPLE_CLIENT_ID", "APPLE_KEY_ID", "APPLE_PRIVATE_KEY",
        "GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET")
PEM = "-----BEGIN PRIVATE KEY-----\nMIGTAgEAMBMGByqGSM49\nAgEGCCqGSM49AwEHBHkwdwIB\n-----END PRIVATE KEY-----"


def _reset():
    from mojo.apps.account.models import Setting
    from mojo.apps.account.services import system_settings
    for key in KEYS:
        Setting.remove(key)
    system_settings._store(system_settings.AUTH_CONFIG, {})


def _provider(data, name):
    return next(p for p in data["providers"] if p["name"] == name)


@th.django_unit_setup()
def setup_admin_signin(opts):
    from mojo.apps.account.models import User
    User.objects.filter(username__in=("signin-admin", "signin-plain")).delete()
    admin = User.objects.create_user(
        email="signin-admin@test.com", username="signin-admin", password="example")
    admin.is_active = True
    admin.is_email_verified = True
    admin.requires_mfa = False
    admin.save()
    admin.add_permission("manage_settings")
    admin.save()
    plain = User.objects.create_user(
        email="signin-plain@test.com", username="signin-plain", password="example")
    plain.is_active = True
    plain.save()
    opts.signin_admin = admin.pk
    opts.signin_plain = plain.pk
    _reset()


@th.django_unit_test("a manage_settings admin configures Apple sign-in end to end over REST")
def test_signin_apple_rest(opts):
    from mojo.helpers.settings import settings
    try:
        assert opts.client.login("signin-admin@test.com", "example"), \
            "the manage_settings admin could not log in"
        resp = opts.client.get("/api/account/admin/signin")
        assert resp.status_code == 200, f"GET signin failed: {resp.response!r}"
        apple = _provider(resp.response.data, "apple")
        assert apple["ready"] is False and "Key ID" in apple["missing"], \
            f"an unconfigured Apple provider reported ready: {apple!r}"
        assert apple["callback_url"] and apple["callback_url"].endswith(
            "/api/auth/oauth/apple/callback"), f"bad callback URL: {apple!r}"

        resp = opts.client.post("/api/account/admin/signin", json={
            "provider": "apple",
            "values": {"APPLE_TEAM_ID": "TEAM123456", "APPLE_CLIENT_ID": "com.example.signin",
                       "APPLE_KEY_ID": "KEY1234567", "APPLE_PRIVATE_KEY": PEM},
            "enabled": True,
        })
        assert resp.status_code == 200, f"saving Apple failed: {resp.response!r}"
        apple = _provider(resp.response.data, "apple")
        assert apple["ready"] is True and apple["enabled"] is True, \
            f"Apple not ready+enabled after save: {apple!r}"
        key_field = next(f for f in apple["fields"] if f["key"] == "APPLE_PRIVATE_KEY")
        assert key_field["value"] is None and key_field["source"] == "admin", \
            f"the private key leaked or has the wrong source: {key_field!r}"
        assert "BEGIN" not in str(resp.response), "the private key appeared in the response"
        team = next(f for f in apple["fields"] if f["key"] == "APPLE_TEAM_ID")
        assert team["value"] == "TEAM123456", f"non-secret value not shown: {team!r}"

        assert settings.get("APPLE_PRIVATE_KEY") == PEM, \
            "the OAuth provider would not read the stored private key back verbatim"
        assert settings.get("APPLE_CLIENT_ID") == "com.example.signin", \
            "the Services ID was not readable by the provider"

        cfg = opts.client.get("/api/auth/config").response.data
        assert "apple" in cfg["login"]["methods"], \
            f"enabling Apple did not reach the hosted login config: {cfg['login']!r}"

        # Blank keeps the stored value; disabling removes it from the methods.
        resp = opts.client.post("/api/account/admin/signin", json={
            "provider": "apple", "values": {"APPLE_PRIVATE_KEY": ""}, "enabled": False})
        apple = _provider(resp.response.data, "apple")
        assert apple["enabled"] is False and apple["ready"] is True, \
            f"disable/keep did not behave: {apple!r}"
        cfg = opts.client.get("/api/auth/config").response.data
        assert "apple" not in cfg["login"]["methods"] and \
            "apple" not in cfg["registration"]["methods"], \
            f"disabling Apple left it offered: {cfg!r}"

        # null clears.
        resp = opts.client.post("/api/account/admin/signin", json={
            "provider": "apple", "values": {"APPLE_KEY_ID": None}})
        apple = _provider(resp.response.data, "apple")
        assert apple["missing"] == ["Key ID"], f"clearing Key ID failed: {apple!r}"
    finally:
        opts.client.logout()
        _reset()


@th.django_unit_test("the system login look and feel is editable by a manage_settings admin")
def test_signin_auth_look_and_feel(opts):
    try:
        assert opts.client.login("signin-admin@test.com", "example"), "admin login failed"
        resp = opts.client.post("/api/account/admin/signin", json={"auth": {
            "theme.app_title": "Acme", "theme.layout": "editorial",
            "login.heading": "Welcome back"}})
        assert resp.status_code == 200, f"saving look and feel failed: {resp.response!r}"
        auth = resp.response.data["auth"]
        assert auth["theme"]["app_title"] == "Acme" and auth["theme"]["layout"] == "editorial", \
            f"theme change not applied: {auth['theme']!r}"
        cfg = opts.client.get("/api/auth/config").response.data
        assert cfg["login"]["heading"] == "Welcome back", \
            f"hosted login did not pick up the heading: {cfg['login']!r}"

        resp = opts.client.post("/api/account/admin/signin", json={
            "auth": {"login.methods": ["magic"]}})
        assert resp.status_code == 400, \
            f"removing password login must be refused: {resp.status_code} {resp.response!r}"
    finally:
        opts.client.logout()
        _reset()


@th.django_unit_test("the Sign-in page refuses users without manage_settings and bad input")
def test_signin_refusals(opts):
    try:
        assert opts.client.login("signin-plain@test.com", "example"), "plain login failed"
        resp = opts.client.get("/api/account/admin/signin")
        assert resp.status_code in (401, 403), \
            f"a user without manage_settings read the Sign-in page: {resp.status_code}"
        opts.client.logout()

        assert opts.client.login("signin-admin@test.com", "example"), "admin login failed"
        resp = opts.client.post("/api/account/admin/signin", json={
            "provider": "apple", "values": {"APPLE_PRIVATE_KEY": "not a key"}})
        assert resp.status_code == 400, f"a non-PEM Apple key was accepted: {resp.response!r}"
        resp = opts.client.post("/api/account/admin/signin", json={
            "provider": "apple", "values": {"GOOGLE_CLIENT_ID": "x"}})
        assert resp.status_code == 400, f"a foreign key was accepted for Apple: {resp.response!r}"
    finally:
        opts.client.logout()
        _reset()
