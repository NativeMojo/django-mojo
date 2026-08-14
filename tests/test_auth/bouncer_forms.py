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
import re
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
    ctx['page_title'] = ctx['login_heading'] if 'login' in template_name else 'Create Account'
    response = render(request, template_name, ctx)
    return response.content.decode('utf-8')


@th.django_unit_test("auth theme query overrides are allowlisted and forwarded across the journey")
def test_auth_theme_query_overrides(opts):
    import objict
    from django.test import RequestFactory
    from mojo.apps.account.rest.bouncer.views import _auth_context

    request = RequestFactory().get('/auth', {
        'auth_theme': 'editorial',
        'auth_appearance': 'light',
        'redirect': '/private/report?tab=one',
    })
    request.DATA = objict.objict(request.GET.dict())
    ctx = _auth_context(request, group=opts.group)
    assert_eq(ctx['auth_layout'], 'editorial',
              f"known query layout must win, got {ctx['auth_layout']!r}")
    assert_eq(ctx['auth_appearance'], 'light',
              f"known query appearance must win, got {ctx['auth_appearance']!r}")
    assert_true('auth_theme=editorial' in ctx['register_url'],
                f"layout override must survive page switching: {ctx['register_url']!r}")
    assert_true('auth_appearance=light' in ctx['register_url'],
                f"appearance override must survive page switching: {ctx['register_url']!r}")

    bad = RequestFactory().get('/auth', {
        'auth_theme': 'editorial body{display:none}',
        'auth_appearance': 'neon',
    })
    bad.DATA = objict.objict(bad.GET.dict())
    bad_ctx = _auth_context(bad, group=opts.group)
    assert_eq(bad_ctx['auth_layout'], 'minimal',
              f"unknown query layout must fall back safely, got {bad_ctx['auth_layout']!r}")
    assert_eq(bad_ctx['auth_appearance'], 'system',
              f"unknown query appearance must fall back safely, got {bad_ctx['auth_appearance']!r}")


@th.django_unit_test("login renders contextual copy, layout classes, and branded back label")
def test_login_renders_contextual_theme(opts):
    opts.group.metadata = {"auth_config": {
        "theme": {
            "layout": "branded-panel",
            "appearance": "dark",
            "back_to_website_label": "Back to NativeMojo BizDev",
        },
        "login": {
            "heading": "Continue to NativeMojo BizDev",
            "supporting_copy": "Use your Maestro account to continue.",
        },
    }}
    opts.group.save(update_fields=['metadata'])
    try:
        html = _render('account/login.html', group=opts.group)
        assert_true('mat-layout-branded-panel' in html and 'mat-appearance-dark' in html,
                    "rendered body must expose independent layout and appearance classes")
        assert_true('Continue to NativeMojo BizDev' in html,
                    "the configured destination-aware login heading must render")
        assert_true('Use your Maestro account to continue.' in html,
                    "the configured account-provider explanation must render")
        assert_true('Back to NativeMojo BizDev' in html,
                    "the branded back label must replace the generic copy")
    finally:
        opts.group.metadata = {}
        opts.group.save(update_fields=['metadata'])


@th.django_unit_test("stored back URL is scheme-guarded and query override targets every layout link")
def test_back_links_share_scheme_guard(opts):
    opts.group.metadata = {"auth_config": {"theme": {
        "back_to_website_url": "javascript:alert(1)",
    }}}
    # Direct model save intentionally mirrors a deployment AUTH_CONFIG value,
    # which can bypass the REST validation gate.
    opts.group.save(update_fields=['metadata'])
    try:
        html = _render('account/login.html', group=opts.group)
        assert_true('href="javascript:' not in html,
                    "unsafe stored back URL must be removed before template rendering")
        assert_true('document.querySelectorAll(".mat-back-to-website")' in html,
                    "the sanitized ?back= override must update hero and inline links")
    finally:
        opts.group.metadata = {}
        opts.group.save(update_fields=['metadata'])


@th.django_unit_test("bouncer challenge renders the resolved destination brand")
def test_bouncer_challenge_uses_group_brand(opts):
    import objict
    from django.test import RequestFactory
    from mojo.apps.account.rest.bouncer.views import _serve_challenge

    opts.group.metadata = {"auth_config": {"theme": {
        "app_title": "NativeMojo BizDev",
        "auth_provider_name": "Maestro",
        "accent_color": "#e44d2e",
    }}}
    opts.group.save(update_fields=['metadata'])
    try:
        request = RequestFactory().get('/auth')
        request.DATA = objict.objict()
        html = _serve_challenge(request, group=opts.group).content.decode('utf-8')
        assert_true('NativeMojo BizDev' in html,
                    "challenge page must name the destination brand instead of hardcoding MOJO VERIFY")
        assert_true('#e44d2e' in html,
                    "challenge page must receive the group's validated accent color")
        assert_true('Secure sign-in via Maestro' in html,
                    "challenge page must explain which account provider protects the destination")
    finally:
        opts.group.metadata = {}
        opts.group.save(update_fields=['metadata'])


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


@th.django_unit_test("register.html renders the GitHub button by default (github is default-on)")
def test_register_github_default_on(opts):
    html = _render('account/register.html', group=None)
    assert_true(
        'id="btn-github"' in html,
        "github is in REGISTRATION_METHODS, so the default config must render "
        "the GitHub button on the hosted register page")


@th.django_unit_test("register.html omits the GitHub button when the group disables github registration")
def test_register_no_github_when_disabled(opts):
    opts.group.metadata = {"auth_config": {
        "registration": {"methods": ["password"]}}}
    opts.group.save(update_fields=["metadata"])
    try:
        html = _render('account/register.html', group=opts.group)
        assert_true(
            'id="btn-github"' not in html,
            "a group whose explicit registration.methods omits github must NOT "
            "render the GitHub button on the register page")
        assert_true(
            'id="btn-google"' not in html,
            "a group whose explicit registration.methods omits google must NOT "
            "render the Google button on the register page")
    finally:
        opts.group.metadata = {}
        opts.group.save(update_fields=["metadata"])


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
# login.html SMS sign-in dead-end (ITEM-006) — honest, anti-enumeration copy
# ---------------------------------------------------------------------------

