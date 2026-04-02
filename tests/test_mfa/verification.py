"""
Tests for email/phone verification feature — security and correctness.

Security contract this file enforces:
  - Tokens are single-use and kind-scoped
  - Tokens are tied to auth_key — rotation immediately invalidates them
  - Expired tokens are rejected
  - Tampered/garbage tokens are rejected
  - Deactivated users cannot use tokens to log back in
  - Deactivated users cannot request new verification emails
  - Resending a verification email invalidates the previous token immediately
  - No user enumeration on public send/resend endpoints
  - Response body for known and unknown user on send is byte-for-byte identical
  - REQUIRE_VERIFIED_EMAIL gate blocks unverified logins with a structured error
  - Wrong password returns 401, not 403 — gate must never leak account existence
  - Email/invite token endpoints bypass the gate (clicking IS the verification act)
  - REQUIRE_VERIFIED_PHONE gate blocks unverified phone logins
  - REQUIRE_VERIFIED_PHONE gate also blocks password login when phone number is the login identifier (ALLOW_PHONE_LOGIN=True)
  - Verified phone users pass the REQUIRE_VERIFIED_PHONE gate on both SMS OTP and password+phone-identifier paths
  - SMS standalone verify auto-sets is_phone_verified (phone receipt = ownership proof)
  - MFA-step SMS verify does NOT auto-set is_phone_verified
  - Token for user A cannot be used to verify/log in as user B

Email OTP code flow (POST /api/auth/verify/email/send + confirm):
  - method=code stores a 6-digit code in user secrets; method=link (default) still works
  - Omitting method is backward-compatible — sends a link, no code generated
  - Code confirm endpoint requires authentication — identity comes from JWT, never from the code
  - Unauthenticated call to confirm is always rejected
  - Wrong code is rejected without consuming the valid code (brute-force safety)
  - Expired code is rejected
  - Code is single-use — second confirm call is rejected
  - Code confirm does NOT issue a new JWT — existing session continues (unlike link flow)
  - User A's code cannot verify User B — JWT identity is the gate, not the code value
  - Already-verified user: send returns 200 but no code is generated
"""
from testit import helpers as th
from testit.helpers import assert_true, assert_eq
from mojo.helpers import dates, crypto

TEST_USER = "verify_test_user"
TEST_PWORD = "verify##mojo99"
TEST_PHONE = "+15550007788"


def _seed_otp(user, code=None):
    """Seed an OTP code directly into user secrets, bypassing real SMS."""
    if code is None:
        code = crypto.random_string(6, allow_digits=True, allow_chars=False, allow_special=False)
    user.set_secret("sms_otp_code", code)
    user.set_secret("sms_otp_ts", int(dates.utcnow().timestamp()))
    user.save()
    return code


@th.django_unit_setup()
def setup_verification(opts):
    from mojo.apps.account.models import User
    from mojo.decorators.limits import clear_rate_limits
    clear_rate_limits(ip="127.0.0.1")

    # Primary test user — unverified email, active
    user = User.objects.filter(username=TEST_USER).last()
    if user is None:
        user = User(username=TEST_USER, email=f"{TEST_USER}@example.com")
        user.save()
    user.is_active = True
    user.is_email_verified = False
    user.is_phone_verified = False
    user.requires_mfa = False
    user.save_password(TEST_PWORD)
    user.save()
    opts.user_id = user.pk

    # Secondary user — for cross-user token tests
    user_b = User.objects.filter(username="verify_test_user_b").last()
    if user_b is None:
        user_b = User(username="verify_test_user_b", email="verify_test_user_b@example.com")
        user_b.save()
    user_b.is_active = True
    user_b.is_email_verified = False
    user_b.is_phone_verified = False
    user_b.requires_mfa = False
    user_b.save_password(TEST_PWORD)
    user_b.save()
    opts.user_b_id = user_b.pk

    # SMS test user — unverified phone
    sms_user = User.objects.filter(username="verify_sms_user").last()
    if sms_user is None:
        sms_user = User(username="verify_sms_user", email="verify_sms_user@example.com")
        sms_user.save()
    User.objects.exclude(pk=sms_user.pk).filter(phone_number=TEST_PHONE).update(phone_number=None)
    sms_user.is_active = True
    sms_user.phone_number = TEST_PHONE
    sms_user.is_phone_verified = False
    sms_user.requires_mfa = False
    sms_user.save_password(TEST_PWORD)
    sms_user.set_secret("sms_otp_code", None)
    sms_user.set_secret("sms_otp_ts", None)
    sms_user.save()
    opts.sms_user_id = sms_user.pk


# ===========================================================================
# Token unit tests — kind, prefix, single-use, expiry, rotation, tampering
# ===========================================================================

@th.django_unit_test("token: email verify token has ev: prefix")
def test_ev_token_prefix(opts):
    from mojo.apps.account.models import User
    from mojo.apps.account.utils import tokens

    user = User.objects.get(pk=opts.user_id)
    tok = tokens.generate_email_verify_token(user)
    assert_true(tok.startswith("ev:"), f"Expected 'ev:' prefix, got: {tok[:10]}")
    tokens.verify_email_verify_token(tok)


@th.django_unit_test("token: invite token has iv: prefix")
def test_iv_token_prefix(opts):
    from mojo.apps.account.models import User
    from mojo.apps.account.utils import tokens

    user = User.objects.get(pk=opts.user_id)
    tok = tokens.generate_invite_token(user)
    assert_true(tok.startswith("iv:"), f"Expected 'iv:' prefix, got: {tok[:10]}")
    tokens.verify_invite_token(tok)


@th.django_unit_test("token: email verify token is single-use — second verify raises")
def test_ev_token_single_use(opts):
    from mojo.apps.account.models import User
    from mojo.apps.account.utils import tokens
    from mojo import errors as merrors

    user = User.objects.get(pk=opts.user_id)
    tok = tokens.generate_email_verify_token(user)
    tokens.verify_email_verify_token(tok)

    raised = False
    try:
        tokens.verify_email_verify_token(tok)
    except merrors.ValueException:
        raised = True
    assert_true(raised, "Reusing an email verify token must raise ValueException")


@th.django_unit_test("token: invite token is single-use — second verify raises")
def test_iv_token_single_use(opts):
    from mojo.apps.account.models import User
    from mojo.apps.account.utils import tokens
    from mojo import errors as merrors

    user = User.objects.get(pk=opts.user_id)
    tok = tokens.generate_invite_token(user)
    tokens.verify_invite_token(tok)

    raised = False
    try:
        tokens.verify_invite_token(tok)
    except merrors.ValueException:
        raised = True
    assert_true(raised, "Reusing an invite token must raise ValueException")


@th.django_unit_test("token: ev token rejected by verify_invite_token (kind mismatch)")
def test_ev_rejected_by_invite_verifier(opts):
    from mojo.apps.account.models import User
    from mojo.apps.account.utils import tokens
    from mojo import errors as merrors

    user = User.objects.get(pk=opts.user_id)
    ev_tok = tokens.generate_email_verify_token(user)

    raised = False
    try:
        tokens.verify_invite_token(ev_tok)
    except merrors.ValueException:
        raised = True
    assert_true(raised, "verify_invite_token must reject a token with kind 'ev'")

    # Consume so JTI is not left in a poisoned state for later tests
    try:
        tokens.verify_email_verify_token(ev_tok)
    except merrors.ValueException:
        pass


@th.django_unit_test("token: iv token rejected by verify_email_verify_token (kind mismatch)")
def test_iv_rejected_by_ev_verifier(opts):
    from mojo.apps.account.models import User
    from mojo.apps.account.utils import tokens
    from mojo import errors as merrors

    user = User.objects.get(pk=opts.user_id)
    iv_tok = tokens.generate_invite_token(user)

    raised = False
    try:
        tokens.verify_email_verify_token(iv_tok)
    except merrors.ValueException:
        raised = True
    assert_true(raised, "verify_email_verify_token must reject a token with kind 'iv'")

    try:
        tokens.verify_invite_token(iv_tok)
    except merrors.ValueException:
        pass


@th.django_unit_test("token: garbage strings are always rejected")
def test_garbage_token_rejected(opts):
    from mojo.apps.account.utils import tokens
    from mojo import errors as merrors

    for bad in ["", "notavalidtoken", "ev:", "ev:zzzz", "xx:deadbeef", "iv:", "   "]:
        raised = False
        try:
            tokens.verify_email_verify_token(bad)
        except (merrors.ValueException, Exception):
            raised = True
        assert_true(raised, f"Garbage token {bad!r} must be rejected")


@th.django_unit_test("token: expired email verify token is rejected")
def test_ev_token_expired(opts):
    """
    Patch _TTL to -1 so any token is immediately expired.
    TTL of -1 means now_ts - ts > -1 is always True.
    """
    from mojo.apps.account.models import User
    from mojo.apps.account.utils import tokens
    import mojo.apps.account.utils.tokens as tok_module
    from mojo import errors as merrors

    user = User.objects.get(pk=opts.user_id)
    orig_ttl = tok_module._TTL[tok_module.KIND_EMAIL_VERIFY]
    tok_module._TTL[tok_module.KIND_EMAIL_VERIFY] = -1
    try:
        tok = tokens.generate_email_verify_token(user)
        raised = False
        try:
            tokens.verify_email_verify_token(tok)
        except merrors.ValueException:
            raised = True
        assert_true(raised, "Expired email verify token must raise ValueException")
    finally:
        tok_module._TTL[tok_module.KIND_EMAIL_VERIFY] = orig_ttl
        # Clear leftover JTI so it cannot interfere with subsequent tests
        user.set_secret(tok_module._JTI_KEYS[tok_module.KIND_EMAIL_VERIFY], None)
        user.save(update_fields=["mojo_secrets", "modified"])


@th.django_unit_test("token: expired invite token is rejected")
def test_iv_token_expired(opts):
    from mojo.apps.account.models import User
    from mojo.apps.account.utils import tokens
    import mojo.apps.account.utils.tokens as tok_module
    from mojo import errors as merrors

    user = User.objects.get(pk=opts.user_id)
    orig_ttl = tok_module._TTL[tok_module.KIND_INVITE]
    tok_module._TTL[tok_module.KIND_INVITE] = -1
    try:
        tok = tokens.generate_invite_token(user)
        raised = False
        try:
            tokens.verify_invite_token(tok)
        except merrors.ValueException:
            raised = True
        assert_true(raised, "Expired invite token must raise ValueException")
    finally:
        tok_module._TTL[tok_module.KIND_INVITE] = orig_ttl
        user.set_secret(tok_module._JTI_KEYS[tok_module.KIND_INVITE], None)
        user.save(update_fields=["mojo_secrets", "modified"])


@th.django_unit_test("token: auth_key rotation immediately invalidates outstanding tokens")
def test_auth_key_rotation_invalidates_token(opts):
    """
    When auth_key changes (password reset, forced logout, account compromise response)
    any previously issued tokens signed with the old key must be dead instantly.
    """
    import uuid
    from mojo.apps.account.models import User
    from mojo.apps.account.utils import tokens
    import mojo.apps.account.utils.tokens as tok_module
    from mojo import errors as merrors

    user = User.objects.get(pk=opts.user_id)
    tok = tokens.generate_email_verify_token(user)
    old_auth_key = user.auth_key

    # Rotate auth_key — simulates password change / force-logout
    User.objects.filter(pk=user.pk).update(auth_key=uuid.uuid4().hex)

    raised = False
    try:
        tokens.verify_email_verify_token(tok)
    except merrors.ValueException:
        raised = True
    assert_true(raised, "Token issued before auth_key rotation must be invalid after rotation")

    # Restore for remaining tests
    User.objects.filter(pk=user.pk).update(auth_key=old_auth_key)
    user.refresh_from_db()
    user.set_secret(tok_module._JTI_KEYS[tok_module.KIND_EMAIL_VERIFY], None)
    user.save(update_fields=["mojo_secrets", "modified"])


