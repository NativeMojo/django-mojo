"""Admin Sign-in page: system login look and feel + OAuth provider setup.

Serial/extended: writes the protected AUTH_CONFIG row and global OAuth
credential Setting rows.
"""

from testit import helpers as th

KEYS = ("APPLE_TEAM_ID", "APPLE_CLIENT_ID", "APPLE_KEY_ID", "APPLE_PRIVATE_KEY",
        "GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET",
        "GITHUB_CLIENT_ID", "GITHUB_CLIENT_SECRET",
        "MICROSOFT_CLIENT_ID", "MICROSOFT_CLIENT_SECRET", "MICROSOFT_TENANT",
        "MICROSOFT_CLIENT_SECRET_EXPIRES")
MS_GROUP_UUID = "msg1234567890abcdef01234567890ab"
OVERRIDE_GROUP_UUID = "sig5547override0123456789abcdef0"
MS_CLIENT_ID = "6731de76-14a6-49ae-97bc-6eba6914391e"
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
        assert "apple" in cfg["login"]["methods"] and "apple" in cfg["registration"]["methods"], \
            f"enabling Apple did not reach the hosted config: {cfg!r}"

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
        assert cfg["theme"]["layout"] == "editorial", \
            f"hosted login did not pick up the layout: {cfg['theme']!r}"

        resp = opts.client.post("/api/account/admin/signin", json={
            "auth": {"login.methods": ["password", "magic"]}})
        assert resp.status_code == 200, f"saving login methods failed: {resp.response!r}"
        cfg = opts.client.get("/api/auth/config").response.data
        # magic is in the defaults, so only the exact list proves the save took.
        assert cfg["login"]["methods"] == ["password", "magic"], \
            f"the saved system login methods did not reach the hosted config: {cfg['login']!r}"

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


def _notices(provider):
    return [(n["tone"], n["text"]) for n in provider.get("notices", [])]


