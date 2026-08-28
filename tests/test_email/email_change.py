"""
Tests for the self-service email-change flow — security and correctness.

Security contract this file enforces (link flow):
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

Security contract this file enforces (code/OTP flow):
  - generate_email_change_otp stores pending_email and OTP in secrets
  - OTP is 6 digits numeric, single-use
  - Wrong OTP rejected without consuming the valid code (brute-force safety)
  - Expired OTP rejected
  - generate_email_change_otp clears any outstanding ec: JTI (mutual exclusivity)
  - generate_email_change_token clears any outstanding OTP (mutual exclusivity)
  - method=code on request stores OTP and does NOT store an ec: token
  - method=link (or omitted) on request stores ec: token and clears any OTP
  - Confirm with code requires authentication — identity from JWT, not the code
  - Unauthenticated confirm with code is always rejected
  - Confirm with code commits new email, sets is_email_verified, rotates auth_key, returns JWT
  - Confirm with code checks email availability (race condition)
  - Confirm with code blocks inactive users
  - Confirm with code mirrors username when it matched the old email
  - Cancel clears pending_email, ec: JTI, AND OTP secrets — covers both paths
  - Submitting neither token nor code returns 4xx

Send-truthfulness contract (#3328):
  - Both send helpers RETURN the transport result instead of discarding it
  - A transport exception is swallowed to None and files no email_change: event
  - A send the provider did not accept answers 503 with the fixed safe body,
    files no raw mojo_rest_error, and records email_change:send_failed only
  - A failed send leaves pending_email, the ec: JTI / OTP and the address alone
  - The accepted path still returns 200 and records email_change:requested
    (provable only in-process — the test project ships no mailbox)
  - The old-address notice runs only after acceptance, is skipped when there is
    no old address, and reports its own failure as email_change:notice_failed
  - A successful POST confirm records account-global email_change:confirmed
"""
from testit import helpers as th
from testit.helpers import assert_true, assert_eq

TESTIT_TIER = "extended"

TEST_USER = "email_change_user"
TEST_PWORD = "change##mojo99"
TEST_NEW_EMAIL = "email_change_new@example.com"
EMPTY_EMAIL_USER = "email_change_no_old_addr"


# ===========================================================================
# Setup / teardown
# ===========================================================================

@th.django_unit_setup()
def setup_email_change(opts):
    """Reset both fixture users and clear leftover email-change secrets.

    This module has NO teardown function, by design. The testit runner collects
    functions by name PREFIX only — `setup_` for the setup phase, `test_`/`quick_`
    for the test phase — and has no teardown phase at all, so a `cleanup_*` /
    `teardown_*` function is never collected regardless of how it is decorated
    (see docs/django_developer/testit/Overview.md). Cleanup therefore lives here
    at the top of setup: the two fixture users are reset (not merely created) and
    the leftover pending-email / OTP / JTI secret keys are cleared before any
    test runs, which is what keeps this long-lived database idempotent across
    repeated runs.
    """
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
    user.set_secret("email_change_otp", None)
    user.set_secret("email_change_otp_ts", None)
    user.save(update_fields=["mojo_secrets", "modified"])

    # Activity rows are matched by this user's pk, so clear anything a previous
    # run left behind for it.
    from mojo.apps.incident.models import Event
    Event.objects.filter(
        uid=user.pk, category__startswith="email_change:").delete()

    # No-old-address fixture: proves the notice to the previous address is
    # skipped (rather than attempted against "") on a first-email account.
    empty = User.objects.filter(username=EMPTY_EMAIL_USER).last()
    if empty is None:
        empty = User(username=EMPTY_EMAIL_USER, email="")
        empty.save()
    empty.email = ""
    empty.is_active = True
    empty.requires_mfa = False
    empty.save_password(TEST_PWORD)
    empty.save()
    opts.empty_email_user_id = empty.pk
    Event.objects.filter(
        uid=empty.pk, category__startswith="email_change:").delete()


# ===========================================================================
# Transport fakes — injected through the handlers' keyword-only send seams
# ===========================================================================
#
# tests/test_email is a default_core package whose cold_budget is exactly
# consumed, so no mock.patch may be added here — and `opts.client` reaches a
# SEPARATE server process where a patch would have no effect anyway. Every
# faked send below is passed in explicitly through the keyword-only `send=` /
# `notify_send=` seams the email-change handlers expose, and every SentMessage
# is built UNSAVED: no SES call, no Mailbox row, no database write.

FAKE_SES_ID = "0100018f-emailchange-test"


class _FakeSend:
    """Stand-in for a template-send callable, injected through a seam.

    Accepts both call shapes used here — the mailbox form
    (``to=``/``template_name=``) and the user form (positional template name) —
    records every call, and returns a caller-chosen result. An ``Exception``
    instance as the result is RAISED instead, which is how a transport blow-up
    is simulated without patching anything.
    """

    def __init__(self, result):
        self.result = result
        self.calls = []

    def __call__(self, *args, **kwargs):
        call = dict(kwargs)
        if args:
            call.setdefault("template_name", args[0])
        self.calls.append(call)
        if isinstance(self.result, Exception):
            raise self.result
        return self.result


def _sent(status, ses_message_id=None, status_reason=None):
    """An UNSAVED SentMessage — the real result shape, no DB write, no SES."""
    from mojo.apps.aws.models import SentMessage
    return SentMessage(
        status=status,
        ses_message_id=ses_message_id,
        status_reason=status_reason,
    )


def _accepted_send():
    """A sender the provider took custody of — sending + a message id."""
    return _FakeSend(_sent("sending", ses_message_id=FAKE_SES_ID))


def _refused_send():
    """A sender SES refused — persisted, truthy, but never accepted."""
    return _FakeSend(_sent(
        "failed",
        status_reason="An error occurred (MessageRejected) calling SendEmail"))


def _request_factory_post(payload):
    """A RequestFactory POST carrying the attributes mojo middleware adds."""
    from django.test import RequestFactory
    from objict import objict

    factory = RequestFactory(REMOTE_ADDR="127.0.0.1")
    request = factory.post("/api/auth/email/change/request", payload or {})
    request.DATA = objict.from_dict(payload or {})
    return request


def _call_request_endpoint(opts, payload, send=None, notify_send=None, user_id=None):
    """Drive the real (decorated) request view in-process with injected sends."""
    from mojo.apps.account.models import User
    from mojo.apps.account.rest import user as user_rest
    from mojo.decorators.limits import clear_rate_limits

    clear_rate_limits(ip="127.0.0.1")
    request = _request_factory_post(payload)
    request.user = User.objects.get(pk=user_id or opts.user_id)
    return user_rest.on_email_change_request(
        request, send=send, notify_send=notify_send)


def _activity(uid, category):
    from mojo.apps.incident.models import Event
    return Event.objects.filter(uid=uid, category=category)


def _clear_email_change_state(opts, user_id=None):
    """Drop pending email-change secrets and this user's email_change: rows."""
    from mojo.apps.account.models import User
    from mojo.apps.incident.models import Event
    import mojo.apps.account.utils.tokens as tok_module

    uid = user_id or opts.user_id
    user = User.objects.get(pk=uid)
    user.set_secret("pending_email", None)
    user.set_secret(tok_module._JTI_KEYS[tok_module.KIND_EMAIL_CHANGE], None)
    user.set_secret("email_change_otp", None)
    user.set_secret("email_change_otp_ts", None)
    user.save(update_fields=["mojo_secrets", "modified"])
    Event.objects.filter(uid=uid, category__startswith="email_change:").delete()


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