@th.django_unit_test("token: resending verification invalidates the previous token")
def test_resend_invalidates_previous_token(opts):
    """
    Each generate call rotates the stored JTI. An old link sitting in the
    user's inbox must become invalid the moment a new one is issued.
    This prevents multiple live links floating in email threads simultaneously.
    """
    from mojo.apps.account.models import User
    from mojo.apps.account.utils import tokens
    from mojo import errors as merrors

    user = User.objects.get(pk=opts.user_id)
    first_tok = tokens.generate_email_verify_token(user)
    second_tok = tokens.generate_email_verify_token(user)  # simulates "resend"

    raised = False
    try:
        tokens.verify_email_verify_token(first_tok)
    except merrors.ValueException:
        raised = True
    assert_true(raised, "First token must be invalid after a second token is generated for the same user")

    # Consume second token cleanly
    try:
        tokens.verify_email_verify_token(second_tok)
    except merrors.ValueException:
        pass


@th.django_unit_test("token: user A's token cannot be redirected to verify user B")
def test_cross_user_token_cannot_verify_different_account(opts):
    """
    A token embeds the issuing user's uid and is signed with their auth_key.
    When submitted to an endpoint it always resolves to the original user.
    User B's is_email_verified must be untouched after User A's token is used.
    """
    from mojo.apps.account.models import User
    from mojo.apps.account.utils import tokens
    from mojo.decorators.limits import clear_rate_limits

    User.objects.filter(pk=opts.user_id).update(is_email_verified=False, is_active=True)
    User.objects.filter(pk=opts.user_b_id).update(is_email_verified=False, is_active=True)

    user_a = User.objects.get(pk=opts.user_id)
    tok_for_a = tokens.generate_email_verify_token(user_a)
    clear_rate_limits(ip="127.0.0.1")

    resp = opts.client.post("/api/auth/email/verify", {"token": tok_for_a})
    assert_eq(resp.status_code, 200, f"Expected 200 for valid token, got {resp.status_code}")

    user_a.refresh_from_db()
    user_b = User.objects.get(pk=opts.user_b_id)
    assert_true(user_a.is_email_verified, "User A should be verified after their own token is used")
    assert_true(not user_b.is_email_verified, "User B must not be verified by User A's token")

    User.objects.filter(pk=opts.user_id).update(is_email_verified=False)


# ===========================================================================
# REST: POST /api/auth/email/verify/send
# ===========================================================================

@th.django_unit_test("verify/send: known user returns 200")
def test_send_known_user(opts):
    from mojo.apps.account.models import User
    from mojo.decorators.limits import clear_rate_limits
    clear_rate_limits(ip="127.0.0.1")
    User.objects.filter(pk=opts.user_id).update(is_email_verified=False, is_active=True)

    resp = opts.client.post("/api/auth/email/verify/send", {"username": TEST_USER})
    assert_eq(resp.status_code, 200, f"Expected 200, got {resp.status_code}")
    assert_true(resp.response.status is True, "response.status should be True")


@th.django_unit_test("verify/send: unknown user returns 200 — no enumeration")
def test_send_unknown_user_no_enumeration(opts):
    from mojo.decorators.limits import clear_rate_limits
    clear_rate_limits(ip="127.0.0.1")

    resp = opts.client.post("/api/auth/email/verify/send", {"username": "ghost_verify_no_exist_xyz9"})
    assert_eq(resp.status_code, 200, f"Expected 200 for unknown user, got {resp.status_code}")
    assert_true(resp.response.status is True, "response.status should be True for unknown user")


@th.django_unit_test("verify/send: response body for known and unknown user is identical")
def test_send_response_identical_known_vs_unknown(opts):
    """
    If the response body differs between a known and unknown user, an attacker
    can enumerate valid accounts by calling this endpoint at scale.
    """
    from mojo.apps.account.models import User
    from mojo.decorators.limits import clear_rate_limits

    User.objects.filter(pk=opts.user_id).update(is_email_verified=False, is_active=True)
    clear_rate_limits(ip="127.0.0.1")
    resp_known = opts.client.post("/api/auth/email/verify/send", {"username": TEST_USER})
    clear_rate_limits(ip="127.0.0.1")
    resp_unknown = opts.client.post("/api/auth/email/verify/send", {"username": "ghost_enum_check_xyz99"})

    msg_known = getattr(resp_known.response, "message", str(resp_known.response))
    msg_unknown = getattr(resp_unknown.response, "message", str(resp_unknown.response))
    assert_eq(msg_known, msg_unknown,
              f"Response body must be identical for known and unknown users.\n"
              f"Known:   {msg_known!r}\nUnknown: {msg_unknown!r}")


@th.django_unit_test("verify/send: already-verified user returns 200, not an error")
def test_send_already_verified_returns_200(opts):
    from mojo.apps.account.models import User
    from mojo.decorators.limits import clear_rate_limits
    clear_rate_limits(ip="127.0.0.1")
    User.objects.filter(pk=opts.user_id).update(is_email_verified=True, is_active=True)

    resp = opts.client.post("/api/auth/email/verify/send", {"username": TEST_USER})
    assert_eq(resp.status_code, 200, f"Expected 200, got {resp.status_code}")
    assert_true(resp.response.status is True, "Should return status True")
    raw = str(resp.response).lower()
    assert_true("already" in raw or "verified" in raw,
                f"Response should indicate email is already verified, got: {raw!r}")

    User.objects.filter(pk=opts.user_id).update(is_email_verified=False)


@th.django_unit_test("verify/send: inactive user silently ignored — no token generated")
def test_send_inactive_user_no_token_generated(opts):
    """
    An inactive (banned/suspended) user must not receive a fresh verification
    link. The response must still return 200 to avoid enumeration, but the
    stored JTI must not change — confirming no token was issued.
    """
    from mojo.apps.account.models import User
    import mojo.apps.account.utils.tokens as tok_module
    from mojo.decorators.limits import clear_rate_limits
    clear_rate_limits(ip="127.0.0.1")

    User.objects.filter(pk=opts.user_id).update(is_active=False, is_email_verified=False)
    user = User.objects.get(pk=opts.user_id)
    jti_before = user.get_secret(tok_module._JTI_KEYS[tok_module.KIND_EMAIL_VERIFY])

    resp = opts.client.post("/api/auth/email/verify/send", {"username": TEST_USER})
    assert_eq(resp.status_code, 200,
              f"Expected 200 even for inactive user (no enumeration), got {resp.status_code}")

    user.refresh_from_db()
    jti_after = user.get_secret(tok_module._JTI_KEYS[tok_module.KIND_EMAIL_VERIFY])
    assert_eq(jti_before, jti_after,
              "No new token JTI must be stored for an inactive user — no token was issued")

    User.objects.filter(pk=opts.user_id).update(is_active=True)


# ===========================================================================
# REST: POST /api/auth/email/verify  (token redemption)
# ===========================================================================

@th.django_unit_test("email/verify: valid token marks is_email_verified=True")
def test_ev_complete_marks_verified(opts):
    from mojo.apps.account.models import User
    from mojo.apps.account.utils import tokens
    from mojo.decorators.limits import clear_rate_limits
    clear_rate_limits(ip="127.0.0.1")

    User.objects.filter(pk=opts.user_id).update(is_email_verified=False, is_active=True)
    user = User.objects.get(pk=opts.user_id)
    tok = tokens.generate_email_verify_token(user)

    resp = opts.client.post("/api/auth/email/verify", {"token": tok})
    assert_eq(resp.status_code, 200, f"Expected 200, got {resp.status_code}: {resp.response}")

    user.refresh_from_db()
    assert_true(user.is_email_verified, "is_email_verified must be True after successful verify")


@th.django_unit_test("email/verify: is_email_verified is persisted to DB before JWT is returned")
def test_ev_complete_field_saved_before_login(opts):
    """
    If the save happened AFTER jwt_login, a REQUIRE_VERIFIED_EMAIL gate would
    block the very endpoint that is supposed to complete verification — a
    deadlock. The DB field must be True by the time the 200 is issued.
    """
    from mojo.apps.account.models import User
    from mojo.apps.account.utils import tokens
    from mojo.decorators.limits import clear_rate_limits
    clear_rate_limits(ip="127.0.0.1")

    User.objects.filter(pk=opts.user_id).update(is_email_verified=False, is_active=True)
    user = User.objects.get(pk=opts.user_id)
    tok = tokens.generate_email_verify_token(user)

    resp = opts.client.post("/api/auth/email/verify", {"token": tok})
    assert_eq(resp.status_code, 200, f"Expected 200, got {resp.status_code}")

    # Re-fetch from DB — not from the in-memory object
    fresh = User.objects.get(pk=opts.user_id)
    assert_true(fresh.is_email_verified,
                "is_email_verified must be committed to the DB before the response is returned")


@th.django_unit_test("email/verify: valid token returns access_token and refresh_token")
def test_ev_complete_issues_jwt(opts):
    from mojo.apps.account.models import User
    from mojo.apps.account.utils import tokens
    from mojo.decorators.limits import clear_rate_limits
    clear_rate_limits(ip="127.0.0.1")

    User.objects.filter(pk=opts.user_id).update(is_email_verified=False, is_active=True)
    user = User.objects.get(pk=opts.user_id)
    tok = tokens.generate_email_verify_token(user)

    resp = opts.client.post("/api/auth/email/verify", {"token": tok})
    assert_eq(resp.status_code, 200, f"Expected 200, got {resp.status_code}")
    data = resp.response.data
    assert_true(bool(getattr(data, "access_token", None)), "Must include access_token")
    assert_true(bool(getattr(data, "refresh_token", None)), "Must include refresh_token")


@th.django_unit_test("email/verify: invalid/garbage token returns 400 or 403")
def test_ev_complete_invalid_token(opts):
    from mojo.decorators.limits import clear_rate_limits
    clear_rate_limits(ip="127.0.0.1")

    resp = opts.client.post("/api/auth/email/verify", {"token": "ev:notavalidtoken"})
    assert resp.status_code in [400, 403], \
        f"Expected 400/403 for invalid token, got {resp.status_code}"


@th.django_unit_test("email/verify: token reuse after first success is rejected")
def test_ev_complete_token_reuse_rejected(opts):
    from mojo.apps.account.models import User
    from mojo.apps.account.utils import tokens
    from mojo.decorators.limits import clear_rate_limits
    clear_rate_limits(ip="127.0.0.1")

    User.objects.filter(pk=opts.user_id).update(is_email_verified=False, is_active=True)
    user = User.objects.get(pk=opts.user_id)
    tok = tokens.generate_email_verify_token(user)

    resp1 = opts.client.post("/api/auth/email/verify", {"token": tok})
    assert_eq(resp1.status_code, 200, f"First use should succeed, got {resp1.status_code}")

    # Reset verified state so the gate is not what blocks the second attempt
    User.objects.filter(pk=opts.user_id).update(is_email_verified=False)
    clear_rate_limits(ip="127.0.0.1")
    resp2 = opts.client.post("/api/auth/email/verify", {"token": tok})
    assert resp2.status_code in [400, 403], \
        f"Second use of same ev token must be rejected, got {resp2.status_code}"


@th.django_unit_test("email/verify: inactive user cannot log in via verify link")
def test_ev_complete_inactive_user_blocked(opts):
    """
    An admin may deactivate an account. The user must not be able to use a
    verification link (valid or not) to bypass that and log back in.
    Token is generated while active, then user is deactivated before click.
    """
    from mojo.apps.account.models import User
    from mojo.apps.account.utils import tokens
    from mojo.decorators.limits import clear_rate_limits
    clear_rate_limits(ip="127.0.0.1")

    User.objects.filter(pk=opts.user_id).update(is_active=True, is_email_verified=False)
    user = User.objects.get(pk=opts.user_id)
    tok = tokens.generate_email_verify_token(user)

    # Deactivate after token is issued — simulates account suspension
    User.objects.filter(pk=opts.user_id).update(is_active=False)

    resp = opts.client.post("/api/auth/email/verify", {"token": tok})
    assert resp.status_code in [400, 403], \
        f"Inactive user must not log in via email verify link, got {resp.status_code}"

    User.objects.filter(pk=opts.user_id).update(is_active=True)