@th.django_unit_test("login.html SMS view discloses a code is sent only if the number has an account")
def test_login_sms_discloses_code_only_if_account(opts):
    """ITEM-006: the SMS sign-in view must set honest expectations up front — a
    code only arrives if the phone is already linked to an account — instead of
    implying a code was definitely sent. Same generic text for everyone, so it
    leaks nothing about account existence (anti-enumeration is preserved)."""
    html = _render('account/login.html', group=opts.group)
    assert_true(
        'id="view-sms"' in html,
        "SMS login view must render (sms is in the default login_methods)")
    assert_true(
        'linked to an account' in html,
        "login.html SMS view must tell the user a code is only sent if the phone "
        "is already linked to an account (honest, generic anti-enumeration copy). "
        "Without it, a user with no account dead-ends on the code screen.")


@th.django_unit_test("login.html SMS view offers a visible Create-an-account path")
def test_login_sms_offers_signup_link(opts):
    """A user with no account needs an obvious way to sign up from the SMS
    sign-in flow (we can't auto-route without leaking existence). The link reuses
    register_url, which already carries the group context."""
    html = _render('account/login.html', group=opts.group)
    assert_true(
        'Create an account' in html,
        "login.html SMS view must surface a visible 'Create an account' sign-up "
        "link so an account-less user isn't stranded on the code screen.")


@th.django_unit_test("login.html hides every signup link when group registration is disabled")
def test_login_hides_signup_links_when_registration_disabled(opts):
    """Invite-only and externally provisioned groups must not offer registration
    paths that their auth config explicitly disables."""
    opts.group.metadata = {"auth_config": {
        "registration": {"enabled": False},
    }}
    opts.group.save(update_fields=["metadata"])
    try:
        html = _render('account/login.html', group=opts.group)
        assert_true(
            'Create an account' not in html,
            "login.html SMS view must hide its registration link when the "
            "resolved group auth config sets registration.enabled=false.")
        assert_true(
            'Create one' not in html,
            "login.html main switcher must hide its registration link when the "
            "resolved group auth config sets registration.enabled=false.")
        assert_true(
            'href="/register' not in html,
            "login.html must not render any /register link when registration is "
            "disabled for the resolved group.")
    finally:
        opts.group.metadata = {}
        opts.group.save(update_fields=["metadata"])


@th.django_unit_test("login.html SMS submit message does not falsely claim a code was sent")
def test_login_sms_post_submit_message_is_honest(opts):
    """After submitting a phone, the page must not assert a code was definitely
    sent (it isn't, for an unknown number). The message stays honest and points
    account-less users toward sign-up."""
    html = _render('account/login.html', group=opts.group)
    assert_true(
        'You may not have an account yet' in html,
        "login.html SMS submit handler must set an honest post-submit message — a "
        "code only arrives if an account exists — not a false 'we sent a code' "
        "certainty.")
    assert_true(
        'we sent to " + phone' not in html,
        "login.html must no longer hard-assert 'the code we sent to <phone>' on "
        "submit — that false certainty is what dead-ends users with no account.")


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


@th.django_unit_test("mojo-auth.js preserves forced-password challenges and exposes completion")
def test_mojo_auth_forced_password_contract(opts):
    import mojo.apps.account as account_pkg
    js_path = os.path.join(
        os.path.dirname(account_pkg.__file__),
        'static', 'account', 'mojo-auth.js',
    )
    with open(js_path, 'r') as fh:
        source = fh.read()

    assert_true(
        "forcedPassword:     '/api/auth/password/forced'" in source,
        "mojo-auth.js must register the forced-password completion endpoint")
    assert_true(
        'if (d && d.requires_password_change) return d;' in source,
        "mojo-auth.js must return a forced-password challenge to the caller "
        "instead of passing it to saveTokens and throwing No access_token")
    assert_true(
        'completeForcedPassword: function (token, newPassword)' in source,
        "mojo-auth.js must expose a helper that exchanges the one-time tp: "
        "credential and permanent password for the normal login package")


@th.django_unit_test("hosted login completes temporary-password replacement in place")
def test_login_html_completes_forced_password(opts):
    html = _render('account/login.html', group=opts.group)

    assert_true(
        'data && data.requires_password_change' in html and
        'window._mat.onPasswordChangeRequired(data)' in html,
        "the shared hosted-page completion handler must route every auth "
        "method's forced-password response to the login page instead of "
        "redirecting a tokenless result")
    assert_true(
        'm.onPasswordChangeRequired = function (data)' in html,
        "login.html must recognize the successful temporary-password response "
        "and open its password replacement view")
    assert_true(
        'MojoAuth.completeForcedPassword(forcedPasswordToken, np)' in html,
        "the password replacement form must submit the tp: credential to the "
        "forced-password endpoint, not the ordinary pr: reset endpoint")
    assert_true(
        'sessionStorage.setItem("mat_forced' not in html,
        "the single-use tp: credential must stay in page memory and never be "
        "persisted in browser storage")

    opts.group.metadata = {"auth_config": {"login": {"methods": ["sms"]}}}
    opts.group.save(update_fields=["metadata"])
    try:
        passwordless_html = _render('account/login.html', group=opts.group)
        assert_true(
            'id="view-set-password"' in passwordless_html,
            "the permanent-password view must remain available when the "
            "group's valid authentication method is passwordless")
    finally:
        opts.group.metadata = {}
        opts.group.save(update_fields=["metadata"])


# ---------------------------------------------------------------------------
# Configurable register form (AUTH_REGISTER_FIELDS)
# ---------------------------------------------------------------------------

def _render_with_test_register_fields(template_name, fields_json, group=None):
    """Render with a per-request X-Mojo-Test-Register-Fields header override.

    Mirrors how the on_register endpoint reads the schema — the bouncer's
    _auth_context calls register_schema.resolve_fields, which honors the
    test-mode header when the gate passes. For RequestFactory-built requests
    we set REMOTE_ADDR=127.0.0.1 so the loopback gate is satisfied.
    """
    from django.test import RequestFactory
    from django.shortcuts import render
    from mojo.apps.account.rest.bouncer.views import _auth_context
    from mojo.apps.account.services import register_schema

    factory = RequestFactory(REMOTE_ADDR='127.0.0.1')
    request = factory.get('/auth' if 'login' in template_name else '/register',
                          HTTP_X_MOJO_TEST_REGISTER_FIELDS=fields_json)

    # is_test_request checks MOJO_TEST_MODE; if that's not on in the harness,
    # call resolve_fields directly so the tests still exercise the rendering.
    fields = register_schema.resolve_fields(group=group, request=request)
    rows = register_schema.field_rows(fields)
    identity_field = register_schema.resolve_identity_field(fields, group=group)
    forgot_channel = 'sms' if identity_field == 'phone' else 'email'
    step1, step2_active, step3_rows = register_schema.partition_for_stepped_flow(fields)

    ctx = _auth_context(request, group=group)
    ctx['page_mode'] = 'login' if 'login' in template_name else 'register'
    ctx['page_title'] = 'Sign In' if 'login' in template_name else 'Create Account'
    ctx['register_fields'] = fields
    ctx['register_field_rows'] = rows
    ctx['register_step1_fields'] = step1
    ctx['register_step2_active'] = step2_active
    ctx['register_step3_field_rows'] = step3_rows
    ctx['identity_field'] = identity_field
    ctx['forgot_channel'] = forgot_channel
    response = render(request, template_name, ctx)
    return response.content.decode('utf-8')