@th.tier("core")
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


@th.tier("core")
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

@th.tier("bug")
@th.django_unit_test("email/change/request: an unaccepted send reports an honest 503, not a cheerful 200")
def test_request_unaccepted_send_reports_failure(opts):
    """Retargeted from the old 200-asserting happy path (#3328).

    The test project ships no mailbox, so nothing this request sends can ever
    be accepted — which used to produce "a confirmation link has been sent".
    """
    from mojo.apps.account.services.email_delivery import EMAIL_SEND_UNAVAILABLE
    from mojo.decorators.limits import clear_rate_limits
    clear_rate_limits(ip="127.0.0.1")

    try:
        opts.client.login(TEST_USER, TEST_PWORD)
        resp = opts.client.post(
            "/api/auth/email/change/request",
            {"email": "happy_req@example.com", "current_password": TEST_PWORD},
        )
        opts.client.logout()
        assert_eq(resp.status_code, 503,
                  f"An unaccepted send must not be reported as sent, got {resp.status_code}")
        assert_eq(resp.json,
                  {"status": False, "code": 503, "error": EMAIL_SEND_UNAVAILABLE},
                  "The 503 body must be exactly the fixed safe-retry payload")
    finally:
        _clear_email_change_state(opts)


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


@th.tier("core")
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


@th.django_unit_test("email/change/request: no current_password still passes the gates and reaches the send (OAuth/passkey users)")
def test_request_no_password_reaches_send(opts):
    """Retargeted (#3328): the password gate is still skipped when no password
    is supplied — the request reaches the send and fails there (503) rather
    than being refused at the gate (401)."""
    from mojo.decorators.limits import clear_rate_limits
    clear_rate_limits(ip="127.0.0.1")

    try:
        opts.client.login(TEST_USER, TEST_PWORD)
        resp = opts.client.post(
            "/api/auth/email/change/request",
            {"email": "no_pw_test@example.com"},
        )
        opts.client.logout()
        assert_eq(resp.status_code, 503,
                  f"Omitting current_password must skip the password gate and reach "
                  f"the send (503 with no mailbox), got {resp.status_code}")
    finally:
        _clear_email_change_state(opts)


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


@th.tier("core")
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


@th.tier("core")
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

@th.django_unit_test("email/change/cancel: cancels pending link change — pending_email and JTI cleared")
def test_cancel_clears_pending_email(opts):
    from mojo.apps.account.models import User
    from mojo.apps.account.utils import tokens
    import mojo.apps.account.utils.tokens as tok_module
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
    assert_eq(user.get_secret(tok_module._JTI_KEYS[tok_module.KIND_EMAIL_CHANGE]), None,
              "ec: JTI must be None after cancel")


@th.django_unit_test("email/change/cancel: cancels pending code change — pending_email and OTP cleared")
def test_cancel_clears_pending_otp(opts):
    from mojo.apps.account.models import User
    from mojo.apps.account.utils import tokens
    from mojo.decorators.limits import clear_rate_limits
    clear_rate_limits(ip="127.0.0.1")

    user = User.objects.get(pk=opts.user_id)
    tokens.generate_email_change_otp(user, "otp_cancel@example.com")
    user.refresh_from_db()
    assert_eq(user.get_secret("pending_email"), "otp_cancel@example.com",
              "precondition: pending_email must be set by OTP flow")
    assert_true(user.get_secret("email_change_otp") is not None,
                "precondition: email_change_otp must be set")

    opts.client.login(TEST_USER, TEST_PWORD)
    resp = opts.client.post("/api/auth/email/change/cancel", {})
    opts.client.logout()
    assert_eq(resp.status_code, 200, f"Cancel must return 200, got {resp.status_code}")

    user.refresh_from_db()
    assert_eq(user.get_secret("pending_email"), None, "pending_email must be None after cancel")
    assert_eq(user.get_secret("email_change_otp"), None, "email_change_otp must be None after cancel")
    assert_eq(user.get_secret("email_change_otp_ts"), None, "email_change_otp_ts must be None after cancel")


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
# OTP / code flow — token unit tests
# ===========================================================================

@th.django_unit_test("email change OTP: generate stores pending_email and 6-digit code in secrets")
def test_email_change_otp_stored_in_secrets(opts):
    from mojo.apps.account.models import User
    from mojo.apps.account.utils import tokens

    user = User.objects.get(pk=opts.user_id)
    otp = tokens.generate_email_change_otp(user, "otp_stored@example.com")
    user.refresh_from_db()
    assert_eq(user.get_secret("pending_email"), "otp_stored@example.com",
              "pending_email must be stored after generate_email_change_otp")
    assert_eq(user.get_secret("email_change_otp"), otp,
              "OTP must be stored in secrets after generate")
    assert_true(user.get_secret("email_change_otp_ts") is not None,
                "OTP timestamp must be stored after generate")
    assert_true(len(otp) == 6 and otp.isdigit(),
                f"OTP must be a 6-digit numeric string, got: {otp!r}")
    # consume cleanly
    tokens.verify_email_change_otp(user, otp)


@th.django_unit_test("email change OTP: correct code returns new_email and clears secrets (single-use)")
def test_email_change_otp_single_use(opts):
    from mojo.apps.account.models import User
    from mojo.apps.account.utils import tokens
    from mojo import errors as merrors

    user = User.objects.get(pk=opts.user_id)
    otp = tokens.generate_email_change_otp(user, "otp_single@example.com")
    result = tokens.verify_email_change_otp(user, otp)
    assert_eq(result, "otp_single@example.com", "verify must return the pending new_email")

    user.refresh_from_db()
    assert_eq(user.get_secret("email_change_otp"), None, "OTP must be cleared after successful verify")
    assert_eq(user.get_secret("pending_email"), None, "pending_email must be cleared after successful verify")

    raised = False
    try:
        tokens.verify_email_change_otp(user, otp)
    except merrors.ValueException:
        raised = True
    assert_true(raised, "Reusing an email change OTP must raise ValueException")


@th.django_unit_test("email change OTP: wrong code rejected without consuming the valid OTP")
def test_email_change_otp_wrong_code_does_not_consume(opts):
    from mojo.apps.account.models import User
    from mojo.apps.account.utils import tokens
    from mojo import errors as merrors

    user = User.objects.get(pk=opts.user_id)
    otp = tokens.generate_email_change_otp(user, "otp_noburn@example.com")

    raised = False
    try:
        tokens.verify_email_change_otp(user, "000000")
    except merrors.ValueException:
        raised = True
    assert_true(raised, "Wrong OTP must raise ValueException")

    # Valid OTP must still work after a wrong guess
    user.refresh_from_db()
    result = tokens.verify_email_change_otp(user, otp)
    assert_eq(result, "otp_noburn@example.com", "Valid OTP must still work after a wrong guess")