# ===========================================================================
# REST: POST /api/auth/invite/accept
# ===========================================================================

@th.django_unit_test("invite/accept: valid token marks is_email_verified=True")
def test_invite_accept_marks_verified(opts):
    from mojo.apps.account.models import User
    from mojo.apps.account.utils import tokens
    from mojo.decorators.limits import clear_rate_limits
    clear_rate_limits(ip="127.0.0.1")

    User.objects.filter(pk=opts.user_id).update(is_email_verified=False, is_active=True)
    user = User.objects.get(pk=opts.user_id)
    tok = tokens.generate_invite_token(user)

    resp = opts.client.post("/api/auth/invite/accept", {"token": tok})
    assert_eq(resp.status_code, 200, f"Expected 200, got {resp.status_code}: {resp.response}")

    user.refresh_from_db()
    assert_true(user.is_email_verified, "is_email_verified must be True after invite accept")


@th.django_unit_test("invite/accept: valid token returns access_token and refresh_token")
def test_invite_accept_issues_jwt(opts):
    from mojo.apps.account.models import User
    from mojo.apps.account.utils import tokens
    from mojo.decorators.limits import clear_rate_limits
    clear_rate_limits(ip="127.0.0.1")

    User.objects.filter(pk=opts.user_id).update(is_email_verified=False, is_active=True)
    user = User.objects.get(pk=opts.user_id)
    tok = tokens.generate_invite_token(user)

    resp = opts.client.post("/api/auth/invite/accept", {"token": tok})
    assert_eq(resp.status_code, 200, f"Expected 200, got {resp.status_code}")
    data = resp.response.data
    assert_true(bool(getattr(data, "access_token", None)), "Must include access_token")
    assert_true(bool(getattr(data, "refresh_token", None)), "Must include refresh_token")


@th.django_unit_test("invite/accept: invalid/garbage token returns 400 or 403")
def test_invite_accept_invalid_token(opts):
    from mojo.decorators.limits import clear_rate_limits
    clear_rate_limits(ip="127.0.0.1")

    resp = opts.client.post("/api/auth/invite/accept", {"token": "iv:notavalidtoken"})
    assert resp.status_code in [400, 403], \
        f"Expected 400/403 for invalid token, got {resp.status_code}"


@th.django_unit_test("invite/accept: token reuse after first success is rejected")
def test_invite_accept_token_reuse_rejected(opts):
    from mojo.apps.account.models import User
    from mojo.apps.account.utils import tokens
    from mojo.decorators.limits import clear_rate_limits
    clear_rate_limits(ip="127.0.0.1")

    User.objects.filter(pk=opts.user_id).update(is_email_verified=False, is_active=True)
    user = User.objects.get(pk=opts.user_id)
    tok = tokens.generate_invite_token(user)

    resp1 = opts.client.post("/api/auth/invite/accept", {"token": tok})
    assert_eq(resp1.status_code, 200, f"First use should succeed, got {resp1.status_code}")

    User.objects.filter(pk=opts.user_id).update(is_email_verified=False)
    clear_rate_limits(ip="127.0.0.1")
    resp2 = opts.client.post("/api/auth/invite/accept", {"token": tok})
    assert resp2.status_code in [400, 403], \
        f"Second use of invite token must be rejected, got {resp2.status_code}"


@th.django_unit_test("invite/accept: ev token is rejected at this endpoint (kind mismatch)")
def test_invite_accept_rejects_ev_token(opts):
    from mojo.apps.account.models import User
    from mojo.apps.account.utils import tokens
    from mojo.decorators.limits import clear_rate_limits
    clear_rate_limits(ip="127.0.0.1")

    User.objects.filter(pk=opts.user_id).update(is_active=True)
    user = User.objects.get(pk=opts.user_id)
    ev_tok = tokens.generate_email_verify_token(user)

    resp = opts.client.post("/api/auth/invite/accept", {"token": ev_tok})
    assert resp.status_code in [400, 403], \
        f"invite/accept must reject a token with kind 'ev', got {resp.status_code}"

    try:
        tokens.verify_email_verify_token(ev_tok)
    except Exception:
        pass


@th.django_unit_test("email/verify: iv token is rejected at this endpoint (kind mismatch)")
def test_ev_endpoint_rejects_iv_token(opts):
    from mojo.apps.account.models import User
    from mojo.apps.account.utils import tokens
    from mojo.decorators.limits import clear_rate_limits
    clear_rate_limits(ip="127.0.0.1")

    User.objects.filter(pk=opts.user_id).update(is_active=True)
    user = User.objects.get(pk=opts.user_id)
    iv_tok = tokens.generate_invite_token(user)

    resp = opts.client.post("/api/auth/email/verify", {"token": iv_tok})
    assert resp.status_code in [400, 403], \
        f"email/verify must reject a token with kind 'iv', got {resp.status_code}"

    try:
        tokens.verify_invite_token(iv_tok)
    except Exception:
        pass


@th.django_unit_test("invite/accept: inactive user cannot log in via invite link")
def test_invite_accept_inactive_user_blocked(opts):
    from mojo.apps.account.models import User
    from mojo.apps.account.utils import tokens
    from mojo.decorators.limits import clear_rate_limits
    clear_rate_limits(ip="127.0.0.1")

    User.objects.filter(pk=opts.user_id).update(is_active=True, is_email_verified=False)
    user = User.objects.get(pk=opts.user_id)
    tok = tokens.generate_invite_token(user)

    # Deactivate before redemption
    User.objects.filter(pk=opts.user_id).update(is_active=False)

    resp = opts.client.post("/api/auth/invite/accept", {"token": tok})
    assert resp.status_code in [400, 403], \
        f"Inactive user must not log in via invite link, got {resp.status_code}"

    User.objects.filter(pk=opts.user_id).update(is_active=True)


# ===========================================================================
# Verification gate — REQUIRE_VERIFIED_EMAIL
# ===========================================================================

@th.django_unit_test("email gate: off by default — unverified user can log in")
def test_email_gate_off_by_default(opts):
    from testit.helpers import TestitSkip
    from mojo.helpers.settings import settings
    from mojo.apps.account.models import User
    from mojo.decorators.limits import clear_rate_limits

    if settings.get("REQUIRE_VERIFIED_EMAIL", False):
        raise TestitSkip("requires REQUIRE_VERIFIED_EMAIL=False (default) — gate is currently ON in server settings")

    # Confirm default behaviour without patching any setting
    User.objects.filter(pk=opts.user_id).update(is_email_verified=False, is_active=True)
    clear_rate_limits(ip="127.0.0.1")

    resp = opts.client.post("/api/login", {"username": TEST_USER, "password": TEST_PWORD})
    assert_eq(resp.status_code, 200,
              f"Unverified user must be able to log in when gate is off, got {resp.status_code}")
    assert_true(bool(getattr(resp.response.data, "access_token", None)),
                "Must return access_token when gate is off")


@th.django_unit_test("email gate: REQUIRE_VERIFIED_EMAIL=True blocks unverified email-identifier login")
def test_email_gate_blocks_unverified(opts):
    from testit.helpers import TestitSkip
    from mojo.helpers.settings import settings
    from mojo.apps.account.models import User
    from mojo.decorators.limits import clear_rate_limits

    if not settings.get("REQUIRE_VERIFIED_EMAIL", False):
        raise TestitSkip("requires REQUIRE_VERIFIED_EMAIL=True in server settings — set it and restart the server to run this test")

    User.objects.filter(pk=opts.user_id).update(is_email_verified=False, is_active=True)
    clear_rate_limits(ip="127.0.0.1")

    # Gate only fires when the login identifier IS an email address
    test_email = f"{TEST_USER}@example.com"
    resp = opts.client.post("/api/login", {"username": test_email, "password": TEST_PWORD})
    assert_eq(resp.status_code, 403,
              f"Expected 403 for unverified email-identifier login with gate on, got {resp.status_code}")
    raw = str(resp.response)
    assert_true("email_not_verified" in raw,
                f"Response must contain 'email_not_verified' error key, got: {raw}")


@th.django_unit_test("email gate: REQUIRE_VERIFIED_EMAIL=True does NOT block username login")
def test_email_gate_does_not_block_username_login(opts):
    from testit.helpers import TestitSkip
    from mojo.helpers.settings import settings
    from mojo.apps.account.models import User
    from mojo.decorators.limits import clear_rate_limits

    if not settings.get("REQUIRE_VERIFIED_EMAIL", False):
        raise TestitSkip("requires REQUIRE_VERIFIED_EMAIL=True in server settings — set it and restart the server to run this test")

    User.objects.filter(pk=opts.user_id).update(is_email_verified=False, is_active=True)
    clear_rate_limits(ip="127.0.0.1")

    # Username login must always work regardless of email verification gate
    resp = opts.client.post("/api/login", {"username": TEST_USER, "password": TEST_PWORD})
    assert_eq(resp.status_code, 200,
              f"Username login must not be blocked by email gate, got {resp.status_code}")
    assert_true(bool(getattr(resp.response.data, "access_token", None)),
                "Must return access_token for username login regardless of email gate")


@th.django_unit_test("email gate: REQUIRE_VERIFIED_EMAIL=True allows verified email-identifier login")
def test_email_gate_allows_verified(opts):
    from testit.helpers import TestitSkip
    from mojo.helpers.settings import settings
    from mojo.apps.account.models import User
    from mojo.decorators.limits import clear_rate_limits

    if not settings.get("REQUIRE_VERIFIED_EMAIL", False):
        raise TestitSkip("requires REQUIRE_VERIFIED_EMAIL=True in server settings — set it and restart the server to run this test")

    User.objects.filter(pk=opts.user_id).update(is_email_verified=True, is_active=True)
    clear_rate_limits(ip="127.0.0.1")

    # Login with the email address as the identifier — gate should pass for verified user
    test_email = f"{TEST_USER}@example.com"
    resp = opts.client.post("/api/login", {"username": test_email, "password": TEST_PWORD})
    assert_eq(resp.status_code, 200,
              f"Verified user must pass the email gate, got {resp.status_code}")
    assert_true(bool(getattr(resp.response.data, "access_token", None)),
                "Must return access_token for verified email-identifier login")
    User.objects.filter(pk=opts.user_id).update(is_email_verified=False)


@th.django_unit_test("email gate: wrong password returns 401 not 403 — gate must not leak account existence")
def test_email_gate_wrong_password_returns_401(opts):
    """
    If the gate returned 403 'email_not_verified' before checking the password,
    an attacker could confirm an account exists by noticing the 403 vs 401
    difference. Password must always be validated first; gate fires after.
    """
    from testit.helpers import TestitSkip
    from mojo.helpers.settings import settings
    from mojo.apps.account.models import User
    from mojo.decorators.limits import clear_rate_limits

    if not settings.get("REQUIRE_VERIFIED_EMAIL", False):
        raise TestitSkip("requires REQUIRE_VERIFIED_EMAIL=True in server settings — set it and restart the server to run this test")

    User.objects.filter(pk=opts.user_id).update(is_email_verified=False, is_active=True)
    clear_rate_limits(ip="127.0.0.1")

    # Use email as identifier so the gate would fire on correct password —
    # wrong password must still return 401, not 403, to avoid account enumeration
    test_email = f"{TEST_USER}@example.com"
    resp = opts.client.post("/api/login", {"username": test_email, "password": "wrongpassword!"})
    assert_eq(resp.status_code, 401,
              f"Wrong password must return 401 regardless of gate setting, got {resp.status_code}")
    raw = str(resp.response)
    assert_true("email_not_verified" not in raw,
                "email_not_verified must not appear in a wrong-password response")


