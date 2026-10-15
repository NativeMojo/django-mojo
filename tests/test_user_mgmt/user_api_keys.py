"""
Tests for UserAPIKey — per-user JWT API tokens.

Coverage:
  - create_for_user() returns token package with id, jti, expires, token
  - JWT validates via User.validate_jwt (token_type="user_api_key")
  - Revoked key is rejected by validate_jwt
  - Expired key is rejected by validate_jwt
  - REST GET lists only the owner's keys
  - REST POST generate_api_key creates a key and returns token
  - REST action revoke deactivates the key
  - Incident logged on generate
  - Incident logged on revoke
"""
from testit import helpers as th
from testit.helpers import assert_true, assert_eq

TEST_USER = "user_api_key_user"
TEST_PWORD = "uapikey##mojo99"
TEST_EMAIL = "user_api_key_user@example.com"


# ===========================================================================
# Setup
# ===========================================================================

@th.django_unit_setup()
def setup_user_api_keys(opts):
    from mojo.apps.account.models import User, UserAPIKey

    user = User.objects.filter(email=TEST_EMAIL).last()
    if user is None:
        user = User(username=TEST_USER, email=TEST_EMAIL)
        user.save()
    user.username = TEST_USER
    user.is_active = True
    user.is_email_verified = True
    user.requires_mfa = False
    user.save_password(TEST_PWORD)
    user.save()
    opts.user_id = user.pk

    UserAPIKey.objects.filter(user=user).delete()


# ===========================================================================
# Unit: create_for_user + validate_jwt
# ===========================================================================

@th.django_unit_test("user_api_key: create_for_user returns token package")
def test_create_for_user(opts):
    from mojo.apps.account.models import User, UserAPIKey

    user = User.objects.get(pk=opts.user_id)
    result = UserAPIKey.create_for_user(user, expire_days=30, label="test key")

    assert_true(result.id is not None, "id must be set")
    assert_true(result.jti, "jti must be set")
    assert_true(result.expires, "expires must be set")
    assert_true(result.token, "token must be returned")

    key_record = UserAPIKey.objects.get(pk=result.id)
    assert_eq(key_record.label, "test key", "label mismatch")
    assert_eq(key_record.is_active, True, "key must be active")
    assert_true(key_record.get_auth_key(), "auth_key must be stored in secrets")

    opts.token = result.token
    opts.key_id = result.id
    opts.jti = result.jti


@th.django_unit_test("user_api_key: validate_jwt succeeds with valid token")
def test_validate_jwt_valid(opts):
    from mojo.apps.account.models import User
    from testit.helpers import get_mock_request

    request = get_mock_request()
    user, error = User.validate_jwt(opts.token, request)

    assert_true(error is None, f"unexpected error: {error}")
    assert_true(user is not None, "user must not be None")
    assert_eq(user.pk, opts.user_id, "wrong user returned")


@th.django_unit_test("user_api_key: validate_jwt rejects bogus token")
def test_validate_jwt_bogus(opts):
    from mojo.apps.account.models import User
    from testit.helpers import get_mock_request

    request = get_mock_request()
    user, error = User.validate_jwt("thisisnotavalidtoken", request)

    assert_true(error is not None, "error must be set for bogus token")


@th.django_unit_test("user_api_key: validate_jwt rejects after revoke")
def test_validate_jwt_after_revoke(opts):
    from mojo.apps.account.models import User, UserAPIKey
    from testit.helpers import get_mock_request

    # Create a fresh key for this test
    user = User.objects.get(pk=opts.user_id)
    result = UserAPIKey.create_for_user(user, expire_days=1, label="revoke test")
    token = result.token

    key_record = UserAPIKey.objects.select_related("user").get(pk=result.id)
    key_record.on_action_revoke(None)

    request = get_mock_request()
    _, error = User.validate_jwt(token, request)
    assert_true(error is not None, "revoked key must be rejected")


