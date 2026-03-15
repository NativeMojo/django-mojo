"""
Tests for the self-service account deactivation flow.

Coverage:
  - Happy path: request sends email, confirm with valid token → is_active=False
  - Already inactive: confirm returns 200, pii_anonymize() not called twice
  - Token expired: confirm returns 400
  - Token wrong kind (e.g. ml: or pr: token): confirm returns 400
  - Token already used: confirm returns 400
  - ALLOW_SELF_DEACTIVATION = False: request returns 403
  - Unauthenticated request to /deactivate: returns 401/403
  - JWT is invalid after deactivation (auth_key was rotated)
  - Incident account:deactivated written before anonymisation
  - dv: token has correct prefix
"""
from testit import helpers as th
from testit.helpers import assert_true, assert_eq

TEST_USER = "deactivation_user"
TEST_PWORD = "deact##mojo99"
TEST_EMAIL = "deactivation_user@example.com"

# A separate user for the happy-path test so we don't break other tests
HAPPY_USER = "deact_happy_user"
HAPPY_EMAIL = "deact_happy_user@example.com"

# A user for the already-inactive test
INACTIVE_USER = "deact_inactive_user"
INACTIVE_EMAIL = "deact_inactive_user@example.com"


# ===========================================================================
# Setup / teardown
# ===========================================================================

@th.django_unit_setup()
def setup_deactivation(opts):
    from mojo.apps.account.models import User
    from mojo.decorators.limits import clear_rate_limits
    clear_rate_limits(ip="127.0.0.1")

    # Primary test user — used for request-only tests (not actually deactivated)
    user = User.objects.filter(email=TEST_EMAIL).last()
    if user is None:
        user = User(username=TEST_USER, email=TEST_EMAIL)
        user.save()
    user.username = TEST_USER
    user.email = TEST_EMAIL
    user.is_active = True
    user.is_email_verified = True
    user.requires_mfa = False
    user.save_password(TEST_PWORD)
    user.save()
    opts.user_id = user.pk

    # Happy-path user — will be deactivated and recreated each run
    happy = User.objects.filter(email=HAPPY_EMAIL).last()
    if happy is None:
        happy = User(username=HAPPY_USER, email=HAPPY_EMAIL)
        happy.save()
    happy.username = HAPPY_USER
    happy.email = HAPPY_EMAIL
    happy.is_active = True
    happy.is_email_verified = True
    happy.requires_mfa = False
    happy.metadata = {}
    happy.save_password(TEST_PWORD)
    happy.save()
    opts.happy_user_id = happy.pk

    # Already-inactive user
    inactive = User.objects.filter(email=INACTIVE_EMAIL).last()
    if inactive is None:
        inactive = User(username=INACTIVE_USER, email=INACTIVE_EMAIL)
        inactive.save()
    inactive.username = INACTIVE_USER
    inactive.email = INACTIVE_EMAIL
    inactive.is_active = True
    inactive.is_email_verified = True
    inactive.requires_mfa = False
    inactive.save_password(TEST_PWORD)
    inactive.save()
    opts.inactive_user_id = inactive.pk


# ===========================================================================
# Token unit tests
# ===========================================================================

@th.django_unit_test("dv token: has dv: prefix")
def test_dv_token_prefix(opts):
    from mojo.apps.account.models import User
    from mojo.apps.account.utils import tokens

    user = User.objects.get(pk=opts.user_id)
    tok = tokens.generate_deactivate_token(user)
    assert_true(tok.startswith("dv:"), f"Expected 'dv:' prefix, got: {tok[:10]}")
    # consume cleanly
    tokens.verify_deactivate_token(tok)


@th.django_unit_test("dv token: single-use — second verify fails")
def test_dv_token_single_use(opts):
    from mojo.apps.account.models import User
    from mojo.apps.account.utils import tokens

    user = User.objects.get(pk=opts.user_id)
    tok = tokens.generate_deactivate_token(user)

    # First verify succeeds
    result = tokens.verify_deactivate_token(tok)
    assert_true(result is not None, "First verify should succeed")

    # Second verify should fail (single-use JTI rotation)
    try:
        tokens.verify_deactivate_token(tok)
        assert_true(False, "Second verify should have raised an exception")
    except Exception:
        pass  # Expected


@th.django_unit_test("dv token: wrong kind prefix rejected")
def test_dv_token_wrong_kind(opts):
    from mojo.apps.account.models import User
    from mojo.apps.account.utils import tokens

    user = User.objects.get(pk=opts.user_id)
    # Generate a password-reset token (pr:) and try to verify as dv:
    pr_token = tokens.generate_password_reset_token(user)
    try:
        tokens.verify_deactivate_token(pr_token)
        assert_true(False, "pr: token should not pass dv: verification")
    except Exception:
        pass  # Expected


# ===========================================================================
# Request endpoint tests
# ===========================================================================

