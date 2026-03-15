"""
Tests for the self-service email-change flow — security and correctness.

Security contract this file enforces:
  - ec: token has the correct kind prefix
  - pending_email is stored in secrets and returned by verify
  - ec: token is single-use
  - ec: token is rejected by every other kind verifier (and vice-versa)
  - Expired ec: tokens are rejected
  - auth_key rotation immediately invalidates an outstanding ec: token
  - Re-requesting a change invalidates any previously issued ec: token
  - Confirm endpoint commits new email, sets is_email_verified, rotates auth_key
  - Confirm mirrors username when it was the old email address
  - Confirm re-checks availability (race: another account claimed the address)
  - Inactive accounts are blocked at confirm time
  - Cancel clears pending_email AND the ec: JTI so the link is dead immediately
  - Cancel with no pending change is a safe no-op
  - Request endpoint: wrong password returns 401 (no 403/account-existence leak)
  - Request endpoint: same-email rejected before token is issued
  - Request endpoint: duplicate email rejected
  - Request endpoint: ALLOW_EMAIL_CHANGE=False blocks the entire flow
  - Request endpoint: requires authentication
"""
from testit import helpers as th
from testit.helpers import assert_true, assert_eq

TEST_USER = "email_change_user"
TEST_PWORD = "change##mojo99"
TEST_NEW_EMAIL = "email_change_new@example.com"


# ===========================================================================
# Setup / teardown
# ===========================================================================

@th.django_unit_setup()
def setup_email_change(opts):
    from mojo.apps.account.models import User
    from mojo.decorators.limits import clear_rate_limits
    clear_rate_limits(ip="127.0.0.1")

    # Primary test user
    user = User.objects.filter(username=TEST_USER).last()
    if user is None:
        user = User(username=TEST_USER, email=f"{TEST_USER}@example.com")
        user.save()
    user.is_active = True
    user.is_email_verified = True
    user.requires_mfa = False
    user.save_password(TEST_PWORD)
    user.save()
    opts.user_id = user.pk
    opts.original_email = str(user.email)
    opts.original_username = str(user.username)

    # Collision user — owns TEST_NEW_EMAIL so we can test duplicate rejection
    collision = User.objects.filter(username="email_change_collision").last()
    if collision is None:
        collision = User(username="email_change_collision", email=TEST_NEW_EMAIL)
        collision.save()
    collision.is_active = True
    collision.save_password(TEST_PWORD)
    collision.save()
    opts.collision_id = collision.pk

    # Clean up any leftover pending state from a previous run
    user.set_secret("pending_email", None)
    import mojo.apps.account.utils.tokens as tok_module
    user.set_secret(tok_module._JTI_KEYS[tok_module.KIND_EMAIL_CHANGE], None)
    user.save(update_fields=["mojo_secrets", "modified"])


# ===========================================================================
# Token unit tests
# ===========================================================================

@th.django_unit_test("ec token: has ec: prefix")
def test_ec_token_prefix(opts):
    from mojo.apps.account.models import User
    from mojo.apps.account.utils import tokens

    user = User.objects.get(pk=opts.user_id)
    tok = tokens.generate_email_change_token(user, "someother@example.com")
    assert_true(tok.startswith("ec:"), f"Expected 'ec:' prefix, got: {tok[:10]}")
    # consume cleanly
    tokens.verify_email_change_token(tok)


@th.django_unit_test("ec token: pending_email stored in secrets during generate")
def test_ec_pending_email_stored(opts):
    from mojo.apps.account.models import User
    from mojo.apps.account.utils import tokens

    user = User.objects.get(pk=opts.user_id)
    tokens.generate_email_change_token(user, "stored_check@example.com")
    user.refresh_from_db()
    pending = user.get_secret("pending_email")
    assert_eq(pending, "stored_check@example.com", "pending_email must be stored in secrets after generate")
    # consume
    tokens.verify_email_change_token(
        tokens.generate_email_change_token(user, "stored_check@example.com")
    )


