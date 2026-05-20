"""
Tests for per-group registration toggle and the passkey-during-registration
prompt.

Contracts enforced:
  - on_register rejects signup when the group's portal config disables it
  - on_register still works for a group with registration enabled
  - _auth_context exposes passkey_prompt + passkey_url
  - register.html redirects to the passkey page when passkey_prompt != off
  - passkey_enroll.html shows the Skip link for 'optional', hides it for 'required'
"""
import uuid as _uuid

from testit import helpers as th
from testit.helpers import assert_true, assert_eq


PP_GROUP_DISABLED_UUID = 'ppd1234567890abcdef01234567890ab'
PP_GROUP_OPTIONAL_UUID = 'ppo1234567890abcdef01234567890ab'
PP_GROUP_REQUIRED_UUID = 'ppr1234567890abcdef01234567890ab'


@th.django_unit_setup()
def setup_passkey_prompt(opts):
    from mojo.apps.account.models import Group
    from mojo.decorators.limits import clear_rate_limits
    clear_rate_limits(ip='127.0.0.1', key='register')

    Group.objects.filter(uuid__in=[
        PP_GROUP_DISABLED_UUID, PP_GROUP_OPTIONAL_UUID, PP_GROUP_REQUIRED_UUID,
    ]).delete()

    opts.group_disabled = Group.objects.create(
        name='test-pp-disabled', uuid=PP_GROUP_DISABLED_UUID, is_active=True,
        metadata={"portal": {"registration": {"enabled": False}}})
    opts.group_optional = Group.objects.create(
        name='test-pp-optional', uuid=PP_GROUP_OPTIONAL_UUID, is_active=True,
        metadata={"portal": {"registration": {"passkey_prompt": "optional"}}})
    opts.group_required = Group.objects.create(
        name='test-pp-required', uuid=PP_GROUP_REQUIRED_UUID, is_active=True,
        metadata={"portal": {"registration": {"passkey_prompt": "required"}}})


def _render(template_name, group=None):
    from django.test import RequestFactory
    from django.shortcuts import render
    from mojo.apps.account.rest.bouncer.views import _auth_context
    factory = RequestFactory()
    request = factory.get('/register')
    ctx = _auth_context(request, group=group)
    ctx['page_mode'] = 'register'
    ctx['page_title'] = 'Create Account'
    return render(request, template_name, ctx).content.decode('utf-8')


# ---------------------------------------------------------------------------
# registration.enabled gate on on_register
# ---------------------------------------------------------------------------

@th.django_unit_test("on_register rejects signup when the group disables registration")
def test_register_disabled_blocks_signup(opts):
    from mojo.apps.account.models import User
    from mojo.decorators.limits import clear_rate_limits
    clear_rate_limits(ip='127.0.0.1', key='register')

    email = f'pp_{_uuid.uuid4().hex[:8]}@disabled.test'
    resp = opts.client.post('/api/auth/register', {
        'email': email,
        'password': 'Reg##99Disabled',
        'group_uuid': PP_GROUP_DISABLED_UUID,
    }, headers={'X-Mojo-Test-Allow-User-Registration': '1'})
    assert_eq(resp.status_code, 403,
              f"register must be rejected when the group disables it, "
              f"got {resp.status_code}: {opts.client.last_response.body}")
    assert_true(not User.objects.filter(email=email).exists(),
                "no user row may be created when registration is disabled")


@th.django_unit_test("on_register works for a group with registration enabled")
def test_register_enabled_allows_signup(opts):
    from mojo.apps.account.models import User
    from mojo.decorators.limits import clear_rate_limits
    clear_rate_limits(ip='127.0.0.1', key='register')

    email = f'pp_{_uuid.uuid4().hex[:8]}@enabled.test'
    try:
        resp = opts.client.post('/api/auth/register', {
            'email': email,
            'password': 'Reg##99Enabled',
            'group_uuid': PP_GROUP_OPTIONAL_UUID,
        }, headers={'X-Mojo-Test-Allow-User-Registration': '1'})
        assert_eq(resp.status_code, 200,
                  f"register must succeed for a registration-enabled group, "
                  f"got {resp.status_code}: {opts.client.last_response.body}")
        assert_true(User.objects.filter(email=email).exists(),
                    "user row must exist after a successful register")
    finally:
        User.objects.filter(email=email).delete()


# ---------------------------------------------------------------------------
# _auth_context + template wiring
# ---------------------------------------------------------------------------

@th.django_unit_test("_auth_context exposes passkey_prompt and passkey_url")
def test_auth_context_passkey_fields(opts):
    from django.test import RequestFactory
    from mojo.apps.account.rest.bouncer.views import _auth_context
    request = RequestFactory().get('/register')
    ctx = _auth_context(request, group=opts.group_required)
    assert_eq(ctx['passkey_prompt'], 'required',
              f"_auth_context must expose the group's passkey_prompt, "
              f"got {ctx.get('passkey_prompt')!r}")
    assert_true(ctx.get('passkey_url', '').startswith('/'),
                f"_auth_context must expose a passkey_url path, got {ctx.get('passkey_url')!r}")


@th.django_unit_test("register.html redirects to the passkey page when passkey_prompt != off")
def test_register_html_passkey_redirect(opts):
    html = _render('account/register.html', group=opts.group_optional)
    assert_true('var PASSKEY_PROMPT = "optional"' in html,
                "register.html (via auth_base) must emit the group's passkey_prompt")
    assert_true('window.location.href = cfg.passkeyUrl' in html,
                "register.html submit handler must redirect to the passkey page "
                "when a passkey prompt is configured")


@th.django_unit_test("register.html default config keeps passkey_prompt off (regression)")
def test_register_html_default_prompt_off(opts):
    html = _render('account/register.html', group=None)
    assert_true('var PASSKEY_PROMPT = "off"' in html,
                "default config must keep passkey_prompt 'off' so single-tenant "
                "registration is unchanged")


# ---------------------------------------------------------------------------
# passkey_enroll.html
# ---------------------------------------------------------------------------

@th.django_unit_test("passkey_enroll.html shows the Skip link when passkey_prompt is optional")
def test_passkey_enroll_optional_shows_skip(opts):
    html = _render('account/passkey_enroll.html', group=opts.group_optional)
    assert_true('id="btn-add-passkey"' in html,
                "passkey enrollment page must render the Add-a-passkey button")
    assert_true('id="btn-skip-passkey"' in html,
                "passkey enrollment page must offer Skip when the prompt is optional")


@th.django_unit_test("passkey_enroll.html hides the Skip link when passkey_prompt is required")
def test_passkey_enroll_required_hides_skip(opts):
    html = _render('account/passkey_enroll.html', group=opts.group_required)
    assert_true('id="btn-add-passkey"' in html,
                "passkey enrollment page must render the Add-a-passkey button")
    assert_true('id="btn-skip-passkey"' not in html,
                "passkey enrollment page must NOT offer Skip when the prompt is required")