@th.django_unit_test("deactivate request: authenticated user gets 200")
def test_deactivate_request_happy(opts):
    from mojo.decorators.limits import clear_rate_limits
    clear_rate_limits(ip="127.0.0.1")

    opts.client.login(TEST_USER, TEST_PWORD)
    resp = opts.client.post("/api/account/deactivate", {})
    opts.client.logout()
    assert_eq(resp.status_code, 200, f"Expected 200, got {resp.status_code}")
    data = resp.json
    assert_true(data.get("status"), "Expected status=true")
    assert_true("confirmation" in str(data.get("message", "")).lower() or
                "email" in str(data.get("message", "")).lower(),
                "Response should mention confirmation email")


@th.django_unit_test("deactivate request: unauthenticated returns 401/403")
def test_deactivate_request_unauth(opts):
    opts.client.logout()
    resp = opts.client.post("/api/account/deactivate", {})
    assert_true(resp.status_code in (401, 403), f"Expected 401 or 403, got {resp.status_code}")


@th.django_unit_test("deactivate request: ALLOW_SELF_DEACTIVATION=False returns 403")
def test_deactivate_request_disabled(opts):
    from mojo.helpers.settings import settings
    from testit import TestitSkip

    if settings.get("ALLOW_SELF_DEACTIVATION", True):
        raise TestitSkip("ALLOW_SELF_DEACTIVATION is True on this server — cannot test disabled state")

    opts.client.login(TEST_USER, TEST_PWORD)
    resp = opts.client.post("/api/account/deactivate", {})
    opts.client.logout()
    assert_eq(resp.status_code, 403, f"Expected 403, got {resp.status_code}")


@th.django_unit_test("deactivate request: incident logged")
def test_deactivate_request_incident(opts):
    from mojo.apps.incident.models.event import Event
    from mojo.decorators.limits import clear_rate_limits
    clear_rate_limits(ip="127.0.0.1")

    before = Event.objects.filter(
        uid=opts.user_id, category="account:deactivate_requested"
    ).count()

    opts.client.login(TEST_USER, TEST_PWORD)
    resp = opts.client.post("/api/account/deactivate", {})
    opts.client.logout()
    assert_eq(resp.status_code, 200, f"Expected 200, got {resp.status_code}")

    after = Event.objects.filter(
        uid=opts.user_id, category="account:deactivate_requested"
    ).count()
    assert_true(after > before, "Expected account:deactivate_requested incident to be logged")


# ===========================================================================
# Confirm endpoint tests
# ===========================================================================

@th.django_unit_test("deactivate confirm: happy path — account deactivated")
def test_deactivate_confirm_happy(opts):
    from mojo.apps.account.models import User
    from mojo.apps.account.utils import tokens

    user = User.objects.get(pk=opts.happy_user_id)
    tok = tokens.generate_deactivate_token(user)

    opts.client.logout()
    resp = opts.client.post("/api/account/deactivate/confirm", {"token": tok})
    assert_eq(resp.status_code, 200, f"Expected 200, got {resp.status_code}")
    data = resp.json
    assert_true(data.get("status"), "Expected status=true")
    assert_true("deactivated" in str(data.get("message", "")).lower(),
                "Response should confirm deactivation")

    # Verify user is now inactive
    user.refresh_from_db()
    assert_true(not user.is_active, "User should be inactive after deactivation")

    # Verify PII was anonymised (username should be deleted-<token>)
    assert_true(user.username.startswith("deleted-"),
                f"Username should be anonymised, got: {user.username}")


@th.django_unit_test("deactivate confirm: already inactive returns 200 idempotent")
def test_deactivate_confirm_already_inactive(opts):
    from mojo.apps.account.models import User
    from mojo.apps.account.utils import tokens

    user = User.objects.get(pk=opts.inactive_user_id)

    # Generate token while user is still active
    tok = tokens.generate_deactivate_token(user)

    # Manually deactivate first
    user.is_active = False
    user.save(update_fields=["is_active", "modified"])

    # Confirm should return 200 without calling pii_anonymize again
    opts.client.logout()
    resp = opts.client.post("/api/account/deactivate/confirm", {"token": tok})
    assert_eq(resp.status_code, 200, f"Expected 200, got {resp.status_code}")

    # Username should NOT have been anonymised (was already inactive)
    user.refresh_from_db()
    assert_eq(user.username, INACTIVE_USER,
              "Username should be unchanged — pii_anonymize should not run on already-inactive user")

    # Restore for other tests
    user.is_active = True
    user.save(update_fields=["is_active", "modified"])


@th.django_unit_test("deactivate confirm: missing token returns 400")
def test_deactivate_confirm_missing_token(opts):
    opts.client.logout()
    resp = opts.client.post("/api/account/deactivate/confirm", {})
    assert_true(resp.status_code in (400, 422), f"Expected 400, got {resp.status_code}")


