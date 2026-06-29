"""
Tests for passwordless registration.

A `registration.fields` schema may omit `password` — the account is created
with an unusable password and the user logs in by SMS code. This is permitted
only when the schema includes an SMS-verified phone.

Contracts enforced:
  - validate_fields_config accepts a passwordless schema (phone + verify:sms)
  - validate_fields_config rejects passwordless without an SMS-verified phone
  - _normalize_field_list no longer auto-appends a password field
  - validate_payload does not require password when it is not in the schema
  - on_register creates a passwordless user (unusable password, verified phone)
  - on_register rejects a passwordless schema lacking an SMS-verified phone
  - a passwordless account can log in end-to-end via the SMS-code flow
  - default email + password registration still works (regression)
"""
import json
import uuid as _uuid

from testit import helpers as th
from testit.helpers import assert_true, assert_eq


# Passwordless: phone identity, SMS-verified, no password.
PASSWORDLESS_FIELDS = [
    {"name": "first_name", "required": True},
    {"name": "last_name", "required": True},
    {"name": "phone", "required": True, "verify": "sms"},
]
# Invalid passwordless: phone present but NOT SMS-verified, no password.
PASSWORDLESS_NO_VERIFY = [
    {"name": "first_name", "required": True},
    {"name": "phone", "required": True},
]


def _clear_limits():
    from mojo.decorators.limits import clear_rate_limits
    clear_rate_limits(ip="127.0.0.1")
    for key in ("register", "phone_register_start", "phone_register_verify"):
        clear_rate_limits(ip="127.0.0.1", key=key)


def _fresh_phone():
    # 7 fully-random digits (10M space) so parallel tests don't collide on the
    # same +1555 number — a collision pollutes account_exists / lookup checks.
    return f"+1555{_uuid.uuid4().int % 10_000_000:07d}"


def _start_and_verify_phone(opts, phone):
    """Run the phone verify-then-register flow; return a verified_phone_token."""
    from mojo.helpers.redis import get_connection
    start = opts.client.post("/api/auth/phone/register/start", {"phone": phone})
    assert start.status_code == 200, \
        f"phone-register start must succeed, got {start.status_code}: {opts.client.last_response.body}"
    session_token = start.response.data.session_token
    raw = get_connection().get(f"phone:register:session:{session_token}")
    assert raw is not None, "phone-register session must be written to redis"
    code = json.loads(raw)["code"]
    verify = opts.client.post(
        "/api/auth/phone/register/verify",
        {"session_token": session_token, "code": code})
    assert verify.status_code == 200, \
        f"phone-register verify must succeed, got {verify.status_code}: {opts.client.last_response.body}"
    return verify.response.data.verified_phone_token


# ---------------------------------------------------------------------------
# register_schema — unit
# ---------------------------------------------------------------------------

@th.django_unit_test("validate_fields_config accepts a passwordless phone+SMS schema")
def test_validate_passwordless_ok(opts):
    from mojo.apps.account.services import register_schema as rs
    fields = rs.validate_fields_config(PASSWORDLESS_FIELDS)
    names = {f["name"] for f in fields}
    assert_true("password" not in names,
                f"a passwordless schema must not gain a password field, got {names}")
    assert_true("phone" in names, "passwordless schema must keep the phone field")


@th.django_unit_test("validate_fields_config rejects a passwordless schema with no phone")
def test_validate_passwordless_no_phone(opts):
    from mojo.apps.account.services import register_schema as rs
    from mojo import errors as merrors
    try:
        rs.validate_fields_config([{"name": "email", "required": True}])
        assert False, "a no-password, no-phone schema must be rejected"
    except merrors.ValueException as e:
        assert "phone" in str(e).lower(), \
            f"error must explain the phone requirement, got: {e}"


