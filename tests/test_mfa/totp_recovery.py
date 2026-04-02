"""
Tests for TOTP recovery codes — generation, masking, consumption, and recovery login.

Security contract this file enforces:
  - Confirm response includes 8 recovery codes in xxxx-xxxx-xxxx format
  - Recovery codes are stored as bcrypt hashes (plaintext never in secrets)
  - GET masked codes returns correct count and masked format
  - Recovery login with valid mfa_token + recovery_code issues JWT
  - Recovery login with invalid recovery_code returns 403
  - Recovery login with invalid/expired mfa_token returns 401
  - Used recovery code cannot be reused (single-use)
  - Regenerate with valid TOTP code produces 8 new codes and invalidates old ones
  - Regenerate with invalid TOTP code returns 403 and preserves old codes
  - GET when TOTP not enabled returns 400
  - Unauthenticated GET / regenerate returns 401/403
"""
from testit import helpers as th
from testit.helpers import assert_true, assert_eq

TEST_USER = "totp_recovery_user"
TEST_PWORD = "recovery##mojo99"


# ===========================================================================
# Setup / teardown
# ===========================================================================

@th.django_unit_setup()
def setup_totp_recovery(opts):
    from mojo.apps.account.models import User
    from mojo.apps.account.models.totp import UserTOTP
    from mojo.decorators.limits import clear_rate_limits
    clear_rate_limits(ip="127.0.0.1")

    user = User.objects.filter(username=TEST_USER).last()
    if user is None:
        user = User(username=TEST_USER, email=f"{TEST_USER}@example.com", display_name="Recovery User")
        user.save()
    user.is_active = True
    user.is_email_verified = True
    user.requires_mfa = False
    user.save_password(TEST_PWORD)
    user.save()
    opts.user_id = user.pk

    # Clean up any existing TOTP records
    UserTOTP.objects.filter(user=user).delete()

    # Set up TOTP: setup + confirm
    assert opts.client.login(TEST_USER, TEST_PWORD), "Login failed during setup"

    resp = opts.client.post("/api/account/totp/setup", {})
    assert resp.status_code == 200, f"TOTP setup failed: {resp.status_code}"
    opts.totp_secret = resp.response.data.secret

    import pyotp
    code = pyotp.TOTP(opts.totp_secret).now()
    resp = opts.client.post("/api/account/totp/confirm", {"code": code})
    assert resp.status_code == 200, f"TOTP confirm failed: {resp.status_code}"

    # Capture recovery codes from confirm response
    opts.recovery_codes = resp.response.data.recovery_codes
    opts.client.logout()


# ===========================================================================
# Confirm response includes recovery codes
# ===========================================================================

@th.django_unit_test("totp recovery: confirm returns 8 recovery codes")
def test_confirm_returns_recovery_codes(opts):
    codes = opts.recovery_codes
    assert_true(codes is not None, "Confirm response must include recovery_codes")
    assert_eq(len(codes), 8, f"Expected 8 recovery codes, got {len(codes)}")


@th.django_unit_test("totp recovery: codes have xxxx-xxxx-xxxx format")
def test_confirm_code_format(opts):
    import re
    pattern = re.compile(r"^[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}$")
    for code in opts.recovery_codes:
        assert_true(pattern.match(code), f"Code '{code}' does not match xxxx-xxxx-xxxx hex format")


# ===========================================================================
# Recovery codes stored as bcrypt (not plaintext)
# ===========================================================================

@th.django_unit_test("totp recovery: codes stored as bcrypt hashes, not plaintext")
def test_codes_stored_as_bcrypt(opts):
    from mojo.apps.account.models.totp import UserTOTP
    totp = UserTOTP.objects.get(user_id=opts.user_id)
    stored = totp.get_secret("recovery_codes")
    assert_true(stored is not None, "recovery_codes secret must exist")
    assert_eq(len(stored), 8, f"Expected 8 stored entries, got {len(stored)}")
    for entry in stored:
        assert_true("hash" in entry, "Each entry must have a 'hash' field")
        assert_true("hint" in entry, "Each entry must have a 'hint' field")
        # bcrypt hashes start with $2b$ or $2a$
        assert_true(
            entry["hash"].startswith("$2b$") or entry["hash"].startswith("$2a$"),
            f"Hash does not look like bcrypt: {entry['hash'][:10]}...",
        )
        # Plaintext codes must NOT appear in any stored field
        for code in opts.recovery_codes:
            assert_true(
                code != entry["hash"],
                "Plaintext code must never be stored as the hash value",
            )


# ===========================================================================
# GET masked recovery codes
# ===========================================================================

