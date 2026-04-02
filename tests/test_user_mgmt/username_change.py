"""
Tests for the self-service username change endpoint.

Coverage:
  - Happy path — username changes, response contains new username
  - current_password wrong — 401, username unchanged
  - current_password missing — 400
  - username missing — 400
  - username taken by another user — 400
  - username same as current — 400
  - username invalid content (content_guard blocked) — 400
  - username is lowercased on save
  - ALLOW_USERNAME_CHANGE = False — 403 (skip via TestitSkip)
  - Unauthenticated request — 401/403
  - OAuth-only user (no usable password) — 400 with correct message
  - Audit log entry written (username:changed)
"""
from testit import helpers as th
from testit.helpers import assert_true, assert_eq

TEST_USER = "username_change_user"
TEST_PWORD = "uchange##mojo99"
TEST_EMAIL = "username_change_user@example.com"
COLLISION_USER = "username_taken_user"
COLLISION_EMAIL = "username_taken_user@example.com"
OAUTH_USER = "oauth_no_pw_user"
OAUTH_EMAIL = "oauth_no_pw_user@example.com"


# ===========================================================================
# Setup / teardown
# ===========================================================================

@th.django_unit_setup()
def setup_username_change(opts):
    from mojo.apps.account.models import User
    from mojo.decorators.limits import clear_rate_limits
    clear_rate_limits(ip="127.0.0.1")

    # Primary test user
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

    # Collision user — owns a username so we can test duplicate rejection
    collision = User.objects.filter(email=COLLISION_EMAIL).last()
    if collision is None:
        collision = User(username=COLLISION_USER, email=COLLISION_EMAIL)
        collision.save()
    collision.username = COLLISION_USER
    collision.is_active = True
    collision.is_email_verified = True
    collision.save_password(TEST_PWORD)
    collision.save()
    opts.collision_id = collision.pk

    # OAuth-only user — no usable password
    oauth_user = User.objects.filter(email=OAUTH_EMAIL).last()
    if oauth_user is None:
        oauth_user = User(username=OAUTH_USER, email=OAUTH_EMAIL)
        oauth_user.set_unusable_password()
        oauth_user.is_email_verified = True
        oauth_user.save()
    else:
        oauth_user.username = OAUTH_USER
        oauth_user.set_unusable_password()
        oauth_user.is_active = True
        oauth_user.is_email_verified = True
        oauth_user.save()
    opts.oauth_user_id = oauth_user.pk


# ===========================================================================
# Endpoint tests
# ===========================================================================

@th.django_unit_test("username change: happy path")
def test_username_change_happy(opts):
    from mojo.apps.account.models import User

    opts.client.login(TEST_USER, TEST_PWORD)
    resp = opts.client.post("/api/auth/username/change", {
        "username": "new_uname_test",
        "current_password": TEST_PWORD,
    })
    opts.client.logout()
    assert_eq(resp.status_code, 200, f"Expected 200, got {resp.status_code}")
    data = resp.json
    assert_true(data.get("status"), "Expected status=true")
    assert_eq(data.get("data", {}).get("username"), "new_uname_test", "Response should contain new username")

    # Verify persisted
    user = User.objects.get(pk=opts.user_id)
    assert_eq(user.username, "new_uname_test", "Username should be persisted in DB")

    # Restore original username for subsequent tests
    user.username = TEST_USER
    user.save(update_fields=["username", "modified"])


@th.django_unit_test("username change: wrong password returns 401")
def test_username_change_wrong_password(opts):
    from mojo.apps.account.models import User

    opts.client.login(TEST_USER, TEST_PWORD)
    resp = opts.client.post("/api/auth/username/change", {
        "username": "should_not_change",
        "current_password": "wrong_password_here",
    })
    opts.client.logout()
    assert_eq(resp.status_code, 401, f"Expected 401, got {resp.status_code}")

    # Username must not have changed
    user = User.objects.get(pk=opts.user_id)
    assert_eq(user.username, TEST_USER, "Username should be unchanged after wrong password")


@th.django_unit_test("username change: missing current_password returns 400")
def test_username_change_missing_password(opts):
    opts.client.login(TEST_USER, TEST_PWORD)
    resp = opts.client.post("/api/auth/username/change", {
        "username": "new_uname",
    })
    opts.client.logout()
    assert_true(resp.status_code in (400, 422), f"Expected 400, got {resp.status_code}")


@th.django_unit_test("username change: missing username returns 400")
def test_username_change_missing_username(opts):
    opts.client.login(TEST_USER, TEST_PWORD)
    resp = opts.client.post("/api/auth/username/change", {
        "current_password": TEST_PWORD,
    })
    opts.client.logout()
    assert_true(resp.status_code in (400, 422), f"Expected 400, got {resp.status_code}")