@th.django_unit_test("ec token: verify returns (user, new_email) tuple")
def test_ec_verify_returns_tuple(opts):
    from mojo.apps.account.models import User
    from mojo.apps.account.utils import tokens

    user = User.objects.get(pk=opts.user_id)
    new_email = "tuple_check@example.com"
    tok = tokens.generate_email_change_token(user, new_email)
    result = tokens.verify_email_change_token(tok)
    assert_true(isinstance(result, tuple) and len(result) == 2, "verify must return (user, new_email) tuple")
    returned_user, returned_email = result
    assert_eq(returned_user.pk, user.pk, "returned user pk must match")
    assert_eq(returned_email, new_email, "returned new_email must match what was stored")


@th.django_unit_test("ec token: pending_email cleared from secrets after verify (single-use data)")
def test_ec_pending_email_cleared_after_verify(opts):
    from mojo.apps.account.models import User
    from mojo.apps.account.utils import tokens

    user = User.objects.get(pk=opts.user_id)
    tok = tokens.generate_email_change_token(user, "clear_check@example.com")
    tokens.verify_email_change_token(tok)
    user.refresh_from_db()
    assert_eq(user.get_secret("pending_email"), None, "pending_email must be cleared after verify")


@th.django_unit_test("ec token: is single-use — second verify raises")
def test_ec_token_single_use(opts):
    from mojo.apps.account.models import User
    from mojo.apps.account.utils import tokens
    from mojo import errors as merrors

    user = User.objects.get(pk=opts.user_id)
    tok = tokens.generate_email_change_token(user, "single_use@example.com")
    tokens.verify_email_change_token(tok)

    raised = False
    try:
        tokens.verify_email_change_token(tok)
    except merrors.ValueException:
        raised = True
    assert_true(raised, "Reusing an ec: token must raise ValueException")


@th.django_unit_test("ec token: rejected by verify_email_verify_token (kind mismatch)")
def test_ec_rejected_by_ev_verifier(opts):
    from mojo.apps.account.models import User
    from mojo.apps.account.utils import tokens
    from mojo import errors as merrors

    user = User.objects.get(pk=opts.user_id)
    ec_tok = tokens.generate_email_change_token(user, "mismatch_ev@example.com")

    raised = False
    try:
        tokens.verify_email_verify_token(ec_tok)
    except merrors.ValueException:
        raised = True
    assert_true(raised, "verify_email_verify_token must reject a token with kind 'ec'")

    # consume so JTI is not left poisoned
    try:
        tokens.verify_email_change_token(ec_tok)
    except merrors.ValueException:
        pass


@th.django_unit_test("ec token: rejected by verify_invite_token (kind mismatch)")
def test_ec_rejected_by_iv_verifier(opts):
    from mojo.apps.account.models import User
    from mojo.apps.account.utils import tokens
    from mojo import errors as merrors

    user = User.objects.get(pk=opts.user_id)
    ec_tok = tokens.generate_email_change_token(user, "mismatch_iv@example.com")

    raised = False
    try:
        tokens.verify_invite_token(ec_tok)
    except merrors.ValueException:
        raised = True
    assert_true(raised, "verify_invite_token must reject a token with kind 'ec'")

    try:
        tokens.verify_email_change_token(ec_tok)
    except merrors.ValueException:
        pass


@th.django_unit_test("ec token: ev token rejected by verify_email_change_token (kind mismatch)")
def test_ev_rejected_by_ec_verifier(opts):
    from mojo.apps.account.models import User
    from mojo.apps.account.utils import tokens
    from mojo import errors as merrors

    user = User.objects.get(pk=opts.user_id)
    ev_tok = tokens.generate_email_verify_token(user)

    raised = False
    try:
        tokens.verify_email_change_token(ev_tok)
    except merrors.ValueException:
        raised = True
    assert_true(raised, "verify_email_change_token must reject a token with kind 'ev'")

    try:
        tokens.verify_email_verify_token(ev_tok)
    except merrors.ValueException:
        pass


