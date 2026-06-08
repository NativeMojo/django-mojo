"""
Tests for step-up ("recent authentication") freshness — ITEM-002.

Two layers:
  - Unit: the `fresh_auth` service logic (window resolution, staleness, API-key
    bypass, missing-claim fail-closed), driven with fabricated requests.
  - Integration: over the wire — login stamps `auth_time`, refresh carries it
    forward unchanged, a stale token gets HTTP 440 `reauth_required`, and the
    folded endpoints work for passwordless callers (no current_password).

The freshness window is set per-request with the `X-Mojo-Test-Fresh-Auth-Window`
header (test-mode only) so these run in parallel without server reloads.
"""
import time

from testit import helpers as th
from testit.helpers import assert_true, assert_eq

WINDOW_HDR = {"X-Mojo-Test-Fresh-Auth-Window": "300"}

USER = "fresh_auth_user@test.com"
PWORD = "fresh_auth_pw_99"
ADMIN = "fresh_auth_admin@test.com"
ADMIN_PWORD = "fresh_auth_admin_99"


@th.django_unit_setup()
def setup_fresh_auth(opts):
    from mojo.apps.account.models import User

    User.objects.filter(email__in=[USER, ADMIN]).delete()

    user = User.objects.create_user(username=USER, email=USER, password=PWORD)
    user.is_active = True
    user.is_email_verified = True
    user.requires_mfa = False
    user.save()
    user.get_auth_key()  # ensure auth_key is set for token crafting
    opts.user_id = user.pk
    opts.user_auth_key = user.get_auth_key()

    admin = User.objects.create_user(username=ADMIN, email=ADMIN, password=ADMIN_PWORD)
    admin.is_active = True
    admin.is_email_verified = True
    admin.requires_mfa = False
    admin.save()
    admin.add_permission("manage_users")
    admin.get_auth_key()
    opts.admin_id = admin.pk
    opts.admin_auth_key = admin.get_auth_key()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _access_token(auth_key, uid, auth_time="__set__"):
    """Mint an access token signed with auth_key. auth_time defaults to now;
    pass None to omit the claim (simulating a legacy token)."""
    from mojo.apps.account.utils.jwtoken import JWToken
    kwargs = dict(uid=uid)
    if auth_time == "__set__":
        kwargs["auth_time"] = int(time.time())
    elif auth_time is not None:
        kwargs["auth_time"] = int(auth_time)
    return JWToken(auth_key).create_access_token(**kwargs)


def _decode(token):
    from mojo.apps.account.utils.jwtoken import JWToken
    return JWToken().decode(token, validate=False)


def _fake_request(auth_time="__set__", bearer="bearer"):
    from objict import objict
    token = _access_token("unit-test-signing-key-0123456789abcdef", 1, auth_time=auth_time)
    return objict(bearer=bearer, auth_token=objict(token=token), META={})


# ===========================================================================
# Unit — service logic
# ===========================================================================

@th.django_unit_test("fresh_auth: None request is always fresh")
def test_unit_none_request(opts):
    from mojo.apps.account.services import fresh_auth
    assert_true(fresh_auth.is_fresh(None, seconds=300),
                "a None request must be treated as fresh (no JWT to gate)")


@th.django_unit_test("fresh_auth: window <= 0 disables the gate")
def test_unit_disabled_window(opts):
    from mojo.apps.account.services import fresh_auth
    stale = _fake_request(auth_time=int(time.time()) - 99999)
    assert_true(fresh_auth.is_fresh(stale, seconds=0),
                "window 0 must bypass — even a very stale token is 'fresh'")


@th.django_unit_test("fresh_auth: API-key (non-JWT) callers bypass the gate")
def test_unit_apikey_bypass(opts):
    from mojo.apps.account.services import fresh_auth
    stale_apikey = _fake_request(auth_time=int(time.time()) - 99999, bearer="apikey")
    assert_true(fresh_auth.is_fresh(stale_apikey, seconds=300),
                "non-JWT (api key) auth has no interactive login to be recent — must bypass")


@th.django_unit_test("fresh_auth: a recent token is fresh")
def test_unit_recent_is_fresh(opts):
    from mojo.apps.account.services import fresh_auth
    assert_true(fresh_auth.is_fresh(_fake_request(), seconds=300),
                "a token minted now must be within a 300s window")


