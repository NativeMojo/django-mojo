"""
SMS_OTP_LENGTH, set for real.

The default-tier tests in tests/test_auth/sms_code_length.py drive a
non-default length through keyword-only seams, because a default-tier test may
not write a setting. These tests write the real DB-backed setting, the way an
operator would, and go through the server: `auth/sms/login` stores a code of
the configured length, `auth/sms/verify` signs in with it, and the hosted
sign-in page sizes its code box from it. A value below 6 still gives 6 digits.

Serial and opt-in: a Setting row is process-wide state.
"""
import uuid

from testit import helpers as th
from testit.helpers import assert_eq, assert_true

KEY = "SMS_OTP_LENGTH"
USERNAME = "scls_user"
PHONE = "+15550006756"
PWORD = "scls##mojo99Length"


def _fresh(pk):
    from mojo.apps.account.models import User
    return User.objects.get(pk=pk)


def _reset(pk):
    """No live code and no send or try counted, so each test starts clean."""
    from mojo.decorators import limits
    user = _fresh(pk)
    user.set_secret("sms_otp_code", None)
    user.set_secret("sms_otp_ts", None)
    user.save()
    limits.clear_code_attempts("sms", pk)
    limits.clear_code_sends("sms", pk)


def _request_code(opts):
    """Ask the server for a sign-in code; return the code it stored."""
    opts.scls.logout()
    resp = opts.scls.post("/api/auth/sms/login", {"username": USERNAME})
    assert_eq(resp.status_code, 200,
              f"auth/sms/login must answer 200, got {resp.status_code}: "
              f"{opts.scls.last_response.body}")
    return _fresh(opts.scls_user_id).get_secret("sms_otp_code")


@th.django_unit_setup()
def setup_sms_code_length_setting(opts):
    from testit.client import RestClient
    from mojo.apps.account.models import User
    from mojo.apps.account.models.setting import Setting

    Setting.remove(KEY)
    # This run's own source address, so the per-IP limits are its own.
    octets = uuid.uuid4().int
    client = RestClient(opts.client.host)
    client.headers["X-Real-IP"] = "10.%d.%d.%d" % (
        (octets >> 16) & 0xFF, (octets >> 8) & 0xFF, octets & 0xFF)
    opts.scls = client

    # Long-lived DB: delete before creating.
    User.objects.filter(username=USERNAME).delete()
    User.objects.filter(phone_number=PHONE).update(phone_number=None)
    user = User(username=USERNAME, display_name=USERNAME, email=f"{USERNAME}@example.com")
    user.phone_number = PHONE
    user.is_phone_verified = True
    user.save()
    user.is_email_verified = True
    user.save_password(PWORD)
    opts.scls_user_id = user.pk
    _reset(user.pk)


@th.django_unit_test("SMS_OTP_LENGTH unset: the server stores a 6-digit sign-in code")
def test_unset_is_six(opts):
    from mojo.apps.account.models.setting import Setting
    Setting.remove(KEY)
    _reset(opts.scls_user_id)
    code = _request_code(opts)
    assert_true(isinstance(code, str) and len(code) == 6 and code.isdigit(),
                f"with SMS_OTP_LENGTH unset the stored code must be 6 digits, got {code!r}")
    _reset(opts.scls_user_id)


@th.django_unit_test("SMS_OTP_LENGTH=8: the server stores an 8-digit code and signs in with it")
def test_set_to_eight(opts):
    from mojo.apps.account.models.setting import Setting
    from mojo.apps.account.utils import tokens
    _reset(opts.scls_user_id)
    try:
        Setting.set(KEY, "8")
        assert_eq(tokens.sms_otp_length(), 8,
                  "a stored SMS_OTP_LENGTH of '8' must be read as the number 8")
        code = _request_code(opts)
        assert_true(isinstance(code, str) and len(code) == 8 and code.isdigit(),
                    f"with SMS_OTP_LENGTH=8 the stored code must be 8 digits, got {code!r}")
        resp = opts.scls.post("/api/auth/sms/verify", {"username": USERNAME, "code": code})
        assert_eq(resp.status_code, 200,
                  f"the 8-digit code must sign the account in, got {resp.status_code}: "
                  f"{opts.scls.last_response.body}")
    finally:
        Setting.remove(KEY)
        _reset(opts.scls_user_id)