@th.django_unit_test("email gate: email/verify endpoint bypasses gate — clicking link must always work")
def test_email_gate_ev_endpoint_bypasses_gate(opts):
    """
    POST /api/auth/email/verify IS the verification act. If the gate blocked
    this endpoint, the user would be permanently locked out — they can't verify
    without clicking the link, and they can't click the link without being verified.
    """
    from testit.helpers import TestitSkip
    from mojo.helpers.settings import settings
    from mojo.apps.account.models import User
    from mojo.apps.account.utils import tokens
    from mojo.decorators.limits import clear_rate_limits

    if not settings.get("REQUIRE_VERIFIED_EMAIL", False):
        raise TestitSkip("requires REQUIRE_VERIFIED_EMAIL=True in server settings — set it and restart the server to run this test")

    User.objects.filter(pk=opts.user_id).update(is_email_verified=False, is_active=True)
    user = User.objects.get(pk=opts.user_id)
    tok = tokens.generate_email_verify_token(user)
    clear_rate_limits(ip="127.0.0.1")

    resp = opts.client.post("/api/auth/email/verify", {"token": tok})
    assert_eq(resp.status_code, 200,
              f"email/verify must succeed with gate on, got {resp.status_code}")
    assert_true(bool(getattr(resp.response.data, "access_token", None)),
                "email/verify must return access_token regardless of gate state")
    User.objects.filter(pk=opts.user_id).update(is_email_verified=False)


@th.django_unit_test("email gate: invite/accept endpoint bypasses gate — invite must always work")
def test_email_gate_invite_endpoint_bypasses_gate(opts):
    from testit.helpers import TestitSkip
    from mojo.helpers.settings import settings
    from mojo.apps.account.models import User
    from mojo.apps.account.utils import tokens
    from mojo.decorators.limits import clear_rate_limits

    if not settings.get("REQUIRE_VERIFIED_EMAIL", False):
        raise TestitSkip("requires REQUIRE_VERIFIED_EMAIL=True in server settings — set it and restart the server to run this test")

    User.objects.filter(pk=opts.user_id).update(is_email_verified=False, is_active=True)
    user = User.objects.get(pk=opts.user_id)
    tok = tokens.generate_invite_token(user)
    clear_rate_limits(ip="127.0.0.1")

    resp = opts.client.post("/api/auth/invite/accept", {"token": tok})
    assert_eq(resp.status_code, 200,
              f"invite/accept must succeed with gate on, got {resp.status_code}")
    assert_true(bool(getattr(resp.response.data, "access_token", None)),
                "invite/accept must return access_token regardless of gate state")
    User.objects.filter(pk=opts.user_id).update(is_email_verified=False)


# ===========================================================================
# Verification gate — REQUIRE_VERIFIED_PHONE
# ===========================================================================

@th.django_unit_test("phone gate: off by default — unverified phone user can log in via password")
def test_phone_gate_off_by_default(opts):
    from testit.helpers import TestitSkip
    from mojo.helpers.settings import settings
    from mojo.apps.account.models import User
    from mojo.decorators.limits import clear_rate_limits

    if settings.get("REQUIRE_VERIFIED_PHONE", False):
        raise TestitSkip("requires REQUIRE_VERIFIED_PHONE=False (default) — gate is currently ON in server settings")

    # Confirm default behaviour without patching any setting
    User.objects.filter(pk=opts.sms_user_id).update(is_phone_verified=False, is_active=True)
    clear_rate_limits(ip="127.0.0.1")

    resp = opts.client.post("/api/login", {"username": "verify_sms_user", "password": TEST_PWORD})
    assert_eq(resp.status_code, 200,
              f"Unverified phone user must log in when phone gate is off, got {resp.status_code}")


@th.django_unit_test("phone gate: REQUIRE_VERIFIED_PHONE=True blocks unverified standalone SMS verify")
def test_phone_gate_blocks_unverified_sms_login(opts):
    """
    With the phone gate enabled, a user with is_phone_verified=False must not
    be able to complete a standalone SMS OTP login. The gate must be checked
    before auto-verification occurs — receiving a code does not bypass the gate.
    """
    from testit.helpers import TestitSkip
    from mojo.helpers.settings import settings
    from mojo.apps.account.models import User
    from mojo.decorators.limits import clear_rate_limits

    if not settings.get("REQUIRE_VERIFIED_PHONE", False):
        raise TestitSkip("requires REQUIRE_VERIFIED_PHONE=True in server settings — set it and restart the server to run this test")

    User.objects.filter(pk=opts.sms_user_id).update(is_phone_verified=False, is_active=True)
    sms_user = User.objects.get(pk=opts.sms_user_id)
    code = _seed_otp(sms_user)
    clear_rate_limits(ip="127.0.0.1")

    resp = opts.client.post("/api/auth/sms/verify", {
        "username": "verify_sms_user",
        "code": code
    })
    assert_eq(resp.status_code, 403,
              f"Expected 403 for unverified phone with gate on, got {resp.status_code}")
    raw = str(resp.response)
    assert_true("phone_not_verified" in raw,
                f"Response must contain 'phone_not_verified', got: {raw}")


@th.django_unit_test("phone gate: REQUIRE_VERIFIED_PHONE=True allows verified phone SMS login")
def test_phone_gate_allows_verified_sms_login(opts):
    from testit.helpers import TestitSkip
    from mojo.helpers.settings import settings
    from mojo.apps.account.models import User
    from mojo.decorators.limits import clear_rate_limits

    if not settings.get("REQUIRE_VERIFIED_PHONE", False):
        raise TestitSkip("requires REQUIRE_VERIFIED_PHONE=True in server settings — set it and restart the server to run this test")

    User.objects.filter(pk=opts.sms_user_id).update(is_phone_verified=True, is_active=True)
    sms_user = User.objects.get(pk=opts.sms_user_id)
    code = _seed_otp(sms_user)
    clear_rate_limits(ip="127.0.0.1")

    resp = opts.client.post("/api/auth/sms/verify", {
        "username": "verify_sms_user",
        "code": code
    })
    assert_eq(resp.status_code, 200,
              f"Verified phone user must pass phone gate, got {resp.status_code}")
    assert_true(bool(getattr(resp.response.data, "access_token", None)),
                "Must return access_token for verified phone user")
    User.objects.filter(pk=opts.sms_user_id).update(is_phone_verified=False)


# ===========================================================================
# Verification gate — REQUIRE_VERIFIED_PHONE on password login (ALLOW_PHONE_LOGIN)
# ===========================================================================

@th.django_unit_test("phone gate: password login via phone identifier — off by default")
def test_phone_gate_password_login_phone_identifier_off_by_default(opts):
    """
    Without REQUIRE_VERIFIED_PHONE, an unverified user must be able to log in
    using their phone number as the username identifier (ALLOW_PHONE_LOGIN path).
    """
    from testit.helpers import TestitSkip
    from mojo.helpers.settings import settings
    from mojo.apps.account.models import User
    from mojo.decorators.limits import clear_rate_limits

    if not settings.get("ALLOW_PHONE_LOGIN", False):
        raise TestitSkip("requires ALLOW_PHONE_LOGIN=True in server settings — set it and restart the server to run this test")
    if settings.get("REQUIRE_VERIFIED_PHONE", False):
        raise TestitSkip("requires REQUIRE_VERIFIED_PHONE=False (default) — gate is currently ON in server settings")

    User.objects.filter(pk=opts.sms_user_id).update(is_phone_verified=False, is_active=True)
    clear_rate_limits(ip="127.0.0.1")

    resp = opts.client.post("/api/login", {"username": TEST_PHONE, "password": TEST_PWORD})
    assert_eq(resp.status_code, 200,
              f"Unverified phone user must log in via phone identifier when gate is off, got {resp.status_code}")


@th.django_unit_test("phone gate: REQUIRE_VERIFIED_PHONE=True blocks unverified password login via phone identifier")
def test_phone_gate_blocks_unverified_password_login_phone_identifier(opts):
    """
    With REQUIRE_VERIFIED_PHONE=True and ALLOW_PHONE_LOGIN=True, a user whose
    phone is not verified must be blocked when they supply their phone number
    as the login identifier. source="phone_number" is returned by
    lookup_from_request_with_source and flows into _check_verification_gate.
    """
    from testit.helpers import TestitSkip
    from mojo.helpers.settings import settings
    from mojo.apps.account.models import User
    from mojo.decorators.limits import clear_rate_limits

    if not settings.get("REQUIRE_VERIFIED_PHONE", False):
        raise TestitSkip("requires REQUIRE_VERIFIED_PHONE=True in server settings — set it and restart the server to run this test")
    if not settings.get("ALLOW_PHONE_LOGIN", False):
        raise TestitSkip("requires ALLOW_PHONE_LOGIN=True in server settings — set it and restart the server to run this test")

    User.objects.filter(pk=opts.sms_user_id).update(is_phone_verified=False, is_active=True)
    clear_rate_limits(ip="127.0.0.1")

    resp = opts.client.post("/api/login", {"username": TEST_PHONE, "password": TEST_PWORD})
    assert_eq(resp.status_code, 403,
              f"Expected 403 for unverified phone identifier login with gate on, got {resp.status_code}")
    raw = str(resp.response)
    assert_true("phone_not_verified" in raw,
                f"Response must contain 'phone_not_verified', got: {raw}")


@th.django_unit_test("phone gate: REQUIRE_VERIFIED_PHONE=True allows verified password login via phone identifier")
def test_phone_gate_allows_verified_password_login_phone_identifier(opts):
    """
    With REQUIRE_VERIFIED_PHONE=True and ALLOW_PHONE_LOGIN=True, a user with
    is_phone_verified=True must be able to log in using their phone number as
    the username identifier without hitting the gate.
    """
    from testit.helpers import TestitSkip
    from mojo.helpers.settings import settings
    from mojo.apps.account.models import User
    from mojo.decorators.limits import clear_rate_limits

    if not settings.get("REQUIRE_VERIFIED_PHONE", False):
        raise TestitSkip("requires REQUIRE_VERIFIED_PHONE=True in server settings — set it and restart the server to run this test")
    if not settings.get("ALLOW_PHONE_LOGIN", False):
        raise TestitSkip("requires ALLOW_PHONE_LOGIN=True in server settings — set it and restart the server to run this test")

    User.objects.filter(pk=opts.sms_user_id).update(is_phone_verified=True, is_active=True)
    clear_rate_limits(ip="127.0.0.1")

    resp = opts.client.post("/api/login", {"username": TEST_PHONE, "password": TEST_PWORD})
    assert_eq(resp.status_code, 200,
              f"Verified phone user must pass gate on password login via phone identifier, got {resp.status_code}")
    assert_true(bool(getattr(resp.response.data, "access_token", None)),
                "Must return access_token for verified phone user")
    User.objects.filter(pk=opts.sms_user_id).update(is_phone_verified=False)


# ===========================================================================
# SMS standalone verify — auto phone verification
# ===========================================================================

@th.django_unit_test("sms auto-verify: standalone verify sets is_phone_verified=True")
def test_sms_standalone_verify_sets_phone_verified(opts):
    from testit.helpers import TestitSkip
    from mojo.helpers.settings import settings
    from mojo.apps.account.models import User
    from mojo.decorators.limits import clear_rate_limits

    if settings.get("REQUIRE_VERIFIED_PHONE", False):
        raise TestitSkip("requires REQUIRE_VERIFIED_PHONE=False — gate fires before auto-verify can run when the phone gate is ON")

    User.objects.filter(pk=opts.sms_user_id).update(is_phone_verified=False, is_active=True)
    sms_user = User.objects.get(pk=opts.sms_user_id)
    code = _seed_otp(sms_user)
    clear_rate_limits(ip="127.0.0.1")

    resp = opts.client.post("/api/auth/sms/verify", {
        "username": "verify_sms_user",
        "code": code
    })
    assert_eq(resp.status_code, 200,
              f"Expected 200 for standalone SMS verify, got {resp.status_code}: {resp.response}")
    assert_true(bool(getattr(resp.response.data, "access_token", None)),
                "Must return access_token after standalone SMS verify")

    sms_user.refresh_from_db()
    assert_true(sms_user.is_phone_verified,
                "is_phone_verified must be True after successful standalone SMS verify")


