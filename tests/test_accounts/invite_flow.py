"""
Regression tests for the invite token flow through the password reset endpoint.

Bug: POST /api/auth/password/reset/token returns 400 "Invalid token kind" when
called with an iv: token. Frontends that use a single "set password" flow for
invite emails are broken.

Issue: planning/issues/password_reset_token_rejects_invite_token.md

Also covers:
  - get_or_generate_invite_token reuses valid token across multiple group invites
  - get_or_generate_invite_token generates fresh token after consumption or expiry
  - Distinct error messages: "Token already used", "Expired token",
    "Invalid token signature", "Invalid token format"
"""
from testit import helpers as th
from testit.helpers import assert_eq, assert_true

TEST_USER = "invite_flow_user"


@th.django_unit_setup()
def setup_invite_flow(opts):
    from mojo.apps.account.models import User

    user = User.objects.filter(username=TEST_USER).last()
    if user is None:
        user = User(username=TEST_USER, email=f"{TEST_USER}@example.com")
        user.save()
    user.set_unusable_password()
    user.last_login = None
    user.is_email_verified = False
    user.is_active = True
    user.save()
    opts.user_id = user.pk


# ---------------------------------------------------------------------------
# Regression: iv: token accepted by /api/auth/password/reset/token
# ---------------------------------------------------------------------------

@th.django_unit_test("invite_flow: iv: token accepted by password reset token endpoint (regression)")
def test_invite_token_accepted(opts):
    from mojo.apps.account.models import User
    from mojo.apps.account.utils import tokens

    user = User.objects.get(pk=opts.user_id)
    tok = tokens.generate_invite_token(user)
    assert_true(tok.startswith("iv:"), f"invite token should start with 'iv:', got {tok[:5]}")

    resp = opts.client.post("/api/auth/password/reset/token", {
        "token": tok,
        "new_password": "NewPass##99",
    })
    assert_eq(resp.status_code, 200,
              f"iv: token should be accepted (got {resp.status_code}: {resp.response.data})")


@th.django_unit_test("invite_flow: iv: token marks is_email_verified on new user")
def test_invite_token_marks_email_verified(opts):
    from mojo.apps.account.models import User
    from mojo.apps.account.utils import tokens

    user = User.objects.get(pk=opts.user_id)
    User.objects.filter(pk=user.pk).update(is_email_verified=False, last_login=None)
    user.refresh_from_db()
    assert_true(not user.is_email_verified, "should start unverified")

    tok = tokens.generate_invite_token(user)
    resp = opts.client.post("/api/auth/password/reset/token", {
        "token": tok,
        "new_password": "NewPass##99",
    })
    assert_eq(resp.status_code, 200,
              f"expected 200, got {resp.status_code}: {resp.response.data}")

    user.refresh_from_db()
    assert_true(user.is_email_verified,
                "is_email_verified should be True after invite token password set")


@th.django_unit_test("invite_flow: iv: token returns JWT (user can log in)")
def test_invite_token_returns_jwt(opts):
    from mojo.apps.account.models import User
    from mojo.apps.account.utils import tokens

    user = User.objects.get(pk=opts.user_id)
    User.objects.filter(pk=user.pk).update(last_login=None)

    tok = tokens.generate_invite_token(user)
    resp = opts.client.post("/api/auth/password/reset/token", {
        "token": tok,
        "new_password": "NewPass##99",
    })
    assert_eq(resp.status_code, 200,
              f"expected 200, got {resp.status_code}: {resp.response.data}")

    assert_true(bool(getattr(resp.response.data, "access_token", None)),
                "response should contain an access_token JWT")


@th.django_unit_test("invite_flow: pr: token still works (no regression)")
def test_password_reset_token_still_works(opts):
    from mojo.apps.account.models import User
    from mojo.apps.account.utils import tokens

    user = User.objects.get(pk=opts.user_id)
    user.save_password("OldPass##88")
    user.save()

    tok = tokens.generate_password_reset_token(user)
    assert_true(tok.startswith("pr:"), f"password reset token should start with 'pr:', got {tok[:5]}")

    resp = opts.client.post("/api/auth/password/reset/token", {
        "token": tok,
        "new_password": "NewPass##99",
    })
    assert_eq(resp.status_code, 200,
              f"pr: token should still work (got {resp.status_code}: {resp.response.data})")


@th.django_unit_test("invite_flow: unknown token prefix rejected with 400")
def test_unknown_token_prefix_rejected(opts):
    resp = opts.client.post("/api/auth/password/reset/token", {
        "token": "zz:thisisnotavalidtoken",
        "new_password": "NewPass##99",
    })
    assert_eq(resp.status_code, 400,
              f"unknown token kind should return 400, got {resp.status_code}")


# ---------------------------------------------------------------------------
# get_or_generate_invite_token: token reuse across multiple group invites
# ---------------------------------------------------------------------------

@th.django_unit_test("invite_flow: get_or_generate reuses valid token (multi-group invite)")
def test_get_or_generate_reuses_valid_token(opts):
    from mojo.apps.account.models import User
    from mojo.apps.account.utils import tokens

    user = User.objects.get(pk=opts.user_id)
    User.objects.filter(pk=user.pk).update(last_login=None)
    user.refresh_from_db()

    tok1 = tokens.get_or_generate_invite_token(user)
    tok2 = tokens.get_or_generate_invite_token(user)

    assert_eq(tok1, tok2,
              "get_or_generate_invite_token should return the same token while still valid")


