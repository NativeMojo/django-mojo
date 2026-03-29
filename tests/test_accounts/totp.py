"""
Tests for TOTP (Google Authenticator) authentication.

Tests verify:
- Setup flow (begin -> confirm)
- Disable TOTP
- Login with MFA: password login returns mfa_token when TOTP enabled
- 2FA verify step (mfa_token + code)
- Standalone TOTP login (username + code, no password)
- Invalid code rejection
- Standalone login fails when TOTP not enabled
"""
from testit import helpers as th

TEST_USER = "totp_user"
TEST_PWORD = "totp##secret99"


@th.django_unit_setup()
def setup_totp_env(opts):
    from mojo.apps.account.models import User
    from mojo.apps.account.models.totp import UserTOTP

    user = User.objects.filter(username=TEST_USER).last()
    if user is None:
        user = User(username=TEST_USER, email=f"{TEST_USER}@example.com", display_name="TOTP User")
        user.save()
    user.is_active = True
    user.is_email_verified = True
    user.save_password(TEST_PWORD)

    # Clean up any existing TOTP records
    UserTOTP.objects.filter(user=user).delete()
    opts.user = user


# -----------------------------------------------------------------
# Setup flow
# -----------------------------------------------------------------

@th.django_unit_test("totp: setup returns secret and QR code")
def test_totp_setup(opts):
    assert opts.client.login(TEST_USER, TEST_PWORD), "Login failed"

    resp = opts.client.post("/api/account/totp/setup", {})
    assert resp.status_code == 200, f"Unexpected status {resp.status_code}: {resp.response}"
    data = resp.response.data
    assert data.secret, "Missing secret"
    assert data.uri, "Missing URI"
    assert data.qr_code, "Missing QR code"
    assert "otpauth://" in data.uri, "URI should be otpauth://"
    assert data.qr_code.startswith("data:image/"), "QR code should be a data URI"

    opts.totp_secret = data.secret


@th.django_unit_test("totp: confirm activates TOTP")
def test_totp_confirm(opts):
    import pyotp
    assert opts.totp_secret, "No secret from setup"
    assert opts.client.login(TEST_USER, TEST_PWORD), "Login failed"

    code = pyotp.TOTP(opts.totp_secret).now()
    resp = opts.client.post("/api/account/totp/confirm", {"code": code})
    assert resp.status_code == 200, f"Unexpected status {resp.status_code}: {resp.response}"
    assert resp.response.data.is_enabled is True, "TOTP should be enabled"


@th.django_unit_test("totp: confirm rejects invalid code")
def test_totp_confirm_invalid(opts):
    # Client is still authenticated from test_totp_confirm — no need to re-login
    # Re-setup to get a fresh unconfirmed secret (TOTP is already enabled so login requires MFA)
    resp = opts.client.post("/api/account/totp/setup", {})
    assert resp.status_code == 200, f"Setup failed: {resp.status_code}"
    opts.totp_secret = resp.response.data.secret

    resp = opts.client.post("/api/account/totp/confirm", {"code": "000000"})
    assert resp.status_code == 400, f"Should reject invalid code, got {resp.status_code}"

    # Re-confirm with valid code so TOTP is active for subsequent tests
    import pyotp
    code = pyotp.TOTP(opts.totp_secret).now()
    opts.client.post("/api/account/totp/confirm", {"code": code})


# -----------------------------------------------------------------
# Password login -> mfa_token flow
# -----------------------------------------------------------------

@th.django_unit_test("totp: password login returns mfa_token when TOTP enabled")
def test_totp_login_returns_mfa(opts):
    resp = opts.client.post("/api/login", {"username": TEST_USER, "password": TEST_PWORD})
    assert resp.status_code == 200, f"Unexpected status {resp.status_code}: {resp.response}"
    data = resp.response.data
    assert data.mfa_required is True, "Should require MFA"
    assert data.mfa_token, "Missing mfa_token"
    assert "totp" in data.mfa_methods, "totp should be in mfa_methods"
    assert data.expires_in > 0, "Missing expires_in"
    opts.mfa_token = data.mfa_token


@th.django_unit_test("totp: mfa_token + valid code issues JWT")
def test_totp_verify_success(opts):
    import pyotp
    assert opts.mfa_token, "No mfa_token from previous test"

    code = pyotp.TOTP(opts.totp_secret).now()
    resp = opts.client.post("/api/auth/totp/verify", {"mfa_token": opts.mfa_token, "code": code})
    assert resp.status_code == 200, f"Unexpected status {resp.status_code}: {resp.response}"
    data = resp.response.data
    assert data.access_token, "Missing access_token"
    assert data.user, "Missing user"


