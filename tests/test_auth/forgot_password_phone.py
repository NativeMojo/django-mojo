"""Tests for phone-based password reset (SMS code dispatch)."""
import uuid as _uuid

from testit import helpers as th


def _clear_forgot_limits():
    from mojo.decorators.limits import clear_rate_limits
    clear_rate_limits(ip="127.0.0.1", key="auth_forgot")
    clear_rate_limits(ip="127.0.0.1", key="password_reset_code")


def _fresh_phone():
    suffix = _uuid.uuid4().hex[:7]
    digits = "".join(c for c in suffix if c.isdigit()).ljust(7, "1")[:7]
    return f"+1555{digits}"


@th.django_unit_setup()
def setup_forgot_phone(opts):
    """Create a phone-only user (no email) for SMS-reset tests."""
    from mojo.apps.account.models import User

    suffix = _uuid.uuid4().hex[:8]
    opts.phone_only_phone = _fresh_phone()
    opts.phone_only_username = opts.phone_only_phone  # mirrors phone-as-identity register
    opts.phone_only_password = "Pw##99Phone"

    # Clean up from previous runs
    User.objects.filter(phone_number=opts.phone_only_phone).delete()
    User.objects.filter(username=opts.phone_only_username).delete()

    u = User(username=opts.phone_only_username, email=None)
    u.phone_number = opts.phone_only_phone
    u.is_phone_verified = True
    u.set_password(opts.phone_only_password)
    u.save()
    opts.phone_only_user_id = u.pk

    # Also create an email-only user for the regression test
    opts.email_user_email = f"email_only_{suffix}@forgot.test"
    User.objects.filter(email=opts.email_user_email).delete()
    e = User(username=opts.email_user_email, email=opts.email_user_email)
    e.is_email_verified = True
    e.set_password("Pw##99Email")
    e.save()
    opts.email_user_id = e.pk


@th.django_unit_test("forgot: phone-only user (no email) dispatches SMS code automatically")
def test_forgot_phone_only_user_auto_sms(opts):
    from mojo.apps.account.models import User
    _clear_forgot_limits()

    resp = opts.client.post(
        "/api/auth/forgot",
        {"phone_number": opts.phone_only_phone, "method": "code"})
    assert resp.status_code == 200, \
        f"forgot must return 200, got {resp.status_code}: {opts.client.last_response.body}"

    # The code lands in the user's password_reset_code secret. Read it back
    # to confirm dispatch fired without needing to mock the SMS gateway.
    user = User.objects.get(pk=opts.phone_only_user_id)
    code = user.get_secret("password_reset_code")
    ts = user.get_secret("password_reset_code_ts")
    assert code and len(code) == 6 and code.isdigit(), \
        f"password_reset_code must be a 6-digit string after SMS dispatch, got {code!r}"
    assert ts, "password_reset_code_ts must be recorded"


@th.django_unit_test("forgot: phone path + reset code completes login")
def test_forgot_phone_reset_code_full_flow(opts):
    from mojo.apps.account.models import User
    _clear_forgot_limits()

    # Step 1: request SMS code
    start = opts.client.post(
        "/api/auth/forgot",
        {"phone_number": opts.phone_only_phone, "method": "code"})
    assert start.status_code == 200, \
        f"forgot must succeed, got {start.status_code}: {opts.client.last_response.body}"

    # Read the code that was generated server-side
    user = User.objects.get(pk=opts.phone_only_user_id)
    code = user.get_secret("password_reset_code")
    assert code, "code must be set after SMS request"

    # Step 2: submit code + new password
    new_password = "NewPw##99Phone"
    finish = opts.client.post(
        "/api/auth/password/reset/code",
        {"phone_number": opts.phone_only_phone, "code": code, "new_password": new_password})
    assert finish.status_code == 200, \
        f"reset/code must succeed for phone path, got {finish.status_code}: {opts.client.last_response.body}"

    # The new password works
    user.refresh_from_db()
    assert user.check_password(new_password), \
        "user.check_password must accept the new password after reset"


@th.django_unit_test("forgot: email-only user with method=code still emails (regression)")
def test_forgot_email_user_still_emails(opts):
    from mojo.apps.account.models import User
    _clear_forgot_limits()

    resp = opts.client.post(
        "/api/auth/forgot",
        {"email": opts.email_user_email, "method": "code"})
    assert resp.status_code == 200, \
        f"email-user forgot must succeed, got {resp.status_code}: {opts.client.last_response.body}"

    # Code is set; the email path also stores it on the user. The dispatch
    # itself is mocked-out by phonehub/email in the test environment, but
    # the code presence proves the email branch ran (and not the SMS one —
    # which would have failed for a user with no phone_number).
    user = User.objects.get(pk=opts.email_user_id)
    code = user.get_secret("password_reset_code")
    assert code and len(code) == 6, \
        f"password_reset_code must be set for email-user path, got {code!r}"


@th.django_unit_test("forgot: channel=sms forces SMS for user with both email and phone")
def test_forgot_channel_sms_forces_sms(opts):
    from mojo.apps.account.models import User
    _clear_forgot_limits()
    # Augment the email user with a phone number so channel=sms is meaningful.
    user = User.objects.get(pk=opts.email_user_id)
    user.phone_number = _fresh_phone()
    user.save()
    try:
        resp = opts.client.post(
            "/api/auth/forgot",
            {"email": opts.email_user_email, "method": "code", "channel": "sms"})
        assert resp.status_code == 200, \
            f"forgot with channel=sms must succeed, got {resp.status_code}: {opts.client.last_response.body}"
        # Code was stored — same secret key for either channel.
        user.refresh_from_db()
        code = user.get_secret("password_reset_code")
        assert code and len(code) == 6, \
            f"password_reset_code must be set when channel=sms forces SMS path, got {code!r}"
    finally:
        user.phone_number = None
        user.save()


@th.django_unit_test("forgot: unknown identifier returns generic 200 (no enumeration)")
def test_forgot_unknown_user_no_enumeration(opts):
    _clear_forgot_limits()
    resp = opts.client.post(
        "/api/auth/forgot",
        {"phone_number": "+15555550009", "method": "code"})
    assert resp.status_code == 200, \
        f"unknown identifier must return generic 200 to avoid enumeration, got {resp.status_code}: {opts.client.last_response.body}"