@th.django_unit_test("sms auto-verify: MFA-step verify does NOT set is_phone_verified")
def test_sms_mfa_step_does_not_auto_verify_phone(opts):
    """
    The MFA path (mfa_token + code) is a second-factor check after a successful
    password login. It must NOT auto-set is_phone_verified. That would allow
    any user with a valid password and phone to self-promote their verification
    status by completing their own MFA challenge — that should be a deliberate
    admin or user action, not a side effect of normal 2FA login.
    """
    from mojo.apps.account.models import User
    from mojo.apps.account.services import mfa as mfa_service
    from mojo.decorators.limits import clear_rate_limits

    User.objects.filter(pk=opts.sms_user_id).update(
        is_phone_verified=False,
        is_active=True,
        requires_mfa=True
    )
    sms_user = User.objects.get(pk=opts.sms_user_id)
    code = _seed_otp(sms_user)
    mfa_token = mfa_service.create_mfa_token(sms_user, ["sms"])
    clear_rate_limits(ip="127.0.0.1")

    resp = opts.client.post("/api/auth/sms/verify", {
        "mfa_token": mfa_token,
        "code": code
    })
    assert_eq(resp.status_code, 200,
              f"MFA verify should succeed, got {resp.status_code}: {resp.response}")

    sms_user.refresh_from_db()
    assert_true(not sms_user.is_phone_verified,
                "MFA-step SMS verify must not auto-set is_phone_verified")

    User.objects.filter(pk=opts.sms_user_id).update(requires_mfa=False)


# ===========================================================================
# REST write-protection: is_email_verified and is_phone_verified
#
# These fields must only be writable by superusers. No other actor —
# including the account owner — may set them via the REST API directly.
# The only legitimate non-superuser paths are the token endpoints and
# the SMS standalone verify flow tested above.
# ===========================================================================

def _setup_write_protection_users(opts):
    """Create actors needed for write-protection tests."""
    from mojo.apps.account.models import User

    # Target user — owns their own account
    target = User.objects.filter(username="verify_wp_target").last()
    if target is None:
        target = User(username="verify_wp_target", email="verify_wp_target@example.com")
        target.save()
    target.is_active = True
    target.is_email_verified = True
    target.is_phone_verified = False
    target.is_superuser = False
    target.requires_mfa = False
    target.save_password("verify##wp99")
    target.save()
    opts.wp_target_id = target.pk

    # Admin with manage_users permission but NOT superuser
    manager = User.objects.filter(username="verify_wp_manager").last()
    if manager is None:
        manager = User(username="verify_wp_manager", email="verify_wp_manager@example.com")
        manager.save()
    manager.is_active = True
    manager.is_superuser = False
    manager.is_email_verified = True
    manager.add_permission(["manage_users"])
    manager.save_password("verify##wp99")
    manager.save()
    opts.wp_manager_id = manager.pk

    # Superuser
    superuser = User.objects.filter(username="verify_wp_super").last()
    if superuser is None:
        superuser = User(username="verify_wp_super", email="verify_wp_super@example.com")
        superuser.save()
    superuser.is_active = True
    superuser.is_superuser = True
    superuser.is_staff = True
    superuser.is_email_verified = True
    superuser.save_password("verify##wp99")
    superuser.save()
    opts.wp_super_id = superuser.pk


@th.django_unit_setup()
def setup_write_protection(opts):
    from mojo.decorators.limits import clear_rate_limits
    clear_rate_limits(ip="127.0.0.1")
    _setup_write_protection_users(opts)


@th.django_unit_test("write-protect: owner cannot set is_email_verified=True on own account")
def test_owner_cannot_set_is_email_verified(opts):
    from mojo.apps.account.models import User
    from mojo.decorators.limits import clear_rate_limits
    clear_rate_limits(ip="127.0.0.1")

    opts.client.login("verify_wp_target", "verify##wp99")
    assert opts.client.is_authenticated, "login failed"
    User.objects.filter(pk=opts.wp_target_id).update(is_email_verified=False)

    resp = opts.client.post(f"/api/user/{opts.wp_target_id}", {"is_email_verified": True})
    assert resp.status_code in [400, 403], \
        f"Owner must not set is_email_verified, got {resp.status_code}"

    target = User.objects.get(pk=opts.wp_target_id)
    assert_true(not target.is_email_verified,
                "is_email_verified must remain False after rejected owner write")


@th.django_unit_test("write-protect: owner cannot set is_phone_verified=True on own account")
def test_owner_cannot_set_is_phone_verified(opts):
    from mojo.apps.account.models import User
    from mojo.decorators.limits import clear_rate_limits
    clear_rate_limits(ip="127.0.0.1")

    User.objects.filter(pk=opts.wp_target_id).update(is_phone_verified=False, is_email_verified=True)
    opts.client.login("verify_wp_target", "verify##wp99")
    assert opts.client.is_authenticated, "login failed"

    resp = opts.client.post(f"/api/user/{opts.wp_target_id}", {"is_phone_verified": True})
    assert resp.status_code in [400, 403], \
        f"Owner must not set is_phone_verified, got {resp.status_code}"

    target = User.objects.get(pk=opts.wp_target_id)
    assert_true(not target.is_phone_verified,
                "is_phone_verified must remain False after rejected owner write")


@th.django_unit_test("write-protect: manage_users admin cannot set is_email_verified")
def test_manager_cannot_set_is_email_verified(opts):
    from mojo.apps.account.models import User
    from mojo.decorators.limits import clear_rate_limits
    clear_rate_limits(ip="127.0.0.1")

    User.objects.filter(pk=opts.wp_target_id).update(is_email_verified=False)
    opts.client.login("verify_wp_manager", "verify##wp99")
    assert opts.client.is_authenticated, "login failed"

    resp = opts.client.post(f"/api/user/{opts.wp_target_id}", {"is_email_verified": True})
    assert resp.status_code in [400, 403], \
        f"manage_users admin must not set is_email_verified, got {resp.status_code}"

    target = User.objects.get(pk=opts.wp_target_id)
    assert_true(not target.is_email_verified,
                "is_email_verified must remain False after rejected manager write")


@th.django_unit_test("write-protect: manage_users admin cannot set is_phone_verified")
def test_manager_cannot_set_is_phone_verified(opts):
    from mojo.apps.account.models import User
    from mojo.decorators.limits import clear_rate_limits
    clear_rate_limits(ip="127.0.0.1")

    User.objects.filter(pk=opts.wp_target_id).update(is_phone_verified=False)
    opts.client.login("verify_wp_manager", "verify##wp99")
    assert opts.client.is_authenticated, "login failed"

    resp = opts.client.post(f"/api/user/{opts.wp_target_id}", {"is_phone_verified": True})
    assert resp.status_code in [400, 403], \
        f"manage_users admin must not set is_phone_verified, got {resp.status_code}"

    target = User.objects.get(pk=opts.wp_target_id)
    assert_true(not target.is_phone_verified,
                "is_phone_verified must remain False after rejected manager write")


@th.django_unit_test("write-protect: superuser can set is_email_verified=True")
def test_superuser_can_set_is_email_verified(opts):
    from mojo.apps.account.models import User
    from mojo.decorators.limits import clear_rate_limits
    clear_rate_limits(ip="127.0.0.1")

    User.objects.filter(pk=opts.wp_target_id).update(is_email_verified=False)
    opts.client.login("verify_wp_super", "verify##wp99")
    assert opts.client.is_authenticated, "superuser login failed"

    resp = opts.client.post(f"/api/user/{opts.wp_target_id}", {"is_email_verified": True})
    assert_eq(resp.status_code, 200,
              f"Superuser must be allowed to set is_email_verified, got {resp.status_code}")

    target = User.objects.get(pk=opts.wp_target_id)
    assert_true(target.is_email_verified,
                "is_email_verified must be True after superuser write")

    # Restore
    User.objects.filter(pk=opts.wp_target_id).update(is_email_verified=False)


@th.django_unit_test("write-protect: superuser can set is_phone_verified=True")
def test_superuser_can_set_is_phone_verified(opts):
    from mojo.apps.account.models import User
    from mojo.decorators.limits import clear_rate_limits
    clear_rate_limits(ip="127.0.0.1")

    User.objects.filter(pk=opts.wp_target_id).update(is_phone_verified=False)
    opts.client.login("verify_wp_super", "verify##wp99")
    assert opts.client.is_authenticated, "superuser login failed"

    resp = opts.client.post(f"/api/user/{opts.wp_target_id}", {"is_phone_verified": True})
    assert_eq(resp.status_code, 200,
              f"Superuser must be allowed to set is_phone_verified, got {resp.status_code}")

    target = User.objects.get(pk=opts.wp_target_id)
    assert_true(target.is_phone_verified,
                "is_phone_verified must be True after superuser write")

    # Restore
    User.objects.filter(pk=opts.wp_target_id).update(is_phone_verified=False)


@th.django_unit_test("write-protect: superuser can set is_email_verified=False (revoke)")
def test_superuser_can_revoke_is_email_verified(opts):
    """Superuser must be able to revoke verification — e.g. after a suspected account takeover."""
    from mojo.apps.account.models import User
    from mojo.decorators.limits import clear_rate_limits
    clear_rate_limits(ip="127.0.0.1")

    User.objects.filter(pk=opts.wp_target_id).update(is_email_verified=True)
    opts.client.login("verify_wp_super", "verify##wp99")
    assert opts.client.is_authenticated, "superuser login failed"

    resp = opts.client.post(f"/api/user/{opts.wp_target_id}", {"is_email_verified": False})
    assert_eq(resp.status_code, 200,
              f"Superuser must be allowed to revoke is_email_verified, got {resp.status_code}")

    target = User.objects.get(pk=opts.wp_target_id)
    assert_true(not target.is_email_verified,
                "is_email_verified must be False after superuser revoke")


@th.django_unit_test("write-protect: owner cannot set is_email_verified during user creation")
def test_owner_cannot_set_verified_on_create(opts):
    """
    The create path (POST /api/user with no pk) must also be blocked.
    on_rest_pre_save runs before the row is inserted, so changed_fields
    includes any field in the payload — the guard fires for creates too.
    """
    from mojo.apps.account.models import User
    from mojo.decorators.limits import clear_rate_limits
    clear_rate_limits(ip="127.0.0.1")

    # Log in as manage_users admin — has permission to create users, but not superuser
    opts.client.login("verify_wp_manager", "verify##wp99")
    assert opts.client.is_authenticated, "login failed"

    resp = opts.client.post("/api/user", {
        "username": "verify_wp_create_attempt",
        "email": "verify_wp_create_attempt@example.com",
        "password": "temp##pass99",
        "is_email_verified": True,
    })
    assert resp.status_code in [400, 403], \
        f"Non-superuser must not create a user with is_email_verified=True, got {resp.status_code}"

    # Confirm user was not created with the verified flag set (or at all with it set)
    created = User.objects.filter(username="verify_wp_create_attempt").first()
    if created is not None:
        assert_true(not created.is_email_verified,
                    "Created user must not have is_email_verified=True even if row was saved")
        created.delete()