@th.django_unit_test("email change OTP: expired OTP is rejected")
def test_email_change_otp_expired(opts):
    from mojo.apps.account.models import User
    from mojo.apps.account.utils import tokens
    import mojo.apps.account.utils.tokens as tok_module
    from mojo import errors as merrors

    user = User.objects.get(pk=opts.user_id)
    orig_ttl = tok_module.EMAIL_CHANGE_CODE_TTL
    tok_module.EMAIL_CHANGE_CODE_TTL = -1
    try:
        otp = tokens.generate_email_change_otp(user, "otp_expired@example.com")
        raised = False
        try:
            tokens.verify_email_change_otp(user, otp)
        except merrors.ValueException:
            raised = True
        assert_true(raised, "Expired email change OTP must raise ValueException")
    finally:
        tok_module.EMAIL_CHANGE_CODE_TTL = orig_ttl
        user.set_secret("pending_email", None)
        user.set_secret("email_change_otp", None)
        user.set_secret("email_change_otp_ts", None)
        user.save(update_fields=["mojo_secrets", "modified"])


@th.django_unit_test("email change OTP: no pending state raises ValueException")
def test_email_change_otp_no_pending_state(opts):
    from mojo.apps.account.models import User
    from mojo.apps.account.utils import tokens
    from mojo import errors as merrors

    user = User.objects.get(pk=opts.user_id)
    user.set_secret("pending_email", None)
    user.set_secret("email_change_otp", None)
    user.set_secret("email_change_otp_ts", None)
    user.save(update_fields=["mojo_secrets", "modified"])

    raised = False
    try:
        tokens.verify_email_change_otp(user, "123456")
    except merrors.ValueException:
        raised = True
    assert_true(raised, "verify_email_change_otp with no pending state must raise ValueException")


@th.django_unit_test("email change OTP: generate_email_change_otp clears outstanding ec: JTI (mutual exclusivity)")
def test_email_change_otp_clears_link_token(opts):
    """
    Generating an OTP must invalidate any in-flight link token so both paths
    cannot be active at the same time.
    """
    from mojo.apps.account.models import User
    from mojo.apps.account.utils import tokens
    from mojo import errors as merrors

    user = User.objects.get(pk=opts.user_id)
    # Issue a link token first
    link_tok = tokens.generate_email_change_token(user, "mutual_link@example.com")

    # Now generate an OTP — must kill the link token
    otp = tokens.generate_email_change_otp(user, "mutual_otp@example.com")

    raised = False
    try:
        tokens.verify_email_change_token(link_tok)
    except merrors.ValueException:
        raised = True
    assert_true(raised, "ec: token must be invalid after generate_email_change_otp clears the JTI")

    # consume the OTP cleanly
    try:
        tokens.verify_email_change_otp(user, otp)
    except merrors.ValueException:
        pass


@th.django_unit_test("email change OTP: generate_email_change_token clears outstanding OTP (mutual exclusivity)")
def test_email_change_link_token_clears_otp(opts):
    """
    Generating a link token must clear any in-flight OTP so both paths
    cannot be active at the same time.
    """
    from mojo.apps.account.models import User
    from mojo.apps.account.utils import tokens
    from mojo import errors as merrors

    user = User.objects.get(pk=opts.user_id)
    # Issue an OTP first
    otp = tokens.generate_email_change_otp(user, "mutual_otp2@example.com")

    # Now generate a link token — must kill the OTP
    link_tok = tokens.generate_email_change_token(user, "mutual_link2@example.com")

    # OTP must now be gone from secrets
    user.refresh_from_db()
    assert_eq(user.get_secret("email_change_otp"), None,
              "email_change_otp must be cleared after generate_email_change_token")

    raised = False
    try:
        tokens.verify_email_change_otp(user, otp)
    except merrors.ValueException:
        raised = True
    assert_true(raised, "OTP must be invalid after generate_email_change_token clears it")

    # consume the link token cleanly
    try:
        tokens.verify_email_change_token(link_tok)
    except merrors.ValueException:
        pass


# ===========================================================================
# REST: POST /api/auth/email/change/request  (method=code)
# ===========================================================================

@th.tier("bug")
@th.django_unit_test("email/change/request method=code: unaccepted send returns 503 and the OTP is still stored")
def test_request_code_send_failure_returns_503(opts):
    """Retargeted from the old 200-asserting code happy path (#3328).

    The OTP assertion is what stops an unconditional-503 stub from passing:
    generation happens before the send and is unchanged by the failure, so the
    person's pending change survives a retry.
    """
    from mojo.apps.account.models import User
    from mojo.apps.account.services.email_delivery import EMAIL_SEND_UNAVAILABLE
    from mojo.decorators.limits import clear_rate_limits
    clear_rate_limits(ip="127.0.0.1")

    try:
        opts.client.login(TEST_USER, TEST_PWORD)
        resp = opts.client.post(
            "/api/auth/email/change/request",
            {"email": "code_req_happy@example.com", "current_password": TEST_PWORD, "method": "code"},
        )
        opts.client.logout()
        assert_eq(resp.status_code, 503,
                  f"An unaccepted code send must not be reported as sent, got {resp.status_code}")
        assert_eq(resp.json,
                  {"status": False, "code": 503, "error": EMAIL_SEND_UNAVAILABLE},
                  "The 503 body must be exactly the fixed safe-retry payload")

        user = User.objects.get(pk=opts.user_id)
        otp = user.get_secret("email_change_otp")
        assert_true(
            otp is not None and len(otp) == 6 and otp.isdigit(),
            f"The 6-digit OTP must still be stored after the failed send, got: {otp!r}",
        )
        assert_eq(user.get_secret("pending_email"), "code_req_happy@example.com",
                  "pending_email must survive a failed send — the change is retryable")
    finally:
        _clear_email_change_state(opts)


@th.django_unit_test("email/change/request method=code: does NOT store ec: link token")
def test_request_code_method_no_ec_token(opts):
    from mojo.apps.account.models import User
    import mojo.apps.account.utils.tokens as tok_module
    from mojo.decorators.limits import clear_rate_limits
    clear_rate_limits(ip="127.0.0.1")

    opts.client.login(TEST_USER, TEST_PWORD)
    opts.client.post(
        "/api/auth/email/change/request",
        {"email": "code_no_ec@example.com", "current_password": TEST_PWORD, "method": "code"},
    )
    opts.client.logout()

    user = User.objects.get(pk=opts.user_id)
    assert_eq(
        user.get_secret(tok_module._JTI_KEYS[tok_module.KIND_EMAIL_CHANGE]), None,
        "method=code must not store an ec: JTI — the link flow must not be activated",
    )

    user.set_secret("pending_email", None)
    user.set_secret("email_change_otp", None)
    user.set_secret("email_change_otp_ts", None)
    user.save(update_fields=["mojo_secrets", "modified"])


@th.django_unit_test("email/change/request method=link: clears any outstanding OTP (mutual exclusivity via REST)")
def test_request_link_method_clears_previous_otp(opts):
    from mojo.apps.account.models import User
    from mojo.apps.account.utils import tokens
    from mojo.decorators.limits import clear_rate_limits
    clear_rate_limits(ip="127.0.0.1")

    # Seed an OTP directly
    user = User.objects.get(pk=opts.user_id)
    tokens.generate_email_change_otp(user, "prev_otp@example.com")
    user.refresh_from_db()
    assert_true(user.get_secret("email_change_otp") is not None, "precondition: OTP must be set")

    # Now request via link method — must clear the OTP
    opts.client.login(TEST_USER, TEST_PWORD)
    opts.client.post(
        "/api/auth/email/change/request",
        {"email": "link_after_otp@example.com", "current_password": TEST_PWORD},
    )
    opts.client.logout()

    user.refresh_from_db()
    assert_eq(user.get_secret("email_change_otp"), None,
              "Switching to link method must clear any outstanding OTP")

    import mojo.apps.account.utils.tokens as tok_module
    user.set_secret("pending_email", None)
    user.set_secret(tok_module._JTI_KEYS[tok_module.KIND_EMAIL_CHANGE], None)
    user.save(update_fields=["mojo_secrets", "modified"])