PHONE_ONLY_FIELDS_JSON = (
    '[{"name":"first_name","required":true},'
    '{"name":"last_name","required":true},'
    '{"name":"phone","required":true,"verify":"sms"},'
    '{"name":"dob","required":true},'
    '{"name":"password","required":true}]'
)


@th.django_unit_test("register.html renders the phone-only field set when configured")
def test_register_html_phone_only_renders(opts):
    html = _render_with_test_register_fields(
        'account/register.html', PHONE_ONLY_FIELDS_JSON, group=opts.group)
    assert_true('id="reg-phone"' in html,
                "phone field input must render for the phone-only config")
    assert_true('type="tel"' in html,
                "phone field must render with type='tel'")
    # DOB now renders as three segmented numeric inputs (post-redesign);
    # the hidden composed-date input keeps id="reg-dob" for the submit handler.
    assert_true('id="reg-dob"' in html,
                "hidden composed DOB input must still render for the phone-only config")
    assert_true('id="reg-dob-mm"' in html and 'id="reg-dob-dd"' in html and 'id="reg-dob-yyyy"' in html,
                "DOB must render as MM / DD / YYYY segmented inputs")
    # Verify-token hidden input is now in the form-level scope (not on the phone field)
    assert_true('verified_phone_token' in html,
                "phone-verify must render a hidden verified_phone_token input")
    # Email row is NOT present
    assert_true('id="reg-email"' not in html,
                "email input must NOT render when email is not in the field set")


@th.django_unit_test("register.html default config still renders the email-based form (regression)")
def test_register_html_default_renders_email(opts):
    # Render via the standard helper (no header override → default schema).
    html = _render('account/register.html', group=opts.group)
    assert_true('id="reg-email"' in html,
                "default config must render the email input (regression guard)")
    assert_true('id="reg-password"' in html,
                "default config must render the password input")
    assert_true('id="reg-phone"' not in html,
                "default config must NOT render a phone input")


@th.django_unit_test("register.html emits reg-fields-data JSON for the submit handler")
def test_register_html_emits_fields_json(opts):
    html = _render_with_test_register_fields(
        'account/register.html', PHONE_ONLY_FIELDS_JSON, group=opts.group)
    assert_true('id="reg-fields-data"' in html,
                "register.html must emit reg-fields-data JSON for the JS loop")
    assert_true('"name": "phone"' in html or '"name":"phone"' in html,
                "reg-fields-data must include the phone field entry")


@th.django_unit_test("login.html forgot-subview renders phone mode when forgot_channel=sms")
def test_login_html_forgot_phone_mode(opts):
    html = _render_with_test_register_fields(
        'account/login.html', PHONE_ONLY_FIELDS_JSON, group=opts.group)
    assert_true('Phone Number' in html,
                "phone mode must label the forgot field 'Phone Number'")
    assert_true('id="forgot-phone"' in html,
                "phone mode must render the forgot-phone input")
    assert_true('data-forgot-channel="sms"' in html,
                "view-forgot must carry data-forgot-channel='sms' for the JS to route correctly")
    # Email-only "Send a link" radio is hidden in phone mode
    assert_true('value="link"' not in html,
                "phone-mode forgot subview must NOT include the link-method radio")


@th.django_unit_test("login.html forgot-subview defaults to email mode (regression)")
def test_login_html_forgot_email_mode_default(opts):
    html = _render('account/login.html', group=opts.group)
    assert_true('id="forgot-email"' in html,
                "default config must render the forgot-email input")
    assert_true('data-forgot-channel="email"' in html,
                "view-forgot must carry data-forgot-channel='email' by default")


@th.django_unit_test("mojo-auth.js exposes startPhoneRegister + verifyPhoneRegister")
def test_mojo_auth_phone_register_helpers(opts):
    import mojo.apps.account as account_pkg
    js_path = os.path.join(
        os.path.dirname(account_pkg.__file__),
        'static', 'account', 'mojo-auth.js',
    )
    with open(js_path, 'r') as fh:
        source = fh.read()
    assert_true('startPhoneRegister: function' in source,
                "mojo-auth.js must expose startPhoneRegister")
    assert_true('verifyPhoneRegister: function' in source,
                "mojo-auth.js must expose verifyPhoneRegister")
    assert_true('phoneRegisterStart' in source and 'phoneRegisterVerify' in source,
                "mojo-auth.js DEFAULT_ENDPOINTS must register phoneRegisterStart and phoneRegisterVerify")


@th.django_unit_test("mojo-auth.js forgotPasswordCode accepts an optional channel arg")
def test_mojo_auth_forgot_channel(opts):
    import mojo.apps.account as account_pkg
    js_path = os.path.join(
        os.path.dirname(account_pkg.__file__),
        'static', 'account', 'mojo-auth.js',
    )
    with open(js_path, 'r') as fh:
        source = fh.read()
    assert_true('forgotPasswordCode: function (identifier, channel)' in source,
                "mojo-auth.js forgotPasswordCode must accept (identifier, channel)")
    assert_true("channel: 'sms'" in source or "channel == 'sms'" in source or "ch === 'sms'" in source,
                "mojo-auth.js must route channel='sms' into the phone payload")


# ---------------------------------------------------------------------------
# Extra (non-canonical) registration fields — auth_config.registration.extra_fields
# ---------------------------------------------------------------------------

