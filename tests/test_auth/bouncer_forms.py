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