@th.django_unit_test("validate_fields_config rejects a passwordless schema with an unverified phone")
def test_validate_passwordless_phone_not_sms_verified(opts):
    from mojo.apps.account.services import register_schema as rs
    from mojo import errors as merrors
    try:
        rs.validate_fields_config(PASSWORDLESS_NO_VERIFY)
        assert False, "no-password schema with a non-SMS-verified phone must be rejected"
    except merrors.ValueException as e:
        assert "sms" in str(e).lower(), \
            f"error must mention the SMS-verify requirement, got: {e}"


@th.django_unit_test("_normalize_field_list no longer auto-appends a password field")
def test_normalize_no_password_append(opts):
    from mojo.apps.account.services import register_schema as rs
    fields = rs._normalize_field_list(PASSWORDLESS_FIELDS)
    names = [f["name"] for f in fields]
    assert_true("password" not in names,
                f"_normalize_field_list must not append password, got {names}")


@th.django_unit_test("validate_payload does not require password when it is not in the schema")
def test_validate_payload_no_password(opts):
    from mojo.apps.account.services import register_schema as rs
    fields = [
        {"name": "phone", "required": True, "verify": "sms"},
        {"name": "first_name", "required": True, "verify": None},
    ]
    out = rs.validate_payload(
        fields,
        {"phone": "+14155551212", "first_name": "Pat"},
        identity_field="phone", min_age=None)
    assert_true("password" not in out,
                f"sanitized payload must omit password for a passwordless schema, got {out}")
    assert_eq(out["phone"], "+14155551212",
              f"phone must still be validated/normalized, got {out.get('phone')!r}")


# ---------------------------------------------------------------------------
# on_register — HTTP
# ---------------------------------------------------------------------------

@th.django_unit_test("passwordless register creates a user with no usable password")
def test_passwordless_register_creates_user(opts):
    from mojo.apps.account.models import User
    _clear_limits()
    phone = _fresh_phone()
    token = _start_and_verify_phone(opts, phone)

    resp = opts.client.post(
        "/api/auth/register",
        {"first_name": "Pat", "last_name": "Passwordless",
         "phone": phone, "verified_phone_token": token},
        headers={
            "X-Mojo-Test-Allow-User-Registration": "1",
            "X-Mojo-Test-Register-Fields": json.dumps(PASSWORDLESS_FIELDS),
        })
    assert_eq(resp.status_code, 200,
              f"passwordless register must succeed, got {resp.status_code}: "
              f"{opts.client.last_response.body}")

    user = User.objects.filter(phone_number=phone).first()
    assert_true(user is not None, f"user must be created (phone={phone})")
    assert_true(user.has_usable_password() is False,
                "a passwordless account must have an unusable password")
    assert_true(user.is_phone_verified is True,
                "phone must be marked verified after a passwordless register")


@th.django_unit_test("passwordless register without a verified_phone_token is rejected")
def test_passwordless_register_requires_token(opts):
    from mojo.apps.account.models import User
    _clear_limits()
    phone = _fresh_phone()

    resp = opts.client.post(
        "/api/auth/register",
        {"first_name": "Pat", "last_name": "NoToken", "phone": phone},
        headers={
            "X-Mojo-Test-Allow-User-Registration": "1",
            "X-Mojo-Test-Register-Fields": json.dumps(PASSWORDLESS_FIELDS),
        })
    assert_true(resp.status_code in (400, 422),
                f"passwordless register without a phone-verify token must be 4xx, "
                f"got {resp.status_code}: {opts.client.last_response.body}")
    assert_true(not User.objects.filter(phone_number=phone).exists(),
                "no user may be created without a verified phone token")


@th.django_unit_test("on_register rejects a passwordless schema with no SMS-verified phone")
def test_passwordless_register_guard(opts):
    from mojo.apps.account.models import User
    _clear_limits()
    phone = _fresh_phone()

    # Schema has a phone but no verify:sms and no password — the on_register
    # defensive guard must reject it (the test header bypasses validate_auth_config).
    resp = opts.client.post(
        "/api/auth/register",
        {"first_name": "Pat", "phone": phone},
        headers={
            "X-Mojo-Test-Allow-User-Registration": "1",
            "X-Mojo-Test-Register-Fields": json.dumps(PASSWORDLESS_NO_VERIFY),
        })
    assert_true(resp.status_code in (400, 422),
                f"passwordless schema without an SMS-verified phone must be 4xx, "
                f"got {resp.status_code}: {opts.client.last_response.body}")
    assert_true(not User.objects.filter(phone_number=phone).exists(),
                "no user may be created when the passwordless guard rejects")