# ===========================================================================
# REST: POST /api/auth/email/change/confirm  (code path)
# ===========================================================================

@th.django_unit_test("email/change/confirm code: happy path commits new email and returns JWT")
def test_confirm_code_happy_path(opts):
    from mojo.apps.account.models import User
    from mojo.apps.account.utils import tokens
    from mojo.decorators.limits import clear_rate_limits
    clear_rate_limits(ip="127.0.0.1")

    User.objects.filter(pk=opts.user_id).update(
        email=opts.original_email,
        username=opts.original_username,
    )
    user = User.objects.get(pk=opts.user_id)
    otp = tokens.generate_email_change_otp(user, "code_confirm_happy@example.com")

    opts.client.login(TEST_USER, TEST_PWORD)
    resp = opts.client.post("/api/auth/email/change/confirm", {"code": otp})
    opts.client.logout()

    assert_eq(resp.status_code, 200, f"Code confirm must return 200, got {resp.status_code}: {resp.content}")
    data = resp.json
    assert_true(data.get("status") is True, "Response status must be True")
    assert_true("data" in data, "Response must contain JWT data envelope")

    user.refresh_from_db()
    assert_eq(str(user.email), "code_confirm_happy@example.com",
              "user.email must be updated to new_email after code confirm")
    assert_true(user.is_email_verified, "is_email_verified must be True after code confirm")

    # Restore
    User.objects.filter(pk=user.pk).update(
        email=opts.original_email,
        username=opts.original_username,
    )


@th.django_unit_test("email/change/confirm code: rotates auth_key (invalidates other sessions)")
def test_confirm_code_rotates_auth_key(opts):
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
    otp = tokens.generate_email_change_otp(user, "code_rotatekey@example.com")

    opts.client.login(TEST_USER, TEST_PWORD)
    opts.client.post("/api/auth/email/change/confirm", {"code": otp})
    opts.client.logout()

    user.refresh_from_db()
    assert_true(
        user.auth_key != old_auth_key,
        "auth_key must be rotated after code-path email change confirm",
    )

    # Restore
    User.objects.filter(pk=user.pk).update(
        email=opts.original_email,
        username=opts.original_username,
    )


@th.django_unit_test("email/change/confirm code: requires authentication — 401 without token")
def test_confirm_code_requires_auth(opts):
    from mojo.apps.account.models import User
    from mojo.apps.account.utils import tokens
    from mojo.decorators.limits import clear_rate_limits
    clear_rate_limits(ip="127.0.0.1")

    user = User.objects.get(pk=opts.user_id)
    otp = tokens.generate_email_change_otp(user, "code_noauth@example.com")

    opts.client.logout()
    resp = opts.client.post("/api/auth/email/change/confirm", {"code": otp})
    assert_true(
        resp.status_code in (401, 403),
        f"Code confirm without auth must return 401/403, got {resp.status_code}",
    )

    # email must be unchanged
    user.refresh_from_db()
    assert_eq(str(user.email), opts.original_email, "email must not change without authentication")

    # clean up the unused OTP
    user.set_secret("pending_email", None)
    user.set_secret("email_change_otp", None)
    user.set_secret("email_change_otp_ts", None)
    user.save(update_fields=["mojo_secrets", "modified"])


@th.django_unit_test("email/change/confirm code: wrong code rejected — email unchanged")
def test_confirm_code_wrong_code_rejected(opts):
    from mojo.apps.account.models import User
    from mojo.apps.account.utils import tokens
    from mojo.decorators.limits import clear_rate_limits
    clear_rate_limits(ip="127.0.0.1")

    user = User.objects.get(pk=opts.user_id)
    tokens.generate_email_change_otp(user, "code_wrongcode@example.com")

    opts.client.login(TEST_USER, TEST_PWORD)
    resp = opts.client.post("/api/auth/email/change/confirm", {"code": "000000"})
    opts.client.logout()

    assert_true(resp.status_code in (400, 422),
                f"Wrong code must return 4xx, got {resp.status_code}")
    user.refresh_from_db()
    assert_eq(str(user.email), opts.original_email, "email must not change after wrong code")

    # clean up the valid OTP
    user.set_secret("pending_email", None)
    user.set_secret("email_change_otp", None)
    user.set_secret("email_change_otp_ts", None)
    user.save(update_fields=["mojo_secrets", "modified"])


@th.django_unit_test("email/change/confirm code: expired code returns 4xx — email unchanged")
def test_confirm_code_expired(opts):
    from mojo.apps.account.models import User
    from mojo.apps.account.utils import tokens
    from mojo.decorators.limits import clear_rate_limits
    clear_rate_limits(ip="127.0.0.1")

    user = User.objects.get(pk=opts.user_id)
    try:
        otp = tokens.generate_email_change_otp(user, "code_expired@example.com")
        # Force the stored timestamp into the distant past so the server's
        # real TTL (600 s) recognises it as expired.  Patching the module-level
        # TTL only affects the test process, not the running server.
        user.set_secret("email_change_otp_ts", 0)
        user.save(update_fields=["mojo_secrets", "modified"])

        opts.client.login(TEST_USER, TEST_PWORD)
        resp = opts.client.post("/api/auth/email/change/confirm", {"code": otp})
        opts.client.logout()
        assert_true(resp.status_code in (400, 422),
                    f"Expired code must return 4xx, got {resp.status_code}")
        user.refresh_from_db()
        assert_eq(str(user.email), opts.original_email, "email must not change after expired code")
    finally:
        user.set_secret("pending_email", None)
        user.set_secret("email_change_otp", None)
        user.set_secret("email_change_otp_ts", None)
        user.save(update_fields=["mojo_secrets", "modified"])


@th.django_unit_test("email/change/confirm code: no pending change returns 4xx")
def test_confirm_code_no_pending_state(opts):
    from mojo.apps.account.models import User
    from mojo.decorators.limits import clear_rate_limits
    clear_rate_limits(ip="127.0.0.1")

    # Ensure no pending OTP
    user = User.objects.get(pk=opts.user_id)
    user.set_secret("pending_email", None)
    user.set_secret("email_change_otp", None)
    user.set_secret("email_change_otp_ts", None)
    user.save(update_fields=["mojo_secrets", "modified"])

    opts.client.login(TEST_USER, TEST_PWORD)
    resp = opts.client.post("/api/auth/email/change/confirm", {"code": "123456"})
    opts.client.logout()
    assert_true(resp.status_code in (400, 422),
                f"Confirm with code and no pending state must return 4xx, got {resp.status_code}")
    user.refresh_from_db()
    assert_eq(str(user.email), opts.original_email, "email must not change when no pending state")