@th.django_unit_test("totp recovery: GET returns masked codes with correct count")
def test_get_masked_codes(opts):
    import re
    from mojo.decorators.limits import clear_rate_limits
    clear_rate_limits(ip="127.0.0.1")

    # Authenticate via MFA flow since TOTP is enabled
    _mfa_login(opts)

    resp = opts.client.get("/api/account/totp/recovery-codes")
    assert_eq(resp.status_code, 200, f"Expected 200, got {resp.status_code}")
    data = resp.response.data
    assert_eq(data.remaining, 8, f"Expected 8 remaining, got {data.remaining}")
    assert_eq(len(data.codes), 8, f"Expected 8 masked codes, got {len(data.codes)}")

    mask_pattern = re.compile(r"^[0-9a-f]{4}-xxxx-xxxx$")
    for masked in data.codes:
        assert_true(mask_pattern.match(masked), f"Masked code '{masked}' has wrong format")

    opts.client.logout()


@th.django_unit_test("totp recovery: GET when TOTP not enabled returns 400")
def test_get_codes_totp_not_enabled(opts):
    from mojo.apps.account.models import User
    from mojo.apps.account.models.totp import UserTOTP
    from mojo.decorators.limits import clear_rate_limits
    clear_rate_limits(ip="127.0.0.1")

    # Temporarily disable TOTP
    UserTOTP.objects.filter(user_id=opts.user_id).update(is_enabled=False)

    # Login directly (no MFA since disabled)
    user = User.objects.get(pk=opts.user_id)
    user.requires_mfa = False
    user.save()
    opts.client.login(TEST_USER, TEST_PWORD)

    resp = opts.client.get("/api/account/totp/recovery-codes")
    assert_true(resp.status_code == 400, f"Expected 400 when TOTP disabled, got {resp.status_code}")
    opts.client.logout()

    # Re-enable TOTP
    UserTOTP.objects.filter(user_id=opts.user_id).update(is_enabled=True)
    user.requires_mfa = True
    user.save()


@th.django_unit_test("totp recovery: unauthenticated GET returns 401/403")
def test_get_codes_unauthenticated(opts):
    from mojo.decorators.limits import clear_rate_limits
    clear_rate_limits(ip="127.0.0.1")

    opts.client.logout()
    resp = opts.client.get("/api/account/totp/recovery-codes")
    assert_true(resp.status_code in (401, 403), f"Expected 401/403, got {resp.status_code}")


# ===========================================================================
# Recovery login happy path
# ===========================================================================

@th.django_unit_test("totp recovery: recovery login with valid mfa_token + code issues JWT")
def test_recovery_login_happy_path(opts):
    from mojo.decorators.limits import clear_rate_limits
    clear_rate_limits(ip="127.0.0.1")

    # Get mfa_token via password login
    resp = opts.client.post("/api/login", {"username": TEST_USER, "password": TEST_PWORD})
    assert_eq(resp.status_code, 200, f"Login failed: {resp.status_code}")
    mfa_token = resp.response.data.mfa_token
    assert_true(mfa_token, "Expected mfa_token from login")

    # Use first recovery code
    recovery_code = opts.recovery_codes[0]
    resp = opts.client.post("/api/auth/totp/recover", {
        "mfa_token": mfa_token,
        "recovery_code": recovery_code,
    })
    assert_eq(resp.status_code, 200, f"Recovery login failed: {resp.status_code}")
    data = resp.response.data
    assert_true(data.access_token, "Missing access_token in recovery login response")
    assert_true(data.user, "Missing user in recovery login response")


# ===========================================================================
# Recovery login — invalid code
# ===========================================================================

@th.django_unit_test("totp recovery: recovery login with invalid code returns 403")
def test_recovery_login_invalid_code(opts):
    from mojo.decorators.limits import clear_rate_limits
    clear_rate_limits(ip="127.0.0.1")

    resp = opts.client.post("/api/login", {"username": TEST_USER, "password": TEST_PWORD})
    mfa_token = resp.response.data.mfa_token

    resp = opts.client.post("/api/auth/totp/recover", {
        "mfa_token": mfa_token,
        "recovery_code": "dead-beef-cafe",
    })
    assert_eq(resp.status_code, 403, f"Expected 403 for invalid recovery code, got {resp.status_code}")


# ===========================================================================
# Recovery login — invalid mfa_token
# ===========================================================================

@th.django_unit_test("totp recovery: recovery login with invalid mfa_token returns 401")
def test_recovery_login_invalid_mfa_token(opts):
    from mojo.decorators.limits import clear_rate_limits
    clear_rate_limits(ip="127.0.0.1")

    resp = opts.client.post("/api/auth/totp/recover", {
        "mfa_token": "totally_bogus_token",
        "recovery_code": opts.recovery_codes[1],
    })
    assert_eq(resp.status_code, 401, f"Expected 401 for invalid mfa_token, got {resp.status_code}")


# ===========================================================================
# Used recovery code cannot be reused
# ===========================================================================

