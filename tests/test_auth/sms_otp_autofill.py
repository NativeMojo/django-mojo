"""
Tests for SMS OTP autofill.

The OTP code autofill relies on the platform's native one-time-code support:
the SMS body is a plain single line, and the hosted login/register pages mark
their code inputs with autocomplete="one-time-code". Chrome (Android) and iOS
both surface/auto-fill from that combination — no WebOTP API wiring needed
(the WebOTP path was dropped because it suppressed Chrome's native chip).

Contracts enforced:
  - _otp_sms_body returns a plain single line carrying the code (with or
    without a request) and never injects an `@host` binding line
  - login.html keeps autocomplete="one-time-code" on the SMS code input
  - register.html keeps autocomplete="one-time-code" on the verify-code input
"""
from testit import helpers as th
from testit.helpers import assert_true


# ---------------------------------------------------------------------------
# Server — plain OTP SMS body
# ---------------------------------------------------------------------------

@th.django_unit_test("_otp_sms_body returns a plain single line with the code")
def test_otp_body_with_request(opts):
    from django.test import RequestFactory
    from mojo.apps.account.rest.sms import _otp_sms_body
    req = RequestFactory().post("/api/auth/sms/login",
                                HTTP_ORIGIN="https://auth.example.com")
    body = _otp_sms_body("123456", req)
    assert_true("123456" in body,
                f"OTP body must contain the code, got {body!r}")
    assert_true("@" not in body,
                f"OTP body must not inject an @host binding line, got {body!r}")
    assert_true("\n" not in body.strip(),
                f"OTP body must be a single line, got {body!r}")


@th.django_unit_test("_otp_sms_body returns the same plain message with no request")
def test_otp_body_no_request(opts):
    from mojo.apps.account.rest.sms import _otp_sms_body
    body = _otp_sms_body("654321", None)
    assert_true("654321" in body, "plain OTP body must still contain the code")
    assert_true("@" not in body,
                f"with no request the body must omit any @host line, got {body!r}")


# ---------------------------------------------------------------------------
# Client — native one-time-code autofill in the hosted pages
# ---------------------------------------------------------------------------

def _render(template_name):
    from django.test import RequestFactory
    from django.shortcuts import render
    from mojo.apps.account.rest.bouncer.views import _auth_context
    is_login = "login" in template_name
    request = RequestFactory().get("/auth" if is_login else "/register")
    ctx = _auth_context(request, group=None)
    ctx["page_mode"] = "login" if is_login else "register"
    ctx["page_title"] = "Sign In" if is_login else "Create Account"
    if not is_login:
        # Exercise the phone-first stepped flow so the SMS verify-code step
        # (reg-phone-code) renders — it is gated behind register_step2_active,
        # which is only on when the register schema uses phone+SMS identity.
        ctx["register_step2_active"] = True
    return render(request, template_name, ctx).content.decode("utf-8")


@th.django_unit_test("login.html marks the SMS code input for native autofill")
def test_login_sms_code_autocomplete(opts):
    html = _render("account/login.html")
    assert_true('id="sms-code"' in html,
                "login.html must render the sms-code input")
    assert_true('autocomplete="one-time-code"' in html,
                "the SMS code input must keep autocomplete=one-time-code for native autofill")


@th.django_unit_test("register.html marks the verify-code input for native autofill")
def test_register_sms_code_autocomplete(opts):
    html = _render("account/register.html")
    assert_true('id="reg-phone-code"' in html,
                "register.html must render the reg-phone-code input")
    assert_true('autocomplete="one-time-code"' in html,
                "the register verify-code input must keep autocomplete=one-time-code for native autofill")