def _render_with_extra_fields(template_name, extra_fields_raw, group=None):
    """Render with an explicit register_extra_fields context.

    Drives the template render directly (gate-independent): _auth_context builds
    the base context, then we override register_extra_fields with the normalized
    list so this test exercises the template, not the test-mode header gate
    (resolve_extra_fields' header path is covered by the schema unit tests).
    """
    from django.test import RequestFactory
    from django.shortcuts import render
    from mojo.apps.account.rest.bouncer.views import _auth_context
    from mojo.apps.account.services import register_schema

    factory = RequestFactory(REMOTE_ADDR='127.0.0.1')
    request = factory.get('/register')
    ctx = _auth_context(request, group=group)
    ctx['page_mode'] = 'register'
    ctx['page_title'] = 'Create Account'
    ctx['register_extra_fields'] = register_schema._normalize_extra_field_list(extra_fields_raw)
    response = render(request, template_name, ctx)
    return response.content.decode('utf-8')


@th.django_unit_test("register.html renders a configured extra field (promo) + wires collectExtras")
def test_register_html_extra_field_renders(opts):
    html = _render_with_extra_fields(
        'account/register.html',
        [{"name": "promo", "label": "Promo code"}],
        group=opts.group)
    assert_true('id="reg-extra-promo"' in html,
                "a configured extra field must render its text input #reg-extra-promo")
    assert_true('id="reg-extra-row-promo"' in html,
                "the extra field must render inside its hideable row #reg-extra-row-promo")
    assert_true('Promo code' in html,
                "the extra field's label must render (as the input placeholder)")
    assert_true('id="reg-extra-fields-data"' in html,
                "register.html must emit reg-extra-fields-data JSON for the JS loop")
    assert_true('if (!collectExtras(payload)) return;' in html,
                "the single-pane submit handler must call collectExtras(payload)")


@th.django_unit_test("register.html renders NO extra-field inputs when none configured (gating regression)")
def test_register_html_no_extra_fields_default(opts):
    html = _render('account/register.html', group=opts.group)
    assert_true('id="reg-extra-row-' not in html,
                "with no extra_fields configured, no extra field row may render — "
                "other brands must see no behavior change")
    assert_true('id="reg-extra-promo"' not in html,
                "default config must not render a promo input")


# ---------------------------------------------------------------------------
# Phone-first stepped flow (register page UX redesign)
# ---------------------------------------------------------------------------

@th.django_unit_test("register.html: phone-verify schema renders three step containers")
def test_register_stepped_flow_renders(opts):
    html = _render_with_test_register_fields(
        'account/register.html', PHONE_ONLY_FIELDS_JSON, group=opts.group)
    # Step indicator
    assert_true('class="mat-steps"' in html or 'data-mat-steps="3"' in html,
                "stepped flow must render the .mat-steps step indicator")
    # Three step containers, each with the expected data-step attr
    assert_true('id="view-reg-step1"' in html and 'data-step="1"' in html,
                "Step 1 container missing — expected #view-reg-step1 with data-step=1")
    assert_true('id="view-reg-step2"' in html and 'data-step="2"' in html,
                "Step 2 container missing — expected #view-reg-step2 with data-step=2")
    assert_true('id="view-reg-step3"' in html and 'data-step="3"' in html,
                "Step 3 container missing — expected #view-reg-step3 with data-step=3")
    # Step 2 must include the 6-digit code input + Verify + Back + Resend
    assert_true('id="reg-phone-code"' in html,
                "Step 2 must render the 6-digit code input")
    assert_true('id="btn-reg-verify"' in html,
                "Step 2 must render the Verify button")
    assert_true('id="btn-reg-back"' in html,
                "Step 2 must render the Back button")
    assert_true('id="btn-reg-resend"' in html,
                "Step 2 must render the Resend button")
    # Step 1 carries the phone field (only)
    assert_true('id="reg-phone"' in html,
                "Step 1 must render the phone input")


@th.django_unit_test("register.html: stepped flow drops the inline Send-code subwidget")
def test_register_stepped_no_inline_send(opts):
    html = _render_with_test_register_fields(
        'account/register.html', PHONE_ONLY_FIELDS_JSON, group=opts.group)
    # The legacy inline Send-code / Verify buttons on the phone field are gone
    # — verification now lives on Step 2.
    assert_true('id="reg-phone-send"' not in html,
                "stepped flow must NOT render the legacy reg-phone-send button "
                "on the phone field; verification lives on Step 2")
    assert_true('id="reg-phone-code-row"' not in html,
                "stepped flow must NOT render the legacy reg-phone-code-row subwidget")


@th.django_unit_test("register.html: default email schema stays single-pane (no step containers)")
def test_register_default_email_single_pane(opts):
    html = _render('account/register.html', group=opts.group)
    assert_true('id="view-reg-step1"' not in html,
                "default email schema must NOT render step containers (single-pane fallback)")
    assert_true('id="view-register"' in html,
                "default email schema must render the single-pane #view-register container")
    assert_true('class="mat-steps"' not in html,
                "default email schema must NOT render the .mat-steps indicator")


@th.django_unit_test("register.html: Enter runs the current step's action, not the final submit")
def test_register_enter_runs_current_step_not_submit(opts):
    """Regression for the stepped register page: pressing Enter on step 1 (phone)
    or step 2 (OTP) must trigger that step's button, NOT the step-3 final submit
    whose first gate is the Terms checkbox (which produced a false "agree to
    Terms" error). Asserted against the rendered JS — the repo has no JS runtime.
    """
    html = _render_with_test_register_fields(
        'account/register.html', PHONE_ONLY_FIELDS_JSON, group=opts.group)

    step1_guard = 'if (STEPPED && currentStep === 1) { $("btn-reg-continue").click(); return; }'
    step2_guard = 'if (STEPPED && currentStep === 2) { $("btn-reg-verify").click(); return; }'
    assert_true(step1_guard in html,
                "register.html submit handler must route Enter on step 1 to the "
                f"Continue button instead of submitting. Missing: {step1_guard!r}")
    assert_true(step2_guard in html,
                "register.html submit handler must route Enter on step 2 to the "
                f"Verify button instead of submitting. Missing: {step2_guard!r}")

    # The step dispatch must come BEFORE the Terms check in source order. That
    # ordering IS the fix: pre-fix, the Terms check is the handler's first
    # statement, so Enter on step 1/2 surfaces the false Terms error.
    i_dispatch = html.find('currentStep === 1')
    i_terms = html.find('Please agree to the Terms & Conditions.')
    assert_true(i_dispatch != -1 and i_terms != -1,
                "expected both the step-1 dispatch and the Terms message in the "
                f"rendered stepped form (dispatch={i_dispatch}, terms={i_terms})")
    assert_true(i_dispatch < i_terms,
                "the step dispatch must run BEFORE the Terms check — otherwise "
                "Enter on step 1/2 reaches the step-3 Terms gate (the reported "
                f"bug). Got dispatch index {i_dispatch} >= terms index {i_terms}.")

    # currentStep must be real state the dispatch can read — set by showStep(n).
    assert_true('var currentStep' in html and 'currentStep = n;' in html,
                "showStep(n) must record the active step (`var currentStep` + "
                "`currentStep = n;`) so the submit handler can dispatch on it.")


