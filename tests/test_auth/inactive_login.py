"""Maestro item #9463 — no sign-in route issues a session to a disabled account.

Password login answered a correct password for a deactivated user with 200 and
a token pair, and so did TOTP, SMS, reset-by-code, magic-link and email-change
sign-in. Every route now refuses before anything is minted:

- a step that checks a secret the person types (password, TOTP code, SMS code,
  reset code) answers exactly as it does for a wrong secret or an unknown
  account, so it confirms neither the secret nor the account's state (#5103);
- a step that redeems a server-issued link (magic link, email-change link)
  answers 403 "Account is disabled", like invite, reset link and email verify
  (#1955).

Each test disables its own user through the ORM; no settings are changed.
"""
import time
import uuid

from testit import helpers as th
from testit.helpers import assert_true, assert_eq

PWORD = "iul##mojo99Inactive"
NEW_PWORD = "iul##Reset77Strong"
RIGHT = "246810"
NEW_EMAIL = "iul_change_new@example.com"
USERS = {
    "iul_active": None,
    "iul_pass": None,
    "iul_mfa": None,
    "iul_totp": None,
    "iul_sms": "+15550009463",
    "iul_reset": None,
    "iul_magic": None,
    "iul_change": None,
    "iul_backstop": None,
}
TOTP_USERS = ("iul_mfa", "iul_totp")
KINDS = ("sms", "reset", "email_change", "totp", "totp_login")


def _fresh(pk):
    from mojo.apps.account.models import User
    return User.objects.get(pk=pk)


def _disable(pk):
    from mojo.apps.account.models import User
    User.objects.filter(pk=pk).update(is_active=False)


def _seed(pk, **secrets):
    user = _fresh(pk)
    for key, value in secrets.items():
        user.set_secret(key, value)
    user.save()


def _new_ip():
    octets = uuid.uuid4().int
    return "10.%d.%d.%d" % ((octets >> 16) & 0xFF, (octets >> 8) & 0xFF, octets & 0xFF)


def _post(opts, path, data):
    """Each request from its own address, so per-IP limits shared with other
    modules posting from loopback never decide a result here."""
    opts.iul.logout()
    resp = opts.iul.post(path, data, headers={"X-Real-IP": _new_ip()})
    return resp, opts.iul.last_response.body


def _assert_no_session(body, what):
    text = str(body)
    for name in ("access_token", "refresh_token", "mfa_token"):
        assert_true(name not in text, f"{what}: no {name} may be issued, got {body}")


def _totp_now(secret):
    import pyotp
    return pyotp.TOTP(secret).now()


@th.django_unit_setup()
def setup_inactive_login(opts):
    from mojo.apps.account.models import User
    from mojo.apps.account.models.totp import UserTOTP
    from mojo.decorators import limits
    from testit.client import RestClient
    import pyotp

    opts.iul = RestClient(opts.client.host)
    User.objects.filter(username__in=list(USERS)).delete()
    User.objects.filter(phone_number__in=[p for p in USERS.values() if p]).update(phone_number=None)
    User.objects.filter(email=NEW_EMAIL).delete()
    for name, phone in USERS.items():
        user = User(username=name, display_name=name, email=f"{name}@example.com")
        user.phone_number = phone
        user.save()
        user.is_email_verified = True
        user.is_phone_verified = bool(phone)
        user.requires_mfa = name == "iul_mfa"
        user.save_password(PWORD)
        setattr(opts, f"{name}_id", user.pk)
        for kind in KINDS:
            limits.clear_code_attempts(kind, user.pk)
        limits.clear_account_limits(user.pk)
        limits.clear_rate_limits(key="login", account_id=user.pk)

    for name in TOTP_USERS:
        totp = UserTOTP(user=_fresh(getattr(opts, f"{name}_id")))
        secret = pyotp.random_base32()
        totp.set_secret("totp_secret", secret)
        totp.is_enabled = True
        totp.save()
        setattr(opts, f"{name}_secret", secret)


# -----------------------------------------------------------------
# Typed secrets: the same answer as a wrong secret
# -----------------------------------------------------------------

@th.django_unit_test("inactive login: an active user's password login is unchanged")
def test_active_password_login(opts):
    resp, body = _post(opts, "/api/login", {"username": "iul_active", "password": PWORD})
    assert_eq(resp.status_code, 200, f"an active user must still sign in, got {body}")
    assert_true("access_token" in str(body), f"an active user must get tokens, got {body}")


@th.django_unit_test("inactive login: password login refuses like a wrong password")
def test_password_login_refused(opts):
    pk = opts.iul_pass_id
    _disable(pk)
    wrong, wrong_body = _post(opts, "/api/login", {"username": "iul_pass", "password": PWORD + "x"})
    resp, body = _post(opts, "/api/login", {"username": "iul_pass", "password": PWORD})
    _assert_no_session(body, "password login")
    assert_eq(resp.status_code, 401, f"a disabled account must be refused with 401, got {body}")
    assert_eq(body, wrong_body,
              "the refusal must be byte-identical to a wrong password, so it confirms "
              "neither the password nor the account's state")
    assert_eq(wrong.status_code, resp.status_code, "the status must match a wrong password")


@th.django_unit_test("inactive login: password login with TOTP issues no mfa_token")
def test_password_login_mfa_refused(opts):
    from mojo.apps.account.models import User

    pk = opts.iul_mfa_id
    User.objects.filter(pk=pk).update(is_active=True)
    resp, body = _post(opts, "/api/login", {"username": "iul_mfa", "password": PWORD})
    assert_true("mfa_token" in str(body),
                f"control: an active MFA user's password login must reach the MFA branch, got {body}")
    _disable(pk)
    resp, body = _post(opts, "/api/login", {"username": "iul_mfa", "password": PWORD})
    _assert_no_session(body, "password login, MFA branch")
    assert_eq(resp.status_code, 401, f"the MFA branch must be refused with 401, got {body}")