@th.django_unit_test("fresh_auth: a stale token is not fresh")
def test_unit_stale_not_fresh(opts):
    from mojo.apps.account.services import fresh_auth
    stale = _fake_request(auth_time=int(time.time()) - 600)
    assert_true(not fresh_auth.is_fresh(stale, seconds=300),
                "auth_time 600s old must fail a 300s window")


@th.django_unit_test("fresh_auth: missing auth_time fails closed")
def test_unit_missing_claim_fails_closed(opts):
    from mojo.apps.account.services import fresh_auth
    legacy = _fake_request(auth_time=None)
    assert_true(not fresh_auth.is_fresh(legacy, seconds=300),
                "a token with no auth_time (legacy) must be treated as stale when enforced")


@th.django_unit_test("fresh_auth: require_fresh raises ReauthRequiredException (440)")
def test_unit_require_fresh_raises(opts):
    from mojo.apps.account.services import fresh_auth
    from mojo import errors as merrors
    stale = _fake_request(auth_time=int(time.time()) - 600)
    raised = None
    try:
        fresh_auth.require_fresh(stale, seconds=300)
    except merrors.ReauthRequiredException as e:
        raised = e
    assert_true(raised is not None, "require_fresh must raise on a stale token")
    assert_eq(raised.code, 440, f"reauth code must be 440, got {raised.code}")
    assert_eq(raised.status, 440, f"reauth HTTP status must be 440, got {raised.status}")
    assert_eq(raised.reason, "reauth_required",
              f"reason must be the machine token 'reauth_required', got {raised.reason!r}")


@th.django_unit_test("fresh_auth: token_auth_time reads the claim")
def test_unit_token_auth_time(opts):
    from mojo.apps.account.services import fresh_auth
    req = _fake_request(auth_time=1234567890)
    assert_eq(fresh_auth.token_auth_time(req), 1234567890,
              "token_auth_time must return the auth_time claim from the JWT")


# ===========================================================================
# Integration — over the wire
# ===========================================================================

@th.django_unit_test("login stamps auth_time into the access token")
def test_login_stamps_auth_time(opts):
    assert opts.client.login(USER, PWORD), "login should succeed"
    before = int(time.time())
    payload = _decode(opts.client.access_token)
    opts.client.logout()
    at = payload.get("auth_time")
    assert_true(at is not None, "issued access token must carry an auth_time claim")
    assert_true(abs(before - int(at)) <= 30,
                f"auth_time should be ~now, got {at} vs {before}")


@th.django_unit_test("refresh carries auth_time forward unchanged")
def test_refresh_preserves_auth_time(opts):
    from mojo.apps.account.models import User
    from mojo.apps.account.utils.jwtoken import JWToken

    user = User.objects.get(pk=opts.user_id)
    original = int(time.time()) - 120  # a login 2 minutes ago
    refresh_token = JWToken(user.get_auth_key()).create_refresh_token(
        uid=user.id, auth_time=original)

    resp = opts.client.post("/api/auth/token/refresh", {"refresh_token": refresh_token})
    assert_eq(resp.status_code, 200, f"refresh should succeed, got {resp.status_code}: {resp.json}")
    new_access = resp.json.get("data", {}).get("access_token")
    assert_true(new_access, "refresh must return a new access_token")
    payload = _decode(new_access)
    assert_eq(int(payload.get("auth_time")), original,
              "refresh MUST carry the original auth_time forward unchanged (not reset to now)")


@th.django_unit_test("refresh of a legacy token (no auth_time) omits the claim")
def test_refresh_legacy_token_no_auth_time(opts):
    from mojo.apps.account.models import User
    from mojo.apps.account.utils.jwtoken import JWToken

    user = User.objects.get(pk=opts.user_id)
    legacy_refresh = JWToken(user.get_auth_key()).create_refresh_token(uid=user.id)

    resp = opts.client.post("/api/auth/token/refresh", {"refresh_token": legacy_refresh})
    assert_eq(resp.status_code, 200, f"refresh should succeed, got {resp.status_code}: {resp.json}")
    payload = _decode(resp.json.get("data", {}).get("access_token"))
    assert_true(payload.get("auth_time") is None,
                "a legacy refresh token with no auth_time must not gain a fabricated one")