@th.django_unit_test("register.html: step-3 final register path stays intact (regression)")
def test_register_step3_final_path_intact(opts):
    html = _render_with_test_register_fields(
        'account/register.html', PHONE_ONLY_FIELDS_JSON, group=opts.group)
    assert_true('Please agree to the Terms & Conditions.' in html,
                "step-3 Terms gate must still exist for the final submit handler")
    assert_true('MojoAuth.register(' in html,
                "step-3 final submit must still call MojoAuth.register(...)")


# ---------------------------------------------------------------------------
# bouncer_challenge.html — JS redirectUrl must not be HTML-entity-escaped
# ---------------------------------------------------------------------------

@th.django_unit_test("bouncer_challenge.html: redirectUrl JS string carries unescaped & (regression for &amp;)")
def test_bouncer_challenge_redirect_url_not_html_entity_escaped(opts):
    """The challenge template interpolates `login_url` into a JS string literal.

    Without `|escapejs`, Django's default HTML autoescape rewrites `&` to
    `&amp;`. The challenge JS then assigns that string verbatim to
    `window.location.href`, so the next page sees `?amp;redirect=...` instead
    of `?redirect=...`. The redirect param is silently dropped and the
    post-register flow lands at AUTH_SUCCESS_REDIRECT (the API root) instead
    of the SPA callback. This test renders _serve_challenge directly and
    asserts the rendered `redirectUrl:` JS line never contains `&amp;`.
    """
    import objict
    from django.test import RequestFactory
    from mojo.apps.account.rest.bouncer.views import _serve_challenge

    callback = 'http://localhost:3011/auth/callback'
    factory = RequestFactory(REMOTE_ADDR='127.0.0.1')
    request = factory.get('/register', {
        'redirect': callback,
        'group_uuid': opts.group.uuid,
    })
    # _serve_challenge reads request.DATA (set by mojo middleware, which
    # RequestFactory bypasses) when forwarding the redirect param. Mirror
    # the middleware so the forwarding branch at views._serve_challenge:319
    # is exercised end-to-end.
    request.DATA = objict.objict({
        'redirect': callback,
        'group_uuid': opts.group.uuid,
    })
    response = _serve_challenge(
        request, challenge_tier=1, page_type='registration', group=opts.group)
    html = response.content.decode('utf-8')

    idx = html.find('redirectUrl:')
    assert_true(idx != -1, "bouncer_challenge.html must declare a `redirectUrl:` JS property")
    # Pull the single-line JS assignment (up to its trailing comma).
    snippet = html[idx:idx+400]
    line_end = snippet.find(',')
    js_line = snippet[:line_end] if line_end != -1 else snippet

    assert_true(
        '&amp;' not in js_line,
        f"bouncer_challenge.html `redirectUrl` JS string must not contain "
        f"`&amp;` — the var is interpolated into a JS string literal, so use "
        f"the `|escapejs` filter, not Django's default HTML autoescape. "
        f"`&amp;` here makes the browser parse the post-challenge URL as "
        f"`?amp;redirect=...` and silently drops the redirect param. "
        f"Got: {js_line!r}"
    )
    # After the fix, escapejs renders `&` as the JS Unicode escape `&`
    # (and `=` as `=`). The JS engine decodes those to literal `&` / `=`
    # at runtime, so the URL the browser navigates to has the real ampersand.
    # We tolerate either form: the unescaped `&redirect=` (e.g. if someone
    # later switches to `|safe`) OR the escapejs form `&redirect`.
    assert_true(
        '&redirect=' in js_line or '\\u0026redirect' in js_line,
        f"bouncer_challenge.html `redirectUrl` must carry the forwarded "
        f"`redirect` param through the challenge — _serve_challenge built "
        f"login_url with the redirect, but the template dropped it. "
        f"Got: {js_line!r}"
    )


@th.django_unit_test("register.html: DOB renders three segmented inputs (MM/DD/YYYY)")
def test_register_dob_segments(opts):
    html = _render_with_test_register_fields(
        'account/register.html', PHONE_ONLY_FIELDS_JSON, group=opts.group)
    assert_true('id="reg-dob-mm"' in html,
                "DOB month segment input must render")
    assert_true('id="reg-dob-dd"' in html,
                "DOB day segment input must render")
    assert_true('id="reg-dob-yyyy"' in html,
                "DOB year segment input must render")
    assert_true('inputmode="numeric"' in html,
                "DOB segments must declare inputmode='numeric' for mobile keyboards")
    # The hidden composed-date input still carries the ISO yyyy-mm-dd
    assert_true('id="reg-dob"' in html,
                "hidden composed #reg-dob input must still render so the submit "
                "handler reads the composed ISO date")
    # And NOT the legacy <input type="date">
    assert_true('type="date"' not in html,
                "DOB must NOT render as <input type='date'> any more")


# ---------------------------------------------------------------------------
# Switcher param forwarding — login <-> register cross-links preserve
# `redirect`, `next`, `returnTo`, `back` (plus `group_uuid`).
# ---------------------------------------------------------------------------

def _render_switcher(template_name, query=None, group=None):
    """Render an auth template with a query string and DATA dict.

    Mirrors _serve_challenge's test pattern (line 402) — RequestFactory bypasses
    the mojo middleware that populates `request.DATA`, so we set it explicitly
    so the forwarding branch in _auth_context is exercised end-to-end.
    """
    import objict
    from django.test import RequestFactory
    from django.shortcuts import render
    from mojo.apps.account.rest.bouncer.views import _auth_context

    factory = RequestFactory()
    path = '/auth' if 'login' in template_name else '/register'
    request = factory.get(path, query or {})
    request.DATA = objict.objict(dict(query or {}))
    ctx = _auth_context(request, group=group)
    ctx['page_mode'] = 'login' if 'login' in template_name else 'register'
    ctx['page_title'] = 'Sign In' if 'login' in template_name else 'Create Account'
    response = render(request, template_name, ctx)
    return response.content.decode('utf-8'), ctx


