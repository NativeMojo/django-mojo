"""
Tests for per-group login-method gating (UX-only soft restriction).

Contracts enforced:
  - assert_login_method is a no-op when no group is resolved
  - assert_login_method raises when the group disables the method
  - password login is rejected when group_uuid resolves a password-disabled
    group, and succeeds when no group_uuid is supplied
  - SMS / passkey login endpoints reject their method when the group disables it
  - login.html omits the password form for a password-disabled group and
    renders the SMS-code view when sms is enabled
  - mojo-auth.js exposes registerPasskey / startSmsLogin / verifySmsLogin
"""
import os
from testit import helpers as th
from testit.helpers import assert_true, assert_eq


LM_USER = 'lm_login_user'
LM_PWORD = 'lm##login99'
LM_PHONE = '+15558675309'
# password disabled — only sms + passkey offered
LM_GROUP_A_UUID = 'lma1234567890abcdef01234567890ab'
# only password offered — sms + passkey disabled
LM_GROUP_B_UUID = 'lmb1234567890abcdef01234567890ab'


@th.django_unit_setup()
def setup_login_methods(opts):
    from mojo.apps.account.models import User, Group
    from mojo.decorators.limits import clear_rate_limits
    clear_rate_limits(ip='127.0.0.1', key='login')

    User.objects.filter(username=LM_USER).delete()
    Group.objects.filter(uuid__in=[LM_GROUP_A_UUID, LM_GROUP_B_UUID]).delete()

    user = User(username=LM_USER, email=f'{LM_USER}@test.com')
    user.save()
    user.is_email_verified = True
    user.phone_number = LM_PHONE
    user.is_phone_verified = True
    user.save()
    user.save_password(LM_PWORD)
    opts.user = user

    opts.group_a = Group.objects.create(
        name='test-lm-group-a', uuid=LM_GROUP_A_UUID, is_active=True,
        metadata={"auth_config": {"login": {"methods": ["sms", "passkey"]}}})
    opts.group_b = Group.objects.create(
        name='test-lm-group-b', uuid=LM_GROUP_B_UUID, is_active=True,
        metadata={"auth_config": {"login": {"methods": ["password"]}}})


# ---------------------------------------------------------------------------
# assert_login_method unit behavior
# ---------------------------------------------------------------------------

@th.django_unit_test("assert_login_method is a no-op when no group is resolved")
def test_assert_no_group_noop(opts):
    from mojo.apps.account.services import auth_config as pc
    # Must not raise — absent group context means no restriction.
    pc.assert_login_method("password", None)


@th.django_unit_test("assert_login_method raises when the group disables the method")
def test_assert_blocked(opts):
    from mojo.apps.account.services import auth_config as pc
    from mojo import errors as merrors
    try:
        pc.assert_login_method("password", opts.group_a)
        assert False, "assert_login_method must raise for a disabled method"
    except merrors.PermissionDeniedException:
        pass
    # An enabled method must NOT raise.
    pc.assert_login_method("sms", opts.group_a)


# ---------------------------------------------------------------------------
# Password login endpoint
# ---------------------------------------------------------------------------

@th.django_unit_test("password login is rejected when the group disables password")
def test_password_login_blocked_by_group(opts):
    from mojo.decorators.limits import clear_rate_limits
    clear_rate_limits(ip='127.0.0.1', key='login')
    resp = opts.client.post('/api/login', {
        'username': LM_USER, 'password': LM_PWORD, 'group_uuid': LM_GROUP_A_UUID,
    })
    assert_eq(resp.status_code, 403,
              f"password login must be rejected for a password-disabled group, "
              f"got {resp.status_code}: {opts.client.last_response.body}")


@th.django_unit_test("password login succeeds when no group_uuid is supplied")
def test_password_login_ok_without_group(opts):
    from mojo.decorators.limits import clear_rate_limits
    clear_rate_limits(ip='127.0.0.1', key='login')
    opts.client.clear_cookies()
    resp = opts.client.post('/api/login', {
        'username': LM_USER, 'password': LM_PWORD,
    })
    assert_eq(resp.status_code, 200,
              f"password login must succeed without a group context (the gate "
              f"is UX-only), got {resp.status_code}: {opts.client.last_response.body}")


