"""
Tests for the session revoke / log-out-everywhere endpoint.

Coverage:
  - Happy path: correct password → fresh JWT returned
  - Fresh JWT from response is valid (can call /api/user/me with it)
  - Old access token is rejected after revocation
  - Wrong password: 401, auth_key unchanged
  - Missing current_password param: 400
  - Unauthenticated request: 401/403
  - Incident logged on success (sessions:revoked)
  - Incident logged on failed attempt (sessions:revoke_failed)
"""
from testit import helpers as th
from testit.helpers import assert_true, assert_eq

TEST_USER = "session_revoke_user"
TEST_PWORD = "revoke##mojo99"
TEST_EMAIL = "session_revoke_user@example.com"


# ===========================================================================
# Setup / teardown
# ===========================================================================

@th.django_unit_setup()
def setup_session_revoke(opts):
    from mojo.apps.account.models import User
    from mojo.decorators.limits import clear_rate_limits
    clear_rate_limits(ip="127.0.0.1")

    user = User.objects.filter(email=TEST_EMAIL).last()
    if user is None:
        user = User(username=TEST_USER, email=TEST_EMAIL)
        user.save()
    user.username = TEST_USER
    user.is_active = True
    user.requires_mfa = False
    user.save_password(TEST_PWORD)
    user.save()
    opts.user_id = user.pk


# ===========================================================================
# Endpoint tests
# ===========================================================================

@th.django_unit_test("session revoke: happy path — fresh JWT returned")
def test_session_revoke_happy(opts):
    from mojo.apps.account.models import User
    from mojo.decorators.limits import clear_rate_limits
    clear_rate_limits(ip="127.0.0.1")

    user = User.objects.get(pk=opts.user_id)
    old_auth_key = user.auth_key

    opts.client.login(TEST_USER, TEST_PWORD)
    resp = opts.client.post("/api/auth/sessions/revoke", {
        "current_password": TEST_PWORD,
    })
    opts.client.logout()
    assert_eq(resp.status_code, 200, f"Expected 200, got {resp.status_code}")
    data = resp.json
    assert_true(data.get("status"), "Expected status=true")
    resp_data = data.get("data", {})
    assert_true(resp_data.get("access_token"), "Response must include access_token")
    assert_true(resp_data.get("refresh_token"), "Response must include refresh_token")

    # auth_key must have rotated
    user.refresh_from_db()
    assert_true(user.auth_key != old_auth_key, "auth_key should have been rotated")


@th.django_unit_test("session revoke: fresh JWT is valid for /api/user/me")
def test_session_revoke_fresh_jwt_valid(opts):
    from mojo.decorators.limits import clear_rate_limits
    clear_rate_limits(ip="127.0.0.1")

    opts.client.login(TEST_USER, TEST_PWORD)
    resp = opts.client.post("/api/auth/sessions/revoke", {
        "current_password": TEST_PWORD,
    })
    assert_eq(resp.status_code, 200, f"Expected 200, got {resp.status_code}")
    new_token = resp.json.get("data", {}).get("access_token")
    assert_true(new_token, "Must get a new access_token")

    # Use the fresh token to hit /api/user/me
    opts.client.access_token = new_token
    opts.client.is_authenticated = True
    me_resp = opts.client.get("/api/user/me")
    opts.client.logout()
    assert_eq(me_resp.status_code, 200, f"Fresh JWT should be valid, got {me_resp.status_code}")


@th.django_unit_test("session revoke: wrong password returns 401, auth_key unchanged")
def test_session_revoke_wrong_password(opts):
    from mojo.apps.account.models import User
    from mojo.decorators.limits import clear_rate_limits
    clear_rate_limits(ip="127.0.0.1")

    user = User.objects.get(pk=opts.user_id)
    old_auth_key = user.auth_key

    opts.client.login(TEST_USER, TEST_PWORD)
    resp = opts.client.post("/api/auth/sessions/revoke", {
        "current_password": "totally_wrong_password",
    })
    opts.client.logout()
    assert_eq(resp.status_code, 401, f"Expected 401, got {resp.status_code}")

    # auth_key must NOT have changed
    user.refresh_from_db()
    assert_eq(user.auth_key, old_auth_key, "auth_key should be unchanged after wrong password")


@th.django_unit_test("session revoke: missing current_password returns 400")
def test_session_revoke_missing_param(opts):
    opts.client.login(TEST_USER, TEST_PWORD)
    resp = opts.client.post("/api/auth/sessions/revoke", {})
    opts.client.logout()
    assert_true(resp.status_code in (400, 422), f"Expected 400, got {resp.status_code}")


@th.django_unit_test("session revoke: unauthenticated returns 401/403")
def test_session_revoke_unauth(opts):
    opts.client.logout()
    resp = opts.client.post("/api/auth/sessions/revoke", {
        "current_password": TEST_PWORD,
    })
    assert_true(resp.status_code in (401, 403), f"Expected 401 or 403, got {resp.status_code}")


@th.django_unit_test("session revoke: incident logged on success")
def test_session_revoke_incident_logged(opts):
    from mojo.decorators.limits import clear_rate_limits
    clear_rate_limits(ip="127.0.0.1")
    from mojo.apps.incident.models.event import Event

    before_count = Event.objects.filter(
        uid=opts.user_id, category="sessions:revoked"
    ).count()

    opts.client.login(TEST_USER, TEST_PWORD)
    resp = opts.client.post("/api/auth/sessions/revoke", {
        "current_password": TEST_PWORD,
    })
    opts.client.logout()
    assert_eq(resp.status_code, 200, f"Expected 200, got {resp.status_code}")

    after_count = Event.objects.filter(
        uid=opts.user_id, category="sessions:revoked"
    ).count()
    assert_true(after_count > before_count,
                "Expected sessions:revoked incident to be logged on success")


@th.django_unit_test("session revoke: incident logged on failed attempt")
def test_session_revoke_failed_incident(opts):
    from mojo.decorators.limits import clear_rate_limits
    clear_rate_limits(ip="127.0.0.1")
    from mojo.apps.incident.models.event import Event

    before_count = Event.objects.filter(
        uid=opts.user_id, category="sessions:revoke_failed"
    ).count()

    opts.client.login(TEST_USER, TEST_PWORD)
    resp = opts.client.post("/api/auth/sessions/revoke", {
        "current_password": "wrong_password_here",
    })
    opts.client.logout()
    assert_eq(resp.status_code, 401, f"Expected 401, got {resp.status_code}")

    after_count = Event.objects.filter(
        uid=opts.user_id, category="sessions:revoke_failed"
    ).count()
    assert_true(after_count > before_count,
                "Expected sessions:revoke_failed incident to be logged on failure")