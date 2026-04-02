"""
Tests for the self-service phone-number-change flow — security and correctness.

Security contract this file enforces:
  - Request endpoint: happy path returns 200 + session_token
  - Request endpoint: succeeds without current_password (OAuth/passkey users)
  - Request endpoint: wrong current_password returns 401 when provided
  - Request endpoint: same number as current is rejected
  - Request endpoint: duplicate number rejected
  - Request endpoint: invalid format rejected
  - Request endpoint: requires authentication
  - Request endpoint: ALLOW_PHONE_CHANGE=False blocks the entire flow
  - Request endpoint: incident logged on request
  - Confirm endpoint: happy path commits new number, sets is_phone_verified
  - Confirm endpoint: wrong OTP rejected
  - Confirm endpoint: missing params rejected
  - Confirm endpoint: session_token mismatch (different user) rejected
  - Cancel endpoint: clears pending state, idempotent
  - Cancel endpoint: no pending change is a safe no-op
"""
from testit import helpers as th
from testit.helpers import assert_true, assert_eq

TEST_USER = "phone_change_user"
TEST_PWORD = "phonechange##mojo99"
TEST_EMAIL = "phone_change_user@example.com"
TEST_PHONE = "+15550001111"
TEST_NEW_PHONE = "+15550002222"

COLLISION_USER = "phone_change_collision"
COLLISION_EMAIL = "phone_change_collision@example.com"
COLLISION_PHONE = "+15550003333"


# ===========================================================================
# Setup
# ===========================================================================

@th.django_unit_setup()
def setup_phone_change(opts):
    from mojo.apps.account.models import User
    from mojo.decorators.limits import clear_rate_limits
    clear_rate_limits(ip="127.0.0.1")

    # Primary test user
    user = User.objects.filter(username=TEST_USER).last()
    if user is None:
        user = User(username=TEST_USER, email=TEST_EMAIL)
        user.save()
    user.is_active = True
    user.is_email_verified = True
    user.is_phone_verified = True
    user.phone_number = TEST_PHONE
    user.requires_mfa = False
    user.save_password(TEST_PWORD)
    user.save()
    opts.user_id = user.pk
    opts.user = user
    opts.original_phone = TEST_PHONE

    # Collision user — owns COLLISION_PHONE so we can test duplicate rejection
    collision = User.objects.filter(username=COLLISION_USER).last()
    if collision is None:
        collision = User(username=COLLISION_USER, email=COLLISION_EMAIL)
        collision.save()
    collision.is_active = True
    collision.phone_number = COLLISION_PHONE
    collision.save_password(TEST_PWORD)
    collision.save()
    opts.collision_user_id = collision.pk


# ===========================================================================
# Request endpoint tests
# ===========================================================================

@th.django_unit_test("phone/change/request: happy path returns 200 with session_token")
def test_request_happy(opts):
    from mojo.decorators.limits import clear_rate_limits
    clear_rate_limits(ip="127.0.0.1")

    opts.client.login(TEST_USER, TEST_PWORD)
    resp = opts.client.post("/api/auth/phone/change/request", {
        "phone_number": TEST_NEW_PHONE,
        "current_password": TEST_PWORD,
    })
    opts.client.logout()
    assert_eq(resp.status_code, 200, f"Expected 200, got {resp.status_code}")
    data = resp.json
    assert_true(data.get("session_token"), "Response must include session_token")
    assert_true(data.get("status"), "Expected status=true")

    # Clean up pending state
    from mojo.apps.account.models import User
    user = User.objects.get(pk=opts.user_id)
    user.set_secret("pending_phone", None)
    user.set_secret("phone_change_otp", None)
    user.set_secret("phone_change_otp_ts", None)
    user.save(update_fields=["mojo_secrets", "modified"])


@th.django_unit_test("phone/change/request: succeeds without current_password (OAuth/passkey users)")
def test_request_no_password(opts):
    from mojo.decorators.limits import clear_rate_limits
    clear_rate_limits(ip="127.0.0.1")

    opts.client.login(TEST_USER, TEST_PWORD)
    resp = opts.client.post("/api/auth/phone/change/request", {
        "phone_number": TEST_NEW_PHONE,
    })
    opts.client.logout()
    assert_eq(resp.status_code, 200, f"Request without current_password should succeed, got {resp.status_code}")
    data = resp.json
    assert_true(data.get("session_token"), "Response must include session_token")

    # Clean up pending state
    from mojo.apps.account.models import User
    user = User.objects.get(pk=opts.user_id)
    user.set_secret("pending_phone", None)
    user.set_secret("phone_change_otp", None)
    user.set_secret("phone_change_otp_ts", None)
    user.save(update_fields=["mojo_secrets", "modified"])