@th.django_unit_test("ec token: expired token is rejected")
def test_ec_token_expired(opts):
    from mojo.apps.account.models import User
    from mojo.apps.account.utils import tokens
    import mojo.apps.account.utils.tokens as tok_module
    from mojo import errors as merrors

    user = User.objects.get(pk=opts.user_id)
    orig_ttl = tok_module._TTL[tok_module.KIND_EMAIL_CHANGE]
    tok_module._TTL[tok_module.KIND_EMAIL_CHANGE] = -1
    try:
        tok = tokens.generate_email_change_token(user, "expired@example.com")
        raised = False
        try:
            tokens.verify_email_change_token(tok)
        except merrors.ValueException:
            raised = True
        assert_true(raised, "Expired ec: token must raise ValueException")
    finally:
        tok_module._TTL[tok_module.KIND_EMAIL_CHANGE] = orig_ttl
        user.set_secret(tok_module._JTI_KEYS[tok_module.KIND_EMAIL_CHANGE], None)
        user.set_secret("pending_email", None)
        user.save(update_fields=["mojo_secrets", "modified"])


@th.django_unit_test("ec token: auth_key rotation immediately invalidates outstanding token")
def test_ec_auth_key_rotation_invalidates(opts):
    import uuid
    from mojo.apps.account.models import User
    from mojo.apps.account.utils import tokens
    import mojo.apps.account.utils.tokens as tok_module
    from mojo import errors as merrors

    user = User.objects.get(pk=opts.user_id)
    tok = tokens.generate_email_change_token(user, "rotate_test@example.com")
    old_auth_key = user.auth_key

    User.objects.filter(pk=user.pk).update(auth_key=uuid.uuid4().hex)

    raised = False
    try:
        tokens.verify_email_change_token(tok)
    except merrors.ValueException:
        raised = True
    assert_true(raised, "ec: token must be invalid after auth_key rotation")

    # restore
    User.objects.filter(pk=user.pk).update(auth_key=old_auth_key)
    user.refresh_from_db()
    user.set_secret(tok_module._JTI_KEYS[tok_module.KIND_EMAIL_CHANGE], None)
    user.set_secret("pending_email", None)
    user.save(update_fields=["mojo_secrets", "modified"])


@th.django_unit_test("ec token: re-requesting a change invalidates the previous token")
def test_ec_rerequest_invalidates_previous_token(opts):
    from mojo.apps.account.models import User
    from mojo.apps.account.utils import tokens
    from mojo import errors as merrors

    user = User.objects.get(pk=opts.user_id)
    first_tok = tokens.generate_email_change_token(user, "first_req@example.com")
    second_tok = tokens.generate_email_change_token(user, "second_req@example.com")

    raised = False
    try:
        tokens.verify_email_change_token(first_tok)
    except merrors.ValueException:
        raised = True
    assert_true(raised, "First ec: token must be invalid after a second one is generated")

    # consume second cleanly
    try:
        tokens.verify_email_change_token(second_tok)
    except merrors.ValueException:
        pass


@th.django_unit_test("ec token: garbage strings are always rejected")
def test_ec_garbage_rejected(opts):
    from mojo.apps.account.utils import tokens
    from mojo import errors as merrors

    for bad in ["", "notavalidtoken", "ec:", "ec:zzzz", "xx:deadbeef", "ev:faketoken", "   "]:
        raised = False
        try:
            tokens.verify_email_change_token(bad)
        except (merrors.ValueException, Exception):
            raised = True
        assert_true(raised, f"Garbage token {bad!r} must be rejected")


# ===========================================================================
# REST: POST /api/auth/email/change/request
# ===========================================================================