# ---------------------------------------------------------------------------
# SMS / passkey login endpoints
# ---------------------------------------------------------------------------

@th.django_unit_test("SMS login is rejected when the group disables sms")
def test_sms_login_blocked_by_group(opts):
    resp = opts.client.post('/api/auth/sms/login', {
        'username': LM_PHONE, 'group_uuid': LM_GROUP_B_UUID,
    })
    assert_eq(resp.status_code, 403,
              f"SMS login must be rejected for an sms-disabled group, "
              f"got {resp.status_code}: {opts.client.last_response.body}")


@th.django_unit_test("passkey login begin is rejected when the group disables passkey")
def test_passkey_login_blocked_by_group(opts):
    resp = opts.client.post('/api/auth/passkeys/login/begin', {
        'group_uuid': LM_GROUP_B_UUID,
    })
    assert_eq(resp.status_code, 403,
              f"passkey login must be rejected for a passkey-disabled group, "
              f"got {resp.status_code}: {opts.client.last_response.body}")


# ---------------------------------------------------------------------------
# login.html method gating
# ---------------------------------------------------------------------------

def _render_login(group=None):
    from django.test import RequestFactory
    from django.shortcuts import render
    from mojo.apps.account.rest.bouncer.views import _auth_context
    factory = RequestFactory()
    request = factory.get('/auth')
    ctx = _auth_context(request, group=group)
    ctx['page_mode'] = 'login'
    ctx['page_title'] = 'Sign In'
    return render(request, 'account/login.html', ctx).content.decode('utf-8')


@th.django_unit_test("login.html opens on the SMS view for a passwordless (no-password) group")
def test_login_html_passwordless_lands_on_sms(opts):
    html = _render_login(group=opts.group_a)
    assert_true('id="signin-password"' not in html,
                "password input must NOT render when password is disabled for the group")
    assert_true('id="view-sms"' in html,
                "the SMS-code login view must render when sms is an enabled method")
    # With no password method, the SMS phone-entry view is the active landing.
    assert_true('id="view-sms" class="mat-view is-active"' in html,
                "with no password method the login page must open directly on "
                "the SMS phone-entry view (is-active)")
    assert_true('id="view-signin" class="mat-view is-active"' not in html,
                "the sign-in form must NOT be the active view for a passwordless config")


@th.django_unit_test("login.html opens on the sign-in form and offers SMS as a button (default config)")
def test_login_html_default_signin_primary(opts):
    html = _render_login(group=None)
    assert_true('id="signin-password"' in html,
                "default config must still render the password input (regression guard)")
    assert_true('id="view-signin" class="mat-view is-active"' in html,
                "default config must open on the sign-in form")
    assert_true('id="btn-go-sms"' in html,
                "SMS login must be offered as a proper button (btn-go-sms), not a "
                "footer link, when sms is among the login methods")
    assert_true('id="link-sms"' not in html,
                "the old footer 'link-sms' must be gone — SMS is now a real button")


# ---------------------------------------------------------------------------
# mojo-auth.js helper surface
# ---------------------------------------------------------------------------

@th.django_unit_test("mojo-auth.js exposes registerPasskey / startSmsLogin / verifySmsLogin")
def test_mojo_auth_new_helpers(opts):
    import mojo.apps.account as account_pkg
    js_path = os.path.join(
        os.path.dirname(account_pkg.__file__), 'static', 'account', 'mojo-auth.js')
    assert_true(os.path.exists(js_path), f"expected mojo-auth.js at {js_path}")
    with open(js_path, 'r') as fh:
        source = fh.read()
    assert_true('registerPasskey: function' in source,
                "mojo-auth.js must expose registerPasskey for passkey enrollment")
    assert_true('startSmsLogin: function' in source,
                "mojo-auth.js must expose startSmsLogin for passwordless SMS login")
    assert_true('verifySmsLogin: function' in source,
                "mojo-auth.js must expose verifySmsLogin for passwordless SMS login")
    assert_true('passkeyRegisterBegin' in source and 'passkeyRegisterComplete' in source,
                "mojo-auth.js DEFAULT_ENDPOINTS must include the passkey register endpoints")
