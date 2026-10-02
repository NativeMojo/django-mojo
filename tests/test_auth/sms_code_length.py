"""
SMS_OTP_LENGTH: how many digits an SMS sign-in code has.

The setting covers the two SMS codes that can sign someone in:

  - the SMS sign-in / second-factor code (`auth/sms/login`, `auth/sms/send`,
    checked by `auth/sms/verify`), and
  - the phone sign-up code (`auth/phone/register/start`, checked by
    `auth/phone/register/verify`), which signs in an existing account when the
    number already has one.

Contracts enforced:

  - unset, both codes are 6 digits, as before;
  - the allowed range is 6 to 10: a value below 6 reads as 6, so the setting
    can only lengthen a code, a value above 10 reads as 10, and a value that
    is not a number reads as 6;
  - a longer code is stored, sent and accepted end to end, over HTTP;
  - a code already sent keeps the length it was sent with;
  - the hosted sign-in and register pages size their code box from the
    setting, so a longer code can be typed.

A non-default length is driven through the keyword-only `length=` seams and a
template context value. No setting is written and nothing is patched: this
package is default_core.
"""
import uuid

from testit import helpers as th
from testit.helpers import assert_eq, assert_true

TESTIT_TIER = "framework"

PWORD = "scl##mojo99Length"
USERS = {
    "scl_default": "+15550006751",
    "scl_long": "+15550006752",
    "scl_live": "+15550006753",
    "scl_floor": "+15550006754",
}
REGISTER_PHONE = "+15550006755"


def _fresh(pk):
    from mojo.apps.account.models import User
    return User.objects.get(pk=pk)


def _new_ip():
    octets = uuid.uuid4().int
    return "10.%d.%d.%d" % ((octets >> 16) & 0xFF, (octets >> 8) & 0xFF, octets & 0xFF)


def _own_client(opts):
    """A client with this run's own source address, so the per-IP limits on
    the verify endpoints are counted apart from every other module's."""
    from testit.client import RestClient
    client = RestClient(opts.client.host)
    client.headers["X-Real-IP"] = opts.scl_ip
    return client


class _FakeSend:
    """Stand-in for phonehub.send_sms, injected through the `send=` seam."""

    def __init__(self):
        self.calls = []

    def __call__(self, phone_number, message, **kwargs):
        from mojo.apps.phonehub.models import SMS
        self.calls.append(dict(phone_number=phone_number, message=message))
        # An UNSAVED row in the real result shape: accepted by the transport.
        return SMS(direction="outbound", from_number="+15550000000",
                   to_number=phone_number, body=message, status="sent")


def _clear_code(pk):
    from mojo.decorators import limits
    user = _fresh(pk)
    user.set_secret("sms_otp_code", None)
    user.set_secret("sms_otp_ts", None)
    user.save()
    limits.clear_code_attempts("sms", pk)
    limits.clear_code_sends("sms", pk)


def _send(pk, **kwargs):
    """Send the account's SMS code in-process. Returns (code stored, texts sent)."""
    from mojo.apps.account.rest.sms import _send_otp
    send = _FakeSend()
    _send_otp(_fresh(pk), None, send=send, **kwargs)
    return _fresh(pk).get_secret("sms_otp_code"), send.calls


@th.django_unit_setup()
def setup_sms_code_length(opts):
    from mojo.apps.account.models import User
    from mojo.decorators import limits

    opts.scl_ip = _new_ip()
    opts.scl = _own_client(opts)
    # Delete before creating: the suite runs against a long-lived database.
    User.objects.filter(username__in=list(USERS)).delete()
    User.objects.filter(phone_number__in=list(USERS.values())).update(phone_number=None)
    for name, phone in USERS.items():
        user = User(username=name, display_name=name, email=f"{name}@example.com")
        user.phone_number = phone
        user.is_phone_verified = True
        user.save()
        user.is_email_verified = True
        user.save_password(PWORD)
        setattr(opts, f"{name}_id", user.pk)
        _clear_code(user.pk)
    limits.clear_code_attempts("phone_register", REGISTER_PHONE)
    limits.clear_code_sends("phone_register", REGISTER_PHONE)


# -----------------------------------------------------------------
# The setting's range
# -----------------------------------------------------------------

@th.django_unit_test("sms code length: unset, the length is 6")
def test_default_length_is_six(opts):
    from mojo.apps.account.utils import tokens
    assert_eq(tokens.sms_otp_length(), 6,
              "with SMS_OTP_LENGTH unset the SMS code must stay 6 digits")