@th.django_unit_test("email/change/request: happy path returns 200 with message")
def test_request_happy_path(opts):
    from mojo.apps.account.models import User
    from mojo.decorators.limits import clear_rate_limits
    clear_rate_limits(ip="127.0.0.1")

    # Use a unique email not owned by anyone
    opts.client.login(TEST_USER, TEST_PWORD)
    resp = opts.client.post(
        "/api/auth/email/change/request",
        {"email": "happy_req@example.com", "current_password": TEST_PWORD},
    )
    opts.client.logout()
    assert_eq(resp.status_code, 200, f"Expected 200, got {resp.status_code}")
    data = resp.json
    assert_true(data.get("status") is True, "Response status must be True")
    assert_true("message" in data, "Response must include a message field")

    # Clean up pending state
    user = User.objects.get(pk=opts.user_id)
    import mojo.apps.account.utils.tokens as tok_module
    user.set_secret("pending_email", None)
    user.set_secret(tok_module._JTI_KEYS[tok_module.KIND_EMAIL_CHANGE], None)
    user.save(update_fields=["mojo_secrets", "modified"])


@th.django_unit_test("email/change/request: pending_email stored after request")
def test_request_stores_pending_email(opts):
    from mojo.apps.account.models import User
    from mojo.decorators.limits import clear_rate_limits
    clear_rate_limits(ip="127.0.0.1")

    new_email = "pending_store@example.com"
    opts.client.login(TEST_USER, TEST_PWORD)
    opts.client.post(
        "/api/auth/email/change/request",
        {"email": new_email, "current_password": TEST_PWORD},
    )
    opts.client.logout()
    user = User.objects.get(pk=opts.user_id)
    assert_eq(user.get_secret("pending_email"), new_email, "pending_email must be stored after request")

    import mojo.apps.account.utils.tokens as tok_module
    user.set_secret("pending_email", None)
    user.set_secret(tok_module._JTI_KEYS[tok_module.KIND_EMAIL_CHANGE], None)
    user.save(update_fields=["mojo_secrets", "modified"])


@th.django_unit_test("email/change/request: requires authentication — 401 without token")
def test_request_requires_auth(opts):
    from mojo.decorators.limits import clear_rate_limits
    clear_rate_limits(ip="127.0.0.1")
    opts.client.logout()

    resp = opts.client.post(
        "/api/auth/email/change/request",
        {"email": "no_auth@example.com", "current_password": TEST_PWORD},
    )
    assert_true(resp.status_code in (401, 403), f"Expected 401/403 without auth, got {resp.status_code}")


@th.django_unit_test("email/change/request: wrong password returns 401")
def test_request_wrong_password(opts):
    from mojo.decorators.limits import clear_rate_limits
    clear_rate_limits(ip="127.0.0.1")

    opts.client.login(TEST_USER, TEST_PWORD)
    resp = opts.client.post(
        "/api/auth/email/change/request",
        {"email": "wrongpw@example.com", "current_password": "definitely_wrong_pw"},
    )
    opts.client.logout()
    assert_eq(resp.status_code, 401, f"Wrong password must return 401, got {resp.status_code}")


@th.django_unit_test("email/change/request: missing current_password returns 400")
def test_request_missing_password(opts):
    from mojo.decorators.limits import clear_rate_limits
    clear_rate_limits(ip="127.0.0.1")

    opts.client.login(TEST_USER, TEST_PWORD)
    resp = opts.client.post(
        "/api/auth/email/change/request",
        {"email": "nopw@example.com"},
    )
    opts.client.logout()
    assert_true(resp.status_code in (400, 422), f"Missing current_password must return 4xx, got {resp.status_code}")


@th.django_unit_test("email/change/request: same email as current is rejected")
def test_request_same_email_rejected(opts):
    from mojo.apps.account.models import User
    from mojo.decorators.limits import clear_rate_limits
    clear_rate_limits(ip="127.0.0.1")

    user = User.objects.get(pk=opts.user_id)
    current_email = str(user.email)

    opts.client.login(TEST_USER, TEST_PWORD)
    resp = opts.client.post(
        "/api/auth/email/change/request",
        {"email": current_email, "current_password": TEST_PWORD},
    )
    opts.client.logout()
    assert_true(resp.status_code in (400, 422), f"Same-email change must be rejected, got {resp.status_code}")