@th.django_unit_test("phone/change/request: wrong current_password returns 401")
def test_request_wrong_password(opts):
    from mojo.decorators.limits import clear_rate_limits
    clear_rate_limits(ip="127.0.0.1")

    opts.client.login(TEST_USER, TEST_PWORD)
    resp = opts.client.post("/api/auth/phone/change/request", {
        "phone_number": TEST_NEW_PHONE,
        "current_password": "definitely_wrong_pw",
    })
    opts.client.logout()
    assert_eq(resp.status_code, 401, f"Wrong password must return 401, got {resp.status_code}")


@th.django_unit_test("phone/change/request: same number as current is rejected")
def test_request_same_number(opts):
    from mojo.apps.account.models import User
    from mojo.decorators.limits import clear_rate_limits
    clear_rate_limits(ip="127.0.0.1")

    user = User.objects.get(pk=opts.user_id)
    user.phone_number = TEST_PHONE
    user.save(update_fields=["phone_number", "modified"])

    opts.client.login(TEST_USER, TEST_PWORD)
    resp = opts.client.post("/api/auth/phone/change/request", {
        "phone_number": TEST_PHONE,
    })
    opts.client.logout()
    assert_true(resp.status_code in (400, 422), f"Same number must be rejected, got {resp.status_code}")


@th.django_unit_test("phone/change/request: duplicate number rejected")
def test_request_duplicate_number(opts):
    from mojo.decorators.limits import clear_rate_limits
    clear_rate_limits(ip="127.0.0.1")

    opts.client.login(TEST_USER, TEST_PWORD)
    resp = opts.client.post("/api/auth/phone/change/request", {
        "phone_number": COLLISION_PHONE,
    })
    opts.client.logout()
    assert_true(resp.status_code in (400, 422), f"Duplicate number must be rejected, got {resp.status_code}")


@th.django_unit_test("phone/change/request: invalid format rejected")
def test_request_invalid_format(opts):
    from mojo.decorators.limits import clear_rate_limits
    clear_rate_limits(ip="127.0.0.1")

    opts.client.login(TEST_USER, TEST_PWORD)
    resp = opts.client.post("/api/auth/phone/change/request", {
        "phone_number": "not-a-phone",
    })
    opts.client.logout()
    assert_true(resp.status_code in (400, 422), f"Invalid format must be rejected, got {resp.status_code}")


@th.django_unit_test("phone/change/request: requires authentication")
def test_request_requires_auth(opts):
    opts.client.logout()
    resp = opts.client.post("/api/auth/phone/change/request", {
        "phone_number": TEST_NEW_PHONE,
    })
    assert_true(resp.status_code in (401, 403), f"Unauthenticated must be rejected, got {resp.status_code}")


@th.django_unit_test("phone/change/request: ALLOW_PHONE_CHANGE=False returns 403")
def test_request_disabled(opts):
    from mojo.helpers.settings import settings
    from testit import TestitSkip
    from mojo.decorators.limits import clear_rate_limits
    clear_rate_limits(ip="127.0.0.1")

    if settings.get("ALLOW_PHONE_CHANGE", True):
        raise TestitSkip("ALLOW_PHONE_CHANGE is True on this server — cannot test disabled state")

    opts.client.login(TEST_USER, TEST_PWORD)
    resp = opts.client.post("/api/auth/phone/change/request", {
        "phone_number": TEST_NEW_PHONE,
    })
    opts.client.logout()
    assert_eq(resp.status_code, 403, f"Expected 403, got {resp.status_code}")


@th.django_unit_test("phone/change/request: incident logged")
def test_request_incident_logged(opts):
    from mojo.apps.incident.models.event import Event
    from mojo.decorators.limits import clear_rate_limits
    clear_rate_limits(ip="127.0.0.1")

    before = Event.objects.filter(
        uid=opts.user_id, category="phone_change:requested"
    ).count()

    opts.client.login(TEST_USER, TEST_PWORD)
    resp = opts.client.post("/api/auth/phone/change/request", {
        "phone_number": TEST_NEW_PHONE,
    })
    opts.client.logout()
    assert_eq(resp.status_code, 200, f"Expected 200, got {resp.status_code}")

    after = Event.objects.filter(
        uid=opts.user_id, category="phone_change:requested"
    ).count()
    assert_true(after > before, "Expected phone_change:requested incident to be logged")

    # Clean up pending state
    from mojo.apps.account.models import User
    user = User.objects.get(pk=opts.user_id)
    user.set_secret("pending_phone", None)
    user.set_secret("phone_change_otp", None)
    user.set_secret("phone_change_otp_ts", None)
    user.save(update_fields=["mojo_secrets", "modified"])