@th.django_unit_test("email/change/confirm code: code is single-use — second confirm rejected")
def test_confirm_code_single_use(opts):
    from mojo.apps.account.models import User
    from mojo.apps.account.utils import tokens
    from mojo.decorators.limits import clear_rate_limits
    clear_rate_limits(ip="127.0.0.1")

    User.objects.filter(pk=opts.user_id).update(
        email=opts.original_email,
        username=opts.original_username,
    )
    user = User.objects.get(pk=opts.user_id)
    otp = tokens.generate_email_change_otp(user, "code_single_use@example.com")

    opts.client.login(TEST_USER, TEST_PWORD)
    resp1 = opts.client.post("/api/auth/email/change/confirm", {"code": otp})
    assert_eq(resp1.status_code, 200, f"First confirm must succeed, got {resp1.status_code}")

    # Restore email so it's not the availability check that blocks the second attempt
    User.objects.filter(pk=user.pk).update(
        email=opts.original_email,
        username=opts.original_username,
    )
    # Re-login: the first confirm rotated auth_key, invalidating the old JWT.
    # Without a fresh JWT the second request fails with 401 (auth) instead of
    # reaching the single-use code check.
    opts.client.logout()
    clear_rate_limits(ip="127.0.0.1")
    opts.client.login(TEST_USER, TEST_PWORD)
    resp2 = opts.client.post("/api/auth/email/change/confirm", {"code": otp})
    opts.client.logout()
    assert_true(resp2.status_code in (400, 422),
                f"Second use of same code must be rejected, got {resp2.status_code}")

    User.objects.filter(pk=user.pk).update(
        email=opts.original_email,
        username=opts.original_username,
    )


@th.django_unit_test("email/change/confirm code: race — email claimed by another account is rejected")
def test_confirm_code_race_email_claimed(opts):
    from mojo.apps.account.models import User
    from mojo.apps.account.utils import tokens
    from mojo.decorators.limits import clear_rate_limits
    clear_rate_limits(ip="127.0.0.1")

    user = User.objects.get(pk=opts.user_id)
    # TEST_NEW_EMAIL is owned by the collision user created in setup
    otp = tokens.generate_email_change_otp(user, TEST_NEW_EMAIL)

    opts.client.login(TEST_USER, TEST_PWORD)
    resp = opts.client.post("/api/auth/email/change/confirm", {"code": otp})
    opts.client.logout()

    assert_true(
        resp.status_code in (400, 409, 422),
        f"Code confirm must reject an email claimed by another account, got {resp.status_code}",
    )
    user.refresh_from_db()
    assert_eq(str(user.email), opts.original_email,
              "email must not change after a code confirm rejected for availability")


@th.django_unit_test("email/change/confirm code: inactive user is blocked (401)")
def test_confirm_code_inactive_user_blocked(opts):
    from mojo.apps.account.models import User
    from mojo.apps.account.utils import tokens
    from mojo.decorators.limits import clear_rate_limits
    clear_rate_limits(ip="127.0.0.1")

    user = User.objects.get(pk=opts.user_id)
    otp = tokens.generate_email_change_otp(user, "code_inactive@example.com")

    # Deactivate after OTP generation
    User.objects.filter(pk=user.pk).update(is_active=False)

    try:
        opts.client.login(TEST_USER, TEST_PWORD)
        resp = opts.client.post("/api/auth/email/change/confirm", {"code": otp})
        opts.client.logout()
        # Since DM-042 validate_jwt rejects is_active=False at the middleware,
        # so the inactive user is stopped with a 401 before reaching the view
        # (previously a 403 from the view's own check).
        assert_eq(resp.status_code, 401,
                  f"Inactive user must be rejected at auth (401) at code confirm, got {resp.status_code}")
    finally:
        # Restore even on assertion failure — later tests in this module reuse
        # this account and cascade-fail if it is left inactive.
        User.objects.filter(pk=user.pk).update(is_active=True)

    user.refresh_from_db()
    user.set_secret("pending_email", None)
    user.set_secret("email_change_otp", None)
    user.set_secret("email_change_otp_ts", None)
    user.save(update_fields=["mojo_secrets", "modified"])


@th.django_unit_test("email/change/confirm code: mirrors username when it matched old email")
def test_confirm_code_mirrors_username(opts):
    from mojo.apps.account.models import User
    from mojo.apps.account.utils import tokens
    from mojo.decorators.limits import clear_rate_limits
    clear_rate_limits(ip="127.0.0.1")

    mirror_email = "code_mirror_old@example.com"
    User.objects.filter(pk=opts.user_id).update(
        email=mirror_email,
        username=mirror_email,
    )
    user = User.objects.get(pk=opts.user_id)
    assert_eq(str(user.username).lower(), str(user.email).lower(),
              "precondition: username must equal email")

    new_email = "code_mirror_new@example.com"
    otp = tokens.generate_email_change_otp(user, new_email)

    opts.client.login(mirror_email, TEST_PWORD)
    opts.client.post("/api/auth/email/change/confirm", {"code": otp})
    opts.client.logout()

    user.refresh_from_db()
    assert_eq(str(user.email), new_email, "email must be updated")
    assert_eq(str(user.username), new_email,
              "username must be mirrored to new_email when it matched old email (code path)")

    # Restore
    User.objects.filter(pk=user.pk).update(
        email=opts.original_email,
        username=opts.original_username,
    )


@th.django_unit_test("email/change/confirm: neither token nor code returns 4xx")
def test_confirm_neither_token_nor_code(opts):
    from mojo.decorators.limits import clear_rate_limits
    clear_rate_limits(ip="127.0.0.1")

    # Unauthenticated — covers both the token path (no token) and code path (no code)
    resp = opts.client.post("/api/auth/email/change/confirm", {})
    assert_true(resp.status_code in (400, 422),
                f"Submitting neither token nor code must return 4xx, got {resp.status_code}")


@th.django_unit_test("email/change/cancel: cancel after code-flow request kills OTP and confirm is rejected")
def test_cancel_then_confirm_code_rejected(opts):
    from mojo.apps.account.models import User
    from mojo.apps.account.utils import tokens
    from mojo.decorators.limits import clear_rate_limits
    clear_rate_limits(ip="127.0.0.1")

    user = User.objects.get(pk=opts.user_id)
    otp = tokens.generate_email_change_otp(user, "cancel_code_confirm@example.com")

    opts.client.login(TEST_USER, TEST_PWORD)
    opts.client.post("/api/auth/email/change/cancel", {})

    # Attempt to confirm with the now-dead OTP
    clear_rate_limits(ip="127.0.0.1")
    resp = opts.client.post("/api/auth/email/change/confirm", {"code": otp})
    opts.client.logout()
    assert_true(
        resp.status_code in (400, 422),
        f"Code confirm after cancel must be rejected, got {resp.status_code}",
    )
    user.refresh_from_db()
    assert_eq(str(user.email), opts.original_email,
              "email must not change after cancel + code confirm attempt")


# ===========================================================================
# REST: GET /api/auth/email/change/confirm  — the landing, and its ?redirect=
#
# Since #3257 the GET is a confirmation LANDING: it renders, it never validates
# or consumes the ec: token, and it never commits the change. The commit moved
# to the POST the page's button calls. These tests therefore assert both halves
# — the page rendered, AND the account did not move — so a regression that
# restores the old commit-on-GET behavior fails here loudly.
#
# The caller-supplied ?redirect= still reaches a server-rendered anchor, so the
# scheme guard is still a one-click script-execution sink if it breaks. Every
# test asserts a POSITIVE page marker before its negative assertion so it cannot
# pass vacuously against a 404 or a 500.
# ===========================================================================