@th.django_unit_test("a passwordless account can log in end-to-end via the SMS-code flow")
def test_passwordless_user_can_sms_login(opts):
    from mojo.apps.account.models import User
    _clear_limits()
    phone = _fresh_phone()
    token = _start_and_verify_phone(opts, phone)

    reg = opts.client.post(
        "/api/auth/register",
        {"first_name": "Sms", "last_name": "Login",
         "phone": phone, "verified_phone_token": token},
        headers={
            "X-Mojo-Test-Allow-User-Registration": "1",
            "X-Mojo-Test-Register-Fields": json.dumps(PASSWORDLESS_FIELDS),
        })
    assert_eq(reg.status_code, 200,
              f"passwordless register must succeed, got {reg.status_code}: "
              f"{opts.client.last_response.body}")

    _clear_limits()
    # Step 1 — request an SMS code.
    sms_start = opts.client.post("/api/auth/sms/login", {"phone_number": phone})
    assert_eq(sms_start.status_code, 200,
              f"sms/login must return 200, got {sms_start.status_code}: "
              f"{opts.client.last_response.body}")

    # Read the OTP the server stored on the user, then verify it.
    user = User.objects.get(phone_number=phone)
    code = user.get_secret("sms_otp_code")
    assert_true(bool(code), "sms/login must store an OTP code on the user")

    sms_verify = opts.client.post(
        "/api/auth/sms/verify", {"phone_number": phone, "code": str(code)})
    assert_eq(sms_verify.status_code, 200,
              f"sms/verify must issue a JWT, got {sms_verify.status_code}: "
              f"{opts.client.last_response.body}")
    assert_true(bool(sms_verify.response.data.access_token),
                "sms/verify must return an access_token for the passwordless account")


@th.django_unit_test("default email + password registration still works (regression)")
def test_default_password_register_regression(opts):
    from mojo.apps.account.models import User
    _clear_limits()
    email = f"pwless_reg_{_uuid.uuid4().hex[:8]}@cfg.test"
    try:
        resp = opts.client.post(
            "/api/auth/register",
            {"email": email, "password": "Reg##99Default"},
            headers={"X-Mojo-Test-Allow-User-Registration": "1"})
        assert_eq(resp.status_code, 200,
                  f"default email+password register must still succeed, got "
                  f"{resp.status_code}: {opts.client.last_response.body}")
        user = User.objects.filter(email=email).first()
        assert_true(user is not None and user.has_usable_password(),
                    "default registration must create a user with a usable password")
    finally:
        User.objects.filter(email=email).delete()


# ---------------------------------------------------------------------------
# register.html — template render
# ---------------------------------------------------------------------------

@th.django_unit_test("register.html renders no password input for a passwordless schema")
def test_register_html_no_password_field(opts):
    from django.test import RequestFactory
    from django.shortcuts import render
    from mojo.apps.account.rest.bouncer.views import _auth_context

    factory = RequestFactory(REMOTE_ADDR="127.0.0.1")
    request = factory.get(
        "/register",
        HTTP_X_MOJO_TEST_REGISTER_FIELDS=json.dumps(PASSWORDLESS_FIELDS))
    ctx = _auth_context(request, group=None)
    ctx["page_mode"] = "register"
    ctx["page_title"] = "Create Account"
    html = render(request, "account/register.html", ctx).content.decode("utf-8")

    assert_true('id="reg-password"' not in html,
                "register.html must NOT render a password input for a passwordless schema")
    assert_true('id="reg-phone"' in html,
                "register.html must still render the phone input for the passwordless schema")