@th.django_unit_test("a manage_settings admin configures Microsoft sign-in: write-only secret, validation, expiry notices, on/off")
def test_signin_microsoft_rest(opts):
    import datetime
    from mojo.apps.account.models import Group
    from mojo.helpers.settings import settings
    Group.objects.filter(uuid=MS_GROUP_UUID).delete()
    # A group with no auth_config of its own inherits the system methods.
    group = Group.objects.create(name="test-signin-ms", uuid=MS_GROUP_UUID, is_active=True)
    try:
        assert opts.client.login("signin-admin@test.com", "example"), "admin login failed"
        resp = opts.client.get("/api/account/admin/signin")
        assert resp.status_code == 200, f"GET signin failed: {resp.response!r}"
        data = resp.response.data
        ms = _provider(data, "microsoft")
        assert ms["label"] == "Microsoft", f"card label: {ms['label']!r}"
        assert ms["enabled"] is False, "Microsoft must start switched off"
        assert ms["ready"] is False and "Client secret value" in ms["missing"], \
            f"Microsoft without a secret must need setup: {ms!r}"
        assert "Tenant" not in ms["missing"], "an empty tenant reads as common and is not missing"
        assert "Secret expires on (YYYY-MM-DD)" not in ms["missing"], "the expiry date is optional"
        assert ms["callback_url"] and ms["callback_url"].endswith(
            "/api/auth/oauth/microsoft/callback"), f"bad callback URL: {ms!r}"
        assert ("info", "Record the secret's expiry date so this page can warn you") in _notices(ms), \
            f"no expiry recorded must give the info notice: {ms.get('notices')!r}"
        assert all(p.get("notices") == [] for p in data["providers"] if p["name"] != "microsoft"), \
            "other providers must report notices: []"
        assert "microsoft" in data["options"]["login_methods"], "microsoft must be an offered login method"

        # Validation: client ID must be a GUID, tenant a word or a GUID, expiry an ISO date.
        for values, why in (
                ({"MICROSOFT_CLIENT_ID": "not-a-guid"}, "a client ID that is not a GUID"),
                ({"MICROSOFT_TENANT": "contoso.com"}, "a tenant domain name"),
                ({"MICROSOFT_CLIENT_SECRET_EXPIRES": "12/31/2027"}, "a non-ISO expiry date")):
            resp = opts.client.post("/api/account/admin/signin",
                                    json={"provider": "microsoft", "values": values})
            assert resp.status_code == 400, f"{why} was accepted: {resp.status_code} {resp.response!r}"

        soon = (datetime.date.today() + datetime.timedelta(days=10)).isoformat()
        resp = opts.client.post("/api/account/admin/signin", json={
            "provider": "microsoft",
            "values": {"MICROSOFT_CLIENT_ID": MS_CLIENT_ID.upper(),
                       "MICROSOFT_CLIENT_SECRET": "ms~secret-value-9105",
                       "MICROSOFT_TENANT": "Organizations",
                       "MICROSOFT_CLIENT_SECRET_EXPIRES": soon},
            "enabled": True,
        })
        assert resp.status_code == 200, f"saving Microsoft failed: {resp.response!r}"
        ms = _provider(resp.response.data, "microsoft")
        assert ms["ready"] is True and ms["enabled"] is True, f"Microsoft not ready+enabled: {ms!r}"
        secret = next(f for f in ms["fields"] if f["key"] == "MICROSOFT_CLIENT_SECRET")
        assert secret["value"] is None and secret["hint"], f"the secret must be write-only with a hint: {secret!r}"
        assert "ms~secret-value-9105" not in str(resp.response), "the secret appeared in the response"
        assert settings.get("MICROSOFT_CLIENT_ID") == MS_CLIENT_ID, "the client ID must be stored lower-case"
        assert settings.get("MICROSOFT_TENANT") == "organizations", "the tenant must be stored lower-case"
        assert ("warning", f"Client secret expires on {soon}") in _notices(ms), \
            f"an expiry under 30 days must warn: {ms.get('notices')!r}"

        cfg = opts.client.get("/api/auth/config").response.data
        assert "microsoft" in cfg["login"]["methods"] and "microsoft" in cfg["registration"]["methods"], \
            f"enabling Microsoft did not reach the hosted config: {cfg!r}"
        resp = opts.client.get(f"/api/auth/oauth/microsoft/begin?group_uuid={MS_GROUP_UUID}")
        assert resp.status_code == 200, f"begin must work once enabled: {resp.status_code} {resp.response!r}"

        past = (datetime.date.today() - datetime.timedelta(days=1)).isoformat()
        resp = opts.client.post("/api/account/admin/signin", json={
            "provider": "microsoft", "values": {"MICROSOFT_CLIENT_SECRET_EXPIRES": past}})
        ms = _provider(resp.response.data, "microsoft")
        assert ("danger", f"Client secret expired on {past}; Microsoft sign-in fails until it is replaced") \
            in _notices(ms), f"a past expiry must give the danger notice: {ms.get('notices')!r}"

        resp = opts.client.post("/api/account/admin/signin", json={
            "provider": "microsoft", "enabled": False})
        ms = _provider(resp.response.data, "microsoft")
        assert ms["enabled"] is False and ms["ready"] is True, f"disable did not behave: {ms!r}"
        cfg = opts.client.get("/api/auth/config").response.data
        assert "microsoft" not in cfg["login"]["methods"] and \
            "microsoft" not in cfg["registration"]["methods"], f"disabling left Microsoft offered: {cfg!r}"
        resp = opts.client.get(f"/api/auth/oauth/microsoft/begin?group_uuid={MS_GROUP_UUID}")
        assert resp.status_code == 403, \
            f"begin must be refused once Microsoft is switched off: {resp.status_code} {resp.response!r}"
    finally:
        opts.client.logout()
        group.delete()
        _reset()