@th.django_unit_test("email/change/request: duplicate email (owned by another account) is rejected")
def test_request_duplicate_email_rejected(opts):
    from mojo.decorators.limits import clear_rate_limits
    clear_rate_limits(ip="127.0.0.1")

    # TEST_NEW_EMAIL is owned by the collision user created in setup
    opts.client.login(TEST_USER, TEST_PWORD)
    resp = opts.client.post(
        "/api/auth/email/change/request",
        {"email": TEST_NEW_EMAIL, "current_password": TEST_PWORD},
    )
    opts.client.logout()
    assert_true(resp.status_code in (400, 422), f"Duplicate email must be rejected, got {resp.status_code}")


@th.django_unit_test("email/change/request: invalid email format is rejected")
def test_request_invalid_email_format(opts):
    from mojo.decorators.limits import clear_rate_limits
    clear_rate_limits(ip="127.0.0.1")

    opts.client.login(TEST_USER, TEST_PWORD)
    for bad_email in ["notanemail", "@nodomain", "missing@", ""]:
        resp = opts.client.post(
            "/api/auth/email/change/request",
            {"email": bad_email, "current_password": TEST_PWORD},
        )
        assert_true(
            resp.status_code in (400, 422),
            f"Invalid email {bad_email!r} must be rejected, got {resp.status_code}",
        )
    opts.client.logout()


@th.django_unit_test("email/change/request: ALLOW_EMAIL_CHANGE=False blocks the endpoint")
def test_request_disallowed_by_setting(opts):
    from testit.helpers import TestitSkip
    from mojo.helpers.settings import settings
    from mojo.decorators.limits import clear_rate_limits
    clear_rate_limits(ip="127.0.0.1")

    if settings.get("ALLOW_EMAIL_CHANGE", True):
        raise TestitSkip("requires ALLOW_EMAIL_CHANGE=False in settings — set it and restart the server to run this test")

    opts.client.login(TEST_USER, TEST_PWORD)
    resp = opts.client.post(
        "/api/auth/email/change/request",
        {"email": "blocked@example.com", "current_password": TEST_PWORD},
    )
    opts.client.logout()
    assert_true(
        resp.status_code in (400, 403),
        f"ALLOW_EMAIL_CHANGE=False must block the request, got {resp.status_code}",
    )


# ===========================================================================
# REST: POST /api/auth/email/change/confirm
# ===========================================================================

@th.django_unit_test("email/change/confirm: happy path commits new email and returns JWT")
def test_confirm_happy_path(opts):
    from mojo.apps.account.models import User
    from mojo.apps.account.utils import tokens
    from mojo.decorators.limits import clear_rate_limits
    clear_rate_limits(ip="127.0.0.1")

    user = User.objects.get(pk=opts.user_id)
    # Reset to known original email in case a previous test changed it
    User.objects.filter(pk=user.pk).update(
        email=opts.original_email,
        username=opts.original_username,
    )
    user.refresh_from_db()

    new_email = "confirm_happy@example.com"
    tok = tokens.generate_email_change_token(user, new_email)

    resp = opts.client.post("/api/auth/email/change/confirm", {"token": tok})
    assert_eq(resp.status_code, 200, f"Confirm must return 200, got {resp.status_code}: {resp.content}")
    data = resp.json
    assert_true(data.get("status") is True, "Response status must be True")
    assert_true("data" in data, "Response must contain JWT data envelope")

    user.refresh_from_db()
    assert_eq(str(user.email), new_email, "user.email must be updated to new_email after confirm")
    assert_true(user.is_email_verified, "is_email_verified must be True after confirm")

    # Restore for subsequent tests
    User.objects.filter(pk=user.pk).update(
        email=opts.original_email,
        username=opts.original_username,
    )