XSS_REDIRECT = "javascript:alert(1)"
READY_MARKER = 'id="mojo-landing-ready"'


def _change_confirm_get(opts, redirect, token="ec:notavalidtoken"):
    """GET the email-change confirm landing with a token and a ?redirect= value."""
    from mojo.decorators.limits import clear_rate_limits
    clear_rate_limits(ip="127.0.0.1")
    resp = opts.client.get(
        "/api/auth/email/change/confirm",
        params={"token": token, "redirect": redirect},
    )
    return resp, (resp.get("text") or "")


@th.django_unit_test("email/change/confirm GET: javascript: redirect is dropped, no link rendered")
def test_change_confirm_get_error_page_omits_javascript_redirect(opts):
    resp, body = _change_confirm_get(opts, XSS_REDIRECT)

    assert_eq(resp.status_code, 200,
              f"The landing must render 200 for any token, got {resp.status_code}")
    assert_true(READY_MARKER in body,
                f"Expected the ready state to render (the landing never validates "
                f"the token), got: {body[:400]!r}")
    assert_true("javascript:" not in body,
                "A javascript: ?redirect= must never reach the rendered page — "
                "it lands in the 'Go back' anchor href and executes on click")
    assert_true("Go back" not in body,
                "A refused ?redirect= must OMIT the button entirely, not render it dead")


@th.django_unit_test("email/change/confirm GET: a real token renders and commits nothing")
def test_change_confirm_get_valid_token_renders_without_committing(opts):
    """#3257: this GET used to verify, consume and commit. Opening a link is
    not consent — a mail scanner or link preview must change nothing."""
    from mojo.apps.account.models import User
    from mojo.apps.account.utils import tokens

    User.objects.filter(pk=opts.user_id).update(
        email=opts.original_email,
        username=opts.original_username,
    )
    user = User.objects.get(pk=opts.user_id)
    new_email = "confirm_get_guard@example.com"
    User.objects.filter(email=new_email).exclude(pk=user.pk).delete()
    token = tokens.generate_email_change_token(user, new_email)

    resp, body = _change_confirm_get(opts, XSS_REDIRECT, token=token)

    assert_eq(resp.status_code, 200, f"The landing must render 200, got {resp.status_code}")
    assert_true(READY_MARKER in body,
                f"Expected the ready state to render, got: {body[:400]!r}")
    assert_true("javascript:" not in body,
                "A javascript: ?redirect= must never reach the rendered page")
    assert_true('http-equiv="refresh"' not in body,
                "The landing must never auto-navigate — no meta refresh, ever")

    user.refresh_from_db()
    assert_eq(str(user.email), opts.original_email,
              "Opening the landing must NOT commit the email change")
    assert_eq(user.get_secret("pending_email"), new_email,
              "Opening the landing must leave the pending change intact — the "
              "token it rendered has to still work")

    # Restore
    User.objects.filter(pk=user.pk).update(
        email=opts.original_email,
        username=opts.original_username,
    )


@th.django_unit_test("email/change/confirm GET: an https redirect is preserved byte-for-byte")
def test_change_confirm_get_keeps_https_redirect(opts):
    destination = "https://example.com/login?next=1"
    resp, body = _change_confirm_get(opts, destination)

    assert_eq(resp.status_code, 200, f"The landing must render 200, got {resp.status_code}")
    assert_true(READY_MARKER in body,
                f"Expected the ready state to render, got: {body[:400]!r}")
    assert_true(f'href="{destination}"' in body,
                f"A cross-origin https destination must render unchanged (host is not allowlisted); "
                f"expected href=\"{destination}\" in the page")
    assert_true("Go back" in body,
                "The 'Go back' button must still be rendered for an accepted destination")


@th.django_unit_test("email/change/confirm GET: a relative redirect is preserved, not normalized to absolute")
def test_change_confirm_get_keeps_relative_redirect(opts):
    destination = "/account/email?next=1"
    resp, body = _change_confirm_get(opts, destination)

    assert_eq(resp.status_code, 200, f"The landing must render 200, got {resp.status_code}")
    assert_true(READY_MARKER in body,
                f"Expected the ready state to render, got: {body[:400]!r}")
    assert_true(f'href="{destination}"' in body,
                f"A relative destination must render unchanged — no normalization to an absolute URL; "
                f"expected href=\"{destination}\" in the page")

# ===========================================================================
# Send truthfulness — the helpers, the endpoint, the activity trail (#3328)
# ===========================================================================

@th.django_unit_test("email change send helpers: return the transport result instead of discarding it")
def test_send_helpers_return_transport_result(opts):
    """The bug: both helpers threw the SentMessage away, so a refused message
    (persisted, truthy, no SES id) was indistinguishable from a sent one."""
    from mojo.apps.account.models import User
    from mojo.apps.account.rest import user as user_rest
    from mojo.apps.account.utils import tokens

    try:
        for msg in (_sent("sending", ses_message_id=FAKE_SES_ID),
                    _sent("failed", status_reason="MessageRejected")):
            user = User.objects.get(pk=opts.user_id)
            token = tokens.generate_email_change_token(user, "helper_ret@example.com")

            sender = _FakeSend(msg)
            result = user_rest._send_email_change_confirm(
                _request_factory_post({}), user, "helper_ret@example.com",
                token, send=sender)
            assert_true(result is msg,
                        f"_send_email_change_confirm must return the transport result "
                        f"itself, got {result!r} for status={msg.status!r}")
            assert_eq(len(sender.calls), 1,
                      f"exactly one send must be attempted: {sender.calls}")
            assert_eq(sender.calls[0].get("template_name"), "email_change_confirm",
                      f"the link branch must send the confirm template: {sender.calls[0]}")
            assert_eq(sender.calls[0].get("to"), "helper_ret@example.com",
                      f"the confirmation must go to the NEW address: {sender.calls[0]}")

            sender = _FakeSend(msg)
            result = user_rest._send_email_change_code(
                user, "helper_ret@example.com", "123456", send=sender)
            assert_true(result is msg,
                        f"_send_email_change_code must return the transport result "
                        f"itself, got {result!r} for status={msg.status!r}")
            assert_eq(sender.calls[0].get("template_name"), "email_change_code",
                      f"the code branch must send the OTP template: {sender.calls[0]}")
            assert_eq(sender.calls[0].get("context", {}).get("code"), "123456",
                      f"the OTP must reach the template: {sender.calls[0]}")
    finally:
        _clear_email_change_state(opts)


@th.django_unit_test("email change send helpers: a transport exception becomes None and files no activity")
def test_confirm_send_helper_swallows_transport_exception(opts):
    """A raising transport must not 500 the endpoint, and must not leave an
    email_change: row behind claiming anything happened."""
    from mojo.apps.account.models import User
    from mojo.apps.account.rest import user as user_rest
    from mojo.apps.account.utils import tokens
    from mojo.apps.incident.models import Event

    try:
        user = User.objects.get(pk=opts.user_id)
        token = tokens.generate_email_change_token(user, "helper_raise@example.com")
        Event.objects.filter(uid=opts.user_id,
                             category__startswith="email_change:").delete()

        result = user_rest._send_email_change_confirm(
            _request_factory_post({}), user, "helper_raise@example.com", token,
            send=_FakeSend(RuntimeError("SES down")))
        assert_true(result is None,
                    f"a raising transport must be swallowed to None, got {result!r}")

        result = user_rest._send_email_change_code(
            user, "helper_raise@example.com", "123456",
            send=_FakeSend(RuntimeError("SES down")))
        assert_true(result is None,
                    f"a raising transport must be swallowed to None, got {result!r}")

        leaked = list(Event.objects.filter(
            uid=opts.user_id, category__startswith="email_change:"))
        assert_eq(len(leaked), 0,
                  f"the send helpers must record no email_change: activity of their "
                  f"own — that is the endpoint's job: {[e.category for e in leaked]}")
    finally:
        _clear_email_change_state(opts)