@th.django_unit_test("write-protect: superuser can set is_email_verified=True on a new user")
def test_superuser_can_create_verified_user(opts):
    """
    Creates a user via ORM (avoids create-path field requirements) then
    confirms a superuser can set is_email_verified=True via the REST update path.
    This exercises the same SUPERUSER_ONLY_FIELDS guard on an existing record.
    """
    from mojo.apps.account.models import User
    from mojo.decorators.limits import clear_rate_limits
    clear_rate_limits(ip="127.0.0.1")

    # Create user directly so we control the initial state precisely
    new_user = User.objects.filter(username="verify_wp_super_created").first()
    if new_user is None:
        new_user = User(username="verify_wp_super_created",
                        email="verify_wp_super_created@example.com")
        new_user.save()
    new_user.is_email_verified = False
    new_user.is_active = True
    new_user.save()

    opts.client.login("verify_wp_super", "verify##wp99")
    assert opts.client.is_authenticated, "superuser login failed"

    resp = opts.client.post(f"/api/user/{new_user.pk}", {"is_email_verified": True})
    assert_eq(resp.status_code, 200,
              f"Superuser must be allowed to set is_email_verified=True, got {resp.status_code}")

    new_user.refresh_from_db()
    assert_true(new_user.is_email_verified,
                "is_email_verified must be True after superuser write")
    new_user.delete()


@th.django_unit_test("write-protect: omitting the field entirely is always allowed")
def test_omitting_verified_fields_is_allowed(opts):
    """
    Sanity check: a normal save that does not touch these fields must still
    succeed. The guard must not interfere with unrelated updates.
    """
    from mojo.apps.account.models import User
    from mojo.decorators.limits import clear_rate_limits
    clear_rate_limits(ip="127.0.0.1")

    User.objects.filter(pk=opts.wp_target_id).update(is_email_verified=True)
    opts.client.login("verify_wp_target", "verify##wp99")
    assert opts.client.is_authenticated, "login failed"

    resp = opts.client.post(f"/api/user/{opts.wp_target_id}", {"display_name": "Write Protect Test"})
    assert_eq(resp.status_code, 200,
              f"Normal field update must succeed, got {resp.status_code}")


@th.django_unit_setup()
def cleanup_write_protection(opts):
    from mojo.apps.account.models import User
    User.objects.filter(pk=opts.wp_target_id).delete()
    User.objects.filter(pk=opts.wp_manager_id).delete()
    User.objects.filter(pk=opts.wp_super_id).delete()


# ===========================================================================
# Field-level write protection — requires_mfa, is_active, org,
#                                last_activity, auth_key
#
# These fields are protected at different permission levels:
#
#   SUPERUSER ONLY: requires_mfa, last_activity, auth_key
#   MANAGE_USERS:   is_active, org
#
# Tests confirm:
#   - Owner (no special perms) is always blocked
#   - manage_users admin is blocked from superuser-only fields
#   - manage_users admin is allowed for manage_users-level fields
#   - Superuser is allowed for everything
#   - The DB value is unchanged after a rejected write (not just the status code)
# ===========================================================================


# ---------------------------------------------------------------------------
# requires_mfa
# ---------------------------------------------------------------------------

@th.django_unit_test("field-protect: owner cannot disable requires_mfa on own account")
def test_owner_cannot_disable_requires_mfa(opts):
    from mojo.apps.account.models import User
    from mojo.decorators.limits import clear_rate_limits
    clear_rate_limits(ip="127.0.0.1")

    User.objects.filter(pk=opts.wp_target_id).update(requires_mfa=True, is_email_verified=True)
    opts.client.login("verify_wp_target", "verify##wp99")
    assert opts.client.is_authenticated, "login failed"

    resp = opts.client.post(f"/api/user/{opts.wp_target_id}", {"requires_mfa": False})
    assert resp.status_code in [400, 403], \
        f"Owner must not disable requires_mfa, got {resp.status_code}"

    target = User.objects.get(pk=opts.wp_target_id)
    assert_true(target.requires_mfa,
                "requires_mfa must remain True after rejected owner write")
    User.objects.filter(pk=opts.wp_target_id).update(requires_mfa=False)


@th.django_unit_test("field-protect: owner cannot enable requires_mfa on own account")
def test_owner_cannot_enable_requires_mfa(opts):
    from mojo.apps.account.models import User
    from mojo.decorators.limits import clear_rate_limits
    clear_rate_limits(ip="127.0.0.1")

    User.objects.filter(pk=opts.wp_target_id).update(requires_mfa=False, is_email_verified=True)
    opts.client.login("verify_wp_target", "verify##wp99")
    assert opts.client.is_authenticated, "login failed"

    resp = opts.client.post(f"/api/user/{opts.wp_target_id}", {"requires_mfa": True})
    assert resp.status_code in [400, 403], \
        f"Owner must not enable requires_mfa, got {resp.status_code}"

    target = User.objects.get(pk=opts.wp_target_id)
    assert_true(not target.requires_mfa,
                "requires_mfa must remain False after rejected owner write")


@th.django_unit_test("field-protect: manage_users admin cannot change requires_mfa")
def test_manager_cannot_change_requires_mfa(opts):
    from mojo.apps.account.models import User
    from mojo.decorators.limits import clear_rate_limits
    clear_rate_limits(ip="127.0.0.1")

    User.objects.filter(pk=opts.wp_target_id).update(requires_mfa=False)
    opts.client.login("verify_wp_manager", "verify##wp99")
    assert opts.client.is_authenticated, "login failed"

    resp = opts.client.post(f"/api/user/{opts.wp_target_id}", {"requires_mfa": True})
    assert resp.status_code in [400, 403], \
        f"manage_users admin must not change requires_mfa, got {resp.status_code}"

    target = User.objects.get(pk=opts.wp_target_id)
    assert_true(not target.requires_mfa,
                "requires_mfa must remain False after rejected manager write")


@th.django_unit_test("field-protect: superuser can change requires_mfa")
def test_superuser_can_change_requires_mfa(opts):
    from mojo.apps.account.models import User
    from mojo.decorators.limits import clear_rate_limits
    clear_rate_limits(ip="127.0.0.1")

    User.objects.filter(pk=opts.wp_target_id).update(requires_mfa=False)
    opts.client.login("verify_wp_super", "verify##wp99")
    assert opts.client.is_authenticated, "superuser login failed"

    resp = opts.client.post(f"/api/user/{opts.wp_target_id}", {"requires_mfa": True})
    assert_eq(resp.status_code, 200,
              f"Superuser must be allowed to set requires_mfa, got {resp.status_code}")

    target = User.objects.get(pk=opts.wp_target_id)
    assert_true(target.requires_mfa,
                "requires_mfa must be True after superuser write")
    User.objects.filter(pk=opts.wp_target_id).update(requires_mfa=False)


# ---------------------------------------------------------------------------
# is_active
# ---------------------------------------------------------------------------

@th.django_unit_test("field-protect: owner cannot deactivate own account")
def test_owner_cannot_deactivate_self(opts):
    from mojo.apps.account.models import User
    from mojo.decorators.limits import clear_rate_limits
    clear_rate_limits(ip="127.0.0.1")

    User.objects.filter(pk=opts.wp_target_id).update(is_active=True, is_email_verified=True)
    opts.client.login("verify_wp_target", "verify##wp99")
    assert opts.client.is_authenticated, "login failed"

    resp = opts.client.post(f"/api/user/{opts.wp_target_id}", {"is_active": False})
    assert resp.status_code in [400, 403], \
        f"Owner must not deactivate own account, got {resp.status_code}"

    target = User.objects.get(pk=opts.wp_target_id)
    assert_true(target.is_active,
                "is_active must remain True after rejected owner write")


@th.django_unit_test("field-protect: owner cannot reactivate a banned account")
def test_owner_cannot_reactivate_banned_account(opts):
    """
    An admin deactivates an account as a ban. The user must not be able to
    flip is_active back to True via a REST POST. Confirming this requires
    that the user can still authenticate (e.g. they still have a valid JWT
    from before the ban was applied), so we seed the token directly.
    """
    from mojo.apps.account.models import User
    from mojo.apps.account.utils.jwtoken import JWToken
    from mojo.decorators.limits import clear_rate_limits
    clear_rate_limits(ip="127.0.0.1")

    # Log in while still active to get a valid token
    User.objects.filter(pk=opts.wp_target_id).update(is_active=True, is_email_verified=True)
    opts.client.login("verify_wp_target", "verify##wp99")
    assert opts.client.is_authenticated, "login failed"

    # Now deactivate — simulates an admin ban after the session was issued
    User.objects.filter(pk=opts.wp_target_id).update(is_active=False)

    resp = opts.client.post(f"/api/user/{opts.wp_target_id}", {"is_active": True})
    assert resp.status_code in [400, 403], \
        f"Banned user must not reactivate own account, got {resp.status_code}"

    target = User.objects.get(pk=opts.wp_target_id)
    assert_true(not target.is_active,
                "is_active must remain False after rejected reactivation attempt")

    User.objects.filter(pk=opts.wp_target_id).update(is_active=True)


@th.django_unit_test("field-protect: manage_users admin can deactivate a user")
def test_manager_can_deactivate_user(opts):
    from mojo.apps.account.models import User
    from mojo.decorators.limits import clear_rate_limits
    clear_rate_limits(ip="127.0.0.1")

    User.objects.filter(pk=opts.wp_target_id).update(is_active=True)
    opts.client.login("verify_wp_manager", "verify##wp99")
    assert opts.client.is_authenticated, "login failed"

    resp = opts.client.post(f"/api/user/{opts.wp_target_id}", {"is_active": False})
    assert_eq(resp.status_code, 200,
              f"manage_users admin must be allowed to deactivate a user, got {resp.status_code}")

    target = User.objects.get(pk=opts.wp_target_id)
    assert_true(not target.is_active,
                "is_active must be False after manager write")
    User.objects.filter(pk=opts.wp_target_id).update(is_active=True)


# ---------------------------------------------------------------------------
# org
# ---------------------------------------------------------------------------

@th.django_unit_test("field-protect: owner cannot reassign own org")
def test_owner_cannot_reassign_org(opts):
    """
    org controls token TTL and push config. A user self-assigning to a
    different org could inherit settings that don't belong to them.
    """
    from mojo.apps.account.models import User, Group
    from mojo.decorators.limits import clear_rate_limits
    clear_rate_limits(ip="127.0.0.1")

    # Use an existing group or create a minimal one for the test
    group = Group.objects.filter(name="verify_wp_test_org").first()
    if group is None:
        group = Group(name="verify_wp_test_org")
        group.save()
    opts.wp_test_org_id = group.pk

    User.objects.filter(pk=opts.wp_target_id).update(org=None, is_email_verified=True)
    opts.client.login("verify_wp_target", "verify##wp99")
    assert opts.client.is_authenticated, "login failed"

    resp = opts.client.post(f"/api/user/{opts.wp_target_id}", {"org": group.pk})
    assert resp.status_code in [400, 403], \
        f"Owner must not reassign own org, got {resp.status_code}"

    target = User.objects.get(pk=opts.wp_target_id)
    assert_true(target.org_id is None,
                "org must remain None after rejected owner write")


@th.django_unit_test("field-protect: manage_users admin can assign org")
def test_manager_can_assign_org(opts):
    from mojo.apps.account.models import User, Group
    from mojo.decorators.limits import clear_rate_limits
    clear_rate_limits(ip="127.0.0.1")

    group = Group.objects.filter(name="verify_wp_test_org").first()
    if group is None:
        group = Group(name="verify_wp_test_org")
        group.save()

    User.objects.filter(pk=opts.wp_target_id).update(org=None)
    opts.client.login("verify_wp_manager", "verify##wp99")
    assert opts.client.is_authenticated, "login failed"

    resp = opts.client.post(f"/api/user/{opts.wp_target_id}", {"org": group.pk})
    assert_eq(resp.status_code, 200,
              f"manage_users admin must be allowed to assign org, got {resp.status_code}")

    target = User.objects.get(pk=opts.wp_target_id)
    assert_eq(target.org_id, group.pk,
              "org must be updated after manager write")
    User.objects.filter(pk=opts.wp_target_id).update(org=None)