@th.django_unit_test("email/change/confirm: auth_key is rotated (old sessions invalidated)")
def test_confirm_rotates_auth_key(opts):
    from mojo.apps.account.models import User
    from mojo.apps.account.utils import tokens
    from mojo.decorators.limits import clear_rate_limits
    clear_rate_limits(ip="127.0.0.1")

    User.objects.filter(pk=opts.user_id).update(
        email=opts.original_email,
        username=opts.original_username,
    )
    user = User.objects.get(pk=opts.user_id)
    old_auth_key = user.auth_key

    tok = tokens.generate_email_change_token(user, "rotatekey@example.com")
    opts.client.post("/api/auth/email/change/confirm", {"token": tok})

    user.refresh_from_db()
    assert_true(
        user.auth_key != old_auth_key,
        "auth_key must be rotated after email change confirm to invalidate old sessions",
    )

    # Restore
    User.objects.filter(pk=user.pk).update(
        email=opts.original_email,
        username=opts.original_username,
    )


@th.django_unit_test("email/change/confirm: username mirrored when it matched old email")
def test_confirm_mirrors_username(opts):
    from mojo.apps.account.models import User
    from mojo.apps.account.utils import tokens
    from mojo.decorators.limits import clear_rate_limits
    clear_rate_limits(ip="127.0.0.1")

    # Force username == email so the mirror logic fires
    mirror_email = "mirror_old@example.com"
    User.objects.filter(pk=opts.user_id).update(
        email=mirror_email,
        username=mirror_email,
    )
    user = User.objects.get(pk=opts.user_id)
    assert_eq(str(user.username).lower(), str(user.email).lower(), "precondition: username must equal email")

    new_email = "mirror_new@example.com"
    tok = tokens.generate_email_change_token(user, new_email)
    opts.client.post("/api/auth/email/change/confirm", {"token": tok})

    user.refresh_from_db()
    assert_eq(str(user.email), new_email, "email must be updated")
    assert_eq(str(user.username), new_email, "username must be mirrored to new_email when it matched old email")

    # Restore
    User.objects.filter(pk=user.pk).update(
        email=opts.original_email,
        username=opts.original_username,
    )


@th.django_unit_test("email/change/confirm: username NOT mirrored when it differed from old email")
def test_confirm_does_not_mirror_unrelated_username(opts):
    from mojo.apps.account.models import User
    from mojo.apps.account.utils import tokens
    from mojo.decorators.limits import clear_rate_limits
    clear_rate_limits(ip="127.0.0.1")

    distinct_username = "distinct_username_handle"
    User.objects.filter(pk=opts.user_id).update(
        email=opts.original_email,
        username=distinct_username,
    )
    user = User.objects.get(pk=opts.user_id)

    tok = tokens.generate_email_change_token(user, "nomirror_new@example.com")
    opts.client.post("/api/auth/email/change/confirm", {"token": tok})

    user.refresh_from_db()
    assert_eq(str(user.username), distinct_username, "username must NOT change when it differs from old email")

    # Restore
    User.objects.filter(pk=user.pk).update(
        email=opts.original_email,
        username=opts.original_username,
    )


@th.django_unit_test("email/change/confirm: inactive user is blocked (403)")
def test_confirm_inactive_user_blocked(opts):
    from mojo.apps.account.models import User
    from mojo.apps.account.utils import tokens
    from mojo.decorators.limits import clear_rate_limits
    clear_rate_limits(ip="127.0.0.1")

    user = User.objects.get(pk=opts.user_id)
    tok = tokens.generate_email_change_token(user, "inactive_confirm@example.com")

    # Deactivate after token generation
    User.objects.filter(pk=user.pk).update(is_active=False)

    resp = opts.client.post("/api/auth/email/change/confirm", {"token": tok})
    assert_eq(resp.status_code, 403, f"Inactive user must receive 403 at confirm, got {resp.status_code}")

    # Restore
    User.objects.filter(pk=user.pk).update(is_active=True)
    user.refresh_from_db()
    import mojo.apps.account.utils.tokens as tok_module
    user.set_secret(tok_module._JTI_KEYS[tok_module.KIND_EMAIL_CHANGE], None)
    user.set_secret("pending_email", None)
    user.save(update_fields=["mojo_secrets", "modified"])


