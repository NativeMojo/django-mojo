"""Maestro item #6226, part two — a password reset or change signs the account
out everywhere else.

Before this, only a forced password change, `revoke_sessions` and closing an
account replaced the account's signing key. A reset by code, a reset by link
and a password change while signed in left every token minted before them
valid: a stolen refresh token kept renewing after the owner had "fixed" the
account with a new password.

Now each of those ends every other session through one helper,
`User.end_sessions`, shared with `revoke_sessions`:

  - the signing key is replaced, so every session token, every OAuth-server
    access token and every unopened emailed link stops verifying;
  - the OAuth-server grants are revoked: their refresh tokens are not signed
    with the key and would otherwise keep minting;
  - the cached invite token is cleared, so the next invite mints one that works;
  - live websockets are dropped.

The device that did the reset or the change stays signed in: a reset answers
with new tokens as before, and a password change on the account save now
carries a `tokens` object beside the account data. An admin changing someone
else's password ends that person's sessions and gets no tokens for them.

An admin's temporary password and the forced change that follows it already
replaced the key. They now go through the same helper, so the grants and the
invite cache are covered there too.

This module posts from its own address, so it neither spends 127.0.0.1's
budget nor has to clear it under another module.
"""
import time
import uuid

from testit import helpers as th
from testit.helpers import assert_true, assert_eq

TESTIT_TIER = "core"

PWORD = "sr##mojo99Rotate"
NEW_PWORD = "sr##Changed77Pass"
RIGHT = "481516"

ADMIN = "sr_admin"
USERS = ("sr_admin", "sr_code", "sr_link", "sr_change", "sr_target",
         "sr_revoke", "sr_action", "sr_invite", "sr_helper", "sr_weak",
         "sr_temp", "sr_forced")

CLIENT_ID = "testit-session-rotation-client"
RESOURCE_PATH = "/api/testit/session-rotation-probe"
RESOURCE = "https://oauth.testit.example" + RESOURCE_PATH


def _new_ip():
    octets = uuid.uuid4().int
    return "10.%d.%d.%d" % ((octets >> 16) & 0xFF, (octets >> 8) & 0xFF, octets & 0xFF)


def _client(opts, ip=None):
    """A client with its own source address: nginx sets X-Real-IP to the true
    client and the framework trusts exactly that header."""
    from testit.client import RestClient
    client = RestClient(opts.client.host)
    client.headers["X-Real-IP"] = ip or _new_ip()
    return client


def _fresh(pk):
    from mojo.apps.account.models import User
    return User.objects.get(pk=pk)


def _reset_account(pk):
    """Back to the starting password, with no counters and no leftover codes."""
    from mojo.decorators import limits

    user = _fresh(pk)
    user.save_password(PWORD)
    limits.clear_account_limits(pk)
    limits.clear_code_attempts("reset", pk)
    return _fresh(pk)


def _sign_in(opts, username, password=PWORD):
    """One signed-in device: its client and the token pair it was given."""
    client = _client(opts)
    resp = client.post("/api/login", {"username": username, "password": password})
    data = resp.response.data
    assert_eq(resp.status_code, 200, f"{username} must be able to sign in, got {resp.status_code}: {resp.response}")
    assert_true(data and data.access_token and data.refresh_token,
                f"sign-in must answer with a token pair, got {data}")
    client.access_token = data.access_token
    client.is_authenticated = True
    return client, data


def _token_works(opts, access_token):
    """True when the access token still reads the account it belongs to."""
    client = _client(opts)
    client.access_token = access_token
    client.is_authenticated = True
    return client.get("/api/user/me").status_code == 200


def _refresh_works(opts, refresh_token):
    """True when the refresh token still buys a new pair."""
    return _client(opts).post("/api/auth/token/refresh", {"refresh_token": refresh_token}).status_code == 200


def _registry():
    from mojo.apps.account.services.oauth_server import resources

    registry = resources.ResourceRegistry()
    registry.register(RESOURCE_PATH, ["mcp"], lambda: True)
    return registry


def _oauth_pair(pk):
    """An OAuth-server grant for the account, and the pair a connector holds."""
    from mojo.apps.account.models import OAuthClient
    from mojo.apps.account.services.oauth_server import tokens

    client = OAuthClient.objects.get(client_id=CLIENT_ID)
    grant = tokens.create_grant(_fresh(pk), client, ["mcp"], RESOURCE, int(time.time()))
    return grant, client, tokens.issue_tokens(grant)