# ---------------------------------------------------------------------------
# last_activity
# ---------------------------------------------------------------------------

@th.django_unit_test("field-protect: last_activity is silently ignored by REST for all actors")
def test_last_activity_is_not_writable_via_rest(opts):
    """
    last_activity is in NO_SAVE_FIELDS, so the REST framework silently ignores
    it for everyone — including superusers. The field is server-managed only.
    A POST containing it returns 200 but the value is never changed.
    """
    from mojo.apps.account.models import User
    from mojo.decorators.limits import clear_rate_limits

    fake_ts = "2000-01-01T00:00:00Z"

    for username, label in [
        ("verify_wp_target", "owner"),
        ("verify_wp_manager", "manager"),
        ("verify_wp_super", "superuser"),
    ]:
        original = User.objects.get(pk=opts.wp_target_id).last_activity
        clear_rate_limits(ip="127.0.0.1")
        User.objects.filter(pk=opts.wp_target_id).update(is_email_verified=True)
        opts.client.login(username, "verify##wp99")
        assert opts.client.is_authenticated, f"{label} login failed"

        resp = opts.client.post(f"/api/user/{opts.wp_target_id}", {"last_activity": fake_ts})
        assert_eq(resp.status_code, 200,
                  f"{label}: last_activity in NO_SAVE_FIELDS — expect 200 (silently ignored), got {resp.status_code}")

        target = User.objects.get(pk=opts.wp_target_id)
        current = target.last_activity
        assert_true(
            current == original or (current is None and original is None),
            f"{label}: last_activity must be unchanged (NO_SAVE_FIELDS). "
            f"Before: {original}, After: {current}"
        )


# ---------------------------------------------------------------------------
# auth_key
# ---------------------------------------------------------------------------

@th.django_unit_test("field-protect: auth_key is silently ignored by REST for all actors")
def test_auth_key_is_not_writable_via_rest(opts):
    """
    auth_key is in NO_SAVE_FIELDS, so the REST framework silently ignores it
    for everyone — including superusers. This is intentional: auth_key rotation
    must go through dedicated session-invalidation flows, not a generic REST
    field update. A POST containing auth_key returns 200 but the value never
    changes — the security property is that the field is unwritable, not that
    a 403 is returned.
    """
    from mojo.apps.account.models import User
    from mojo.decorators.limits import clear_rate_limits

    for username, label in [
        ("verify_wp_target", "owner"),
        ("verify_wp_manager", "manager"),
        ("verify_wp_super", "superuser"),
    ]:
        original_key = User.objects.get(pk=opts.wp_target_id).auth_key
        clear_rate_limits(ip="127.0.0.1")
        User.objects.filter(pk=opts.wp_target_id).update(is_email_verified=True)
        opts.client.login(username, "verify##wp99")
        assert opts.client.is_authenticated, f"{label} login failed"

        resp = opts.client.post(f"/api/user/{opts.wp_target_id}", {"auth_key": "attempt_to_overwrite"})
        assert_eq(resp.status_code, 200,
                  f"{label}: auth_key in NO_SAVE_FIELDS — expect 200 (silently ignored), got {resp.status_code}")

        target = User.objects.get(pk=opts.wp_target_id)
        assert_eq(target.auth_key, original_key,
                  f"{label}: auth_key must be unchanged regardless of who posts it")


@th.django_unit_setup()
def cleanup_field_protection(opts):
    from mojo.apps.account.models import Group
    Group.objects.filter(name="verify_wp_test_org").delete()


# ===========================================================================
# Email verify — OTP code unit tests
# ===========================================================================

@th.django_unit_test("email verify code: generate stores 6-digit code and timestamp in user secrets")
def test_email_verify_code_stored_in_secrets(opts):
    from mojo.apps.account.models import User
    from mojo.apps.account.utils import tokens

    User.objects.filter(pk=opts.user_id).update(is_email_verified=False)
    user = User.objects.get(pk=opts.user_id)
    code = tokens.generate_email_verify_code(user)
    user.refresh_from_db()
    assert_eq(user.get_secret("email_verify_code"), code,
              "code must be stored in secrets after generate")
    assert_true(user.get_secret("email_verify_code_ts") is not None,
                "timestamp must be stored after generate")
    assert_true(len(code) == 6 and code.isdigit(),
                f"code must be a 6-digit numeric string, got: {code!r}")
    # consume cleanly
    tokens.verify_email_verify_code(user, code)


@th.django_unit_test("email verify code: correct code succeeds and is consumed (single-use)")
def test_email_verify_code_single_use(opts):
    from mojo.apps.account.models import User
    from mojo.apps.account.utils import tokens
    from mojo import errors as merrors

    User.objects.filter(pk=opts.user_id).update(is_email_verified=False)
    user = User.objects.get(pk=opts.user_id)
    code = tokens.generate_email_verify_code(user)
    tokens.verify_email_verify_code(user, code)

    # Secrets must be cleared after first verify
    user.refresh_from_db()
    assert_eq(user.get_secret("email_verify_code"), None,
              "code must be cleared from secrets after successful verify")

    # Second call must raise
    raised = False
    try:
        tokens.verify_email_verify_code(user, code)
    except merrors.ValueException:
        raised = True
    assert_true(raised, "Reusing an email verify code must raise ValueException")


@th.django_unit_test("email verify code: wrong code rejected without consuming the valid code")
def test_email_verify_code_wrong_code_does_not_consume(opts):
    from mojo.apps.account.models import User
    from mojo.apps.account.utils import tokens
    from mojo import errors as merrors

    User.objects.filter(pk=opts.user_id).update(is_email_verified=False)
    user = User.objects.get(pk=opts.user_id)
    code = tokens.generate_email_verify_code(user)

    raised = False
    try:
        tokens.verify_email_verify_code(user, "000000")
    except merrors.ValueException:
        raised = True
    assert_true(raised, "Wrong code must raise ValueException")

    # Valid code must still work — a wrong guess must not burn the real code
    user.refresh_from_db()
    tokens.verify_email_verify_code(user, code)


@th.django_unit_test("email verify code: expired code is rejected")
def test_email_verify_code_expired(opts):
    from mojo.apps.account.models import User
    from mojo.apps.account.utils import tokens
    import mojo.apps.account.utils.tokens as tok_module
    from mojo import errors as merrors

    User.objects.filter(pk=opts.user_id).update(is_email_verified=False)
    user = User.objects.get(pk=opts.user_id)
    orig_ttl = tok_module.EMAIL_VERIFY_CODE_TTL
    tok_module.EMAIL_VERIFY_CODE_TTL = -1
    try:
        code = tokens.generate_email_verify_code(user)
        raised = False
        try:
            tokens.verify_email_verify_code(user, code)
        except merrors.ValueException:
            raised = True
        assert_true(raised, "Expired email verify code must raise ValueException")
    finally:
        tok_module.EMAIL_VERIFY_CODE_TTL = orig_ttl
        user.set_secret("email_verify_code", None)
        user.set_secret("email_verify_code_ts", None)
        user.save(update_fields=["mojo_secrets", "modified"])


# ===========================================================================
# REST: POST /api/auth/verify/email/send  (authenticated, method param)
# ===========================================================================

@th.django_unit_test("verify/email/send: method=code returns 200 and stores code in secrets")
def test_email_verify_send_code_happy_path(opts):
    from mojo.apps.account.models import User
    from mojo.decorators.limits import clear_rate_limits
    clear_rate_limits(ip="127.0.0.1")

    User.objects.filter(pk=opts.user_id).update(is_email_verified=False, is_active=True)
    opts.client.login(TEST_USER, TEST_PWORD)
    resp = opts.client.post("/api/auth/verify/email/send", {"method": "code"})
    opts.client.logout()

    assert_eq(resp.status_code, 200, f"Expected 200, got {resp.status_code}")
    assert_true(resp.response.status is True, "Response status must be True")

    user = User.objects.get(pk=opts.user_id)
    stored_code = user.get_secret("email_verify_code")
    assert_true(
        stored_code is not None and len(stored_code) == 6 and stored_code.isdigit(),
        f"6-digit numeric code must be stored in secrets after send, got: {stored_code!r}",
    )

    # clean up
    user.set_secret("email_verify_code", None)
    user.set_secret("email_verify_code_ts", None)
    user.save(update_fields=["mojo_secrets", "modified"])


@th.django_unit_test("verify/email/send: method=code requires authentication — 401 without token")
def test_email_verify_send_code_requires_auth(opts):
    from mojo.decorators.limits import clear_rate_limits
    clear_rate_limits(ip="127.0.0.1")
    opts.client.logout()

    resp = opts.client.post("/api/auth/verify/email/send", {"method": "code"})
    assert_true(
        resp.status_code in (401, 403),
        f"method=code send must require authentication, got {resp.status_code}",
    )


@th.django_unit_test("verify/email/send: method=code when already verified returns 200 without generating code")
def test_email_verify_send_code_already_verified_no_code_generated(opts):
    from mojo.apps.account.models import User
    from mojo.decorators.limits import clear_rate_limits
    clear_rate_limits(ip="127.0.0.1")

    User.objects.filter(pk=opts.user_id).update(is_email_verified=True, is_active=True)
    opts.client.login(TEST_USER, TEST_PWORD)
    resp = opts.client.post("/api/auth/verify/email/send", {"method": "code"})
    opts.client.logout()

    assert_eq(resp.status_code, 200, f"Expected 200, got {resp.status_code}")
    user = User.objects.get(pk=opts.user_id)
    assert_eq(
        user.get_secret("email_verify_code"), None,
        "No code must be stored when email is already verified",
    )
    User.objects.filter(pk=opts.user_id).update(is_email_verified=False)


@th.django_unit_test("verify/email/send: omitting method sends link — backward compatible")
def test_email_verify_send_omit_method_sends_link(opts):
    """
    Existing callers that omit 'method' must receive a link token, not a code.
    The ev: JTI must be stored; the OTP code secret must remain absent.
    """
    from mojo.apps.account.models import User
    import mojo.apps.account.utils.tokens as tok_module
    from mojo.decorators.limits import clear_rate_limits
    clear_rate_limits(ip="127.0.0.1")

    User.objects.filter(pk=opts.user_id).update(is_email_verified=False, is_active=True)
    opts.client.login(TEST_USER, TEST_PWORD)
    resp = opts.client.post("/api/auth/verify/email/send", {})
    opts.client.logout()

    assert_eq(resp.status_code, 200, f"Expected 200, got {resp.status_code}")
    user = User.objects.get(pk=opts.user_id)
    assert_true(
        user.get_secret(tok_module._JTI_KEYS[tok_module.KIND_EMAIL_VERIFY]) is not None,
        "ev: JTI must be stored when method is omitted (link flow)",
    )
    assert_eq(
        user.get_secret("email_verify_code"), None,
        "email_verify_code must NOT be set when method is omitted",
    )
    # clear leftover JTI
    user.set_secret(tok_module._JTI_KEYS[tok_module.KIND_EMAIL_VERIFY], None)
    user.save(update_fields=["mojo_secrets", "modified"])


@th.django_unit_test("verify/email/send: method=link explicit sends link — backward compatible")
def test_email_verify_send_explicit_link_method(opts):
    from mojo.apps.account.models import User
    import mojo.apps.account.utils.tokens as tok_module
    from mojo.decorators.limits import clear_rate_limits
    clear_rate_limits(ip="127.0.0.1")

    User.objects.filter(pk=opts.user_id).update(is_email_verified=False, is_active=True)
    opts.client.login(TEST_USER, TEST_PWORD)
    resp = opts.client.post("/api/auth/verify/email/send", {"method": "link"})
    opts.client.logout()

    assert_eq(resp.status_code, 200, f"Expected 200, got {resp.status_code}")
    user = User.objects.get(pk=opts.user_id)
    assert_true(
        user.get_secret(tok_module._JTI_KEYS[tok_module.KIND_EMAIL_VERIFY]) is not None,
        "ev: JTI must be stored for method=link",
    )
    assert_eq(user.get_secret("email_verify_code"), None,
              "OTP code must NOT be set for method=link")
    user.set_secret(tok_module._JTI_KEYS[tok_module.KIND_EMAIL_VERIFY], None)
    user.save(update_fields=["mojo_secrets", "modified"])