@th.django_unit_test("login switcher: ?redirect= survives the hop to /register")
def test_switcher_preserves_redirect_param(opts):
    html, ctx = _render_switcher(
        'account/login.html', query={'redirect': '/dashboard'})
    assert_eq(
        ctx['register_url'], '/register?redirect=%2Fdashboard',
        "register_url must carry the URL-encoded redirect param so the user "
        "who clicks 'Create one' from /auth?redirect=/dashboard lands on "
        f"/register?redirect=%2Fdashboard. Got: {ctx['register_url']!r}",
    )
    assert_true(
        'href="/register?redirect=%2Fdashboard"' in html
        or 'href="/register?redirect=%2Fdashboard"'.replace('&', '&amp;') in html,
        "rendered login.html switcher href must contain the encoded redirect "
        f"target. Got context register_url={ctx['register_url']!r}",
    )


@th.django_unit_test("register switcher: all four forwarding keys are preserved together")
def test_switcher_preserves_all_forwarding_keys(opts):
    html, ctx = _render_switcher(
        'account/register.html',
        query={'next': '/x', 'returnTo': '/y', 'back': '/z'})
    url = ctx['auth_url']
    for key, val in [('next', '%2Fx'), ('returnTo', '%2Fy'), ('back', '%2Fz')]:
        assert_true(
            f'{key}={val}' in url,
            f"auth_url must carry {key}={val} from the source query. "
            f"Got: {url!r}",
        )
    assert_true(
        url.startswith('/auth?'),
        f"auth_url must start with /auth? when forwarding params are present. "
        f"Got: {url!r}",
    )


@th.django_unit_test("switcher whitelists: token/code/state are NOT forwarded")
def test_switcher_whitelists_against_non_forwarded_params(opts):
    html, ctx = _render_switcher(
        'account/login.html',
        query={'token': 'ml:abc', 'redirect': '/d', 'code': 'X', 'state': 'Y'})
    url = ctx['register_url']
    assert_true(
        'redirect=%2Fd' in url,
        f"register_url must forward `redirect` even when non-whitelisted "
        f"params are also present. Got: {url!r}",
    )
    for leaked in ('token', 'code', 'state'):
        assert_true(
            f'{leaked}=' not in url,
            f"register_url must NOT leak `{leaked}` across the switcher — "
            f"OAuth callback and magic-link params would mis-trigger logic "
            f"on the receiving page. Got: {url!r}",
        )


@th.django_unit_test("switcher merges group_uuid with forwarding params under one ?")
def test_switcher_merges_group_uuid_with_forwarding(opts):
    html, ctx = _render_switcher(
        'account/login.html',
        query={'redirect': '/d'},
        group=opts.group)
    url = ctx['register_url']
    # urlencode preserves dict insertion order: group_uuid first, then redirect.
    expected = f'/register?group_uuid={opts.group.uuid}&redirect=%2Fd'
    assert_eq(
        url, expected,
        f"register_url must merge group_uuid and redirect under a single `?` "
        f"with one `&` join. Got: {url!r}",
    )
    # And explicitly: no double `?` or `?&` artifacts.
    assert_true(
        url.count('?') == 1 and '?&' not in url,
        f"register_url must have exactly one `?` and no `?&` artifact. "
        f"Got: {url!r}",
    )


@th.django_unit_test("switcher regression: no query params -> unchanged URLs")
def test_switcher_no_params_unchanged_regression(opts):
    html, ctx = _render_switcher('account/login.html', query=None)
    assert_eq(
        ctx['register_url'], '/register',
        "Plain /auth (no query params, no group) must yield the same "
        "register_url as before the forwarding change — '/register' with no "
        f"query string. Got: {ctx['register_url']!r}",
    )
    assert_eq(
        ctx['auth_url'], '/auth',
        f"Plain /auth must yield auth_url='/auth' (regression). "
        f"Got: {ctx['auth_url']!r}",
    )


# ---------------------------------------------------------------------------
# Raw-text <script>/<style> interpolations must be escape-safe — in auth_base.html
# and in every shipped template.
#
# The server-built auth/register/passkey URLs and theme.success_redirect are
# interpolated into JS string literals in auth_base.html's inline <script>.
# <script> is a raw-text element, so the HTML parser never decodes entities
# inside it: without |escapejs, autoescape rewrites the `&` joining two query
# params to `&amp;` and the literal reaches the browser holding `&amp;`
# verbatim. register.html assigns cfg.passkeyUrl straight to
# window.location.href, so the destination parses `amp;redirect=` and silently
# drops the forwarded param on every group-scoped portal.
#
# The first three tests pin that fix for auth_base.html specifically. The last
# test generalizes the criterion into a standing audit over EVERY shipped
# template — the same bug class was already pinned for bouncer_challenge.html
# above — so the next omission, in any template, is caught automatically.
# ---------------------------------------------------------------------------

def _decode_js(s):
    """Decode the `\\uXXXX` escapes Django's `escapejs` filter emits.

    escapejs rewrites `&`, `=`, `-`, `;`, `<`, `>`, quotes, the backtick and
    the control range as `\\uXXXX`. A JS engine decodes those when it parses
    the string literal, so a test that wants to see what the browser sees has
    to do the same. A blanket `unicode_escape` decode is NOT equivalent — it
    mangles any non-ASCII character in the value.
    """
    return re.sub(r'\\u([0-9a-fA-F]{4})',
                  lambda m: chr(int(m.group(1), 16)), s)


def _js_literal(html, var_name):
    """Return the contents of the double-quoted JS string assigned to `var_name`."""
    marker = f'var {var_name} = "'
    idx = html.find(marker)
    assert_true(idx != -1,
                f"rendered auth page must declare `{marker}...` in its inline "
                f"script — the assignment was not found")
    start = idx + len(marker)
    end = html.find('"', start)
    assert_true(end != -1,
                f"`var {var_name}` string literal is unterminated in the "
                f"rendered page (searched from index {start})")
    return html[start:end]