@th.django_unit_test("sms code length: 6 to 10 is allowed; below 6 reads as 6, above 10 as 10")
def test_length_range(opts):
    from mojo.apps.account.utils import tokens
    for given, expected in ((6, 6), (7, 7), (8, 8), (10, 10), ("8", 8)):
        assert_eq(tokens.sms_otp_length(given), expected,
                  f"a length of {given!r} is inside the range and must be used as it is")
    for given in (5, 4, 1, 0, -3):
        assert_eq(tokens.sms_otp_length(given), 6,
                  f"a length of {given!r} must read as 6: the setting may never shorten the code")
    for given in (11, 12, 99):
        assert_eq(tokens.sms_otp_length(given), 10,
                  f"a length of {given!r} must read as 10, the longest allowed")
    for given in ("eight", "", [], {}):
        assert_eq(tokens.sms_otp_length(given), 6,
                  f"a length of {given!r} is not a number and must read as 6")


# -----------------------------------------------------------------
# SMS sign-in code
# -----------------------------------------------------------------

@th.django_unit_test("sms code length: unset, the sign-in code sent is 6 digits")
def test_sign_in_code_default(opts):
    pk = opts.scl_default_id
    _clear_code(pk)
    code, texts = _send(pk)
    assert_true(isinstance(code, str) and len(code) == 6 and code.isdigit(),
                f"with the setting unset the stored code must be 6 digits, got {code!r}")
    assert_eq(len(texts), 1, f"one text must be sent, got {len(texts)}")
    assert_true(code in texts[0]["message"],
                "the text must carry the code that was stored")
    _clear_code(pk)


@th.django_unit_test("sms code length: an 8-digit code is stored, sent and signs the account in")
def test_sign_in_code_longer(opts):
    pk = opts.scl_long_id
    _clear_code(pk)
    code, texts = _send(pk, length=8)
    assert_true(isinstance(code, str) and len(code) == 8 and code.isdigit(),
                f"at length 8 the stored code must be 8 digits, got {code!r}")
    assert_true(code in texts[0]["message"],
                "the text must carry the 8-digit code that was stored")

    opts.scl.logout()
    resp = opts.scl.post("/api/auth/sms/verify", {"username": "scl_long", "code": code[:6]})
    assert_eq(resp.status_code, 401,
              f"the first 6 digits of an 8-digit code must not sign in, got {resp.status_code}")
    resp = opts.scl.post("/api/auth/sms/verify", {"username": "scl_long", "code": code})
    assert_eq(resp.status_code, 200,
              f"the full 8-digit code must sign the account in, got {resp.status_code}: "
              f"{opts.scl.last_response.body}")
    assert_true(_fresh(pk).get_secret("sms_otp_code") is None,
                "a code that signed in must be used up")
    _clear_code(pk)


@th.django_unit_test("sms code length: a code already sent keeps its length when the setting changes")
def test_live_code_keeps_its_length(opts):
    pk = opts.scl_live_id
    _clear_code(pk)
    first, _ = _send(pk, length=6)
    again, texts = _send(pk, length=8)
    assert_eq(again, first,
              "a live code must be sent again as it is, not replaced by a longer one")
    assert_true(first in texts[0]["message"],
                "the second text must carry the same live code")

    _clear_code(pk)
    first, _ = _send(pk, length=8)
    again, _ = _send(pk, length=6)
    assert_eq(again, first,
              "a live 8-digit code must be sent again as it is, not replaced by a shorter one")
    _clear_code(pk)


@th.django_unit_test("sms code length: a length below 6 still sends a 6-digit code")
def test_sign_in_code_never_shorter_than_six(opts):
    pk = opts.scl_floor_id
    _clear_code(pk)
    code, _ = _send(pk, length=4)
    assert_true(isinstance(code, str) and len(code) == 6 and code.isdigit(),
                f"a length of 4 must still give a 6-digit code, got {code!r}")
    _clear_code(pk)
    code, _ = _send(pk, length=40)
    assert_true(isinstance(code, str) and len(code) == 10 and code.isdigit(),
                f"a length of 40 must give a 10-digit code, got {code!r}")
    _clear_code(pk)


# -----------------------------------------------------------------
# Phone sign-up code
# -----------------------------------------------------------------

@th.django_unit_test("sms code length: unset, the sign-up code is 6 digits; below 6 is still 6")
def test_sign_up_code_default_and_floor(opts):
    from mojo.apps.account.services import phone_register
    _token, code, _ttl = phone_register.start(REGISTER_PHONE)
    assert_true(isinstance(code, str) and len(code) == 6 and code.isdigit(),
                f"with the setting unset the sign-up code must be 6 digits, got {code!r}")
    _token, code, _ttl = phone_register.start(REGISTER_PHONE, length=3)
    assert_true(isinstance(code, str) and len(code) == 6 and code.isdigit(),
                f"a length of 3 must still give a 6-digit sign-up code, got {code!r}")


