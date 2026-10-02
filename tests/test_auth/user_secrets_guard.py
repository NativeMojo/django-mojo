"""Maestro item #6226, Step 0 — an account's stored codes and protected state
cannot be written through REST or the websocket.

The REST save calls any `set_<key>` method for a posted key. On User that used
to reach `set_secrets`, where every one-time code is stored, so
`POST /api/user/me {"secrets": {"phone_verify_code": ...}}` planted a code
with no guessing, and an admin could plant a sign-in code on another account.
`permanent_password` reached `set_permanent_password`, which sets a password
without asking for the current one. The websocket `set_meta` message wrote any
metadata key, including the `protected` subtree REST reserves for a superuser.
"""
from testit import helpers as th
from testit.helpers import assert_true, assert_eq

TESTIT_TIER = "core"

OWNER = "usg_owner"
ADMIN = "usg_admin"
TARGET = "usg_target"
PWORD = "usg##mojo99Guard"
PLANTED = "123456"
STORED = "111111"
HIJACK_PWORD = "usg##Hijacked77pw"
PROTECTED = {"bouncer_state": "kept"}

# Keys the REST save may hand to a `set_<key>` method on User. Each one checks
# who is asking, in the setter or in on_rest_pre_save.
REST_SETTERS = frozenset((
    "password", "new_password", "username", "is_superuser", "is_staff",
    "permissions", "phone_number", "dob",
))


@th.django_unit_setup()
def setup_user_secrets_guard(opts):
    from mojo.apps.account.models import User
    from mojo.decorators.limits import clear_rate_limits

    clear_rate_limits(ip="127.0.0.1", key="login")
    User.objects.filter(username__in=[OWNER, ADMIN, TARGET]).delete()
    for name in (OWNER, ADMIN, TARGET):
        user = User(username=name, display_name=name, email=f"{name}@example.com")
        user.save()
        user.is_email_verified = True
        user.save_password(PWORD)
        user.remove_all_permissions()
        if name == ADMIN:
            user.add_permission(["manage_users"])
        setattr(opts, f"{name}_id", user.pk)

    target = User.objects.get(pk=opts.usg_target_id)
    target.set_secret("sms_otp_code", STORED)
    target.save()
    owner = User.objects.get(pk=opts.usg_owner_id)
    owner.metadata = {"protected": dict(PROTECTED), "theme": "light"}
    owner.save()


def _fresh(pk):
    from mojo.apps.account.models import User
    return User.objects.get(pk=pk)


@th.django_unit_test("user save: the owner cannot plant a stored code through `secrets`")
def test_owner_cannot_write_own_secrets(opts):
    import time

    assert_true(opts.client.login(OWNER, PWORD), "the owner must be able to log in")
    resp = opts.client.post("/api/user/me", {
        "secrets": {"phone_verify_code": PLANTED, "phone_verify_ts": int(time.time())}})
    assert_eq(resp.status_code, 200, f"an ignored key must not fail the save, got {resp.status_code}")
    owner = _fresh(opts.usg_owner_id)
    assert_eq(owner.get_secret("phone_verify_code"), None,
              "a posted `secrets` value must not reach the stored secrets")
    assert_eq(owner.get_secret("phone_verify_ts"), None,
              "a posted `secrets` value must not reach the stored secrets")


@th.django_unit_test("user save: `mojo_secrets` cannot be overwritten")
def test_owner_cannot_overwrite_mojo_secrets(opts):
    owner = _fresh(opts.usg_owner_id)
    owner.set_secret("usg_marker", "kept")
    owner.save()
    before = _fresh(opts.usg_owner_id).mojo_secrets
    assert_true(before, "setup must have stored an encrypted secrets blob")

    assert_true(opts.client.login(OWNER, PWORD), "the owner must be able to log in")
    resp = opts.client.post("/api/user/me", {"mojo_secrets": "usg-overwritten"})
    assert_eq(resp.status_code, 200, f"an ignored key must not fail the save, got {resp.status_code}")
    after = _fresh(opts.usg_owner_id)
    assert_true(after.mojo_secrets == before, "the encrypted secrets column must be unchanged")
    assert_eq(after.get_secret("usg_marker"), "kept", "the stored secrets must still decrypt")


@th.django_unit_test("user save: a manage_users admin cannot plant a sign-in code on another account")
def test_admin_cannot_write_another_accounts_secrets(opts):
    assert_true(opts.client.login(ADMIN, PWORD), "the admin must be able to log in")
    resp = opts.client.post(f"/api/user/{opts.usg_target_id}", {
        "secrets": {"sms_otp_code": PLANTED}})
    assert_eq(resp.status_code, 200, f"an ignored key must not fail the save, got {resp.status_code}")
    assert_eq(_fresh(opts.usg_target_id).get_secret("sms_otp_code"), STORED,
              "an admin's posted `secrets` must not replace the account's stored code")


@th.django_unit_test("user save: `permanent_password` cannot set a password without the current one")
def test_owner_cannot_set_password_through_permanent_password(opts):
    assert_true(opts.client.login(OWNER, PWORD), "the owner must be able to log in")
    resp = opts.client.post("/api/user/me", {"permanent_password": HIJACK_PWORD})
    assert_eq(resp.status_code, 200, f"an ignored key must not fail the save, got {resp.status_code}")
    owner = _fresh(opts.usg_owner_id)
    assert_true(owner.check_password(PWORD),
                "the password must be unchanged when no current password was given")
    assert_true(not owner.check_password(HIJACK_PWORD),
                "`permanent_password` must not set a new password")