@th.django_unit_test("auth_base.html: forwarded ?redirect= survives autoescape in the JS URL literals")
def test_auth_base_forwarded_urls_survive_autoescape(opts):
    """A group-scoped portal forwarding ?redirect= must reach the browser intact.

    Fails on the pre-fix template: the literal holds `&amp;`, so the browser
    navigates to `?group_uuid=…&amp;redirect=…` and the destination parses
    `amp;redirect` instead of `redirect`.
    """
    from urllib.parse import parse_qs, urlparse

    redirect = '/app/dashboard'
    html, ctx = _render_switcher(
        'account/register.html', query={'redirect': redirect}, group=opts.group)

    # PRECONDITION — without this the test could pass vacuously against a
    # broken template. _auth_context derives group_uuid from its `group=`
    # kwarg ONLY (views.py: `group_uuid = group.uuid if group else ''`); it
    # never reads group_uuid out of request.DATA, whose forwarding loop
    # whitelists redirect/next/returnTo/back. So a `group_uuid` query param
    # with group=None yields ONE forwarded param, no `&`, nothing to corrupt.
    assert_true(
        '&' in ctx['register_url'],
        f"PRECONDITION FAILED: register_url must carry a literal `&` (two or "
        f"more query params) for this regression to have any teeth. Pass "
        f"group=opts.group — a group_uuid query param alone will NOT produce "
        f"one. Got: {ctx['register_url']!r}")

    for var_name, ctx_key in (('AUTH_URL', 'auth_url'),
                              ('REGISTER_URL', 'register_url'),
                              ('PASSKEY_URL', 'passkey_url')):
        literal = _js_literal(html, var_name)
        assert_true(
            '&amp;' not in literal,
            f"auth_base.html `var {var_name}` is a JS string literal, so the "
            f"interpolation must carry |escapejs. Django's HTML autoescape "
            f"rewrote `&` to `&amp;`, and <script> is a raw-text element so "
            f"the parser never decodes it back — the forwarded param is "
            f"silently dropped at the destination. Got: {literal!r}")

        decoded = _decode_js(literal)
        assert_eq(
            decoded, ctx[ctx_key],
            f"the JS literal for {var_name} must decode to exactly the "
            f"server-built {ctx_key}. Got {decoded!r}, expected "
            f"{ctx[ctx_key]!r}")

        params = parse_qs(urlparse(decoded).query)
        assert_eq(
            params.get('redirect'), [redirect],
            f"{var_name} must carry the forwarded redirect param intact so the "
            f"visitor lands where they came from. Decoded {decoded!r} parsed "
            f"as {params!r}")
        assert_eq(
            params.get('group_uuid'), [opts.group.uuid],
            f"{var_name} must still carry group_uuid. Decoded {decoded!r} "
            f"parsed as {params!r}")
        assert_true(
            'amp;redirect' not in params,
            f"{var_name} parsed to an `amp;redirect` key — that IS the "
            f"&amp; corruption this test guards. Decoded {decoded!r} parsed "
            f"as {params!r}")


@th.django_unit_test("auth_base.html: multi-param theme.success_redirect survives autoescape")
def test_auth_base_success_redirect_survives_autoescape(opts):
    """`var ON_SUCCESS` is the post-auth navigation target and is tenant-writable.

    A group configuring a success_redirect with two query params hits the same
    corruption as the forwarded URLs. Fails on the pre-fix template.
    """
    from urllib.parse import parse_qs, urlparse

    target = 'https://app.example.com/landing?tenant=acme&view=home'
    opts.group.metadata = {"auth_config": {"theme": {"success_redirect": target}}}
    opts.group.save(update_fields=["metadata"])
    try:
        html, ctx = _render_switcher('account/login.html', group=opts.group)
        assert_eq(
            ctx['success_redirect'], target,
            f"PRECONDITION: the group's theme.success_redirect must reach the "
            f"context unchanged (two params, one `&`). Got: "
            f"{ctx['success_redirect']!r}")

        literal = _js_literal(html, 'ON_SUCCESS')
        assert_true(
            '&amp;' not in literal,
            f"auth_base.html `var ON_SUCCESS` must carry |escapejs — it is a "
            f"JS string literal that flows to window.location.href, so an "
            f"`&amp;` here drops every query param after the first. Got: "
            f"{literal!r}")

        decoded = _decode_js(literal)
        assert_eq(
            decoded, target,
            f"ON_SUCCESS must decode to exactly the configured "
            f"theme.success_redirect. Got {decoded!r}, expected {target!r}")

        params = parse_qs(urlparse(decoded).query)
        assert_eq(
            params.get('view'), ['home'],
            f"the second query param of theme.success_redirect must survive. "
            f"Decoded {decoded!r} parsed as {params!r}")
        assert_true(
            'amp;view' not in params,
            f"ON_SUCCESS parsed to an `amp;view` key — the &amp; corruption. "
            f"Decoded {decoded!r} parsed as {params!r}")
    finally:
        opts.group.metadata = {}
        opts.group.save(update_fields=["metadata"])


@th.django_unit_test("auth_base.html: single-param group URLs are unchanged (common-case regression)")
def test_auth_base_single_param_urls_unchanged(opts):
    """The overwhelmingly common case is `?group_uuid=` alone — no `&` at all.

    escapejs still rewrites the `=` to `\\u003D` in the HTML SOURCE, but the
    runtime string a JS engine builds is byte-identical to today's. This test
    pins that, so the fix cannot be mistaken for a behavior change.
    """
    html, ctx = _render_switcher('account/login.html', group=opts.group)

    for var_name, ctx_key, path in (('AUTH_URL', 'auth_url', '/auth'),
                                    ('REGISTER_URL', 'register_url', '/register'),
                                    ('PASSKEY_URL', 'passkey_url', '/passkey')):
        decoded = _decode_js(_js_literal(html, var_name))
        assert_eq(
            decoded, ctx[ctx_key],
            f"{var_name} must decode to exactly the server-built {ctx_key}. "
            f"Got {decoded!r}, expected {ctx[ctx_key]!r}")
        assert_eq(
            decoded, f'{path}?group_uuid={opts.group.uuid}',
            f"{var_name} must still be the plain single-param group URL — the "
            f"escapejs fix must not change what the browser navigates to in "
            f"the common case. Got: {decoded!r}")
        assert_true(
            '&' not in decoded,
            f"{var_name} must contain no `&` when group_uuid is the only "
            f"forwarded param. Got: {decoded!r}")


