"""
Tests for SMS OTP authentication.

Because we can't send real SMS in tests, we seed the OTP code directly
into the user's secrets (same pattern as the passkey tests that seed
credentials directly into the database).

Tests verify:
- 2FA flow: password login returns mfa_token when SMS MFA enabled
- SMS send (mocked via direct secret seeding)
- SMS verify with mfa_token + code
- Standalone SMS login (send then verify)
- Invalid/expired code rejection
- Missing phone number handling
"""
from testit import helpers as th
from mojo.helpers import dates, crypto

TEST_USER = "sms_user"
TEST_PWORD = "sms##secret99"
TEST_PHONE = "+15550001234"

SMS_OTP_TTL = 600


def _seed_otp(user, code=None):
    """Directly seed an OTP code into user secrets, bypassing SMS sending."""
    if code is None:
        code = crypto.random_string(6, allow_digits=True, allow_chars=False, allow_special=False)
    user.set_secret("sms_otp_code", code)
    user.set_secret("sms_otp_ts", int(dates.utcnow().timestamp()))
    user.save()
    return code


@th.django_unit_setup()
def setup_sms_env(opts):
    from mojo.apps.account.models import User

    user = User.objects.filter(username=TEST_USER).last()
    if user is None:
        user = User(username=TEST_USER, email=f"{TEST_USER}@example.com", display_name="SMS User")
        user.save()
    user.is_active = True
    user.phone_number = TEST_PHONE
    user.is_phone_verified = True
    user.save_password(TEST_PWORD)
    # Clear any leftover OTP
    user.set_secret("sms_otp_code", None)
    user.set_secret("sms_otp_ts", None)
    user.save()
    opts.user = user


# -----------------------------------------------------------------
# 2FA flow (password login -> mfa_token -> SMS verify)
# -----------------------------------------------------------------

@th.django_unit_test("sms: password login returns mfa_token when SMS enabled")
def test_sms_login_returns_mfa(opts):
    resp = opts.client.post("/api/login", {"username": TEST_USER, "password": TEST_PWORD})
    assert resp.status_code == 200, f"Unexpected status {resp.status_code}: {resp.response}"
    data = resp.response.data
    assert data.mfa_required is True, "Should require MFA"
    assert data.mfa_token, "Missing mfa_token"
    assert "sms" in data.mfa_methods, "sms should be in mfa_methods"
    opts.mfa_token = data.mfa_token


@th.django_unit_test("sms: verify with mfa_token + valid code issues JWT")
def test_sms_verify_with_mfa_token(opts):
    assert opts.mfa_token, "No mfa_token from previous test"

    # Seed code directly (bypass actual SMS)
    code = _seed_otp(opts.user)

    resp = opts.client.post("/api/auth/sms/verify", {"mfa_token": opts.mfa_token, "code": code})
    assert resp.status_code == 200, f"Unexpected status {resp.status_code}: {resp.response}"
    data = resp.response.data
    assert data.access_token, "Missing access_token"
    assert data.user, "Missing user"


@th.django_unit_test("sms: verify with mfa_token + invalid code is rejected")
def test_sms_verify_invalid_code(opts):
    # Get a fresh mfa_token
    resp = opts.client.post("/api/login", {"username": TEST_USER, "password": TEST_PWORD})
    mfa_token = resp.response.data.mfa_token

    _seed_otp(opts.user, code="111111")
    resp = opts.client.post("/api/auth/sms/verify", {"mfa_token": mfa_token, "code": "999999"})
    assert resp.status_code in [401, 403], f"Should reject wrong code, got {resp.status_code}"


@th.django_unit_test("sms: verify rejects expired code")
def test_sms_verify_expired_code(opts):
    resp = opts.client.post("/api/login", {"username": TEST_USER, "password": TEST_PWORD})
    mfa_token = resp.response.data.mfa_token

    # Seed code with an old timestamp
    code = "222222"
    opts.user.set_secret("sms_otp_code", code)
    opts.user.set_secret("sms_otp_ts", int(dates.utcnow().timestamp()) - SMS_OTP_TTL - 10)
    opts.user.save()

    resp = opts.client.post("/api/auth/sms/verify", {"mfa_token": mfa_token, "code": code})
    assert resp.status_code in [401, 403], f"Should reject expired code, got {resp.status_code}"


@th.django_unit_test("sms: invalid mfa_token is rejected")
def test_sms_verify_invalid_token(opts):
    code = _seed_otp(opts.user)
    resp = opts.client.post("/api/auth/sms/verify", {"mfa_token": "notavalidtoken", "code": code})
    assert resp.status_code in [401, 403], f"Should reject invalid token, got {resp.status_code}"


# -----------------------------------------------------------------
# Standalone SMS login (username -> send -> verify)
# -----------------------------------------------------------------

@th.django_unit_test("sms: standalone login send returns success without leaking user existence")
def test_sms_standalone_send(opts):
    # Known user
    resp = opts.client.post("/api/auth/sms/login", {"username": TEST_USER})
    assert resp.status_code == 200, f"Unexpected status {resp.status_code}"
    assert resp.response.status is True

    # Unknown user — should also return 200 (no enumeration)
    resp = opts.client.post("/api/auth/sms/login", {"username": "ghost_xyz_123"})
    assert resp.status_code == 200, "Should return 200 even for unknown user"


@th.django_unit_test("sms: standalone verify with username + valid code issues JWT")
def test_sms_standalone_verify(opts):
    code = _seed_otp(opts.user)
    resp = opts.client.post("/api/auth/sms/verify", {"username": TEST_USER, "code": code})
    assert resp.status_code == 200, f"Unexpected status {resp.status_code}: {resp.response}"
    data = resp.response.data
    assert data.access_token, "Missing access_token"


@th.django_unit_test("sms: standalone verify rejects invalid code")
def test_sms_standalone_invalid_code(opts):
    _seed_otp(opts.user, code="333333")
    resp = opts.client.post("/api/auth/sms/verify", {"username": TEST_USER, "code": "999999"})
    assert resp.status_code in [401, 403], f"Should reject wrong code, got {resp.status_code}"


@th.django_unit_test("sms: verify requires either mfa_token or username")
def test_sms_verify_requires_identifier(opts):
    code = _seed_otp(opts.user)
    resp = opts.client.post("/api/auth/sms/verify", {"code": code})
    assert resp.status_code == 400, f"Should require mfa_token or username, got {resp.status_code}"


# -----------------------------------------------------------------
# No phone number
# -----------------------------------------------------------------

@th.django_unit_test("sms: user without phone cannot use SMS login")
def test_sms_no_phone(opts):
    from mojo.apps.account.models import User

    no_phone_user = User.objects.filter(username="sms_nophone").last()
    if no_phone_user is None:
        no_phone_user = User(username="sms_nophone", email="sms_nophone@example.com")
        no_phone_user.save()
    no_phone_user.phone_number = None
    no_phone_user.is_phone_verified = False
    no_phone_user.save_password(TEST_PWORD)
    no_phone_user.save()

    # Password login should return JWT directly — no SMS MFA
    resp = opts.client.post("/api/login", {"username": "sms_nophone", "password": TEST_PWORD})
    assert resp.status_code == 200, f"Unexpected status {resp.status_code}"
    data = resp.response.data
    assert not getattr(data, "mfa_required", False), "Should not require MFA without phone"
    assert data.access_token, "Should return JWT directly"
