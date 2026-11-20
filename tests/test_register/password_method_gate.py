"""
The "password" sign-up method is enforced (maestro #11790).

/api/auth/register is the `password` registration method: email + password,
SMS-verified phone and passwordless schemas alike. A resolved auth config that
leaves `password` out of registration.methods refuses a NEW account there, with
or without a group (Ian, 2026-11-20), with the same 403 as registration closed.
A returning phone still signs in, a refused phone token stays unspent, and
invites, admin-created accounts and OAuth sign-up never reach this endpoint.

The hosted register page follows the same rules: no form without `password`,
"Sign-up isn't available here." when nothing is left, and the login page then
drops its register links.

Each case makes its own groups, emails and phones, so the module is
parallel-safe.
"""
import json
import uuid as _uuid
from pathlib import Path

from testit import helpers as th
from testit.helpers import assert_true, assert_eq
from tests.test_register import _capture

TESTIT_TIER = "extended"

ROOT = Path(__file__).resolve().parents[2]
ALLOW = {"X-Mojo-Test-Allow-User-Registration": "1"}
PHONE_FIELDS = [
    {"name": "first_name", "required": True},
    {"name": "last_name", "required": True},
    {"name": "phone", "required": True, "verify": "sms"},
]
CAPTURE_REGISTER = "tests.test_register._capture.capture_register"
REFUSED = "Registration is not enabled"


def _suffix():
    return _uuid.uuid4().hex[:10]


def _clear_limits():
    from mojo.decorators.limits import clear_rate_limits
    clear_rate_limits(ip="127.0.0.1")
    for key in ("register", "phone_register_start", "phone_register_verify"):
        clear_rate_limits(ip="127.0.0.1", key=key)


def _group(name, registration=None):
    from mojo.apps.account.models import Group
    metadata = {"auth_config": {"registration": registration}} if registration is not None else {}
    return Group.objects.create(
        name=f"test-11790-{name}-{_suffix()}", uuid=_uuid.uuid4().hex,
        is_active=True, metadata=metadata)


def _email(tag):
    return f"pmg-{tag}-{_suffix()}@pmg.test"


def _phone():
    return f"+1555{_uuid.uuid4().int % 10_000_000:07d}"


def _phone_token(opts, phone):
    """Run the phone verify-then-register steps; return a verified_phone_token."""
    from mojo.helpers.redis import get_connection
    start = opts.client.post("/api/auth/phone/register/start", {"phone": phone})
    assert_eq(start.status_code, 200, f"phone start must succeed, got {start.status_code}")
    session_token = start.response.data.session_token
    code = json.loads(get_connection().get(f"phone:register:session:{session_token}"))["code"]
    verify = opts.client.post(
        "/api/auth/phone/register/verify", {"session_token": session_token, "code": code})
    assert_eq(verify.status_code, 200, f"phone verify must succeed, got {verify.status_code}")
    return verify.response.data.verified_phone_token


def _register(opts, body, group=None, headers=None):
    _clear_limits()
    if group is not None:
        body = dict(body, group_uuid=group.uuid)
    return opts.client.post("/api/auth/register", body, headers=dict(ALLOW, **(headers or {})))


def _assert_refused(resp, what):
    assert_eq(resp.status_code, 403, f"{what} must be refused, got {resp.status_code}: {resp.response}")
    message = resp.response.get("error") or resp.response.get("message") or ""
    assert_true(REFUSED in str(message), f"{what} must give the registration-closed answer, got {resp.response}")


# ---------------------------------------------------------------------------
# /api/auth/register
# ---------------------------------------------------------------------------

@th.django_unit_test("register: a group without password in registration.methods refuses email + password")
def test_group_without_password_refuses(opts):
    from mojo.apps.account.models import User
    from mojo.apps.account.models.member import GroupMember
    group = _group("nopw", {"methods": ["google"]})
    email = _email("nopw")
    capture_id = _capture.new_capture_id()
    try:
        resp = _register(opts, {"email": email, "password": "Pmg##99pass"}, group,
                         {"X-Mojo-Test-Capture-Id": capture_id,
                          "X-Mojo-Test-User-Registered-Handler": CAPTURE_REGISTER})
        _assert_refused(resp, "email + password sign-up")
        assert_true(not User.objects.filter(email=email).exists(), "no user may be created")
        assert_true(not GroupMember.objects.filter(group=group).exists(), "no membership may be created")
        assert_eq(_capture.read_capture(capture_id).get("register", []), [], "the handler must not fire")
    finally:
        _capture.clear_capture(capture_id)
        User.objects.filter(email=email).delete()
        group.delete()