@th.django_unit_test("email/change/confirm: email claimed by another account in the interim is rejected")
def test_confirm_race_email_claimed(opts):
    from mojo.apps.account.models import User
    from mojo.apps.account.utils import tokens
    from mojo.decorators.limits import clear_rate_limits
    clear_rate_limits(ip="127.0.0.1")

    user = User.objects.get(pk=opts.user_id)
    # Issue token for TEST_NEW_EMAIL
    tok = tokens.generate_email_change_token(user, TEST_NEW_EMAIL)
    # At this point collision user already owns TEST_NEW_EMAIL (created in setup)

    resp = opts.client.post("/api/auth/email/change/confirm", {"token": tok})
    assert_true(
        resp.status_code in (400, 409, 422),
        f"Confirm must reject an email claimed by another account, got {resp.status_code}",
    )

    # user.email must be unchanged
    user.refresh_from_db()
    assert_eq(str(user.email), opts.original_email, "email must not have changed after a rejected confirm")


@th.django_unit_test("email/change/confirm: token is single-use — second call rejected")
def test_confirm_token_single_use(opts):
    from mojo.apps.account.models import User
    from mojo.apps.account.utils import tokens
    from mojo.decorators.limits import clear_rate_limits
    clear_rate_limits(ip="127.0.0.1")

    User.objects.filter(pk=opts.user_id).update(
        email=opts.original_email,
        username=opts.original_username,
    )
    user = User.objects.get(pk=opts.user_id)
    tok = tokens.generate_email_change_token(user, "single_use_confirm@example.com")

    resp1 = opts.client.post("/api/auth/email/change/confirm", {"token": tok})
    assert_eq(resp1.status_code, 200, f"First confirm must succeed, got {resp1.status_code}")

    clear_rate_limits(ip="127.0.0.1")
    resp2 = opts.client.post("/api/auth/email/change/confirm", {"token": tok})
    assert_true(resp2.status_code in (400, 422), f"Second confirm must be rejected, got {resp2.status_code}")

    # Restore
    User.objects.filter(pk=user.pk).update(
        email=opts.original_email,
        username=opts.original_username,
    )


@th.django_unit_test("email/change/confirm: ev token rejected (wrong kind)")
def test_confirm_rejects_ev_token(opts):
    from mojo.apps.account.models import User
    from mojo.apps.account.utils import tokens
    from mojo.decorators.limits import clear_rate_limits
    clear_rate_limits(ip="127.0.0.1")

    user = User.objects.get(pk=opts.user_id)
    ev_tok = tokens.generate_email_verify_token(user)

    resp = opts.client.post("/api/auth/email/change/confirm", {"token": ev_tok})
    assert_true(resp.status_code in (400, 422), f"ev: token must be rejected by confirm endpoint, got {resp.status_code}")

    # consume ev token cleanly
    try:
        tokens.verify_email_verify_token(ev_tok)
    except Exception:
        pass


@th.django_unit_test("email/change/confirm: missing token param returns 4xx")
def test_confirm_missing_token(opts):
    from mojo.decorators.limits import clear_rate_limits
    clear_rate_limits(ip="127.0.0.1")

    resp = opts.client.post("/api/auth/email/change/confirm", {})
    assert_true(resp.status_code in (400, 422), f"Missing token must return 4xx, got {resp.status_code}")


# ===========================================================================
# REST: POST /api/auth/email/change/cancel
# ===========================================================================

@th.django_unit_test("email/change/cancel: cancels pending change — pending_email cleared")
def test_cancel_clears_pending_email(opts):
    from mojo.apps.account.models import User
    from mojo.apps.account.utils import tokens
    from mojo.decorators.limits import clear_rate_limits
    clear_rate_limits(ip="127.0.0.1")

    user = User.objects.get(pk=opts.user_id)
    tokens.generate_email_change_token(user, "to_cancel@example.com")
    user.refresh_from_db()
    assert_eq(user.get_secret("pending_email"), "to_cancel@example.com", "precondition: pending_email must be set")

    opts.client.login(TEST_USER, TEST_PWORD)
    resp = opts.client.post("/api/auth/email/change/cancel", {})
    opts.client.logout()
    assert_eq(resp.status_code, 200, f"Cancel must return 200, got {resp.status_code}")
    data = resp.json
    assert_true(data.get("status") is True, "Cancel response status must be True")

    user.refresh_from_db()
    assert_eq(user.get_secret("pending_email"), None, "pending_email must be None after cancel")