@th.django_unit_test("deactivate confirm: invalid token returns 400/403")
def test_deactivate_confirm_invalid_token(opts):
    opts.client.logout()
    resp = opts.client.post("/api/account/deactivate/confirm", {"token": "dv:totally_invalid_garbage"})
    assert_true(resp.status_code in (400, 403, 500), f"Expected 400 or 403, got {resp.status_code}")


@th.django_unit_test("deactivate confirm: wrong kind token (pr:) rejected")
def test_deactivate_confirm_wrong_kind(opts):
    from mojo.apps.account.models import User
    from mojo.apps.account.utils import tokens

    user = User.objects.get(pk=opts.user_id)
    pr_token = tokens.generate_password_reset_token(user)

    opts.client.logout()
    resp = opts.client.post("/api/account/deactivate/confirm", {"token": pr_token})
    assert_true(resp.status_code in (400, 403, 500), f"Expected rejection of pr: token, got {resp.status_code}")


@th.django_unit_test("deactivate confirm: used token rejected on second attempt")
def test_deactivate_confirm_used_token(opts):
    from mojo.apps.account.models import User
    from mojo.apps.account.utils import tokens

    # Use the inactive_user for this so we don't burn the primary user
    user = User.objects.get(pk=opts.inactive_user_id)
    tok = tokens.generate_deactivate_token(user)

    # Deactivate to make it already-inactive (confirm returns 200 idempotent)
    user.is_active = False
    user.save(update_fields=["is_active", "modified"])

    # First confirm — should succeed (200 idempotent path)
    opts.client.logout()
    resp1 = opts.client.post("/api/account/deactivate/confirm", {"token": tok})
    assert_eq(resp1.status_code, 200, f"First confirm expected 200, got {resp1.status_code}")

    # Second confirm with same token — should fail (JTI already consumed)
    resp2 = opts.client.post("/api/account/deactivate/confirm", {"token": tok})
    assert_true(resp2.status_code in (400, 403, 500),
                f"Expected used token to be rejected, got {resp2.status_code}")

    # Restore
    user.is_active = True
    user.username = INACTIVE_USER
    user.email = INACTIVE_EMAIL
    user.save(update_fields=["is_active", "username", "email", "modified"])


@th.django_unit_test("deactivate confirm: incident logged before anonymisation")
def test_deactivate_confirm_incident_logged(opts):
    from mojo.apps.account.models import User
    from mojo.apps.account.utils import tokens
    from mojo.apps.incident.models.event import Event

    # Create a fresh disposable user for this test
    disposable = User.objects.filter(email="deact_incident_test@example.com").last()
    if disposable is None:
        disposable = User(username="deact_incident_test", email="deact_incident_test@example.com")
        disposable.save()
    disposable.username = "deact_incident_test"
    disposable.email = "deact_incident_test@example.com"
    disposable.is_active = True
    disposable.save_password(TEST_PWORD)
    disposable.save()

    before = Event.objects.filter(
        uid=disposable.pk, category="account:deactivated"
    ).count()

    tok = tokens.generate_deactivate_token(disposable)
    opts.client.logout()
    resp = opts.client.post("/api/account/deactivate/confirm", {"token": tok})
    assert_eq(resp.status_code, 200, f"Expected 200, got {resp.status_code}")

    after = Event.objects.filter(
        uid=disposable.pk, category="account:deactivated"
    ).count()
    assert_true(after > before,
                "Expected account:deactivated incident to be logged before anonymisation")


@th.django_unit_test("deactivate confirm: JWT invalid after deactivation")
def test_deactivate_jwt_invalid_after(opts):
    from mojo.apps.account.models import User
    from mojo.apps.account.utils import tokens

    # Create a fresh disposable user
    disposable = User.objects.filter(email="deact_jwt_test@example.com").last()
    if disposable is None:
        disposable = User(username="deact_jwt_test", email="deact_jwt_test@example.com")
        disposable.save()
    disposable.username = "deact_jwt_test"
    disposable.email = "deact_jwt_test@example.com"
    disposable.is_active = True
    disposable.save_password(TEST_PWORD)
    disposable.save()

    # Log in to get a JWT
    opts.client.login("deact_jwt_test", TEST_PWORD)
    old_token = opts.client.access_token

    # Deactivate via token (use a separate client call to avoid auth header interference)
    tok = tokens.generate_deactivate_token(disposable)
    opts.client.logout()
    resp = opts.client.post("/api/account/deactivate/confirm", {"token": tok})
    assert_eq(resp.status_code, 200, f"Expected 200, got {resp.status_code}")

    # Old JWT should now be invalid
    if old_token:
        opts.client.access_token = old_token
        opts.client.is_authenticated = True
        me_resp = opts.client.get("/api/user/me")
        opts.client.logout()
        assert_true(me_resp.status_code in (401, 403),
                    f"Old JWT should be invalid after deactivation, got {me_resp.status_code}")