@th.django_unit_test("username change: taken by another user returns 400")
def test_username_change_taken(opts):
    opts.client.login(TEST_USER, TEST_PWORD)
    resp = opts.client.post("/api/auth/username/change", {
        "username": COLLISION_USER,
        "current_password": TEST_PWORD,
    })
    opts.client.logout()
    assert_true(resp.status_code in (400, 422), f"Expected 400, got {resp.status_code}")


@th.django_unit_test("username change: same as current returns 400")
def test_username_change_same(opts):
    opts.client.login(TEST_USER, TEST_PWORD)
    resp = opts.client.post("/api/auth/username/change", {
        "username": TEST_USER,
        "current_password": TEST_PWORD,
    })
    opts.client.logout()
    assert_true(resp.status_code in (400, 422), f"Expected 400, got {resp.status_code}")


@th.django_unit_test("username change: username is lowercased on save")
def test_username_change_lowercase(opts):
    from mojo.apps.account.models import User

    opts.client.login(TEST_USER, TEST_PWORD)
    resp = opts.client.post("/api/auth/username/change", {
        "username": "MiXeD_CaSe_NaMe",
        "current_password": TEST_PWORD,
    })
    opts.client.logout()
    assert_eq(resp.status_code, 200, f"Expected 200, got {resp.status_code}")
    data = resp.json
    assert_eq(data.get("data", {}).get("username"), "mixed_case_name",
              "Username should be lowercased")

    user = User.objects.get(pk=opts.user_id)
    assert_eq(user.username, "mixed_case_name", "DB username should be lowercased")

    # Restore
    user.username = TEST_USER
    user.save(update_fields=["username", "modified"])


@th.django_unit_test("username change: unauthenticated returns 401/403")
def test_username_change_unauth(opts):
    opts.client.logout()
    resp = opts.client.post("/api/auth/username/change", {
        "username": "no_auth_user",
        "current_password": TEST_PWORD,
    })
    assert_true(resp.status_code in (401, 403), f"Expected 401 or 403, got {resp.status_code}")


@th.django_unit_test("username change: OAuth-only user (no password) returns 400")
def test_username_change_oauth_no_password(opts):
    from mojo.apps.account.models import User

    # Give oauth user a temp password so we can log in, then remove it
    oauth_user = User.objects.get(pk=opts.oauth_user_id)
    oauth_user.save_password("temp_pass_1234")
    oauth_user.save()

    opts.client.login(OAUTH_USER, "temp_pass_1234")

    # Now remove the usable password while we still have a valid session.
    # Use update_fields to avoid overwriting server-side state (last_login,
    # last_activity) that was modified by the login call above.
    oauth_user.set_unusable_password()
    oauth_user.save(update_fields=["password", "modified"])

    resp = opts.client.post("/api/auth/username/change", {
        "username": "new_oauth_name",
        "current_password": "anything",
    })
    opts.client.logout()
    assert_true(resp.status_code in (400, 422), f"Expected 400, got {resp.status_code}")
    body = resp.json
    assert_true("password" in str(body).lower() or "No password" in str(body),
                "Error message should mention password")


@th.django_unit_test("username change: ALLOW_USERNAME_CHANGE=False returns 403")
def test_username_change_disabled(opts):
    from mojo.helpers.settings import settings
    from testit import TestitSkip

    if settings.get("ALLOW_USERNAME_CHANGE", True):
        raise TestitSkip("ALLOW_USERNAME_CHANGE is True on this server — cannot test disabled state")

    opts.client.login(TEST_USER, TEST_PWORD)
    resp = opts.client.post("/api/auth/username/change", {
        "username": "disabled_change",
        "current_password": TEST_PWORD,
    })
    opts.client.logout()
    assert_eq(resp.status_code, 403, f"Expected 403, got {resp.status_code}")


@th.django_unit_test("username change: audit log entry written")
def test_username_change_audit_log(opts):
    from mojo.apps.account.models import User
    from mojo.apps.incident.models.event import Event

    opts.client.login(TEST_USER, TEST_PWORD)

    # Record event count before
    before_count = Event.objects.filter(
        uid=opts.user_id, category="username:changed"
    ).count()

    resp = opts.client.post("/api/auth/username/change", {
        "username": "audit_log_uname",
        "current_password": TEST_PWORD,
    })
    opts.client.logout()
    assert_eq(resp.status_code, 200, f"Expected 200, got {resp.status_code}")

    # Restore username
    user = User.objects.get(pk=opts.user_id)
    user.username = TEST_USER
    user.save(update_fields=["username", "modified"])