@th.django_unit_test("invite_flow: get_or_generate generates fresh token after consumption")
def test_get_or_generate_fresh_after_consumption(opts):
    from mojo.apps.account.models import User
    from mojo.apps.account.utils import tokens

    user = User.objects.get(pk=opts.user_id)
    User.objects.filter(pk=user.pk).update(last_login=None)
    user.refresh_from_db()

    tok1 = tokens.get_or_generate_invite_token(user)

    # Simulate consumption — clear the JTI as _verify does
    user.set_secret("invite_jti", None)
    user.set_secret("invite_token", None)
    user.save()

    tok2 = tokens.get_or_generate_invite_token(user)

    assert_true(tok1 != tok2,
                "get_or_generate_invite_token should generate a new token after the old one is consumed")
    assert_true(tok2.startswith("iv:"), f"new token should be an iv: token, got {tok2[:5]}")


@th.django_unit_test("invite_flow: get_or_generate generates fresh token after expiry")
def test_get_or_generate_fresh_after_expiry(opts):
    from mojo.apps.account.models import User
    from mojo.apps.account.utils import tokens
    from unittest.mock import patch
    from mojo.helpers import dates
    import datetime

    user = User.objects.get(pk=opts.user_id)
    user.refresh_from_db()

    tok1 = tokens.get_or_generate_invite_token(user)

    # Backdate invite_ts in secrets so get_or_generate sees it as expired
    expired_ts = int(__import__('time').time()) - (604800 + 1)
    user.set_secret("invite_ts", expired_ts)
    user.save()

    tok2 = tokens.get_or_generate_invite_token(user)

    assert_true(tok1 != tok2,
                "get_or_generate_invite_token should generate a new token after the old one expires")


# ---------------------------------------------------------------------------
# Distinct error messages from _verify
# ---------------------------------------------------------------------------

@th.django_unit_test("invite_flow: consumed token returns 'Token already used'")
def test_consumed_token_error_message(opts):
    from mojo.apps.account.models import User
    from mojo.apps.account.utils import tokens

    user = User.objects.get(pk=opts.user_id)
    User.objects.filter(pk=user.pk).update(last_login=None)
    user.refresh_from_db()

    tok = tokens.generate_invite_token(user)

    # Overwrite the JTI — simulates a second invite being sent
    user.set_secret("invite_jti", "differentjti00")
    user.save()

    resp = opts.client.post("/api/auth/password/reset/token", {
        "token": tok,
        "new_password": "NewPass##99",
    })
    assert_eq(resp.status_code, 400,
              f"overwritten JTI should return 400, got {resp.status_code}")
    assert_eq(resp.response.error, "Token already used",
              f"expected 'Token already used', got '{resp.response.error}'")


@th.django_unit_test("invite_flow: expired token raises 'Expired token'")
def test_expired_token_error_message(opts):
    from mojo.apps.account.models import User
    from mojo.apps.account.utils import tokens
    from unittest.mock import patch
    from mojo import errors as merrors
    import datetime
    import pytz

    user = User.objects.get(pk=opts.user_id)
    User.objects.filter(pk=user.pk).update(last_login=None)
    user.refresh_from_db()

    tok = tokens.generate_invite_token(user)

    # Call verify_invite_token directly in this process so the patch takes effect
    future = datetime.datetime.now(tz=pytz.UTC) + datetime.timedelta(days=8)
    error_msg = None
    with patch("mojo.apps.account.utils.tokens.dates.utcnow", return_value=future):
        try:
            tokens.verify_invite_token(tok)
        except merrors.ValueException as e:
            error_msg = str(e)

    assert_eq(error_msg, "Expired token",
              f"expected 'Expired token', got '{error_msg}'")


@th.django_unit_test("invite_flow: tampered signature returns 'Invalid token signature'")
def test_tampered_signature_error_message(opts):
    from mojo.apps.account.models import User
    from mojo.apps.account.utils import tokens

    user = User.objects.get(pk=opts.user_id)
    User.objects.filter(pk=user.pk).update(last_login=None)
    user.refresh_from_db()

    tok = tokens.generate_invite_token(user)

    # Flip the last character of the 6-char signature to corrupt it
    last = tok[-1]
    corrupted = tok[:-1] + ('a' if last != 'a' else 'b')

    resp = opts.client.post("/api/auth/password/reset/token", {
        "token": corrupted,
        "new_password": "NewPass##99",
    })
    assert_eq(resp.status_code, 400,
              f"tampered token should return 400, got {resp.status_code}")
    assert_eq(resp.response.error, "Invalid token signature",
              f"expected 'Invalid token signature', got '{resp.response.error}'")


@th.django_unit_test("invite_flow: garbage token returns 'Invalid token format'")
def test_garbage_token_error_message(opts):
    resp = opts.client.post("/api/auth/password/reset/token", {
        "token": "iv:thisiscompletelygarbageandcannotdecode",
        "new_password": "NewPass##99",
    })
    assert_eq(resp.status_code, 400,
              f"garbage token should return 400, got {resp.status_code}")
    assert_eq(resp.response.error, "Invalid token format",
              f"expected 'Invalid token format', got '{resp.response.error}'")