# ===========================================================================
# REST: POST /api/auth/verify/email/confirm  (code path — authenticated)
# ===========================================================================

@th.django_unit_test("verify/email/confirm POST: correct code marks is_email_verified=True")
def test_email_verify_confirm_code_marks_verified(opts):
    from mojo.apps.account.models import User
    from mojo.apps.account.utils import tokens
    from mojo.decorators.limits import clear_rate_limits
    clear_rate_limits(ip="127.0.0.1")

    User.objects.filter(pk=opts.user_id).update(is_email_verified=False, is_active=True)
    user = User.objects.get(pk=opts.user_id)
    code = tokens.generate_email_verify_code(user)

    opts.client.login(TEST_USER, TEST_PWORD)
    resp = opts.client.post("/api/auth/verify/email/confirm", {"code": code})
    opts.client.logout()

    assert_eq(resp.status_code, 200, f"Expected 200, got {resp.status_code}: {resp.content}")
    data = resp.json
    assert_true(data.get("status") is True, "Response status must be True")

    user.refresh_from_db()
    assert_true(user.is_email_verified, "is_email_verified must be True after code confirm")


@th.django_unit_test("verify/email/confirm POST: does NOT issue a new JWT — existing session continues")
def test_email_verify_confirm_code_no_jwt_issued(opts):
    """
    The code flow is for in-portal use — the user is already authenticated.
    Unlike the link flow, no new JWT should be returned and auth_key must not
    be rotated.
    """
    from mojo.apps.account.models import User
    from mojo.apps.account.utils import tokens
    from mojo.decorators.limits import clear_rate_limits
    clear_rate_limits(ip="127.0.0.1")

    User.objects.filter(pk=opts.user_id).update(is_email_verified=False, is_active=True)
    user = User.objects.get(pk=opts.user_id)
    old_auth_key = user.auth_key
    code = tokens.generate_email_verify_code(user)

    opts.client.login(TEST_USER, TEST_PWORD)
    resp = opts.client.post("/api/auth/verify/email/confirm", {"code": code})
    opts.client.logout()

    assert_eq(resp.status_code, 200, f"Expected 200, got {resp.status_code}")
    data = resp.json
    # Must not contain JWT data envelope
    jwt_data = data.get("data") or {}
    has_jwt = bool(getattr(jwt_data, "access_token", None) or
                   (isinstance(jwt_data, dict) and jwt_data.get("access_token")))
    assert_true(not has_jwt, "Code confirm must not return a JWT — existing session continues")

    user.refresh_from_db()
    assert_eq(user.auth_key, old_auth_key,
              "auth_key must not be rotated by email verify code confirm")


@th.django_unit_test("verify/email/confirm POST: requires authentication — 401 without token")
def test_email_verify_confirm_code_requires_auth(opts):
    from mojo.apps.account.models import User
    from mojo.apps.account.utils import tokens
    from mojo.decorators.limits import clear_rate_limits
    clear_rate_limits(ip="127.0.0.1")

    User.objects.filter(pk=opts.user_id).update(is_email_verified=False, is_active=True)
    user = User.objects.get(pk=opts.user_id)
    code = tokens.generate_email_verify_code(user)

    opts.client.logout()
    resp = opts.client.post("/api/auth/verify/email/confirm", {"code": code})
    assert_true(
        resp.status_code in (401, 403),
        f"Code confirm must require authentication, got {resp.status_code}",
    )

    # clean up the unused code
    user.set_secret("email_verify_code", None)
    user.set_secret("email_verify_code_ts", None)
    user.save(update_fields=["mojo_secrets", "modified"])


@th.django_unit_test("verify/email/confirm POST: wrong code returns 4xx and does not mark verified")
def test_email_verify_confirm_code_wrong_code_rejected(opts):
    from mojo.apps.account.models import User
    from mojo.apps.account.utils import tokens
    from mojo.decorators.limits import clear_rate_limits
    clear_rate_limits(ip="127.0.0.1")

    User.objects.filter(pk=opts.user_id).update(is_email_verified=False, is_active=True)
    user = User.objects.get(pk=opts.user_id)
    tokens.generate_email_verify_code(user)

    opts.client.login(TEST_USER, TEST_PWORD)
    resp = opts.client.post("/api/auth/verify/email/confirm", {"code": "000000"})
    opts.client.logout()

    assert_true(resp.status_code in (400, 422),
                f"Wrong code must return 4xx, got {resp.status_code}")
    user.refresh_from_db()
    assert_true(not user.is_email_verified,
                "is_email_verified must remain False after wrong code")

    # clean up the valid code
    user.set_secret("email_verify_code", None)
    user.set_secret("email_verify_code_ts", None)
    user.save(update_fields=["mojo_secrets", "modified"])


@th.django_unit_test("verify/email/confirm POST: expired code returns 4xx and does not mark verified")
def test_email_verify_confirm_code_expired(opts):
    from mojo.apps.account.models import User
    from mojo.apps.account.utils import tokens
    from mojo.decorators.limits import clear_rate_limits
    clear_rate_limits(ip="127.0.0.1")

    User.objects.filter(pk=opts.user_id).update(is_email_verified=False, is_active=True)
    user = User.objects.get(pk=opts.user_id)
    try:
        code = tokens.generate_email_verify_code(user)
        # Force the stored timestamp into the distant past so the server's
        # real TTL recognises it as expired.  Patching the module-level
        # TTL only affects the test process, not the running server.
        user.set_secret("email_verify_code_ts", 0)
        user.save(update_fields=["mojo_secrets", "modified"])

        opts.client.login(TEST_USER, TEST_PWORD)
        resp = opts.client.post("/api/auth/verify/email/confirm", {"code": code})
        opts.client.logout()
        assert_true(resp.status_code in (400, 422),
                    f"Expired code must return 4xx, got {resp.status_code}")
        user.refresh_from_db()
        assert_true(not user.is_email_verified,
                    "is_email_verified must remain False after expired code")
    finally:
        user.set_secret("email_verify_code", None)
        user.set_secret("email_verify_code_ts", None)
        user.save(update_fields=["mojo_secrets", "modified"])


@th.django_unit_test("verify/email/confirm POST: code is single-use — second confirm rejected")
def test_email_verify_confirm_code_single_use(opts):
    from mojo.apps.account.models import User
    from mojo.apps.account.utils import tokens
    from mojo.decorators.limits import clear_rate_limits
    clear_rate_limits(ip="127.0.0.1")

    User.objects.filter(pk=opts.user_id).update(is_email_verified=False, is_active=True)
    user = User.objects.get(pk=opts.user_id)
    code = tokens.generate_email_verify_code(user)

    opts.client.login(TEST_USER, TEST_PWORD)
    resp1 = opts.client.post("/api/auth/verify/email/confirm", {"code": code})
    assert_eq(resp1.status_code, 200, f"First confirm must succeed, got {resp1.status_code}")

    # Reset verified state so the gate is not what blocks the second attempt
    User.objects.filter(pk=opts.user_id).update(is_email_verified=False)
    clear_rate_limits(ip="127.0.0.1")
    resp2 = opts.client.post("/api/auth/verify/email/confirm", {"code": code})
    opts.client.logout()
    assert_true(resp2.status_code in (400, 422),
                f"Second use of same code must be rejected, got {resp2.status_code}")


@th.django_unit_test("verify/email/confirm POST: missing code param returns 4xx")
def test_email_verify_confirm_code_missing_param(opts):
    from mojo.decorators.limits import clear_rate_limits
    clear_rate_limits(ip="127.0.0.1")

    opts.client.login(TEST_USER, TEST_PWORD)
    resp = opts.client.post("/api/auth/verify/email/confirm", {})
    opts.client.logout()
    assert_true(resp.status_code in (400, 422),
                f"Missing code param must return 4xx, got {resp.status_code}")


@th.django_unit_test("verify/email/confirm POST: user A's code cannot verify user B (identity from JWT)")
def test_email_verify_confirm_code_cross_user_rejected(opts):
    """
    The code path resolves identity from the Bearer token, not from the code
    itself. User B cannot submit User A's code to verify User B's account —
    the code lives in User A's secrets and will not match User B's (empty)
    secrets.
    """
    from mojo.apps.account.models import User
    from mojo.apps.account.utils import tokens
    from mojo.decorators.limits import clear_rate_limits
    clear_rate_limits(ip="127.0.0.1")

    User.objects.filter(pk=opts.user_id).update(is_email_verified=False, is_active=True)
    User.objects.filter(pk=opts.user_b_id).update(is_email_verified=False, is_active=True)

    # Generate a code for User A
    user_a = User.objects.get(pk=opts.user_id)
    code_for_a = tokens.generate_email_verify_code(user_a)

    # Authenticate as User B and submit User A's code
    opts.client.login("verify_test_user_b", TEST_PWORD)
    clear_rate_limits(ip="127.0.0.1")
    resp = opts.client.post("/api/auth/verify/email/confirm", {"code": code_for_a})
    opts.client.logout()

    # User B has no code stored — must be rejected
    assert_true(resp.status_code in (400, 422),
                f"User A's code must not verify User B, got {resp.status_code}")

    user_b = User.objects.get(pk=opts.user_b_id)
    assert_true(not user_b.is_email_verified,
                "User B must not be verified by submitting User A's code")

    # User A's code was never consumed — clean up
    user_a.set_secret("email_verify_code", None)
    user_a.set_secret("email_verify_code_ts", None)
    user_a.save(update_fields=["mojo_secrets", "modified"])


@th.django_unit_test("verify/email/confirm POST: wrong guess does not burn the valid code (brute-force safety)")
def test_email_verify_confirm_code_wrong_guess_preserves_valid_code(opts):
    """
    A wrong code submission must NOT consume the stored code. If it did, any
    single bad request — accidental typo or attacker-injected garbage — would
    force the user to request a completely new code.
    """
    from mojo.apps.account.models import User
    from mojo.apps.account.utils import tokens
    from mojo.decorators.limits import clear_rate_limits
    clear_rate_limits(ip="127.0.0.1")

    User.objects.filter(pk=opts.user_id).update(is_email_verified=False, is_active=True)
    user = User.objects.get(pk=opts.user_id)
    code = tokens.generate_email_verify_code(user)

    opts.client.login(TEST_USER, TEST_PWORD)

    # First: submit wrong code
    clear_rate_limits(ip="127.0.0.1")
    resp_bad = opts.client.post("/api/auth/verify/email/confirm", {"code": "000000"})
    assert_true(resp_bad.status_code in (400, 422),
                f"Wrong code must be rejected, got {resp_bad.status_code}")

    # Then: the real code must still work
    clear_rate_limits(ip="127.0.0.1")
    resp_good = opts.client.post("/api/auth/verify/email/confirm", {"code": code})
    opts.client.logout()
    assert_eq(resp_good.status_code, 200,
              f"Valid code must still work after a wrong guess, got {resp_good.status_code}")

    user.refresh_from_db()
    assert_true(user.is_email_verified,
                "is_email_verified must be True after correct code following a wrong guess")


# ===========================================================================
# Cleanup
# ===========================================================================

@th.django_unit_setup()
def cleanup_verification(opts):
    from mojo.apps.account.models import User
    User.objects.filter(pk=opts.user_id).delete()
    User.objects.filter(pk=opts.user_b_id).delete()
    User.objects.filter(pk=opts.sms_user_id).delete()