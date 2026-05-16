"""
Tests for bouncer-hosted register/login forms forwarding `group_uuid`.

Contracts enforced:
  - register.html submit handler reads cfg.groupUuid and adds it to payload
  - register.html injects var GROUP_UUID = "<uuid>" when group context is set
  - register.html injects var GROUP_UUID = "" when no group context
  - login.html submit handler passes { group_uuid: cfg.groupUuid } to MojoAuth.login
  - mojo-auth.js login() helper accepts an optional third options arg and
    forwards options.group_uuid into the POST body

The form submit handlers cannot be exercised via Python because they are
JavaScript. These tests assert against the rendered template HTML and the
static JS source — guarding against an accidental revert of the wiring.
"""
import os
from testit import helpers as th
from testit.helpers import assert_true, assert_eq


BFORMS_GROUP_NAME = 'test-bouncer-forms-operator'
# Hex-only uuid (no hyphens) — matches production `uuid.uuid4().hex` shape
# and avoids `escapejs` rewriting `-` to `-` in the rendered JS literal.
BFORMS_GROUP_UUID = 'bf01234567890abcdef01234567890ab'


@th.django_unit_setup()
def setup_bouncer_forms(opts):
    from mojo.apps.account.models import Group

    Group.objects.filter(uuid=BFORMS_GROUP_UUID).delete()
    group = Group.objects.create(
        name=BFORMS_GROUP_NAME,
        uuid=BFORMS_GROUP_UUID,
        is_active=True,
        kind='operator',
    )
    opts.group = group


def _render(template_name, group=None):
    """Render an account auth template via the bouncer view helper."""
    from django.test import RequestFactory
    from django.shortcuts import render
    from mojo.apps.account.rest.bouncer.views import _auth_context

    factory = RequestFactory()
    request = factory.get('/auth' if 'login' in template_name else '/register')
    ctx = _auth_context(request, group=group)
    ctx['page_mode'] = 'login' if 'login' in template_name else 'register'
    ctx['page_title'] = 'Sign In' if 'login' in template_name else 'Create Account'
    response = render(request, template_name, ctx)
    return response.content.decode('utf-8')


# ---------------------------------------------------------------------------
# register.html
# ---------------------------------------------------------------------------

@th.django_unit_test("register.html emits payload.group_uuid = cfg.groupUuid in submit handler")
def test_register_form_emits_group_uuid_assignment(opts):
    html = _render('account/register.html', group=opts.group)
    assert_true(
        'if (cfg.groupUuid) payload.group_uuid = cfg.groupUuid' in html,
        "register.html must forward cfg.groupUuid into the register payload "
        "so REQUIRE_GROUP_ON_REGISTRATION can be satisfied. Submit handler "
        "is missing the group_uuid assignment line."
    )


@th.django_unit_test("register.html declares cfg = window._matConfig in submit-scope IIFE")
def test_register_form_declares_cfg(opts):
    html = _render('account/register.html', group=opts.group)
    assert_true(
        'var cfg = window._matConfig' in html,
        "register.html submit IIFE must declare `var cfg = window._matConfig;` "
        "so the submit handler can read cfg.groupUuid."
    )


@th.django_unit_test("register.html injects var GROUP_UUID = \"<uuid>\" when group is set")
def test_register_form_groupuuid_populated_from_context(opts):
    html = _render('account/register.html', group=opts.group)
    expected = f'var GROUP_UUID = "{opts.group.uuid}"'
    idx = html.find('var GROUP_UUID =')
    snippet = html[idx:idx+80] if idx != -1 else '(GROUP_UUID assignment not found)'
    assert_true(
        expected in html,
        f"Expected `{expected}` in rendered register.html when group is set "
        f"(opts.group.uuid={opts.group.uuid!r}). Got snippet: {snippet!r}"
    )


@th.django_unit_test("register.html injects var GROUP_UUID = \"\" when no group context")
def test_register_form_groupuuid_empty_without_group(opts):
    html = _render('account/register.html', group=None)
    assert_true(
        'var GROUP_UUID = ""' in html,
        "Expected `var GROUP_UUID = \"\"` in rendered register.html when no "
        "group is in context (single-tenant deployments must be unchanged)."
    )


# ---------------------------------------------------------------------------
# login.html
# ---------------------------------------------------------------------------

@th.django_unit_test("login.html submit handler passes { group_uuid: cfg.groupUuid } to MojoAuth.login")
def test_login_form_passes_group_uuid_option(opts):
    html = _render('account/login.html', group=opts.group)
    expected = 'MojoAuth.login(u, p, { group_uuid: cfg.groupUuid })'
    assert_true(
        expected in html,
        f"login.html submit handler must call `{expected}` so request.group "
        f"middleware and USER_LOGIN_HANDLER receive the operator. Got HTML "
        f"snippet: {html[html.find('MojoAuth.login'):html.find('MojoAuth.login')+80] if 'MojoAuth.login' in html else '(MojoAuth.login not found)'}"
    )


@th.django_unit_test("login.html still emits GROUP_UUID context binding")
def test_login_form_groupuuid_populated_from_context(opts):
    html = _render('account/login.html', group=opts.group)
    expected = f'var GROUP_UUID = "{opts.group.uuid}"'
    idx = html.find('var GROUP_UUID =')
    snippet = html[idx:idx+80] if idx != -1 else '(GROUP_UUID assignment not found)'
    assert_true(
        expected in html,
        f"Expected `{expected}` in rendered login.html when group is set "
        f"(opts.group.uuid={opts.group.uuid!r}). Got snippet: {snippet!r}"
    )


# ---------------------------------------------------------------------------
# mojo-auth.js helper signature
# ---------------------------------------------------------------------------

@th.django_unit_test("mojo-auth.js login() accepts options arg and forwards group_uuid")
def test_mojo_auth_login_signature_accepts_options(opts):
    # Locate mojo-auth.js relative to the django-mojo project root.
    # The test runs from the project's `tests/` cwd; resolve via the
    # installed package's __file__ rather than a brittle relative path.
    import mojo.apps.account as account_pkg
    js_path = os.path.join(
        os.path.dirname(account_pkg.__file__),
        'static', 'account', 'mojo-auth.js',
    )
    assert_true(os.path.exists(js_path),
                f"Expected mojo-auth.js at {js_path}")
    with open(js_path, 'r') as fh:
        source = fh.read()

    assert_true(
        'login: function (username, password, options)' in source,
        "mojo-auth.js login() must accept a third `options` arg so the "
        "hosted login form can forward group_uuid."
    )
    assert_true(
        'options.group_uuid' in source,
        "mojo-auth.js login() must read options.group_uuid and add it to "
        "the POST payload."
    )