@th.tier("bug")
@th.django_unit_test("email/change/request: an unaccepted send answers 503 and files no raw mojo_rest_error")
def test_request_send_failure_returns_503(opts):
    """The 503 is RETURNED, not raised: raising would make the dispatcher file a
    second, raw mojo_rest_error carrying the request body and a stack trace."""
    from mojo.apps.account.services.email_delivery import EMAIL_SEND_UNAVAILABLE
    from mojo.apps.incident.models import Event
    from mojo.decorators.limits import clear_rate_limits
    from mojo.helpers import dates
    clear_rate_limits(ip="127.0.0.1")

    started = dates.utcnow()
    try:
        opts.client.login(TEST_USER, TEST_PWORD)
        resp = opts.client.post(
            "/api/auth/email/change/request",
            {"email": "send_fail_503@example.com", "current_password": TEST_PWORD},
        )
        opts.client.logout()

        assert_eq(resp.status_code, 503,
                  f"Expected a retryable 503, got {resp.status_code}: {resp.content}")
        body = resp.json
        assert_true(body.get("status") is False, f"status must be False: {body}")
        assert_eq(body.get("code"), 503, f"the body must carry code 503: {body}")
        assert_eq(body.get("error"), EMAIL_SEND_UNAVAILABLE,
                  f"the error must be the fixed safe-retry copy: {body}")

        raw = list(Event.objects.filter(
            uid=opts.user_id, category="mojo_rest_error", created__gte=started))
        assert_eq(len(raw), 0,
                  f"the failure must be RETURNED, not raised — a raised exception "
                  f"files a raw mojo_rest_error with the request body and a stack "
                  f"trace: {[e.title for e in raw]}")
    finally:
        _clear_email_change_state(opts)


@th.tier("bug")
@th.django_unit_test("email/change/request: a failed send records safe send_failed activity and no 'requested'")
def test_request_send_failure_records_only_safe_activity(opts):
    """The account history must not say a confirmation was sent when it wasn't,
    and the row it does write must carry no secret and no provider text."""
    import json
    from mojo.apps.account.models import User
    from mojo.decorators.limits import clear_rate_limits
    import mojo.apps.account.utils.tokens as tok_module
    clear_rate_limits(ip="127.0.0.1")

    try:
        _clear_email_change_state(opts)
        opts.client.login(TEST_USER, TEST_PWORD)
        opts.client.post(
            "/api/auth/email/change/request",
            {"email": "send_fail_activity@example.com", "current_password": TEST_PWORD},
        )
        opts.client.logout()

        failed = list(_activity(opts.user_id, "email_change:send_failed"))
        assert_eq(len(failed), 1,
                  f"exactly one email_change:send_failed row must be written, got "
                  f"{len(failed)}")
        assert_eq(_activity(opts.user_id, "email_change:requested").count(), 0,
                  "a failed send must NOT record email_change:requested — that is "
                  "the false history this item exists to remove")

        event = failed[0]
        blob = json.dumps({
            "title": event.title,
            "details": event.details,
            "metadata": event.metadata,
        })
        user = User.objects.get(pk=opts.user_id)
        jti = user.get_secret(tok_module._JTI_KEYS[tok_module.KIND_EMAIL_CHANGE])
        assert_true(jti is not None,
                    "precondition: the ec: JTI must still be stored after the failure")
        assert_true(jti not in blob,
                    "the confirmation secret must never appear in account activity")
        for marker in ("SES", "botocore", "Traceback", "MessageId", "MessageRejected"):
            assert_true(marker not in blob,
                        f"provider/diagnostic text {marker!r} must never reach the "
                        f"activity row: {blob[:400]}")

        assert_true(event.metadata.get("failure_class") in ("not_sent", "not_accepted"),
                    f"failure_class must be one of the closed enum, got "
                    f"{event.metadata.get('failure_class')!r}")
        assert_eq(event.scope, "account",
                  f"Event.scope (the RuleSet lookup key) must stay 'account', got "
                  f"{event.scope!r}")
        assert_eq(event.metadata.get("security_activity_scope"), "brand",
                  f"a request-time row is brand-provenance, never account-global: "
                  f"{event.metadata}")
    finally:
        _clear_email_change_state(opts)


@th.django_unit_test("email/change/request: a failed send leaves the pending change intact (retryable)")
def test_request_send_failure_retains_pending_state(opts):
    from mojo.apps.account.models import User
    import mojo.apps.account.utils.tokens as tok_module
    from mojo.decorators.limits import clear_rate_limits
    clear_rate_limits(ip="127.0.0.1")

    try:
        opts.client.login(TEST_USER, TEST_PWORD)
        resp = opts.client.post(
            "/api/auth/email/change/request",
            {"email": "send_fail_pending@example.com", "current_password": TEST_PWORD},
        )
        opts.client.logout()
        assert_eq(resp.status_code, 503,
                  f"precondition: the send must be reported as failed, got {resp.status_code}")

        user = User.objects.get(pk=opts.user_id)
        assert_eq(user.get_secret("pending_email"), "send_fail_pending@example.com",
                  "pending_email must survive a failed send — no cleanup race")
        assert_true(
            user.get_secret(tok_module._JTI_KEYS[tok_module.KIND_EMAIL_CHANGE]) is not None,
            "the ec: JTI must survive a failed send — the change stays retryable")
    finally:
        _clear_email_change_state(opts)


@th.django_unit_test("email/change/request: a failed send does not change the address or username")
def test_request_send_failure_does_not_change_email(opts):
    from mojo.apps.account.models import User
    from mojo.decorators.limits import clear_rate_limits
    clear_rate_limits(ip="127.0.0.1")

    try:
        before = User.objects.get(pk=opts.user_id)
        old_email, old_username = str(before.email), str(before.username)

        opts.client.login(TEST_USER, TEST_PWORD)
        resp = opts.client.post(
            "/api/auth/email/change/request",
            {"email": "send_fail_noswap@example.com", "current_password": TEST_PWORD},
        )
        opts.client.logout()
        assert_eq(resp.status_code, 503,
                  f"precondition: the send must be reported as failed, got {resp.status_code}")

        after = User.objects.get(pk=opts.user_id)
        assert_eq(str(after.email), old_email,
                  "a failed request must never move the account email")
        assert_eq(str(after.username), old_username,
                  "a failed request must never move the account username")
    finally:
        _clear_email_change_state(opts)