@th.django_unit_test("sms code length: an 8-digit sign-up code is accepted over HTTP")
def test_sign_up_code_longer(opts):
    from mojo.apps.account.services import phone_register
    from mojo.decorators import limits
    limits.clear_code_attempts("phone_register", REGISTER_PHONE)
    session, code, _ttl = phone_register.start(REGISTER_PHONE, length=8)
    assert_true(isinstance(code, str) and len(code) == 8 and code.isdigit(),
                f"at length 8 the sign-up code must be 8 digits, got {code!r}")

    opts.scl.logout()
    resp = opts.scl.post("/api/auth/phone/register/verify",
                         {"session_token": session, "code": code[:6]})
    assert_eq(resp.status_code, 400,
              f"the first 6 digits of an 8-digit sign-up code must be refused, got {resp.status_code}")
    resp = opts.scl.post("/api/auth/phone/register/verify",
                         {"session_token": session, "code": code})
    assert_eq(resp.status_code, 200,
              f"the full 8-digit sign-up code must verify the phone, got {resp.status_code}: "
              f"{opts.scl.last_response.body}")
    data = (opts.scl.last_response.body or {}).get("data") or {}
    assert_true(bool(data.get("verified_phone_token")),
                f"a verified sign-up code must mint a verified_phone_token, got {data}")
    limits.clear_code_attempts("phone_register", REGISTER_PHONE)


# -----------------------------------------------------------------
# Hosted pages
# -----------------------------------------------------------------

def _render(template_name, length=None):
    from django.shortcuts import render
    from django.test import RequestFactory
    from mojo.apps.account.rest.bouncer.views import _auth_context
    is_login = "login" in template_name
    request = RequestFactory().get("/auth" if is_login else "/register")
    ctx = _auth_context(request, group=None)
    ctx["page_mode"] = "login" if is_login else "register"
    ctx["page_title"] = "Sign In" if is_login else "Create Account"
    if is_login:
        # Show the SMS view whatever sign-in methods this deployment has on.
        ctx["login_methods"] = ["password", "sms"]
    else:
        # The SMS code step only renders in the phone-first stepped flow.
        ctx["register_step2_active"] = True
    if length is not None:
        ctx["sms_code_length"] = length
    return ctx, render(request, template_name, ctx).content.decode("utf-8")


def _code_input(html, input_id):
    """The one <input> tag carrying this id."""
    start = html.rfind("<input", 0, html.index(f'id="{input_id}"'))
    return html[start:html.index(">", start) + 1]


@th.django_unit_test("sms code length: the hosted pages are given the length, 6 by default")
def test_hosted_pages_default(opts):
    ctx, html = _render("account/login.html")
    assert_eq(ctx.get("sms_code_length"), 6,
              "the page context must carry the SMS code length, 6 with the setting unset")
    tag = _code_input(html, "sms-code")
    assert_true('maxlength="6"' in tag,
                f"with the setting unset the sign-in code box must take 6 characters, got {tag}")
    assert_true("We'll text a 6-digit code only if" in html,
                "with the setting unset the sign-in page's wording must be unchanged")

    _ctx, html = _render("account/register.html")
    tag = _code_input(html, "reg-phone-code")
    assert_true('maxlength="6"' in tag,
                f"with the setting unset the sign-up code box must take 6 characters, got {tag}")
    assert_true("6-digit code" in html,
                "with the setting unset the register page must say '6-digit code'")


@th.django_unit_test("sms code length: at 8 the hosted code boxes take 8 characters and say so")
def test_hosted_pages_longer(opts):
    _ctx, html = _render("account/login.html", length=8)
    tag = _code_input(html, "sms-code")
    assert_true('maxlength="8"' in tag,
                f"at length 8 the sign-in code box must take 8 characters, got {tag}")
    assert_true('placeholder="8-digit code"' in tag,
                f"at length 8 the sign-in code box must say '8-digit code', got {tag}")
    assert_true("We'll text an 8-digit code" in html and "Enter the 8-digit code." in html,
                "at length 8 the sign-in page's SMS wording must say 8 digits")

    _ctx, html = _render("account/register.html", length=8)
    tag = _code_input(html, "reg-phone-code")
    assert_true('maxlength="8"' in tag,
                f"at length 8 the sign-up code box must take 8 characters, got {tag}")
    assert_true('placeholder="8-digit code"' in tag,
                f"at length 8 the sign-up code box must say '8-digit code', got {tag}")
    assert_true('"We sent an 8-digit code to "' in html,
                "at length 8 the register page must say it sent an 8-digit code")
    assert_true("6-digit" not in html,
                "at length 8 the register page must not still say 6 digits anywhere")
