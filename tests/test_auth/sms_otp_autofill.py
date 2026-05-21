"""
Tests for SMS OTP autofill.

The OTP texts (login + registration) carry an origin-bound one-time-code
line (`@host #code`) so browsers can offer/auto-fill the code, and the hosted
login/register pages wire up the WebOTP API.

Contracts enforced:
  - _otp_sms_body appends `@host #code` when the request host is known
  - _otp_sms_body falls back to the plain message with no request
  - _otp_host derives the host from Origin, then the Host header
  - auth_base exposes the _mat.watchOtp WebOTP helper
  - login.html and register.html call watchOtp on their SMS code inputs
"""
from testit import helpers as th
from testit.helpers import assert_true, assert_eq


# ---------------------------------------------------------------------------
# Server — origin-bound OTP SMS body
# ---------------------------------------------------------------------------

@th.django_unit_test("_otp_sms_body appends the origin-bound @host #code line")
def test_otp_body_with_origin(opts):
    from django.test import RequestFactory
    from mojo.apps.account.rest.sms import _otp_sms_body
    req = RequestFactory().post("/api/auth/sms/login",
                                HTTP_ORIGIN="https://auth.example.com")
    body = _otp_sms_body("123456", req)
    assert_true("123456" in body,
                f"OTP body must contain the code, got {body!r}")
    assert_true("@auth.example.com #123456" in body,
                f"OTP body must carry the origin-bound autofill line, got {body!r}")


@th.django_unit_test("_otp_sms_body falls back to a plain message with no request")
def test_otp_body_no_request(opts):
    from mojo.apps.account.rest.sms import _otp_sms_body
    body = _otp_sms_body("654321", None)
    assert_true("654321" in body, "plain OTP body must still contain the code")
    assert_true("@" not in body,
                f"with no request host the body must omit the @host line, got {body!r}")


@th.django_unit_test("_otp_host derives the host from Origin, then the Host header")
def test_otp_host(opts):
    from django.test import RequestFactory
    from mojo.apps.account.rest.sms import _otp_host
    rf = RequestFactory()
    assert_eq(_otp_host(rf.post("/x", HTTP_ORIGIN="https://Auth.Example.COM:8443")),
              "auth.example.com",
              "host must come from Origin — lowercased, port stripped")
    req = rf.post("/x")
    req.META["HTTP_ORIGIN"] = ""
    req.META["HTTP_HOST"] = "portal.example.com:443"
    assert_eq(_otp_host(req), "portal.example.com",
              "host must fall back to the Host header when Origin is absent")
    assert_eq(_otp_host(None), "", "no request must yield an empty host")


# ---------------------------------------------------------------------------
# Client — WebOTP wiring in the hosted pages
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
    return render(request, template_name, ctx).content.decode("utf-8")


@th.django_unit_test("auth_base exposes the watchOtp WebOTP helper")
def test_auth_base_has_watchotp(opts):
    html = _render("account/login.html")
    assert_true("watchOtp: function" in html,
                "auth_base.html must expose _mat.watchOtp for SMS-code autofill")
    assert_true("OTPCredential" in html,
                "watchOtp must feature-detect window.OTPCredential (WebOTP)")


@th.django_unit_test("login.html wires WebOTP autofill into the SMS code step")
def test_login_sms_webotp_wired(opts):
    html = _render("account/login.html")
    assert_true('m.watchOtp($("sms-code")' in html,
                "login.html must call m.watchOtp on the sms-code input")
    assert_true('autocomplete="one-time-code"' in html,
                "the SMS code input must keep autocomplete=one-time-code for iOS")


@th.django_unit_test("register.html wires WebOTP autofill into the stepped SMS verify")
def test_register_sms_webotp_wired(opts):
    html = _render("account/register.html")
    assert_true("function watchRegOtp" in html,
                "register.html must define watchRegOtp for SMS-code autofill")
    assert_true('m.watchOtp($("reg-phone-code")' in html,
                "register.html must call m.watchOtp on the reg-phone-code input")