@th.django_unit_test("user save: every `set_` method on User is REST-safe or not REST-writable")
def test_every_user_setter_is_accounted_for(opts):
    from mojo.apps.account.models import User

    setters = {name[4:] for name in dir(User)
               if name.startswith("set_") and callable(getattr(User, name))}
    no_save = set(User.get_rest_meta_prop("NO_SAVE_FIELDS", []))
    unguarded = sorted(setters - REST_SETTERS - no_save)
    assert_eq(unguarded, [],
              f"a posted key reaches User.set_<key>: add {unguarded} to "
              f"NO_SAVE_FIELDS, or to REST_SETTERS once the setter checks its caller")
    for key in ("secrets", "mojo_secrets"):
        assert_true(key in no_save, f"`{key}` must be in User.RestMeta.NO_SAVE_FIELDS")


@th.django_unit_test("websocket: `set_meta` cannot write metadata, protected or not")
def test_realtime_set_meta_writes_nothing(opts):
    ws_user = _fresh(opts.usg_owner_id)
    before = dict(ws_user.metadata or {})
    for key, value in (("protected", {"bouncer_state": "cleared"}), ("theme", "dark")):
        result = ws_user.on_realtime_message(
            {"message_type": "set_meta", "key": key, "value": value})
        response = (result or {}).get("response") or {}
        assert_true("key" not in response and "value" not in response,
                    f"`set_meta` must not acknowledge a write, got {result!r}")
    after = _fresh(opts.usg_owner_id).metadata or {}
    assert_eq(after.get("protected"), PROTECTED,
              "`set_meta` must not replace the protected metadata subtree")
    assert_eq(after.get("theme"), before.get("theme"),
              "`set_meta` must not write an ordinary metadata key either")


@th.django_unit_test("api key save: the owner cannot replace the key's signing secret")
def test_owner_cannot_write_user_api_key_secrets(opts):
    from mojo.apps.account.models import User
    from mojo.apps.account.models.user_api_key import UserAPIKey

    owner = User.objects.get(pk=opts.usg_owner_id)
    UserAPIKey.objects.filter(user=owner).delete()
    key = UserAPIKey.create_for_user(owner, expire_days=1, label="usg")
    before = UserAPIKey.objects.get(pk=key.id).get_auth_key()
    assert_true(before, "setup must have stored a signing secret")

    assert_true(opts.client.login(OWNER, PWORD), "the owner must be able to log in")
    resp = opts.client.post(f"/api/account/api_keys/{key.id}", {
        "label": "usg-renamed", "secrets": {"auth_key": "usg-planted"}})
    assert_eq(resp.status_code, 200, f"an ignored key must not fail the save, got {resp.status_code}")
    record = UserAPIKey.objects.get(pk=key.id)
    assert_eq(record.label, "usg-renamed", "an ordinary field in the same save must still be written")
    assert_true(record.get_auth_key() == before,
                "a posted `secrets` must not replace the key's signing secret")


@th.django_unit_test("account models that store secrets do not take them over REST")
def test_secret_stores_are_not_rest_writable(opts):
    from mojo.apps.account.models.api_key import ApiKey
    from mojo.apps.account.models.oauth import OAuthConnection
    from mojo.apps.account.models.user_api_key import UserAPIKey

    for model in (UserAPIKey, OAuthConnection, ApiKey):
        no_save = model.get_rest_meta_prop("NO_SAVE_FIELDS", [])
        for key in ("secrets", "mojo_secrets", "secret"):
            assert_true(key in no_save,
                        f"`{key}` must be in {model.__name__}.RestMeta.NO_SAVE_FIELDS")
    # A declared list replaces the framework default, so ApiKey restates it.
    for key in ("id", "pk", "created", "uuid"):
        assert_true(key in ApiKey.get_rest_meta_prop("NO_SAVE_FIELDS", []),
                    f"`{key}` must stay in ApiKey.RestMeta.NO_SAVE_FIELDS")


@th.django_unit_test("sign-in connection save: an admin cannot re-point another user's provider identity")
def test_admin_cannot_repoint_oauth_connection(opts):
    from mojo.apps.account.models.oauth import OAuthConnection

    OAuthConnection.objects.filter(provider_uid__startswith="usg-").delete()
    conn = OAuthConnection.objects.create(
        user_id=opts.usg_target_id, provider="google", provider_uid="usg-target-uid",
        email=f"{TARGET}@example.com")

    assert_true(opts.client.login(ADMIN, PWORD), "the admin must be able to log in")
    resp = opts.client.post(f"/api/account/oauth_connection/{conn.pk}", {
        "provider": "github", "provider_uid": "usg-admin-uid",
        "email": "usg-admin@example.com", "is_active": False})
    assert_eq(resp.status_code, 200, f"an ignored key must not fail the save, got {resp.status_code}")
    saved = OAuthConnection.objects.get(pk=conn.pk)
    assert_eq((saved.provider, saved.provider_uid, saved.email),
              ("google", "usg-target-uid", f"{TARGET}@example.com"),
              "the provider identity sign-in resolves by must not be writable through a save")
    assert_eq(saved.user_id, opts.usg_target_id, "the connection must stay with its user")
    assert_eq(saved.is_active, False, "an admin must still be able to deactivate a connection")