@th.tier("bug")
@th.django_unit_test("email/change/request: an ACCEPTED send still returns 200 and records 'requested'")
def test_request_accepted_path_in_process(opts):
    """The only place the 200 branch is provable: over the wire the test project
    has no mailbox, so no send can ever be accepted."""
    import json
    from mojo.apps.account.models import User

    try:
        _clear_email_change_state(opts)
        # --- link branch ---
        sender, notifier = _accepted_send(), _accepted_send()
        response = _call_request_endpoint(
            opts, {"email": "accepted_link@example.com", "current_password": TEST_PWORD},
            send=sender, notify_send=notifier)
        body = json.loads(response.content.decode("utf-8"))

        assert_eq(response.status_code, 200,
                  f"an accepted send must keep returning 200, got {response.status_code}: {body}")
        assert_true(body.get("status") is True, f"status must be True on success: {body}")
        assert_true("confirmation link" in body.get("message", "").lower(),
                    f"the success copy must be unchanged: {body}")
        assert_eq(len(sender.calls), 1,
                  f"exactly one confirmation send must be attempted: {sender.calls}")
        assert_eq(_activity(opts.user_id, "email_change:requested").count(), 1,
                  "an accepted send must record exactly one email_change:requested row")
        assert_eq(_activity(opts.user_id, "email_change:send_failed").count(), 0,
                  "an accepted send must record no failure row")
        assert_eq(len(notifier.calls), 1,
                  f"the old address must be notified after acceptance: {notifier.calls}")
        assert_eq(notifier.calls[0].get("template_name"), "email_change_notify",
                  f"the notice must use the notify template: {notifier.calls[0]}")

        requested = _activity(opts.user_id, "email_change:requested").last()
        assert_eq(requested.metadata.get("security_activity_scope"), "brand",
                  f"a request-time row is brand-provenance: {requested.metadata}")
        assert_eq(requested.scope, "account",
                  f"Event.scope must stay 'account', got {requested.scope!r}")

        _clear_email_change_state(opts)

        # --- code branch ---
        sender, notifier = _accepted_send(), _accepted_send()
        response = _call_request_endpoint(
            opts, {"email": "accepted_code@example.com",
                   "current_password": TEST_PWORD, "method": "code"},
            send=sender, notify_send=notifier)
        body = json.loads(response.content.decode("utf-8"))

        assert_eq(response.status_code, 200,
                  f"an accepted code send must keep returning 200, got {response.status_code}: {body}")
        assert_true("verification code" in body.get("message", "").lower(),
                    f"the code success copy must be unchanged: {body}")
        assert_eq(_activity(opts.user_id, "email_change:requested_code").count(), 1,
                  "an accepted code send must record exactly one requested_code row")
        assert_eq(len(notifier.calls), 1,
                  f"the old address must be notified after acceptance: {notifier.calls}")
        stored = User.objects.get(pk=opts.user_id).get_secret("email_change_otp")
        assert_eq(sender.calls[0].get("context", {}).get("code"), stored,
                  "the emailed code must be the one stored in secrets")
    finally:
        _clear_email_change_state(opts)


@th.django_unit_test("email/change/request: no old address means no notice attempt and no failure row")
def test_notify_skipped_when_old_address_empty(opts):
    """A first-email account has nothing to notify — the old code sent to "",
    which raised inside the mailbox and filed a raw event."""
    import json

    uid = opts.empty_email_user_id
    try:
        _clear_email_change_state(opts, user_id=uid)
        sender, notifier = _accepted_send(), _accepted_send()
        response = _call_request_endpoint(
            opts, {"email": "first_email@example.com"},
            send=sender, notify_send=notifier, user_id=uid)
        body = json.loads(response.content.decode("utf-8"))

        assert_eq(response.status_code, 200,
                  f"an accepted send must return 200, got {response.status_code}: {body}")
        assert_eq(len(notifier.calls), 0,
                  f"there is no old address to notify: {notifier.calls}")
        assert_eq(_activity(uid, "email_change:notice_failed").count(), 0,
                  "a skipped notice is not a failed notice")
        assert_eq(_activity(uid, "email_change:send_failed").count(), 0,
                  "the confirmation was accepted — no failure row")
        assert_eq(_activity(uid, "email_change:requested").count(), 1,
                  "the accepted request must still be recorded")
    finally:
        _clear_email_change_state(opts, user_id=uid)


@th.django_unit_test("email/change/request: a failed old-address notice records notice_failed only")
def test_notify_failure_records_notice_failed_only(opts):
    """A notice that fails after an accepted confirmation must not relabel the
    confirmation, and must never escape to the caller."""
    import json

    try:
        for notifier in (_refused_send(), _FakeSend(RuntimeError("SES down"))):
            _clear_email_change_state(opts)
            sender = _accepted_send()
            response = _call_request_endpoint(
                opts, {"email": "notice_fail@example.com", "current_password": TEST_PWORD},
                send=sender, notify_send=notifier)
            body = json.loads(response.content.decode("utf-8"))

            assert_eq(response.status_code, 200,
                      f"a failed notice must not change the request outcome, got "
                      f"{response.status_code}: {body}")
            assert_eq(_activity(opts.user_id, "email_change:notice_failed").count(), 1,
                      "exactly one email_change:notice_failed row must be written")
            assert_eq(_activity(opts.user_id, "email_change:send_failed").count(), 0,
                      "a notice failure must never be labelled a confirmation failure")
            assert_eq(_activity(opts.user_id, "email_change:requested").count(), 1,
                      "the accepted confirmation keeps its 'requested' row")
            _clear_email_change_state(opts)
    finally:
        _clear_email_change_state(opts)


@th.tier("bug")
@th.django_unit_test("email/change/confirm: records account-global email_change:confirmed for the token's subject")
def test_confirm_records_account_global_activity(opts):
    from mojo.apps.account.models import User
    from mojo.apps.account.utils import tokens
    from mojo.decorators.limits import clear_rate_limits
    clear_rate_limits(ip="127.0.0.1")

    try:
        _clear_email_change_state(opts)
        User.objects.filter(pk=opts.user_id).update(
            email=opts.original_email, username=opts.original_username)
        user = User.objects.get(pk=opts.user_id)
        old_auth_key = str(user.auth_key)
        token = tokens.generate_email_change_token(user, "confirmed_activity@example.com")

        resp = opts.client.post("/api/auth/email/change/confirm", {"token": token})
        assert_eq(resp.status_code, 200,
                  f"confirm must still return 200, got {resp.status_code}: {resp.content}")
        assert_true(resp.json.get("status") is True, "Response status must be True")
        assert_true("data" in resp.json, "Response must still contain the JWT envelope")

        user.refresh_from_db()
        assert_eq(str(user.email), "confirmed_activity@example.com",
                  "confirm must still commit the new address")
        assert_true(user.is_email_verified, "confirm must still mark the address verified")
        assert_true(str(user.auth_key) != old_auth_key,
                    "confirm must still rotate auth_key")

        rows = list(_activity(opts.user_id, "email_change:confirmed"))
        assert_eq(len(rows), 1,
                  f"exactly one email_change:confirmed row must be written, got {len(rows)}")
        event = rows[0]
        assert_eq(event.uid, opts.user_id,
                  "the row must be attributed to the TOKEN's verified subject")
        assert_true(event.group_id is None,
                    f"an account-global row carries no group, got {event.group_id!r}")
        assert_eq(event.metadata.get("security_activity_scope"), "account",
                  f"the account marker is what makes the row globally visible: "
                  f"{event.metadata}")
        assert_eq(event.scope, "account",
                  f"Event.scope must stay 'account', got {event.scope!r}")
    finally:
        from mojo.apps.account.models import User as _User
        _User.objects.filter(pk=opts.user_id).update(
            email=opts.original_email, username=opts.original_username)
        _clear_email_change_state(opts)