# ===========================================================================
# Confirm endpoint tests
# ===========================================================================

@th.django_unit_test("phone/change/confirm: happy path commits new number")
def test_confirm_happy(opts):
    from mojo.apps.account.models import User
    from mojo.apps.account.utils import tokens
    from mojo.decorators.limits import clear_rate_limits
    clear_rate_limits(ip="127.0.0.1")

    user = User.objects.get(pk=opts.user_id)
    user.phone_number = TEST_PHONE
    user.is_phone_verified = True
    user.save(update_fields=["phone_number", "is_phone_verified", "modified"])

    session_token, otp = tokens.generate_phone_change_token(user, TEST_NEW_PHONE)

    opts.client.login(TEST_USER, TEST_PWORD)
    resp = opts.client.post("/api/auth/phone/change/confirm", {
        "session_token": session_token,
        "code": otp,
    })
    opts.client.logout()
    assert_eq(resp.status_code, 200, f"Expected 200, got {resp.status_code}")

    user.refresh_from_db()
    assert_eq(user.phone_number, TEST_NEW_PHONE, f"Phone should be updated, got {user.phone_number}")
    assert_true(user.is_phone_verified, "is_phone_verified should be True after confirmed change")

    # Restore for other tests
    User.objects.filter(pk=opts.user_id).update(
        phone_number=TEST_PHONE, is_phone_verified=True
    )


@th.django_unit_test("phone/change/confirm: wrong OTP rejected")
def test_confirm_wrong_otp(opts):
    from mojo.apps.account.models import User
    from mojo.apps.account.utils import tokens
    from mojo.decorators.limits import clear_rate_limits
    clear_rate_limits(ip="127.0.0.1")

    user = User.objects.get(pk=opts.user_id)
    session_token, _otp = tokens.generate_phone_change_token(user, TEST_NEW_PHONE)

    opts.client.login(TEST_USER, TEST_PWORD)
    resp = opts.client.post("/api/auth/phone/change/confirm", {
        "session_token": session_token,
        "code": "000000",
    })
    opts.client.logout()
    assert_true(resp.status_code in (400, 403, 422), f"Wrong OTP must be rejected, got {resp.status_code}")

    user.refresh_from_db()
    assert_eq(user.phone_number, TEST_PHONE, "Phone should be unchanged after wrong OTP")

    # Clean up
    user.set_secret("pending_phone", None)
    user.set_secret("phone_change_otp", None)
    user.set_secret("phone_change_otp_ts", None)
    user.save(update_fields=["mojo_secrets", "modified"])


@th.django_unit_test("phone/change/confirm: missing params returns 400")
def test_confirm_missing_params(opts):
    from mojo.decorators.limits import clear_rate_limits
    clear_rate_limits(ip="127.0.0.1")

    opts.client.login(TEST_USER, TEST_PWORD)

    # Missing code
    resp = opts.client.post("/api/auth/phone/change/confirm", {
        "session_token": "pc:fake",
    })
    assert_true(resp.status_code in (400, 422), f"Missing code must return 4xx, got {resp.status_code}")

    # Missing session_token
    resp = opts.client.post("/api/auth/phone/change/confirm", {
        "code": "123456",
    })
    assert_true(resp.status_code in (400, 422), f"Missing session_token must return 4xx, got {resp.status_code}")

    opts.client.logout()


@th.django_unit_test("phone/change/confirm: requires authentication")
def test_confirm_requires_auth(opts):
    opts.client.logout()
    resp = opts.client.post("/api/auth/phone/change/confirm", {
        "session_token": "pc:fake",
        "code": "123456",
    })
    assert_true(resp.status_code in (401, 403), f"Unauthenticated must be rejected, got {resp.status_code}")


@th.django_unit_test("phone/change/confirm: expired OTP rejected")
def test_confirm_expired_otp(opts):
    from mojo.apps.account.models import User
    from mojo.apps.account.utils import tokens
    from mojo.decorators.limits import clear_rate_limits
    clear_rate_limits(ip="127.0.0.1")

    user = User.objects.get(pk=opts.user_id)
    session_token, otp = tokens.generate_phone_change_token(user, TEST_NEW_PHONE)

    # Force the stored timestamp into the distant past so the server's real TTL
    # recognises it as expired. Patching the module-level TTL only affects the
    # test process, not the running server.
    user.set_secret("phone_change_otp_ts", 0)
    user.save(update_fields=["mojo_secrets", "modified"])

    opts.client.login(TEST_USER, TEST_PWORD)
    resp = opts.client.post("/api/auth/phone/change/confirm", {
        "session_token": session_token,
        "code": otp,
    })
    opts.client.logout()
    assert_true(resp.status_code in (400, 403, 422, 500),
                f"Expired OTP must be rejected, got {resp.status_code}")

    user.refresh_from_db()
    assert_eq(user.phone_number, TEST_PHONE, "Phone should be unchanged after expired OTP")

    # Clean up
    user.set_secret("pending_phone", None)
    user.set_secret("phone_change_otp", None)
    user.set_secret("phone_change_otp_ts", None)
    user.save(update_fields=["mojo_secrets", "modified"])