@th.django_unit_test("stale token + enabled window => HTTP 440 reauth_required")
def test_stale_token_blocked_440(opts):
    from mojo.apps.account.models import User

    user = User.objects.get(pk=opts.user_id)
    pre_auth_key = user.get_auth_key()
    stale = _access_token(pre_auth_key, user.id, auth_time=int(time.time()) - 600)

    opts.client.access_token = stale
    opts.client.is_authenticated = True
    opts.client.bearer = "bearer"
    resp = opts.client.post("/api/auth/sessions/revoke", {}, headers=WINDOW_HDR)
    opts.client.logout()

    assert_eq(resp.status_code, 440,
              f"stale token under a 300s window must get 440, got {resp.status_code}: {resp.json}")
    assert_eq(resp.json.get("error"), "reauth_required",
              f"body error must be 'reauth_required', got {resp.json.get('error')!r}")
    assert_eq(resp.json.get("code"), 440, f"body code must be 440, got {resp.json.get('code')}")
    user.refresh_from_db()
    assert_eq(user.auth_key, pre_auth_key,
              "auth_key must NOT rotate — the gate blocks before the action runs")


@th.django_unit_test("fresh token + enabled window => allowed")
def test_fresh_token_allowed_with_window(opts):
    from mojo.apps.account.models import User

    assert opts.client.login(USER, PWORD), "login should succeed"
    pre_auth_key = User.objects.get(pk=opts.user_id).get_auth_key()
    resp = opts.client.post("/api/auth/sessions/revoke", {}, headers=WINDOW_HDR)
    opts.client.logout()

    assert_eq(resp.status_code, 200,
              f"a freshly-issued token must pass the window, got {resp.status_code}: {resp.json}")
    post_auth_key = User.objects.get(pk=opts.user_id).get_auth_key()
    assert_true(post_auth_key != pre_auth_key, "revoke should rotate auth_key on success")


@th.django_unit_test("admin acting on another user is gated by the admin's own freshness")
def test_admin_on_other_stale_blocked_440(opts):
    from mojo.apps.account.models import User

    admin = User.objects.get(pk=opts.admin_id)
    stale = _access_token(admin.get_auth_key(), admin.id, auth_time=int(time.time()) - 600)

    opts.client.access_token = stale
    opts.client.is_authenticated = True
    opts.client.bearer = "bearer"
    resp = opts.client.post(f"/api/user/{opts.user_id}", {"revoke_sessions": True}, headers=WINDOW_HDR)
    opts.client.logout()

    assert_eq(resp.status_code, 440,
              f"admin with a stale token must get 440 on a sensitive action, got {resp.status_code}: {resp.json}")
    assert_eq(resp.json.get("error"), "reauth_required",
              f"body error must be 'reauth_required', got {resp.json.get('error')!r}")


@th.django_unit_test("passwordless: username change needs no current_password")
def test_passwordless_username_change(opts):
    from mojo.apps.account.models import User

    new_username = "fa_renamed_user"
    User.objects.filter(username=new_username).delete()

    assert opts.client.login(USER, PWORD), "login should succeed"
    resp = opts.client.post("/api/auth/username/change", {"username": new_username})
    opts.client.logout()

    assert_eq(resp.status_code, 200,
              f"username change must succeed without current_password, got {resp.status_code}: {resp.json}")
    assert_eq(User.objects.get(pk=opts.user_id).username, new_username,
              "username should be updated")
    User.objects.filter(pk=opts.user_id).update(username=USER)


@th.django_unit_test("passwordless: sessions revoke needs no current_password")
def test_passwordless_sessions_revoke(opts):
    from mojo.apps.account.models import User

    pre_auth_key = User.objects.get(pk=opts.user_id).get_auth_key()
    assert opts.client.login(USER, PWORD), "login should succeed"
    resp = opts.client.post("/api/auth/sessions/revoke", {})
    opts.client.logout()

    assert_eq(resp.status_code, 200,
              f"sessions revoke must succeed without current_password, got {resp.status_code}: {resp.json}")
    post_auth_key = User.objects.get(pk=opts.user_id).get_auth_key()
    assert_true(post_auth_key != pre_auth_key, "auth_key should rotate after revoke")