@th.django_unit_test("user_api_key: validate_jwt rejects expired key")
def test_validate_jwt_expired(opts):
    from mojo.apps.account.models import User, UserAPIKey
    from mojo.helpers import dates
    from testit.helpers import get_mock_request

    user = User.objects.get(pk=opts.user_id)
    result = UserAPIKey.create_for_user(user, expire_days=1, label="expire test")
    token = result.token

    # Force-expire it
    UserAPIKey.objects.filter(pk=result.id).update(
        expires=dates.utcnow() - dates.timedelta(seconds=1)
    )

    request = get_mock_request()
    _, error = User.validate_jwt(token, request)
    assert_true(error is not None, "expired key must be rejected")


# ===========================================================================
# REST: generate + list + revoke
# ===========================================================================

@th.django_unit_test("user_api_key: REST generate creates key and returns token")
def test_rest_generate(opts):
    opts.client.login(TEST_USER, TEST_PWORD)
    resp = opts.client.post("/api/auth/generate_api_key", {
        "label": "my test key",
        "expire_days": 90,
    })
    opts.client.logout()

    assert_eq(resp.status_code, 200, f"Expected 200, got {resp.status_code}: {resp.response}")
    data = resp.response.data
    assert_true(data.get("token"), "token must be returned")
    assert_true(data.get("jti"), "jti must be returned")
    assert_true(data.get("id") is not None, "id must be returned")

    opts.rest_key_id = data.get("id")
    opts.rest_token = data.get("token")


@th.django_unit_test("user_api_key: REST list returns owner keys")
def test_rest_list(opts):
    opts.client.login(TEST_USER, TEST_PWORD)
    resp = opts.client.get("/api/account/api_keys")
    opts.client.logout()

    assert_eq(resp.status_code, 200, f"Expected 200, got {resp.status_code}")
    data = resp.json.get("data", [])
    assert_true(len(data) > 0, "should have at least one key")
    ids = [d.get("id") for d in data]
    assert_true(opts.rest_key_id in ids, "generated key must appear in list")


@th.django_unit_test("user_api_key: REST token authenticates requests")
def test_rest_token_auth(opts):
    opts.client.logout()
    opts.client.access_token = opts.rest_token
    opts.client.is_authenticated = True

    resp = opts.client.get("/api/user/me")
    opts.client.logout()

    assert_eq(resp.status_code, 200, f"API key token should authenticate, got {resp.status_code}")


@th.django_unit_test("user_api_key: REST revoke action deactivates key")
def test_rest_revoke(opts):
    from mojo.apps.account.models import UserAPIKey

    opts.client.login(TEST_USER, TEST_PWORD)
    resp = opts.client.post(f"/api/account/api_keys/{opts.rest_key_id}", {
        "revoke": "revoke",
    })
    opts.client.logout()

    assert_eq(resp.status_code, 200, f"Expected 200, got {resp.status_code}: {resp.response}")

    key_record = UserAPIKey.objects.get(pk=opts.rest_key_id)
    assert_eq(key_record.is_active, False, "key must be inactive after revoke")


@th.django_unit_test("user_api_key: revoked token is rejected")
def test_revoked_token_rejected(opts):
    from mojo.apps.account.models import User
    from testit.helpers import get_mock_request

    request = get_mock_request()
    _, error = User.validate_jwt(opts.rest_token, request)
    assert_true(error is not None, "revoked token must be rejected")


# ===========================================================================
# Incidents
# ===========================================================================

@th.django_unit_test("user_api_key: incident logged on generate")
def test_incident_on_generate(opts):
    from mojo.apps.logit.models import Log

    before = Log.objects.filter(model_name="account.User", model_id=opts.user_id, kind="api_key:generated").count()

    opts.client.login(TEST_USER, TEST_PWORD)
    resp = opts.client.post("/api/auth/generate_api_key", {"label": "incident test"})
    opts.client.logout()

    assert_eq(resp.status_code, 200, f"Expected 200, got {resp.status_code}")
    after = Log.objects.filter(model_name="account.User", model_id=opts.user_id, kind="api_key:generated").count()
    assert_true(after > before, "api_key:generated log must be created")


