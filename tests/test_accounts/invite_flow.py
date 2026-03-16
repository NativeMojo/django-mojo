"""
Regression tests for the invite token flow through the password reset endpoint.

Bug: POST /api/auth/password/reset/token returns 400 "Invalid token kind" when
called with an iv: token. Frontends that use a single "set password" flow for
invite emails are broken.

Issue: planning/issues/password_reset_token_rejects_invite_token.md
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