# Reviewed <style> interpolations. A <style> element is raw-text like <script>,
# but there is no `escapecss` filter, so every {{ }} interpolated into one must
# be individually justified here as not attacker-influenced. Keyed by
# (template_filename, normalized_expression). Adding an entry is a security
# decision — make the safety argument in the comment before adding it.
_STYLE_INTERPOLATIONS_REVIEWED = {
    # auth_base.html injects a tenant's custom stylesheet verbatim. |safe is
    # deliberate (the value IS CSS) and it is admin-set group config, not
    # visitor input — the same trust level as uploading a .css file.
    ("auth_base.html", "custom_css|safe"),
    # bouncer_challenge.html suffixes every generated class/keyframe name with a
    # per-render nonce (secrets.token_hex(6) — hex only, no CSS metacharacters)
    # so selectors cannot be cached across renders. Not attacker-influenced.
    ("bouncer_challenge.html", "render_ctx.css_nonce"),
    # bouncer_decoy.html paints one accent colour, sourced from
    # settings.get_static (conf-file-only, never DB/Redis-writable), into a
    # :root custom property. Operator config, not request input.
    ("bouncer_decoy.html", "accent_color"),
}


@th.django_unit_test("shipped templates: raw-text <script>/<style> interpolations are escape-safe")
def test_shipped_templates_raw_text_interpolations_are_safe(opts):
    """Codebase-wide standing audit — the generalized form of the #1092 fix.

    Walks EVERY shipped template (no hardcoded list), extracts <script>/<style>
    BODIES only (an external `<script src="{{ }}">` attribute is never swept),
    and for each `{{ }}` interpolation:

      * <script>, inside a JS string literal -> the expression MUST carry
        |escapejs. <script> is a raw-text element, so autoescape's `&amp;` is
        never decoded back; a value with two query params reaches the browser
        corrupted (`amp;redirect=`) and the forwarded param is dropped.
      * <script>, OUTSIDE a literal -> no requirement (an unquoted boolean or
        numeric interpolation is not a string).
      * <style> -> there is no `escapecss`, so it must appear in the reviewed
        allowlist above, each entry carrying a written safety argument.
    """
    from pathlib import Path
    import mojo

    root = Path(mojo.__file__).resolve().parent
    templates = sorted(p for p in root.rglob("*.html") if "/templates/" in str(p))
    assert_true(bool(templates), f"no shipped templates found under {root}")

    comment_re = re.compile(r'\{%\s*comment\s*%\}.*?\{%\s*endcomment\s*%\}', re.S)
    block_re = re.compile(r'<(script|style)\b[^>]*>(.*?)</\1>', re.S | re.I)
    tag_re = re.compile(r'\{\{.*?\}\}', re.S)

    def mask_tags(text):
        # Replace each {{ ... }} span with an equal-length, quote-free filler
        # (newlines preserved so per-line quote counting stays aligned). This is
        # what stops a quote INSIDE a tag (e.g. default:'login', yesno:"a,b")
        # from toggling the literal state computed from the surrounding JS.
        return tag_re.sub(
            lambda m: "".join("\n" if ch == "\n" else "x" for ch in m.group(0)),
            text)

    def in_js_string_literal(masked_body, tag_start):
        line_start = masked_body.rfind("\n", 0, tag_start) + 1
        prefix = masked_body[line_start:tag_start]
        for quote in ('"', "'", "`"):
            count = 0
            for i, ch in enumerate(prefix):
                if ch == quote and (i == 0 or prefix[i - 1] != "\\"):
                    count += 1
            if count % 2 == 1:
                return True
        return False

    def expr_of(tag):
        return tag[2:-2].strip()

    failures = []               # (template, expr) <script> literals missing escapejs
    style_unreviewed = []       # (template, expr) <style> interpolations not allowlisted
    script_required = {}        # template -> count of required <script>-literal interps
    total_required = 0

    for tpl in templates:
        name = tpl.name
        src = comment_re.sub("", tpl.read_text(encoding="utf-8"))
        for block in block_re.finditer(src):
            kind = block.group(1).lower()
            body = block.group(2)
            if kind == "style":
                for tag in tag_re.finditer(body):
                    pair = (name, expr_of(tag.group(0)))
                    if pair not in _STYLE_INTERPOLATIONS_REVIEWED:
                        style_unreviewed.append(pair)
                continue
            # kind == "script"
            masked = mask_tags(body)
            for tag in tag_re.finditer(body):
                if not in_js_string_literal(masked, tag.start()):
                    continue    # unquoted boolean/numeric — not a string literal
                script_required[name] = script_required.get(name, 0) + 1
                total_required += 1
                if "escapejs" not in expr_of(tag.group(0)):
                    failures.append((name, expr_of(tag.group(0))))

    # <style> gate — a raw-text <style> has no escapejs equivalent.
    assert_true(
        not style_unreviewed,
        f"unreviewed <style> interpolation(s): {style_unreviewed!r}. A <style> "
        f"element is raw-text and has no `escapecss` filter. Add each "
        f"(template, expression) pair to _STYLE_INTERPOLATIONS_REVIEWED in this "
        f"file ONLY after arguing the value is not attacker-influenced (admin "
        f"config, a hex nonce, a conf-only setting) — never a visitor-supplied "
        f"value.")

    # <script> gate — every JS-string-literal interpolation carries |escapejs.
    assert_true(
        not failures,
        f"shipped template(s) interpolate a value into a raw-text <script> JS "
        f"string literal without |escapejs: {failures!r}. <script> is a "
        f"raw-text element, so autoescape's `&amp;` is never decoded back and a "
        f"value carrying two query params reaches the browser corrupted. Add "
        f"|escapejs (AFTER any default: filter). NEVER add it to an href/src "
        f"attribute — those ARE entity-decoded, and escapejs would inject a "
        f"literal backslash-u escape into the link.")

    # Floors — prove the audit actually walked the known surface (a broken
    # slice would make every gate above pass vacuously).
    assert_true(
        "auth_base.html" in script_required,
        f"auth_base.html must be among the walked templates and contribute "
        f"required <script>-literal interpolations; the audit saw "
        f"{sorted(script_required)!r}")
    assert_true(
        script_required.get("auth_base.html", 0) >= 7,
        f"auth_base.html must contribute >= 7 required <script>-literal "
        f"interpolations (api_base, success_redirect, auth_url, register_url, "
        f"passkey_url, passkey_prompt, group_uuid); saw "
        f"{script_required.get('auth_base.html', 0)}")
    assert_true(
        total_required >= 14,
        f"expected >= 14 required <script>-literal interpolations across all "
        f"shipped templates; saw {total_required}. If sites were genuinely "
        f"removed, lower this floor deliberately; if the slice broke, fix the "
        f"extraction.")