@th.django_unit_test("email/change/cancel: cancels JTI so outstanding ec: token is dead")
def test_cancel_kills_ec_token(opts):
    from mojo.apps.account.models import User
    from mojo.apps.account.utils import tokens
    from mojo import errors as merrors
    from mojo.decorators.limits import clear_rate_limits
    clear_rate_limits(ip="127.0.0.1")

    user = User.objects.get(pk=opts.user_id)
    tok = tokens.generate_email_change_token(user, "jti_kill@example.com")

    # Cancel the pending change
    opts.client.login(TEST_USER, TEST_PWORD)
    opts.client.post("/api/auth/email/change/cancel", {})
    opts.client.logout()

    # The outstanding token must now be invalid
    clear_rate_limits(ip="127.0.0.1")
    raised = False
    try:
        tokens.verify_email_change_token(tok)
    except merrors.ValueException:
        raised = True
    assert_true(raised, "ec: token must be invalid after cancel clears the JTI")


@th.django_unit_test("email/change/cancel: no pending change is a safe no-op (200)")
def test_cancel_no_pending_is_noop(opts):
    from mojo.apps.account.models import User
    from mojo.decorators.limits import clear_rate_limits
    clear_rate_limits(ip="127.0.0.1")

    # Ensure no pending state
    user = User.objects.get(pk=opts.user_id)
    import mojo.apps.account.utils.tokens as tok_module
    user.set_secret("pending_email", None)
    user.set_secret(tok_module._JTI_KEYS[tok_module.KIND_EMAIL_CHANGE], None)
    user.save(update_fields=["mojo_secrets", "modified"])

    opts.client.login(TEST_USER, TEST_PWORD)
    resp = opts.client.post("/api/auth/email/change/cancel", {})
    opts.client.logout()
    assert_eq(resp.status_code, 200, f"Cancel with no pending change must return 200, got {resp.status_code}")
    data = resp.json
    assert_true(data.get("status") is True, "No-op cancel must still return status True")


@th.django_unit_test("email/change/cancel: requires authentication — 401 without token")
def test_cancel_requires_auth(opts):
    from mojo.decorators.limits import clear_rate_limits
    clear_rate_limits(ip="127.0.0.1")
    opts.client.logout()

    resp = opts.client.post("/api/auth/email/change/cancel", {})
    assert_true(resp.status_code in (401, 403), f"Cancel without auth must return 401/403, got {resp.status_code}")


@th.django_unit_test("email/change/cancel: confirm after cancel is rejected")
def test_cancel_then_confirm_rejected(opts):
    from mojo.apps.account.models import User
    from mojo.apps.account.utils import tokens
    from mojo.decorators.limits import clear_rate_limits
    clear_rate_limits(ip="127.0.0.1")

    user = User.objects.get(pk=opts.user_id)
    tok = tokens.generate_email_change_token(user, "cancel_then_confirm@example.com")

    # Cancel first
    opts.client.login(TEST_USER, TEST_PWORD)
    opts.client.post("/api/auth/email/change/cancel", {})
    opts.client.logout()

    # Attempt to confirm with the now-dead token
    clear_rate_limits(ip="127.0.0.1")
    resp = opts.client.post("/api/auth/email/change/confirm", {"token": tok})
    assert_true(
        resp.status_code in (400, 422),
        f"Confirm after cancel must be rejected, got {resp.status_code}",
    )

    # email must be unchanged
    user.refresh_from_db()
    assert_eq(str(user.email), opts.original_email, "email must not change after cancel+confirm attempt")


# ===========================================================================
# Teardown
# ===========================================================================

@th.django_unit_setup()
def cleanup_email_change(opts):
    from mojo.apps.account.models import User
    User.objects.filter(pk=opts.user_id).delete()
    User.objects.filter(pk=opts.collision_id).delete()