def _oauth_refresh_works(client, pair):
    from mojo.apps.account.services.oauth_server import tokens

    try:
        tokens.refresh_grant(pair["refresh_token"], client, registry=_registry())
    except tokens.TokenError:
        return False
    return True


def _grant_active(grant):
    from mojo.apps.account.models import OAuthGrant
    return OAuthGrant.objects.get(pk=grant.pk).is_active


def _assert_signed_out(opts, before, grant, oauth_client, pair, what):
    """Everything the account held before the change must be dead."""
    assert_true(not _token_works(opts, before.access_token),
                f"{what}: an access token from before must be refused")
    assert_true(not _refresh_works(opts, before.refresh_token),
                f"{what}: a refresh token from before must be refused, "
                f"or a stolen one keeps renewing after the password is fixed")
    assert_true(not _grant_active(grant),
                f"{what}: the account's OAuth-server grants must be revoked")
    assert_true(not _oauth_refresh_works(oauth_client, pair),
                f"{what}: an OAuth-server refresh token from before must be refused; "
                f"it is not signed with the account key, so only revoking the grant stops it")


def _assert_signed_in(opts, data, what):
    """The pair handed to the device that asked must work."""
    assert_true(data and data.access_token and data.refresh_token,
                f"{what}: the device that asked must get a token pair, got {data}")
    assert_true(_token_works(opts, data.access_token),
                f"{what}: the new access token must work")
    assert_true(_refresh_works(opts, data.refresh_token),
                f"{what}: the new refresh token must work")


@th.django_unit_setup()
def setup_session_rotation(opts):
    from mojo.apps.account.models import OAuthClient, User
    from mojo.decorators import limits

    User.objects.filter(username__in=list(USERS)).delete()
    OAuthClient.objects.filter(client_id=CLIENT_ID).delete()
    for name in USERS:
        user = User(username=name, display_name=name, email=f"{name}@example.com")
        user.save()
        user.is_email_verified = True
        user.save_password(PWORD)
        user.remove_all_permissions()
        if name == ADMIN:
            user.add_permission(["manage_users"])
        setattr(opts, f"{name}_id", user.pk)
        limits.clear_account_limits(user.pk)
    OAuthClient(client_id=CLIENT_ID, kind="dcr", client_name="Session Rotation",
                redirect_uris=["http://127.0.0.1:8200/cb"]).save()


# -----------------------------------------------------------------
# The helper
# -----------------------------------------------------------------

@th.django_unit_test("end_sessions: a new key, grants revoked, the invite cache cleared, sockets dropped")
def test_end_sessions_does_all_four(opts):
    from mojo.apps.account.utils import tokens

    pk = opts.sr_helper_id
    user = _reset_account(pk)
    old_key = user.get_auth_key()
    invite = tokens.get_or_generate_invite_token(user)
    grant, _client_row, _pair = _oauth_pair(pk)
    dropped = []

    user = _fresh(pk)
    user.end_sessions("testit", drop_sockets=lambda account, request=None: dropped.append(account.pk))

    stored = _fresh(pk)
    assert_true(stored.auth_key and stored.auth_key != old_key, "the stored signing key must be replaced")
    assert_eq(user.auth_key, stored.auth_key,
              "the caller's own copy must carry the new key, or the tokens it mints next are dead")
    assert_true(not _grant_active(grant), "the account's OAuth-server grants must be revoked")
    assert_eq(dropped, [pk], "the account's live websockets must be dropped, once")
    for key in ("invite_token", "invite_jti", "invite_ts"):
        assert_eq(stored.get_secret(key), None,
                  f"{key} must be cleared: the cached invite was signed with the old key")
    again = tokens.get_or_generate_invite_token(_fresh(pk))
    assert_true(again != invite, "the next invite must mint a new token, not hand back the dead one")
    assert_eq(tokens.verify_invite_token(again, consume=False).pk, pk, "the new invite token must verify")


@th.django_unit_test("end_sessions: the caller's later save can't write the old key or the old secrets back")
def test_end_sessions_refreshes_the_callers_copy(opts):
    pk = opts.sr_helper_id
    user = _reset_account(pk)
    user.set_secret("invite_token", "iv:stale")
    user.set_secret("invite_ts", int(time.time()))
    user.save()

    user = _fresh(pk)
    user.secrets  # loaded before the change, as a request's copy would be
    user.end_sessions("testit", drop_sockets=lambda account, request=None: None)
    new_key = user.auth_key
    user.display_name = "sr helper renamed"
    user.save()

    stored = _fresh(pk)
    assert_eq(stored.auth_key, new_key, "a later save on the caller's copy must keep the new key")
    assert_eq(stored.get_secret("invite_token"), None,
              "a later save on the caller's copy must not bring the cleared invite token back")