@th.django_unit_test("register: a refused new phone keeps its verification token unspent")
def test_new_phone_refused_token_unspent(opts):
    from mojo.apps.account.models import User
    closed = _group("phone", {"methods": ["google"]})
    open_group = _group("phone-open")
    phone = _phone()
    fields = {"X-Mojo-Test-Register-Fields": json.dumps(PHONE_FIELDS)}
    try:
        _clear_limits()
        token = _phone_token(opts, phone)
        body = {"first_name": "Pat", "last_name": "Gate", "phone": phone, "verified_phone_token": token}
        _assert_refused(_register(opts, body, closed, fields), "a new phone sign-up")
        assert_true(not User.objects.filter(phone_number=phone).exists(), "no user may be created")
        # The same token still works where password sign-up is allowed.
        resp = _register(opts, body, open_group, fields)
        assert_eq(resp.status_code, 200, f"the unspent token must still register, got {resp.status_code}: {resp.response}")
        assert_true(User.objects.filter(phone_number=phone).exists(), "the open group must create the account")
    finally:
        User.objects.filter(phone_number=phone).delete()
        closed.delete()
        open_group.delete()


@th.django_unit_test("register: an existing phone still signs in and joins a group without password sign-up")
def test_existing_phone_still_signs_in(opts):
    from mojo.apps.account.models import User
    from mojo.apps.account.models.member import GroupMember
    group = _group("existing-phone", {"methods": ["google"]})
    phone = _phone()
    user = User(username=f"pmg_{_suffix()}", phone_number=phone, is_active=True)
    user.save()
    capture_id = _capture.new_capture_id()
    try:
        _clear_limits()
        token = _phone_token(opts, phone)
        resp = _register(
            opts, {"phone": phone, "verified_phone_token": token}, group,
            {"X-Mojo-Test-Register-Fields": json.dumps(PHONE_FIELDS),
             "X-Mojo-Test-Capture-Id": capture_id,
             "X-Mojo-Test-User-Registered-Handler": CAPTURE_REGISTER})
        assert_eq(resp.status_code, 200, f"the returning phone must sign in, got {resp.status_code}: {resp.response}")
        assert_true(bool(resp.response.data.access_token), "the sign-in must issue a token")
        assert_eq(User.objects.filter(phone_number=phone).count(), 1, "no second account may be created")
        assert_true(GroupMember.objects.filter(group=group, user=user).exists(), "the account must join the group")
        calls = _capture.read_capture(capture_id).get("register", [])
        assert_true(len(calls) == 1 and calls[0]["source"] == "sms",
                    f"the handler must fire once with source sms, got {calls}")
    finally:
        _capture.clear_capture(capture_id)
        User.objects.filter(phone_number=phone).delete()
        group.delete()


@th.django_unit_test("register: an existing email gets the same refusal, not 'already exists'")
def test_existing_email_same_refusal(opts):
    from mojo.apps.account.models import User
    group = _group("existing-email", {"methods": ["google"]})
    email = _email("existing")
    User.objects.create_user(username=email, email=email, password="Pmg##99exist")
    try:
        resp = _register(opts, {"email": email, "password": "Pmg##99pass"}, group)
        _assert_refused(resp, "a sign-up with an existing email")
        assert_true("already exists" not in json.dumps(resp.response), "the answer must not say the account exists")
    finally:
        User.objects.filter(email=email).delete()
        group.delete()


@th.django_unit_test("register: a group with the default config still creates the account")
def test_default_group_unchanged(opts):
    from mojo.apps.account.models import User
    group = _group("default")
    email = _email("default")
    try:
        resp = _register(opts, {"email": email, "password": "Pmg##99pass"}, group)
        assert_eq(resp.status_code, 200, f"default sign-up must succeed, got {resp.status_code}: {resp.response}")
        assert_true(User.objects.filter(email=email).exists(), "the account must be created")
    finally:
        User.objects.filter(email=email).delete()
        group.delete()


@th.django_unit_test("register: with no group the deployment's registration.methods applies")
def test_no_group_uses_deployment_config(opts):
    from mojo.apps.account.models import User
    refused, created = _email("nogroup-off"), _email("nogroup-on")
    try:
        resp = _register(opts, {"email": refused, "password": "Pmg##99pass"},
                         headers={"X-Mojo-Test-Auth-Config": json.dumps({"registration": {"methods": ["google"]}})})
        _assert_refused(resp, "a no-group sign-up with password off")
        assert_true(not User.objects.filter(email=refused).exists(), "no user may be created")
        resp = _register(opts, {"email": created, "password": "Pmg##99pass"})
        assert_eq(resp.status_code, 200, f"a no-group default sign-up must succeed, got {resp.status_code}: {resp.response}")
    finally:
        User.objects.filter(email__in=[refused, created]).delete()