@th.django_unit_test("SMS_OTP_LENGTH below 6 or not a number: the code is still 6 digits")
def test_set_too_short_or_garbage(opts):
    from mojo.apps.account.models.setting import Setting
    from mojo.apps.account.utils import tokens
    try:
        for stored in ("4", "0", "four"):
            _reset(opts.scls_user_id)
            Setting.set(KEY, stored)
            assert_eq(tokens.sms_otp_length(), 6,
                      f"a stored SMS_OTP_LENGTH of {stored!r} must read as 6")
            code = _request_code(opts)
            assert_true(isinstance(code, str) and len(code) == 6 and code.isdigit(),
                        f"with SMS_OTP_LENGTH={stored!r} the stored code must be 6 digits, "
                        f"got {code!r}")
        _reset(opts.scls_user_id)
        Setting.set(KEY, "25")
        assert_eq(tokens.sms_otp_length(), 10,
                  "a stored SMS_OTP_LENGTH of '25' must read as 10, the longest allowed")
    finally:
        Setting.remove(KEY)
        _reset(opts.scls_user_id)


def _sign_in_box(length_note):
    """Render the hosted sign-in page now; return how many characters its code box takes."""
    import re
    from django.shortcuts import render
    from django.test import RequestFactory
    from mojo.apps.account.rest.bouncer.views import _auth_context
    request = RequestFactory().get("/auth")
    ctx = _auth_context(request, group=None)
    ctx["page_mode"] = "login"
    ctx["page_title"] = "Sign In"
    ctx["login_methods"] = ["password", "sms"]
    html = render(request, "account/login.html", ctx).content.decode("utf-8")
    start = html.rfind("<input", 0, html.index('id="sms-code"'))
    tag = html[start:html.index(">", start) + 1]
    found = re.search(r'maxlength="(\d+)"', tag)
    assert_true(found is not None, f"{length_note}: the code box must carry a maxlength, got {tag}")
    return ctx, tag, int(found.group(1))


@th.django_unit_test("SMS_OTP_LENGTH=8: the hosted sign-in page says 8 digits and its box takes the code")
def test_hosted_page_follows_the_setting(opts):
    from mojo.apps.account.models.setting import Setting
    try:
        Setting.set(KEY, "8")
        ctx, tag, box = _sign_in_box("SMS_OTP_LENGTH=8")
        assert_eq(ctx.get("sms_code_length"), 8,
                  "the page context must carry the configured SMS code length")
        assert_true(box >= 8,
                    f"with SMS_OTP_LENGTH=8 the sign-in code box must take 8 characters, got {tag}")
        assert_true('placeholder="8-digit code"' in tag,
                    f"with SMS_OTP_LENGTH=8 the sign-in code box must say '8-digit code', got {tag}")
    finally:
        Setting.remove(KEY)


@th.django_unit_test("SMS_OTP_LENGTH lowered 8 to 6 with a code live: the page takes it and it signs in")
def test_lowered_with_a_live_code(opts):
    from mojo.apps.account.models.setting import Setting
    _reset(opts.scls_user_id)
    try:
        Setting.set(KEY, "8")
        live = _request_code(opts)
        assert_true(isinstance(live, str) and len(live) == 8,
                    f"with SMS_OTP_LENGTH=8 the stored code must be 8 digits, got {live!r}")
        Setting.set(KEY, "6")
        again = _request_code(opts)
        assert_eq(again, live,
                  "asking again while the 8-digit code is live must send that same code")
        _ctx, tag, box = _sign_in_box("SMS_OTP_LENGTH lowered to 6")
        assert_true(box >= len(live),
                    f"the sign-in code box takes {box} characters, but the live code has {len(live)}: {tag}")
        resp = opts.scls.post("/api/auth/sms/verify", {"username": USERNAME, "code": live})
        assert_eq(resp.status_code, 200,
                  f"the live 8-digit code must still sign the account in, got {resp.status_code}: "
                  f"{opts.scls.last_response.body}")
    finally:
        Setting.remove(KEY)
        _reset(opts.scls_user_id)


@th.django_unit_test("SMS_OTP_LENGTH stored as an infinite number, cache cold: sign-in still sends a 6-digit code")
def test_stored_infinite_number(opts):
    from mojo.apps.account.models.setting import Setting
    from mojo.apps.account.utils import tokens
    try:
        for stored in (float("inf"), float("-inf")):
            _reset(opts.scls_user_id)
            # A secret row keeps its type; with the cache cold the reader gets
            # the number itself, not its text.
            Setting.set(KEY, stored, is_secret=True)
            Setting.objects.get(key=KEY, group=None).remove_from_cache()
            assert_eq(tokens.sms_otp_length(), 6,
                      f"a stored SMS_OTP_LENGTH of {stored!r} must read as 6, not raise")
            Setting.objects.get(key=KEY, group=None).remove_from_cache()
            code = _request_code(opts)
            assert_true(isinstance(code, str) and len(code) == 6 and code.isdigit(),
                        f"with SMS_OTP_LENGTH={stored!r} the stored code must be 6 digits, got {code!r}")
    finally:
        Setting.remove(KEY)
        _reset(opts.scls_user_id)