@th.django_unit_test("inactive login: TOTP second step refuses an mfa_token minted while active")
def test_totp_verify_refused(opts):
    from mojo.apps.account.services import mfa as mfa_service

    pk = opts.iul_mfa_id
    from mojo.apps.account.models import User
    User.objects.filter(pk=pk).update(is_active=True)
    token = mfa_service.create_mfa_token(_fresh(pk), ["totp"])
    _disable(pk)
    resp, body = _post(opts, "/api/auth/totp/verify",
                       {"mfa_token": token, "code": _totp_now(opts.iul_mfa_secret)})
    _assert_no_session(body, "TOTP second step")
    assert_true(resp.status_code in (401, 403), f"must be refused, got {resp.status_code}: {body}")


@th.django_unit_test("inactive login: TOTP passwordless login refuses")
def test_totp_login_refused(opts):
    _disable(opts.iul_totp_id)
    resp, body = _post(opts, "/api/auth/totp/login",
                       {"username": "iul_totp", "code": _totp_now(opts.iul_totp_secret)})
    _assert_no_session(body, "TOTP login")
    assert_true(resp.status_code in (401, 403), f"must be refused, got {resp.status_code}: {body}")


@th.django_unit_test("inactive login: SMS code login refuses")
def test_sms_login_refused(opts):
    pk = opts.iul_sms_id
    _disable(pk)
    _seed(pk, sms_otp_code=RIGHT, sms_otp_ts=int(time.time()))
    resp, body = _post(opts, "/api/auth/sms/verify", {"username": "iul_sms", "code": RIGHT})
    _assert_no_session(body, "SMS login")
    assert_true(resp.status_code in (401, 403), f"must be refused, got {resp.status_code}: {body}")


@th.django_unit_test("inactive login: SMS second step refuses an mfa_token minted while active")
def test_sms_mfa_refused(opts):
    from mojo.apps.account.models import User
    from mojo.apps.account.services import mfa as mfa_service

    pk = opts.iul_sms_id
    User.objects.filter(pk=pk).update(is_active=True)
    token = mfa_service.create_mfa_token(_fresh(pk), ["sms"])
    _disable(pk)
    _seed(pk, sms_otp_code=RIGHT, sms_otp_ts=int(time.time()))
    resp, body = _post(opts, "/api/auth/sms/verify", {"mfa_token": token, "code": RIGHT})
    _assert_no_session(body, "SMS second step")
    assert_true(resp.status_code in (401, 403), f"must be refused, got {resp.status_code}: {body}")


@th.django_unit_test("inactive login: reset by code answers Invalid code and sets no password")
def test_reset_code_refused(opts):
    pk = opts.iul_reset_id
    _disable(pk)
    _seed(pk, password_reset_code=RIGHT, password_reset_code_ts=int(time.time()))
    resp, body = _post(opts, "/api/auth/password/reset/code",
                       {"username": "iul_reset", "code": RIGHT, "new_password": NEW_PWORD})
    _assert_no_session(body, "reset by code")
    assert_true("Invalid code" in str(body),
                f"a disabled account must get the unknown-account answer, got {body}")
    user = _fresh(pk)
    assert_true(user.check_password(PWORD), "the old password must still be the password")
    assert_true(not user.check_password(NEW_PWORD), "no new password may be set")


# -----------------------------------------------------------------
# Server-issued links: 403 "Account is disabled"
# -----------------------------------------------------------------

@th.django_unit_test("inactive login: magic link answers 403 Account is disabled")
def test_magic_link_refused(opts):
    from mojo.apps.account.utils import tokens

    pk = opts.iul_magic_id
    token = tokens.generate_magic_login_token(_fresh(pk))
    _disable(pk)
    resp, body = _post(opts, "/api/auth/magic/login", {"token": token})
    _assert_no_session(body, "magic link")
    assert_eq(resp.status_code, 403, f"a disabled account's link must be refused with 403, got {body}")
    assert_true("Account is disabled" in str(body), f"the reason must be given, got {body}")


@th.django_unit_test("inactive login: email-change link answers 403 and keeps the email")
def test_email_change_link_refused(opts):
    from mojo.apps.account.utils import tokens

    pk = opts.iul_change_id
    token = tokens.generate_email_change_token(_fresh(pk), NEW_EMAIL)
    _disable(pk)
    resp, body = _post(opts, "/api/auth/email/change/confirm", {"token": token})
    _assert_no_session(body, "email-change link")
    assert_eq(resp.status_code, 403, f"a disabled account's link must be refused with 403, got {body}")
    assert_true("Account is disabled" in str(body), f"the reason must be given, got {body}")
    assert_eq(_fresh(pk).email, "iul_change@example.com", "the email must not change")


# -----------------------------------------------------------------
# The backstop
# -----------------------------------------------------------------

@th.django_unit_test("inactive login: jwt_login itself never mints for a disabled account")
def test_jwt_login_backstop(opts):
    from mojo import errors as merrors
    from mojo.apps.account.rest.user import jwt_login

    pk = opts.iul_backstop_id
    _disable(pk)
    refused = None
    try:
        jwt_login(None, _fresh(pk), source="password")
    except merrors.PermissionDeniedException as err:
        refused = err
    assert_true(refused is not None, "jwt_login must refuse a disabled account")
    assert_eq(refused.status, 401, "the backstop refusal must be a 401")