@th.django_unit_test("register: a restrictive group leaves another group's sign-up alone")
def test_other_group_unaffected(opts):
    from mojo.apps.account.models import User
    closed = _group("strict", {"methods": ["google"]})
    open_group = _group("other")
    email = _email("other")
    try:
        resp = _register(opts, {"email": email, "password": "Pmg##99pass"}, open_group)
        assert_eq(resp.status_code, 200, f"the open group must still create, got {resp.status_code}: {resp.response}")
    finally:
        User.objects.filter(email=email).delete()
        closed.delete()
        open_group.delete()


@th.django_unit_test("register: invites and admin-created accounts are unaffected by the gate")
def test_invite_and_admin_create_unaffected(opts):
    from mojo.apps.account.models import User
    group = _group("invite", {"enabled": True, "methods": ["google"]})
    invited, made = _email("invite"), _email("admin")
    admin_name, admin_password = f"pmg_admin_{_suffix()}", "Pmg##99admin"
    admin = User.objects.create_user(username=admin_name, email=f"{admin_name}@pmg.test", password=admin_password)
    admin.is_email_verified = True
    admin.requires_mfa = False
    admin.save()
    admin.add_permission("manage_users")
    try:
        group.invite(invited)
        assert_true(User.objects.filter(email=invited).exists(), "the invite must create the account")
        _clear_limits()
        assert_true(opts.client.login(admin_name, admin_password), "the admin must be able to sign in")
        try:
            resp = opts.client.post("/api/user", {"email": made, "username": f"pmg_made_{_suffix()}"})
        finally:
            opts.client.logout()
        assert_eq(resp.status_code, 200, f"admin create must succeed, got {resp.status_code}: {resp.response}")
        assert_true(User.objects.filter(email=made).exists(), "the admin create must make the account")
    finally:
        User.objects.filter(email__in=[invited, made, f"{admin_name}@pmg.test"]).delete()
        group.delete()


# ---------------------------------------------------------------------------
# Hosted pages
# ---------------------------------------------------------------------------

def _render(template_name, auth_config=None):
    from django.test import RequestFactory
    from django.shortcuts import render
    from mojo.apps.account.rest.bouncer.views import _auth_context
    is_login = template_name.endswith("login.html")
    headers = {}
    if auth_config is not None:
        headers["HTTP_X_MOJO_TEST_AUTH_CONFIG"] = json.dumps(auth_config)
    request = RequestFactory().get("/auth" if is_login else "/register", REMOTE_ADDR="127.0.0.1", **headers)
    ctx = _auth_context(request)
    ctx["page_mode"] = "login" if is_login else "register"
    ctx["page_title"] = "Sign In" if is_login else "Create Account"
    return render(request, template_name, ctx).content.decode("utf-8")


UNAVAILABLE = "Sign-up isn't available here."


@th.django_unit_test("register page: password off shows no form, keeps the Google button and drops the divider")
def test_register_page_password_off(opts):
    html = _render("account/register.html", {"registration": {"methods": ["google"]}})
    assert_true('id="form-register"' not in html, "the password form must not render")
    assert_true('id="btn-google"' in html, "the Google button must still render")
    assert_true("Or register with" not in html, "the divider must not render without the form")
    assert_true(UNAVAILABLE not in html, "sign-up is still available through Google")


@th.django_unit_test("register page: nothing to sign up with, or registration closed, says so")
def test_register_page_unavailable(opts):
    for config in ({"registration": {"methods": []}}, {"registration": {"enabled": False}}):
        html = _render("account/register.html", config)
        assert_true(UNAVAILABLE in html, f"{config}: the page must say sign-up isn't available")
        assert_true('id="form-register"' not in html, f"{config}: no form")
        assert_true('class="mat-oauth-row"' not in html, f"{config}: no provider buttons")


@th.django_unit_test("login page: no register links when sign-up is unavailable; both with the default config")
def test_login_page_register_links(opts):
    closed = _render("account/login.html", {"registration": {"methods": []}})
    assert_true('href="/register' not in closed, "the login page must drop its register links")
    default = _render("account/login.html")
    assert_true(default.count('href="/register') >= 2, "the default login page keeps both register links")


@th.django_unit_test("register page: the default config renders the form, divider and buttons as before")
def test_register_page_default(opts):
    html = _render("account/register.html")
    assert_true('id="form-register"' in html, "the form must render")
    assert_true("Or register with" in html, "the divider must render")
    assert_true('id="btn-google"' in html, "the default Google button must render")
    assert_true(UNAVAILABLE not in html, "no unavailable notice")


@th.django_unit_test("register page: the form handler is attached only when the form exists")
def test_register_form_handler_guarded(opts):
    source = (ROOT / "mojo/apps/account/templates/account/register.html").read_text()
    assert_true('$("form-register").addEventListener' not in source,
                "the form handler must be guarded so the provider buttons attach without the form")