@th.django_unit_test("end_sessions: a failing socket drop doesn't undo the sign-out")
def test_end_sessions_survives_a_socket_failure(opts):
    pk = opts.sr_helper_id
    user = _reset_account(pk)
    old_key = user.get_auth_key()

    def broken(account, request=None):
        raise RuntimeError("realtime is down")

    user.end_sessions("testit", drop_sockets=broken)
    assert_true(_fresh(pk).auth_key != old_key,
                "the key is the guarantee: it must be replaced even when the socket drop fails")


# -----------------------------------------------------------------
# B1 — a password reset
# -----------------------------------------------------------------

@th.django_unit_test("reset by code: every session from before is ended, and the reset's own tokens work")
def test_reset_by_code_signs_out_other_devices(opts):
    pk = opts.sr_code_id
    user = _reset_account(pk)
    _other, before = _sign_in(opts, "sr_code")
    grant, oauth_client, pair = _oauth_pair(pk)
    assert_true(_refresh_works(opts, before.refresh_token), "the old refresh token must work before the reset")

    user = _fresh(pk)
    user.set_secret("password_reset_code", RIGHT)
    user.set_secret("password_reset_code_ts", int(time.time()))
    user.save()
    resp = _client(opts).post("/api/auth/password/reset/code", {
        "username": "sr_code", "code": RIGHT, "new_password": NEW_PWORD})
    assert_eq(resp.status_code, 200, f"the reset must succeed, got {resp.status_code}: {resp.response}")

    _assert_signed_out(opts, before, grant, oauth_client, pair, "reset by code")
    _assert_signed_in(opts, resp.response.data, "reset by code")


@th.django_unit_test("reset by link: every session from before is ended, and the reset's own tokens work")
def test_reset_by_link_signs_out_other_devices(opts):
    from mojo.apps.account.utils import tokens

    pk = opts.sr_link_id
    _reset_account(pk)
    _other, before = _sign_in(opts, "sr_link")
    grant, oauth_client, pair = _oauth_pair(pk)

    link = tokens.generate_password_reset_token(_fresh(pk))
    resp = _client(opts).post("/api/auth/password/reset/token", {"token": link, "new_password": NEW_PWORD})
    assert_eq(resp.status_code, 200, f"the reset must succeed, got {resp.status_code}: {resp.response}")

    _assert_signed_out(opts, before, grant, oauth_client, pair, "reset by link")
    _assert_signed_in(opts, resp.response.data, "reset by link")
    assert_eq(_fresh(pk).get_secret("password_reset_jti"), None,
              "the link must stay consumed: ending the sessions must not write old secrets back")


@th.django_unit_test("reset: a weak new password changes nothing and ends no session")
def test_refused_reset_ends_no_session(opts):
    from mojo.apps.account.utils import tokens

    pk = opts.sr_weak_id
    user = _reset_account(pk)
    old_key = user.get_auth_key()
    _other, before = _sign_in(opts, "sr_weak")

    link = tokens.generate_password_reset_token(_fresh(pk))
    resp = _client(opts).post("/api/auth/password/reset/token", {"token": link, "new_password": "abc"})
    assert_eq(resp.status_code, 400, f"a weak password must be refused, got {resp.status_code}: {resp.response}")
    assert_eq(_fresh(pk).auth_key, old_key, "a refused reset must not replace the key")
    assert_true(_refresh_works(opts, before.refresh_token), "a refused reset must not sign anyone out")


@th.django_unit_test("reset then invite: the next invite carries a link that works")
def test_reinvite_after_reset_yields_a_working_link(opts):
    from mojo.apps.account.utils import tokens

    pk = opts.sr_invite_id
    _reset_account(pk)
    first = tokens.get_or_generate_invite_token(_fresh(pk))

    link = tokens.generate_password_reset_token(_fresh(pk))
    resp = _client(opts).post("/api/auth/password/reset/token", {"token": link, "new_password": NEW_PWORD})
    assert_eq(resp.status_code, 200, f"the reset must succeed, got {resp.status_code}: {resp.response}")

    second = tokens.get_or_generate_invite_token(_fresh(pk))
    assert_true(second != first,
                "an invite cached before the reset was signed with the old key; "
                "the next invite must mint a new one")
    accept = _client(opts).post("/api/auth/password/reset/token", {"token": second, "new_password": PWORD})
    assert_eq(accept.status_code, 200,
              f"the invite sent after a reset must work, got {accept.status_code}: {accept.response}")


