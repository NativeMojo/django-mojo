"""
Regression tests for passkey error UX (maestro item 75).

Passkey sign-in / enrollment used to render the browser's raw WebAuthn
DOMException verbatim — including a W3C spec URL — with no guidance or recovery
path. These tests lock in the library-level fix:

Contracts enforced (this is client-side JS — the suite has no JS runtime, so
these are source / render assertions, the same pattern as
`tests/test_auth/login_methods.py`):
  - every browser-prompt call site (both credentials.get + credentials.create)
    maps its rejection through friendlyWebAuthnError, and both copy maps carry
    the NotAllowedError string
  - getError() strips URLs (stripUrls) and has a DOMException backstop, and
    sanitizeMessage is exported
  - the rendered login page wires the SMS recovery action + the message-action
    button class
  - the rendered auth page carries the showMessage error-sanitize backstop
  - the theme ships the .mat-message-action style

No DB fixtures are needed — pages render with group=None.
"""
import os

from testit import helpers as th
from testit.helpers import assert_true


def _read_static(filename):
    """Read a shipped static asset from mojo.apps.account/static/account/."""
    import mojo.apps.account as account_pkg
    path = os.path.join(
        os.path.dirname(account_pkg.__file__), 'static', 'account', filename)
    assert os.path.exists(path), f"expected static asset at {path}"
    with open(path, 'r') as fh:
        return fh.read()


def _render_login(group=None):
    """Render account/login.html the same way the hosted page does."""
    from django.test import RequestFactory
    from django.shortcuts import render
    from mojo.apps.account.rest.bouncer.views import _auth_context
    factory = RequestFactory()
    request = factory.get('/auth')
    ctx = _auth_context(request, group=group)
    ctx['page_mode'] = 'login'
    ctx['page_title'] = 'Sign In'
    return render(request, 'account/login.html', ctx).content.decode('utf-8')


# ---------------------------------------------------------------------------
# mojo-auth.js — WebAuthn rejection mapping
# ---------------------------------------------------------------------------

@th.django_unit_test("every WebAuthn browser-prompt call site maps its rejection")
def test_every_webauthn_call_is_mapped(opts):
    source = _read_static('mojo-auth.js')
    # Match the actual call form `.get({` / `.create({` — this excludes the
    # `typeof navigator.credentials.get === 'function'` feature-check lines.
    call_forms = ['navigator.credentials.get({', 'navigator.credentials.create({']
    sites = 0
    for form in call_forms:
        start = 0
        while True:
            idx = source.find(form, start)
            if idx == -1:
                break
            sites += 1
            window = source[idx:idx + 200]
            assert_true('friendlyWebAuthnError' in window,
                        f"WebAuthn call at offset {idx} ({form}) must map its "
                        f"rejection via friendlyWebAuthnError within 200 chars so "
                        f"the raw DOMException never propagates; window was: {window!r}")
            start = idx + len(form)
    assert_true(sites >= 3,
                f"expected at least 3 browser-prompt call sites (2 credentials.get "
                f"+ 1 credentials.create), found {sites}")
    assert_true(
        'Passkey sign-in was cancelled, timed out, or no passkey was found on this device.'
        in source,
        "the login copy map must carry the NotAllowedError sign-in string")
    assert_true(
        'Passkey setup was cancelled or timed out. You can try again.' in source,
        "the enrollment copy map must carry the NotAllowedError setup string")


@th.django_unit_test("getError sanitizes URLs and has a DOMException backstop")
def test_get_error_sanitizes(opts):
    source = _read_static('mojo-auth.js')
    assert_true('function stripUrls' in source,
                "mojo-auth.js must define stripUrls() to strip spec URLs from "
                "user-facing error text")
    ge_idx = source.find('function getError(')
    assert_true(ge_idx != -1, "mojo-auth.js must still define getError()")
    ge_window = source[ge_idx:ge_idx + 700]
    assert_true('instanceof DOMException' in ge_window,
                "getError must include a DOMException backstop branch so any raw "
                "browser credential error that escaped a mapping layer is mapped "
                "to friendly copy")
    assert_true('sanitizeMessage' in source,
                "mojo-auth.js must export sanitizeMessage (the public URL-stripper)")


# ---------------------------------------------------------------------------
# Rendered auth page wiring
# ---------------------------------------------------------------------------

@th.django_unit_test("login page wires the SMS recovery action on passkey failure")
def test_login_page_wires_recovery_action(opts):
    html = _render_login(group=None)
    assert_true('Sign in with a text code instead' in html,
                "login.html passkey catch must offer the SMS-code recovery action "
                "label when the group offers SMS login")
    assert_true('mat-message-action' in html,
                "the rendered page must reference the mat-message-action button "
                "class (created by showMessage for the recovery action)")


@th.django_unit_test("rendered auth page carries the showMessage sanitize backstop")
def test_rendered_page_has_sanitize_backstop(opts):
    html = _render_login(group=None)
    assert_true('sanitizeMessage' in html,
                "auth_base.html showMessage must call MojoAuth.sanitizeMessage as a "
                "render-layer backstop — so register/contact/enroll pages that "
                "extend the same base are covered too")


# ---------------------------------------------------------------------------
# Theme
# ---------------------------------------------------------------------------

@th.django_unit_test("theme ships the .mat-message-action recovery-link style")
def test_theme_ships_action_style(opts):
    css = _read_static('mojo-auth-theme.css')
    assert_true('.mat-message-action' in css,
                "mojo-auth-theme.css must define the .mat-message-action style so "
                "the recovery action renders as an underlined link-button")