@th.django_unit_test("user_api_key: incident logged on revoke")
def test_incident_on_revoke(opts):
    from mojo.apps.account.models import User, UserAPIKey
    from mojo.apps.logit.models import Log

    user = User.objects.get(pk=opts.user_id)
    result = UserAPIKey.create_for_user(user, expire_days=1, label="incident revoke test")

    before = Log.objects.filter(model_name="account.User", model_id=opts.user_id, kind="api_key:revoked").count()

    key_record = UserAPIKey.objects.select_related("user").get(pk=result.id)
    key_record.on_action_revoke(None)

    after = Log.objects.filter(model_name="account.User", model_id=opts.user_id, kind="api_key:revoked").count()
    assert_true(after > before, "api_key:revoked log must be created")


# ===========================================================================
# #7635: a per-user key cannot mint, change or revoke keys
# ===========================================================================

def _use_key(opts, token):
    opts.client.logout()
    opts.client.access_token = token
    opts.client.is_authenticated = True


@th.django_unit_test("user_api_key #7635: setup a caller key and a second key")
def test_key_guard_setup(opts):
    from mojo.apps.account.models import User, UserAPIKey

    user = User.objects.get(pk=opts.user_id)
    caller = UserAPIKey.create_for_user(user, expire_days=1, label="7635 caller")
    other = UserAPIKey.create_for_user(user, expire_days=1, label="7635 other")
    opts.caller_token = caller.token
    opts.other_id = other.id
    opts.other_token = other.token


@th.django_unit_test("user_api_key #7635: a per-user key cannot generate a key")
def test_key_cannot_generate(opts):
    from mojo.apps.account.models import UserAPIKey

    before = UserAPIKey.objects.filter(user_id=opts.user_id).count()
    _use_key(opts, opts.caller_token)
    resp = opts.client.post("/api/auth/generate_api_key", {
        "label": "minted by a key", "expire_days": 360})
    opts.client.logout()

    assert_eq(resp.status_code, 403,
              f"a per-user key must not mint a key, got {resp.status_code}: {resp.response}")
    assert_eq(UserAPIKey.objects.filter(user_id=opts.user_id).count(), before,
              "a refused generate must create no key row")


@th.django_unit_test("user_api_key #7635: a per-user key cannot change or revoke another key")
def test_key_cannot_change_keys(opts):
    from mojo.apps.account.models import User, UserAPIKey
    from testit.helpers import get_mock_request

    attempts = (
        ("revoke", "post", {"revoke": "revoke"}),
        ("is_active", "post", {"is_active": False}),
        ("allowed_ips", "post", {"allowed_ips": ["203.0.113.7"]}),
        ("label", "post", {"label": "renamed by a key"}),
        ("delete", "delete", None),
    )
    for name, verb, body in attempts:
        _use_key(opts, opts.caller_token)
        path = f"/api/account/api_keys/{opts.other_id}"
        resp = opts.client.delete(path) if verb == "delete" else opts.client.post(path, body)
        opts.client.logout()
        assert_eq(resp.status_code, 403,
                  f"{name}: a per-user key must not change the person's keys, "
                  f"got {resp.status_code}: {resp.response}")

    other = UserAPIKey.objects.get(pk=opts.other_id)
    assert_eq(other.is_active, True, "the other key must still be active")
    assert_eq(other.allowed_ips, [], "the other key's IP list must be unchanged")
    assert_eq(other.label, "7635 other", "the other key's label must be unchanged")
    user, error = User.validate_jwt(opts.other_token, get_mock_request())
    assert_true(error is None and user is not None and user.pk == opts.user_id,
                f"the other key must still authenticate: {error}")


@th.django_unit_test("user_api_key #7635: a per-user key can still list the keys")
def test_key_can_list_keys(opts):
    _use_key(opts, opts.caller_token)
    resp = opts.client.get("/api/account/api_keys")
    opts.client.logout()

    assert_eq(resp.status_code, 200,
              f"reads stay open to a per-user key, got {resp.status_code}: {resp.response}")
    ids = [d.get("id") for d in resp.json.get("data", [])]
    assert_true(opts.other_id in ids, f"the list must show the other key: {ids}")