@th.django_unit_test("temporary password from an admin: the person's sessions and grants are ended")
def test_temporary_password_signs_the_person_out(opts):
    from types import SimpleNamespace
    from mojo.apps.account.services import admin_passwords

    pk = opts.sr_temp_id
    _reset_account(pk)
    _theirs, before = _sign_in(opts, "sr_temp")
    grant, oauth_client, pair = _oauth_pair(pk)

    result = admin_passwords.issue_temporary_password(
        SimpleNamespace(user=_fresh(opts.sr_admin_id)), pk)
    assert_true(result.get("temporary_password"), "the temporary password must be issued")

    _assert_signed_out(opts, before, grant, oauth_client, pair, "temporary password")


@th.django_unit_test("forced password change: completing it ends sessions and grants, and its own tokens work")
def test_forced_password_change_signs_out_other_devices(opts):
    from mojo.apps.account.models import User
    from mojo.apps.account.utils import tokens

    pk = opts.sr_forced_id
    _reset_account(pk)
    _theirs, before = _sign_in(opts, "sr_forced")
    grant, oauth_client, pair = _oauth_pair(pk)
    User.objects.filter(pk=pk).update(requires_password_change=True)

    credential = tokens.generate_forced_password_token(_fresh(pk))
    resp = _client(opts).post("/api/auth/password/forced", {"token": credential, "new_password": NEW_PWORD})
    assert_eq(resp.status_code, 200, f"the forced change must succeed, got {resp.status_code}: {resp.response}")

    _assert_signed_out(opts, before, grant, oauth_client, pair, "forced password change")
    _assert_signed_in(opts, resp.response.data, "forced password change")


# -----------------------------------------------------------------
# B2 — a password change while signed in
# -----------------------------------------------------------------

@th.django_unit_test("password change: other devices are signed out, and the save answers with tokens that work")
def test_own_password_change_signs_out_other_devices(opts):
    pk = opts.sr_change_id
    _reset_account(pk)
    _other, before = _sign_in(opts, "sr_change")
    mine, mine_before = _sign_in(opts, "sr_change")
    grant, oauth_client, pair = _oauth_pair(pk)

    resp = mine.post("/api/user/me", {"current_password": PWORD, "new_password": NEW_PWORD})
    assert_eq(resp.status_code, 200, f"the change must succeed, got {resp.status_code}: {resp.response}")
    body = resp.response
    assert_eq(body.data.id, pk, "the save must still answer with the account, as before")
    assert_true("access_token" not in body.data and "refresh_token" not in body.data,
                "the tokens must sit beside the account data, not inside it")

    _assert_signed_out(opts, before, grant, oauth_client, pair, "password change")
    assert_true(not _token_works(opts, mine_before.access_token),
                "password change: the changing device's old access token must be refused too")
    _assert_signed_in(opts, body.tokens, "password change")
    assert_true(_fresh(pk).check_password(NEW_PWORD), "the new password must be stored")


@th.django_unit_test("password change: the new tokens keep the session's sign-in time, they don't renew it")
def test_own_password_change_keeps_auth_time(opts):
    from mojo.apps.account.utils.jwtoken import JWToken

    pk = opts.sr_change_id
    _reset_account(pk)
    mine, before = _sign_in(opts, "sr_change")
    was = JWToken().decode(before.access_token, validate=False).get("auth_time")

    resp = mine.post("/api/user/me", {"current_password": PWORD, "new_password": NEW_PWORD})
    assert_eq(resp.status_code, 200, f"the change must succeed, got {resp.status_code}: {resp.response}")
    for name in ("access_token", "refresh_token"):
        now = JWToken().decode(resp.response.tokens[name], validate=False)
        assert_eq(now.get("auth_time"), was,
                  f"{name}: a password change is not a new sign-in, so auth_time must be carried over")
        assert_eq(now.get("uid"), pk, f"{name} must belong to the same account")


@th.django_unit_test("password change: a save that changes no password ends no session and carries no tokens")
def test_other_saves_end_no_session(opts):
    pk = opts.sr_change_id
    user = _reset_account(pk)
    old_key = user.get_auth_key()
    mine, before = _sign_in(opts, "sr_change")

    resp = mine.post("/api/user/me", {"display_name": "sr change renamed"})
    assert_eq(resp.status_code, 200, f"the save must succeed, got {resp.status_code}: {resp.response}")
    assert_true(resp.response.get("tokens") is None, "a save that changes no password must carry no tokens")
    assert_eq(_fresh(pk).auth_key, old_key, "a save that changes no password must not replace the key")
    assert_true(_refresh_works(opts, before.refresh_token), "a save that changes no password must sign nobody out")