@th.django_unit_test("a group's auth_config still wins over the system config saved on the Sign-in page")
def test_signin_group_override_wins_over_system(opts):
    from mojo.apps.account.models import Group
    Group.objects.filter(uuid=OVERRIDE_GROUP_UUID).delete()
    group = Group.objects.create(
        name="test-signin-override", uuid=OVERRIDE_GROUP_UUID, is_active=True,
        metadata={"auth_config": {
            "theme": {"app_title": "Group Title"},
            "login": {"methods": ["password", "passkey"]},
        }})
    try:
        assert opts.client.login("signin-admin@test.com", "example"), "admin login failed"
        resp = opts.client.post("/api/account/admin/signin", json={"auth": {
            "theme.app_title": "System Title",
            "login.heading": "System heading 5547",
            "login.methods": ["password", "magic", "sms"]}})
        assert resp.status_code == 200, f"saving the system config failed: {resp.response!r}"

        cfg = opts.client.get("/api/auth/config").response.data
        assert cfg["theme"]["app_title"] == "System Title", \
            f"the system title did not reach the hosted config: {cfg['theme']!r}"

        cfg = opts.client.get(f"/api/auth/config?group_uuid={OVERRIDE_GROUP_UUID}").response.data
        assert cfg["theme"]["app_title"] == "Group Title", \
            f"the group's title must win over the system title: {cfg['theme']!r}"
        assert list(cfg["login"]["methods"]) == ["password", "passkey"], \
            f"the group's login methods must replace the system's: {cfg['login']!r}"
        assert cfg["login"]["heading"] == "System heading 5547", \
            f"a key the group does not set must inherit the system value: {cfg['login']!r}"
    finally:
        opts.client.logout()
        group.delete()
        _reset()


@th.django_unit_test("a secret saved on the Sign-in page is never returned by the generic settings endpoints")
def test_signin_secret_not_readable_via_generic_settings(opts):
    from mojo.apps.account.models import Setting
    secret = "gsecret-5547-never-shown"
    try:
        assert opts.client.login("signin-admin@test.com", "example"), "admin login failed"
        resp = opts.client.post("/api/account/admin/signin", json={
            "provider": "google", "values": {"GOOGLE_CLIENT_SECRET": secret}})
        assert resp.status_code == 200, f"saving the Google secret failed: {resp.response!r}"
        row = Setting.objects.filter(key="GOOGLE_CLIENT_SECRET", group__isnull=True).first()
        assert row is not None and row.is_secret, \
            f"the Google secret must be stored as a secret Setting row: {row!r}"

        resp = opts.client.get("/api/settings?search=GOOGLE_CLIENT_SECRET")
        assert resp.status_code == 200, f"settings search failed: {resp.status_code} {resp.response!r}"
        keys = [item.get("key") for item in (resp.response.data or [])]
        assert "GOOGLE_CLIENT_SECRET" in keys, \
            f"the search must find the row, or the leak check below proves nothing: {keys!r}"
        assert secret not in str(resp.response), "the secret appeared in the settings search"

        resp = opts.client.get(f"/api/settings/{row.pk}")
        assert resp.status_code == 200, f"settings detail failed: {resp.status_code} {resp.response!r}"
        assert resp.response.data.get("key") == "GOOGLE_CLIENT_SECRET", \
            f"settings detail returned another row: {resp.response!r}"
        assert secret not in str(resp.response), "the secret appeared in the settings detail"

        resp = opts.client.get("/api/account/admin/settings")
        assert resp.status_code == 200, \
            f"admin settings catalog failed: {resp.status_code} {resp.response!r}"
        assert secret not in str(resp.response), "the secret appeared in the admin settings catalog"

        resp = opts.client.get("/api/account/admin/signin")
        assert secret not in str(resp.response), "the secret appeared on the Sign-in page read"
    finally:
        opts.client.logout()
        _reset()


def _begin_client_id(opts, provider):
    from urllib.parse import parse_qs, urlsplit
    resp = opts.client.get(f"/api/auth/oauth/{provider}/begin")
    assert resp.status_code == 200, \
        f"{provider} begin failed once switched on: {resp.status_code} {resp.response!r}"
    auth_url = resp.response.data["auth_url"]
    return parse_qs(urlsplit(auth_url).query).get("client_id", [None])[0], auth_url