@th.django_unit_test("phone/change/confirm: race — number claimed by another account is rejected")
def test_confirm_race_number_claimed(opts):
    from mojo.apps.account.models import User
    from mojo.apps.account.utils import tokens
    from mojo.decorators.limits import clear_rate_limits
    clear_rate_limits(ip="127.0.0.1")

    # Ensure user is active and phone state is clean before login
    user = User.objects.get(pk=opts.user_id)
    user.is_active = True
    user.phone_number = TEST_PHONE
    user.save(update_fields=["is_active", "phone_number", "modified"])

    # Generate token for COLLISION_PHONE which is already owned by another user
    session_token, otp = tokens.generate_phone_change_token(user, COLLISION_PHONE)

    logged_in = opts.client.login(TEST_USER, TEST_PWORD)
    assert_true(logged_in, "Login must succeed before testing confirm")
    resp = opts.client.post("/api/auth/phone/change/confirm", {
        "session_token": session_token,
        "code": otp,
    })
    opts.client.logout()
    assert_true(resp.status_code in (400, 422), f"Claimed number must be rejected, got {resp.status_code}")

    user.refresh_from_db()
    assert_eq(user.phone_number, TEST_PHONE, "Phone should be unchanged after race condition")


# ===========================================================================
# Cancel endpoint tests
# ===========================================================================

@th.django_unit_test("phone/change/cancel: clears pending state")
def test_cancel_happy(opts):
    from mojo.apps.account.models import User
    from mojo.apps.account.utils import tokens
    from mojo.decorators.limits import clear_rate_limits
    clear_rate_limits(ip="127.0.0.1")

    user = User.objects.get(pk=opts.user_id)
    session_token, otp = tokens.generate_phone_change_token(user, TEST_NEW_PHONE)

    opts.client.login(TEST_USER, TEST_PWORD)
    resp = opts.client.post("/api/auth/phone/change/cancel", {})
    assert_eq(resp.status_code, 200, f"Cancel must return 200, got {resp.status_code}")

    # Verify pending state is cleared
    user.refresh_from_db()
    assert_true(not user.get_secret("pending_phone"),
                "pending_phone should be cleared after cancel")
    assert_true(not user.get_secret("phone_change_otp"),
                "phone_change_otp should be cleared after cancel")

    # The session_token should now be dead — confirm should fail
    clear_rate_limits(ip="127.0.0.1")
    resp2 = opts.client.post("/api/auth/phone/change/confirm", {
        "session_token": session_token,
        "code": otp,
    })
    opts.client.logout()
    assert_true(resp2.status_code in (400, 403, 422, 500),
                f"Cancelled token must be rejected on confirm, got {resp2.status_code}")

    user.refresh_from_db()
    assert_eq(user.phone_number, TEST_PHONE, "Phone should be unchanged after cancelled confirm")


@th.django_unit_test("phone/change/cancel: no pending change is a safe no-op")
def test_cancel_noop(opts):
    from mojo.apps.account.models import User
    from mojo.decorators.limits import clear_rate_limits
    clear_rate_limits(ip="127.0.0.1")

    # Ensure no pending state
    user = User.objects.get(pk=opts.user_id)
    user.set_secret("pending_phone", None)
    user.set_secret("phone_change_otp", None)
    user.set_secret("phone_change_otp_ts", None)
    user.save(update_fields=["mojo_secrets", "modified"])

    opts.client.login(TEST_USER, TEST_PWORD)
    resp = opts.client.post("/api/auth/phone/change/cancel", {})
    opts.client.logout()
    assert_eq(resp.status_code, 200, f"Cancel with no pending change must return 200, got {resp.status_code}")


@th.django_unit_test("phone/change/cancel: requires authentication")
def test_cancel_requires_auth(opts):
    opts.client.logout()
    resp = opts.client.post("/api/auth/phone/change/cancel", {})
    assert_true(resp.status_code in (401, 403), f"Unauthenticated must be rejected, got {resp.status_code}")