@th.django_unit_test("password change: a wrong current password changes nothing and ends no session")
def test_refused_password_change_ends_no_session(opts):
    pk = opts.sr_change_id
    user = _reset_account(pk)
    old_key = user.get_auth_key()
    mine, before = _sign_in(opts, "sr_change")

    resp = mine.post("/api/user/me", {"current_password": "sr##not-the-password", "new_password": NEW_PWORD})
    assert_eq(resp.status_code, 400, f"a wrong current password must be refused, got {resp.status_code}")
    assert_true(resp.response.get("tokens") is None, "a refused change must carry no tokens")
    assert_eq(_fresh(pk).auth_key, old_key, "a refused change must not replace the key")
    assert_true(_refresh_works(opts, before.refresh_token), "a refused change must sign nobody out")


@th.django_unit_test("password change by an admin: the other person is signed out, and no tokens for them are returned")
def test_admin_password_change_returns_no_tokens(opts):
    pk = opts.sr_target_id
    _reset_account(pk)
    _reset_account(opts.sr_admin_id)
    _theirs, before = _sign_in(opts, "sr_target")
    grant, oauth_client, pair = _oauth_pair(pk)
    admin, admin_before = _sign_in(opts, ADMIN)

    resp = admin.post(f"/api/user/{pk}", {"new_password": NEW_PWORD})
    assert_eq(resp.status_code, 200, f"the admin's change must succeed, got {resp.status_code}: {resp.response}")
    assert_true(resp.response.get("tokens") is None,
                "an admin changing someone else's password must not be handed that person's tokens")

    _assert_signed_out(opts, before, grant, oauth_client, pair, "admin password change")
    assert_true(_token_works(opts, admin_before.access_token), "the admin's own session must be untouched")
    assert_true(_refresh_works(opts, admin_before.refresh_token), "the admin's own refresh token must be untouched")


@th.django_unit_test("password change by an admin on their own account: the save answers with tokens that work")
def test_admin_own_password_change_returns_tokens(opts):
    pk = opts.sr_admin_id
    _reset_account(pk)
    _other, before = _sign_in(opts, ADMIN)
    admin, _admin_before = _sign_in(opts, ADMIN)

    resp = admin.post(f"/api/user/{pk}", {"new_password": NEW_PWORD})
    assert_eq(resp.status_code, 200, f"the change must succeed, got {resp.status_code}: {resp.response}")
    assert_true(not _refresh_works(opts, before.refresh_token),
                "the admin's other devices must be signed out like anyone else's")
    _assert_signed_in(opts, resp.response.tokens, "admin's own password change")
    _reset_account(pk)


# -----------------------------------------------------------------
# The helper is shared with "sign out everywhere"
# -----------------------------------------------------------------

@th.django_unit_test("sessions/revoke: it revokes OAuth-server grants too, and the caller's new tokens work")
def test_sessions_revoke_uses_the_same_helper(opts):
    pk = opts.sr_revoke_id
    _reset_account(pk)
    _other, before = _sign_in(opts, "sr_revoke")
    mine, _mine_before = _sign_in(opts, "sr_revoke")
    grant, oauth_client, pair = _oauth_pair(pk)

    resp = mine.post("/api/auth/sessions/revoke", {})
    assert_eq(resp.status_code, 200, f"the revoke must succeed, got {resp.status_code}: {resp.response}")

    _assert_signed_out(opts, before, grant, oauth_client, pair, "sessions/revoke")
    _assert_signed_in(opts, resp.response.data, "sessions/revoke")


@th.django_unit_test("revoke_sessions action: it revokes OAuth-server grants too")
def test_revoke_sessions_action_uses_the_same_helper(opts):
    pk = opts.sr_action_id
    _reset_account(pk)
    _other, before = _sign_in(opts, "sr_action")
    mine, _mine_before = _sign_in(opts, "sr_action")
    grant, oauth_client, pair = _oauth_pair(pk)

    resp = mine.post("/api/user/me", {"revoke_sessions": {}})
    assert_eq(resp.status_code, 200, f"the action must succeed, got {resp.status_code}: {resp.response}")

    _assert_signed_out(opts, before, grant, oauth_client, pair, "revoke_sessions action")