@th.django_unit_test("Google credentials saved on the Sign-in page are what sign-in uses, with no restart")
def test_signin_google_rest(opts):
    client_id = "5547-admin.apps.googleusercontent.com"
    secret = "gsecret-5547-begin"
    try:
        assert opts.client.login("signin-admin@test.com", "example"), "admin login failed"
        google = _provider(opts.client.get("/api/account/admin/signin").response.data, "google")
        assert google["callback_url"] and google["callback_url"].endswith(
            "/api/auth/oauth/google/callback"), f"bad callback URL: {google!r}"

        resp = opts.client.post("/api/account/admin/signin", json={
            "provider": "google",
            "values": {"GOOGLE_CLIENT_ID": client_id, "GOOGLE_CLIENT_SECRET": secret},
            "enabled": True})
        assert resp.status_code == 200, f"saving Google failed: {resp.response!r}"
        google = _provider(resp.response.data, "google")
        assert google["ready"] is True and google["enabled"] is True, \
            f"Google not ready+enabled after save: {google!r}"
        assert secret not in str(resp.response), "the Google secret appeared in the response"
        cfg = opts.client.get("/api/auth/config").response.data
        assert "google" in cfg["login"]["methods"] and "google" in cfg["registration"]["methods"], \
            f"enabling Google did not reach the hosted config: {cfg!r}"

        sent, auth_url = _begin_client_id(opts, "google")
        assert sent == client_id, \
            f"begin must use the client ID saved in Admin, got {sent!r} in {auth_url!r}"
        assert secret not in auth_url, "the client secret must never reach the browser"

        # Switching off hides the button only; /begin is not refused (Ian, #9275).
        resp = opts.client.post("/api/account/admin/signin", json={
            "provider": "google", "enabled": False})
        google = _provider(resp.response.data, "google")
        assert google["enabled"] is False and google["ready"] is True, f"disable did not behave: {google!r}"
        cfg = opts.client.get("/api/auth/config").response.data
        assert "google" not in cfg["login"]["methods"] and \
            "google" not in cfg["registration"]["methods"], f"disabling left Google offered: {cfg!r}"
    finally:
        opts.client.logout()
        _reset()


@th.django_unit_test("GitHub credentials saved on the Sign-in page override server config, with no restart")
def test_signin_github_rest(opts):
    client_id = "gh-admin-5547"
    secret = "ghsecret-5547-begin"
    try:
        assert opts.client.login("signin-admin@test.com", "example"), "admin login failed"
        github = _provider(opts.client.get("/api/account/admin/signin").response.data, "github")
        field = next(f for f in github["fields"] if f["key"] == "GITHUB_CLIENT_ID")
        assert field["source"] == "deployment" and field["configured"] is True, \
            f"the test project's GITHUB_CLIENT_ID must show as set in server config: {field!r}"
        assert github["callback_url"] and github["callback_url"].endswith(
            "/api/auth/oauth/github/callback"), f"bad callback URL: {github!r}"

        resp = opts.client.post("/api/account/admin/signin", json={
            "provider": "github",
            "values": {"GITHUB_CLIENT_ID": client_id, "GITHUB_CLIENT_SECRET": secret},
            "enabled": True})
        assert resp.status_code == 200, f"saving GitHub failed: {resp.response!r}"
        github = _provider(resp.response.data, "github")
        assert github["ready"] is True and github["enabled"] is True, \
            f"GitHub not ready+enabled after save: {github!r}"
        field = next(f for f in github["fields"] if f["key"] == "GITHUB_CLIENT_ID")
        assert field["source"] == "admin" and field["value"] == client_id, \
            f"the Admin value must replace the server-config one: {field!r}"
        assert secret not in str(resp.response), "the GitHub secret appeared in the response"
        cfg = opts.client.get("/api/auth/config").response.data
        assert "github" in cfg["login"]["methods"] and "github" in cfg["registration"]["methods"], \
            f"enabling GitHub did not reach the hosted config: {cfg!r}"

        sent, auth_url = _begin_client_id(opts, "github")
        assert sent == client_id, \
            f"begin must use the client ID saved in Admin, got {sent!r} in {auth_url!r}"
        assert secret not in auth_url, "the client secret must never reach the browser"

        # Switching off hides the button only; /begin is not refused (Ian, #9275).
        resp = opts.client.post("/api/account/admin/signin", json={
            "provider": "github", "enabled": False})
        github = _provider(resp.response.data, "github")
        assert github["enabled"] is False and github["ready"] is True, f"disable did not behave: {github!r}"
        cfg = opts.client.get("/api/auth/config").response.data
        assert "github" not in cfg["login"]["methods"] and \
            "github" not in cfg["registration"]["methods"], f"disabling left GitHub offered: {cfg!r}"
    finally:
        opts.client.logout()
        _reset()