@th.django_unit_test("user_api_key #7635: a signed-in session can still change and revoke keys")
def test_session_can_change_keys(opts):
    from mojo.apps.account.models import UserAPIKey

    opts.client.login(TEST_USER, TEST_PWORD)
    path = f"/api/account/api_keys/{opts.other_id}"
    resp_ips = opts.client.post(path, {"allowed_ips": ["203.0.113.7"]})
    resp_revoke = opts.client.post(path, {"revoke": "revoke"})
    opts.client.logout()

    assert_eq(resp_ips.status_code, 200,
              f"a session may edit its keys, got {resp_ips.status_code}: {resp_ips.response}")
    assert_eq(resp_revoke.status_code, 200,
              f"a session may revoke its keys, got {resp_revoke.status_code}: {resp_revoke.response}")
    other = UserAPIKey.objects.get(pk=opts.other_id)
    assert_eq(other.allowed_ips, ["203.0.113.7"], "the session's IP edit must be saved")
    assert_eq(other.is_active, False, "the session's revoke must deactivate the key")


# ===========================================================================
# #7635 step 2a: an administrator's per-user key cannot issue a temporary
# password, send a reset link or act on an API key
# ===========================================================================

ADMIN_USER = "uak_7635_admin"
ADMIN_EMAIL = "uak_7635_admin@example.com"
TARGET_USER = "uak_7635_target"
TARGET_EMAIL = "uak_7635_target@example.com"
TARGET_GROUP = "uak_7635_group"
TARGET_KEY = "uak_7635_key"


def _admin_cleanup():
    from mojo.apps.account.models import ApiKey, Group, User, UserAPIKey
    ApiKey.objects.filter(name=TARGET_KEY).delete()
    Group.objects.filter(name=TARGET_GROUP).delete()
    UserAPIKey.objects.filter(user__username__in=[ADMIN_USER, TARGET_USER]).delete()
    User.objects.filter(username__in=[ADMIN_USER, TARGET_USER]).delete()


def _reset_link_logs(opts):
    from mojo.apps.logit.models import Log
    return Log.objects.filter(model_name="account.User", model_id=opts.admin_target_id,
                              kind="password:admin_reset_link").count()


@th.django_unit_test("user_api_key #7635: setup an administrator, a target user and a group key")
def test_admin_key_guard_setup(opts):
    from mojo.apps.account.models import ApiKey, Group, User, UserAPIKey
    from mojo.decorators.limits import clear_rate_limits
    clear_rate_limits(ip="127.0.0.1")
    _admin_cleanup()

    admin = User(username=ADMIN_USER, email=ADMIN_EMAIL)
    target = User(username=TARGET_USER, email=TARGET_EMAIL)
    for user in (admin, target):
        user.is_active = True
        user.is_email_verified = True
        user.requires_mfa = False
        user.save()
        user.save_password(TEST_PWORD)
    admin.add_permission(["manage_users", "users", "manage_groups"])
    admin.save()

    group = Group.objects.create(name=TARGET_GROUP, kind="organization")
    key, token = ApiKey.create_for_group(group=group, name=TARGET_KEY)

    opts.admin_target_id = target.pk
    opts.admin_target_hash = User.objects.get(pk=target.pk).password
    opts.admin_group_key_id = key.pk
    opts.admin_group_key_token = token
    opts.admin_key_token = UserAPIKey.create_for_user(
        admin, expire_days=1, label="7635 admin key").token


@th.django_unit_test("user_api_key #7635: an administrator's per-user key cannot issue a temporary password")
def test_admin_key_cannot_issue_temporary_password(opts):
    from mojo.apps.account.models import User

    _use_key(opts, opts.admin_key_token)
    resp = opts.client.post("/api/account/admin/user/password/temporary",
                            {"user": opts.admin_target_id})
    opts.client.logout()

    assert_eq(resp.status_code, 403,
              f"a per-user key must not issue a temporary password, got "
              f"{resp.status_code}: {resp.response}")
    assert_eq(User.objects.get(pk=opts.admin_target_id).password, opts.admin_target_hash,
              "a refused call must leave the target's password unchanged")


