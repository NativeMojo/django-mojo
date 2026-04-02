"""
Tests for magic login link flow — email and SMS channels.

Regression: on_magic_login_send was hardcoded to send via email only.
on_magic_login_complete always marked is_email_verified regardless of channel.
Both are now channel-aware via the magic_login_channel secret.
"""
from testit import helpers as th
from testit.helpers import assert_true, assert_eq

TEST_USER = "magic_login_user"
TEST_PWORD = "magic##mojo99"


@th.django_unit_setup()
def setup_magic_login(opts):
    from mojo.apps.account.models import User
    from mojo.decorators.limits import clear_rate_limits
    clear_rate_limits(ip="127.0.0.1")

    user = User.objects.filter(username=TEST_USER).last()
    if user is None:
        user = User(username=TEST_USER, email=f"{TEST_USER}@example.com")
        user.save()
    User.objects.exclude(pk=user.pk).filter(phone_number="+15550009999").update(phone_number=None)
    User.objects.filter(pk=user.pk).update(phone_number=None)
    user.refresh_from_db()
    user.phone_number = "+15550009999"
    user.is_email_verified = False
    user.is_phone_verified = False
    user.is_active = True
    user.save_password(TEST_PWORD)
    user.save()
    opts.user_id = user.pk


# ---------------------------------------------------------------------------
# Unit: token channel stored and returned correctly
# ---------------------------------------------------------------------------

@th.django_unit_test("magic_login: generate stores email channel by default")
def test_token_channel_default(opts):
    from mojo.apps.account.models import User
    from mojo.apps.account.utils import tokens

    user = User.objects.get(pk=opts.user_id)
    tok = tokens.generate_magic_login_token(user)
    channel = user.get_secret("magic_login_channel")
    assert_eq(channel, "email", "default channel should be email")
    # consume so it doesn't interfere
    tokens.verify_magic_login_token(tok)


@th.django_unit_test("magic_login: generate stores sms channel when specified")
def test_token_channel_sms(opts):
    from mojo.apps.account.models import User
    from mojo.apps.account.utils import tokens

    user = User.objects.get(pk=opts.user_id)
    tok = tokens.generate_magic_login_token(user, channel="sms")
    channel = user.get_secret("magic_login_channel")
    assert_eq(channel, "sms", "channel should be sms")
    tokens.verify_magic_login_token(tok)


@th.django_unit_test("magic_login: verify returns (user, channel) tuple")
def test_verify_returns_tuple(opts):
    from mojo.apps.account.models import User
    from mojo.apps.account.utils import tokens

    user = User.objects.get(pk=opts.user_id)
    tok = tokens.generate_magic_login_token(user, channel="sms")
    result = tokens.verify_magic_login_token(tok)
    assert_true(isinstance(result, tuple) and len(result) == 2, "verify should return (user, channel) tuple")
    returned_user, returned_channel = result
    assert_eq(returned_user.pk, user.pk, "returned user pk should match")
    assert_eq(returned_channel, "sms", "returned channel should be sms")


@th.django_unit_test("magic_login: channel secret cleared after verify (single-use)")
def test_channel_cleared_after_verify(opts):
    from mojo.apps.account.models import User
    from mojo.apps.account.utils import tokens

    user = User.objects.get(pk=opts.user_id)
    tok = tokens.generate_magic_login_token(user, channel="sms")
    tokens.verify_magic_login_token(tok)
    user.refresh_from_db()
    assert_eq(user.get_secret("magic_login_channel"), None, "channel secret should be cleared after verify")


# ---------------------------------------------------------------------------
# REST: email channel marks is_email_verified
# ---------------------------------------------------------------------------

@th.django_unit_test("magic_login REST: email channel marks is_email_verified")
def test_rest_email_marks_email_verified(opts):
    from mojo.apps.account.models import User
    from mojo.apps.account.utils import tokens

    user = User.objects.get(pk=opts.user_id)
    assert_true(not user.is_email_verified, "should start unverified")

    tok = tokens.generate_magic_login_token(user, channel="email")
    resp = opts.client.post("/api/auth/magic/login", {"token": tok})
    assert_eq(resp.status_code, 200, f"magic login should return 200, got {resp.status_code}")

    user.refresh_from_db()
    assert_true(user.is_email_verified, "is_email_verified should be True after email magic login")
    assert_true(not user.is_phone_verified, "is_phone_verified should remain False for email channel")


# ---------------------------------------------------------------------------
# REST: sms channel marks is_phone_verified (regression test)
# ---------------------------------------------------------------------------

@th.django_unit_test("magic_login REST: sms channel marks is_phone_verified (regression)")
def test_rest_sms_marks_phone_verified(opts):
    from mojo.apps.account.models import User
    from mojo.apps.account.utils import tokens

    user = User.objects.get(pk=opts.user_id)
    User.objects.filter(pk=user.pk).update(is_email_verified=False, is_phone_verified=False)
    user.refresh_from_db()

    tok = tokens.generate_magic_login_token(user, channel="sms")
    resp = opts.client.post("/api/auth/magic/login", {"token": tok})
    assert_eq(resp.status_code, 200, f"magic login should return 200, got {resp.status_code}")

    user.refresh_from_db()
    assert_true(user.is_phone_verified, "is_phone_verified should be True after sms magic login")
    assert_true(not user.is_email_verified, "is_email_verified should remain False for sms channel")


# ---------------------------------------------------------------------------
# REST: send endpoint accepts method param
# ---------------------------------------------------------------------------

@th.django_unit_test("magic_login REST: send with method=email succeeds")
def test_rest_send_email(opts):
    resp = opts.client.post("/api/auth/magic/send", {"email": f"{TEST_USER}@example.com", "method": "email"})
    assert_eq(resp.status_code, 200, f"send should return 200, got {resp.status_code}")


@th.django_unit_test("magic_login REST: send with method=sms succeeds")
def test_rest_send_sms(opts):
    # phonehub.send_sms may not be configured in test env — we just verify
    # the endpoint doesn't 500 and returns the standard success envelope
    resp = opts.client.post("/api/auth/magic/send", {"phone_number": "+15550009999", "method": "sms"})
    assert_eq(resp.status_code, 200, f"send should return 200, got {resp.status_code}")


@th.django_unit_setup()
def cleanup_magic_login(opts):
    from mojo.apps.account.models import User
    User.objects.filter(pk=opts.user_id).delete()