@th.django_unit_test("totp recovery: used recovery code cannot be reused")
def test_recovery_code_single_use(opts):
    from mojo.decorators.limits import clear_rate_limits
    clear_rate_limits(ip="127.0.0.1")

    # opts.recovery_codes[0] was consumed in the happy-path test above
    used_code = opts.recovery_codes[0]

    resp = opts.client.post("/api/login", {"username": TEST_USER, "password": TEST_PWORD})
    mfa_token = resp.response.data.mfa_token

    resp = opts.client.post("/api/auth/totp/recover", {
        "mfa_token": mfa_token,
        "recovery_code": used_code,
    })
    assert_eq(resp.status_code, 403, f"Reused code should be rejected with 403, got {resp.status_code}")


# ===========================================================================
# Regenerate — happy path
# ===========================================================================

@th.django_unit_test("totp recovery: regenerate with valid TOTP code returns 8 new codes")
def test_regenerate_happy_path(opts):
    import pyotp
    from mojo.decorators.limits import clear_rate_limits
    clear_rate_limits(ip="127.0.0.1")

    _mfa_login(opts)

    code = pyotp.TOTP(opts.totp_secret).now()
    resp = opts.client.post("/api/account/totp/recovery-codes/regenerate", {"code": code})
    assert_eq(resp.status_code, 200, f"Regenerate failed: {resp.status_code}")
    new_codes = resp.response.data.recovery_codes
    assert_eq(len(new_codes), 8, f"Expected 8 new codes, got {len(new_codes)}")

    # Old codes (the ones not yet consumed) should no longer work
    old_unused = opts.recovery_codes[1]  # code[0] was consumed, code[1] was unused
    from mojo.apps.account.models.totp import UserTOTP
    totp = UserTOTP.objects.get(user_id=opts.user_id)
    result = totp.verify_and_consume_recovery_code(old_unused)
    assert_true(result is False, "Old recovery code should be invalid after regenerate")

    # Update opts with new codes for subsequent tests
    opts.recovery_codes = new_codes
    opts.client.logout()


# ===========================================================================
# Regenerate — invalid TOTP code
# ===========================================================================

@th.django_unit_test("totp recovery: regenerate with invalid TOTP code returns 403, old codes preserved")
def test_regenerate_invalid_totp(opts):
    from mojo.decorators.limits import clear_rate_limits
    clear_rate_limits(ip="127.0.0.1")

    _mfa_login(opts)

    resp = opts.client.post("/api/account/totp/recovery-codes/regenerate", {"code": "000000"})
    assert_eq(resp.status_code, 403, f"Expected 403 for invalid TOTP code, got {resp.status_code}")

    # Verify old codes are still intact
    from mojo.apps.account.models.totp import UserTOTP
    totp = UserTOTP.objects.get(user_id=opts.user_id)
    stored = totp.get_secret("recovery_codes") or []
    assert_eq(len(stored), 8, f"Old codes should be preserved, got {len(stored)} entries")

    opts.client.logout()


# ===========================================================================
# Unauthenticated regenerate
# ===========================================================================

@th.django_unit_test("totp recovery: unauthenticated regenerate returns 401/403")
def test_regenerate_unauthenticated(opts):
    from mojo.decorators.limits import clear_rate_limits
    clear_rate_limits(ip="127.0.0.1")

    opts.client.logout()
    resp = opts.client.post("/api/account/totp/recovery-codes/regenerate", {"code": "123456"})
    assert_true(resp.status_code in (401, 403), f"Expected 401/403, got {resp.status_code}")


# ===========================================================================
# Helpers
# ===========================================================================

def _mfa_login(opts):
    """Authenticate via the full MFA flow (password -> mfa_token -> TOTP verify)."""
    import pyotp
    from mojo.decorators.limits import clear_rate_limits
    clear_rate_limits(ip="127.0.0.1")

    resp = opts.client.post("/api/login", {"username": TEST_USER, "password": TEST_PWORD})
    assert resp.status_code == 200, f"Password login failed: {resp.status_code}"
    mfa_token = resp.response.data.mfa_token
    code = pyotp.TOTP(opts.totp_secret).now()
    resp = opts.client.post("/api/auth/totp/verify", {"mfa_token": mfa_token, "code": code})
    assert resp.status_code == 200, f"MFA verify failed: {resp.status_code}"
    opts.client.access_token = resp.response.data.access_token
    opts.client.is_authenticated = True


# ===========================================================================
# Teardown
# ===========================================================================

@th.django_unit_setup()
def cleanup_totp_recovery(opts):
    from mojo.apps.account.models import User
    from mojo.apps.account.models.totp import UserTOTP
    UserTOTP.objects.filter(user_id=opts.user_id).delete()
    User.objects.filter(pk=opts.user_id).delete()