@th.django_unit_test("user_api_key #7635: an administrator's per-user key cannot send a reset link")
def test_admin_key_cannot_send_reset_link(opts):
    before = _reset_link_logs(opts)
    _use_key(opts, opts.admin_key_token)
    resp = opts.client.post("/api/account/admin/user/password/reset",
                            {"user": opts.admin_target_id})
    opts.client.logout()

    assert_eq(resp.status_code, 403,
              f"a per-user key must not send a reset link, got "
              f"{resp.status_code}: {resp.response}")
    assert_eq(_reset_link_logs(opts), before, "a refused call must issue no reset link")


@th.django_unit_test("user_api_key #7635: an administrator's per-user key cannot act on an API key")
def test_admin_key_cannot_act_on_api_key(opts):
    from mojo.apps.account.models import ApiKey

    for action in ("rotate", "deactivate", "revoke"):
        _use_key(opts, opts.admin_key_token)
        resp = opts.client.post("/api/account/admin/apikey/action",
                                {"api_key": opts.admin_group_key_id, "action": action})
        opts.client.logout()
        assert_eq(resp.status_code, 403,
                  f"{action}: a per-user key must not act on an API key, got "
                  f"{resp.status_code}: {resp.response}")

    key = ApiKey.objects.get(pk=opts.admin_group_key_id)
    assert_eq(key.is_active, True, "the group key must still be active")
    assert_eq(key.get_token(), opts.admin_group_key_token, "the group key's token must be unchanged")
    _identity, error = ApiKey.validate_token(opts.admin_group_key_token, th.get_mock_request())
    assert_eq(error, None, "the group key must still authenticate")


@th.django_unit_test("user_api_key #7635: the administrator's signed-in session can still use the three routes")
def test_admin_session_can_use_the_three_routes(opts):
    from mojo.apps.account.models import ApiKey, User

    opts.client.login(ADMIN_USER, TEST_PWORD)
    assert_true(opts.client.is_authenticated, "administrator login failed")
    resp_temp = opts.client.post("/api/account/admin/user/password/temporary",
                                 {"user": opts.admin_target_id})
    before = _reset_link_logs(opts)
    resp_reset = opts.client.post("/api/account/admin/user/password/reset",
                                  {"user": opts.admin_target_id})
    resp_key = opts.client.post("/api/account/admin/apikey/action",
                                {"api_key": opts.admin_group_key_id, "action": "deactivate"})
    opts.client.logout()

    assert_eq(resp_temp.status_code, 200,
              f"a session may issue a temporary password, got {resp_temp.status_code}: {resp_temp.response}")
    assert_true(User.objects.get(pk=opts.admin_target_id).password != opts.admin_target_hash,
                "the session's temporary password must be set")
    assert_eq(resp_reset.status_code, 200,
              f"a session may send a reset link, got {resp_reset.status_code}: {resp_reset.response}")
    assert_eq(_reset_link_logs(opts), before + 1, "the session's reset link must be logged")
    assert_eq(resp_key.status_code, 200,
              f"a session may act on an API key, got {resp_key.status_code}: {resp_key.response}")
    assert_eq(ApiKey.objects.get(pk=opts.admin_group_key_id).is_active, False,
              "the session's deactivate must switch the key off")


@th.django_unit_test("user_api_key #7635: cleanup the administrator fixtures")
def test_admin_key_guard_cleanup(opts):
    _admin_cleanup()


# ===========================================================================
# Cleanup
# ===========================================================================

@th.django_unit_test("user_api_key: cleanup")
def test_cleanup(opts):
    from mojo.apps.account.models import User, UserAPIKey

    user = User.objects.filter(pk=opts.user_id).first()
    if user:
        UserAPIKey.objects.filter(user=user).delete()
        user.delete()