@th.django_unit_test("totp: mfa_token + invalid code is rejected")
def test_totp_verify_invalid_code(opts):
    # Get a fresh mfa_token
    resp = opts.client.post("/api/login", {"username": TEST_USER, "password": TEST_PWORD})
    mfa_token = resp.response.data.mfa_token

    resp = opts.client.post("/api/auth/totp/verify", {"mfa_token": mfa_token, "code": "000000"})
    assert resp.status_code in [401, 403], f"Should reject invalid code, got {resp.status_code}"


@th.django_unit_test("totp: expired/invalid mfa_token is rejected")
def test_totp_verify_invalid_token(opts):
    import pyotp
    code = pyotp.TOTP(opts.totp_secret).now()
    resp = opts.client.post("/api/auth/totp/verify", {"mfa_token": "notavalidtoken", "code": code})
    assert resp.status_code in [401, 403], f"Should reject invalid mfa_token, got {resp.status_code}"


@th.django_unit_test("totp: mfa_token is single-use")
def test_totp_mfa_token_single_use(opts):
    import pyotp
    # Get a fresh token
    resp = opts.client.post("/api/login", {"username": TEST_USER, "password": TEST_PWORD})
    mfa_token = resp.response.data.mfa_token
    code = pyotp.TOTP(opts.totp_secret).now()

    # First use succeeds
    resp = opts.client.post("/api/auth/totp/verify", {"mfa_token": mfa_token, "code": code})
    assert resp.status_code == 200, "First use should succeed"

    # Second use with same token fails (consumed)
    resp = opts.client.post("/api/auth/totp/verify", {"mfa_token": mfa_token, "code": code})
    assert resp.status_code in [401, 403], "Second use should fail — token consumed"


# -----------------------------------------------------------------
# Standalone TOTP login (no password)
# -----------------------------------------------------------------

@th.django_unit_test("totp: standalone login with valid code issues JWT")
def test_totp_standalone_login(opts):
    import pyotp
    code = pyotp.TOTP(opts.totp_secret).now()
    resp = opts.client.post("/api/auth/totp/login", {"username": TEST_USER, "code": code})
    assert resp.status_code == 200, f"Unexpected status {resp.status_code}: {resp.response}"
    data = resp.response.data
    assert data.access_token, "Missing access_token"


@th.django_unit_test("totp: standalone login rejects invalid code")
def test_totp_standalone_invalid(opts):
    resp = opts.client.post("/api/auth/totp/login", {"username": TEST_USER, "code": "000000"})
    assert resp.status_code in [401, 403], f"Should reject invalid code, got {resp.status_code}"


@th.django_unit_test("totp: standalone login rejects unknown user")
def test_totp_standalone_unknown_user(opts):
    resp = opts.client.post("/api/auth/totp/login", {"username": "ghost_user_xyz", "code": "123456"})
    assert resp.status_code in [401, 403], f"Should reject unknown user, got {resp.status_code}"


# -----------------------------------------------------------------
# Disable
# -----------------------------------------------------------------

@th.django_unit_test("totp: disable removes TOTP requirement from login")
def test_totp_disable(opts):
    # TOTP is active so standard login returns MFA challenge — use MFA flow to authenticate
    import pyotp
    resp = opts.client.post("/api/login", {"username": TEST_USER, "password": TEST_PWORD})
    mfa_token = resp.response.data.mfa_token
    code = pyotp.TOTP(opts.totp_secret).now()
    login_resp = opts.client.post("/api/auth/totp/verify", {"mfa_token": mfa_token, "code": code})
    assert login_resp.status_code == 200, f"MFA login failed: {login_resp.response}"
    opts.client.access_token = login_resp.response.data.access_token
    opts.client.is_authenticated = True

    resp = opts.client.delete("/api/account/totp")
    assert resp.status_code == 200, f"Unexpected status {resp.status_code}: {resp.response}"

    # Password login should now return JWT directly (no MFA)
    resp = opts.client.post("/api/login", {"username": TEST_USER, "password": TEST_PWORD})
    assert resp.status_code == 200, f"Unexpected status {resp.status_code}"
    data = resp.response.data
    assert not getattr(data, "mfa_required", False), "Should not require MFA after disable"
    assert data.access_token, "Should return JWT directly after disabling TOTP"


@th.django_unit_test("totp: setup requires authentication")
def test_totp_setup_requires_auth(opts):
    opts.client.logout()
    resp = opts.client.post("/api/account/totp/setup", {})
    assert resp.status_code in [401, 403], f"Should require auth, got {resp.status_code}"
