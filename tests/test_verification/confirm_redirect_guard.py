"""
Regression tests for the ?redirect= scheme guard on the email-verify landing.

GET /api/auth/verify/email/confirm renders account/email_verify_landing.html
and puts the caller-supplied ?redirect= value on the template context, where it
becomes the "Go back" anchor. An unvalidated javascript: value is therefore a
one-click script-execution sink on the auth origin.

Since #3257 that page is a confirmation LANDING: it renders, it never validates
or consumes the ev: token, and it never marks the address verified — that moved
to POST /api/auth/email/verify/confirm, which the page's button calls. The
tests below assert both halves, so a regression that restores the old
verify-on-GET behavior fails here loudly.

Contract this file enforces:
  - A non-http(s) scheme never reaches the rendered page
  - A refused value OMITS the link rather than rendering it dead
  - http(s) and relative values render byte-identically — the host is
    deliberately NOT allowlisted and relative paths keep working
  - Opening the page with a real token changes nothing

Every test asserts a POSITIVE page marker before its negative assertion so it
can never pass vacuously against a 404 or a 500.
"""
from testit import helpers as th
from testit.helpers import assert_eq, assert_true

TESTIT_TIER = "bug"

CRG_USER = "confirm_redirect_guard_user"
CRG_PWORD = "crguard##mojo99"
XSS_REDIRECT = "javascript:alert(1)"
READY_MARKER = 'id="mojo-landing-ready"'


@th.django_unit_setup()
def setup_confirm_redirect_guard(opts):
    from mojo.apps.account.models import User
    from mojo.decorators.limits import clear_rate_limits
    clear_rate_limits(ip="127.0.0.1")

    # Delete before creating — the suite runs against a long-lived database.
    User.objects.filter(username=CRG_USER).delete()

    user = User(username=CRG_USER, email=f"{CRG_USER}@example.com")
    user.save()
    user.is_active = True
    user.is_email_verified = False
    user.requires_mfa = False
    user.save_password(CRG_PWORD)
    user.save()
    opts.crg_user_id = user.pk


def _confirm_get(opts, redirect, token="ev:notavalidtoken"):
    """GET the verify landing with a token and a ?redirect= value."""
    from mojo.decorators.limits import clear_rate_limits
    clear_rate_limits(ip="127.0.0.1")
    resp = opts.client.get(
        "/api/auth/verify/email/confirm",
        params={"token": token, "redirect": redirect},
    )
    return resp, (resp.get("text") or "")


@th.django_unit_test("verify landing: javascript: redirect is dropped, no link rendered")
def test_verify_confirm_error_page_omits_javascript_redirect(opts):
    resp, body = _confirm_get(opts, XSS_REDIRECT)

    assert_eq(resp.status_code, 200,
              f"The landing must render 200 for any token, got {resp.status_code}")
    assert_true(READY_MARKER in body,
                f"Expected the ready state to render (the landing never validates "
                f"the token), got: {body[:400]!r}")
    assert_true("javascript:" not in body,
                "A javascript: ?redirect= must never reach the rendered page — "
                "it lands in the 'Go back' anchor href and executes on click")
    assert_true("Go back" not in body,
                "A refused ?redirect= must OMIT the button entirely, not render it dead")


@th.django_unit_test("verify landing: a real token renders and verifies nothing")
def test_verify_confirm_success_page_omits_javascript_redirect(opts):
    from mojo.apps.account.models import User
    from mojo.apps.account.utils import tokens

    User.objects.filter(pk=opts.crg_user_id).update(is_email_verified=False, is_active=True)
    user = User.objects.get(pk=opts.crg_user_id)
    token = tokens.generate_email_verify_token(user)

    resp, body = _confirm_get(opts, XSS_REDIRECT, token=token)

    assert_eq(resp.status_code, 200, f"The landing must render 200, got {resp.status_code}")
    assert_true(READY_MARKER in body,
                f"Expected the ready state to render, got: {body[:400]!r}")
    assert_true("javascript:" not in body,
                "A javascript: ?redirect= must reach neither the anchor nor any "
                "other sink on the page")
    assert_true('http-equiv="refresh"' not in body,
                "The landing must never auto-navigate — no meta refresh, ever")

    user.refresh_from_db()
    assert_true(not user.is_email_verified,
                "#3257: opening the landing must NOT verify the address — only "
                "the explicit POST does")


@th.django_unit_test("verify landing: an https redirect is preserved byte-for-byte")
def test_verify_confirm_keeps_https_redirect(opts):
    destination = "https://example.com/app?next=1"
    resp, body = _confirm_get(opts, destination)

    assert_eq(resp.status_code, 200, f"The landing must render 200, got {resp.status_code}")
    assert_true(READY_MARKER in body,
                f"Expected the ready state to render, got: {body[:400]!r}")
    assert_true(f'href="{destination}"' in body,
                f"A cross-origin https destination must render unchanged (host is not allowlisted); "
                f"expected href=\"{destination}\" in the page")
    assert_true("Go back" in body,
                "The 'Go back' button must still be rendered for an accepted destination")


@th.django_unit_test("verify landing: a relative redirect is preserved, not normalized to absolute")
def test_verify_confirm_keeps_relative_redirect(opts):
    destination = "/dashboard?next=1"
    resp, body = _confirm_get(opts, destination)

    assert_eq(resp.status_code, 200, f"The landing must render 200, got {resp.status_code}")
    assert_true(READY_MARKER in body,
                f"Expected the ready state to render, got: {body[:400]!r}")
    assert_true(f'href="{destination}"' in body,
                f"A relative destination must render unchanged — no normalization to an absolute URL; "
                f"expected href=\"{destination}\" in the page")


@th.django_unit_test("verify landing: non-web app schemes are refused the same way")
def test_verify_confirm_refuses_custom_app_scheme(opts):
    resp, body = _confirm_get(opts, "myapp://home")

    assert_eq(resp.status_code, 200, f"The landing must render 200, got {resp.status_code}")
    assert_true(READY_MARKER in body,
                f"Expected the ready state to render, got: {body[:400]!r}")
    assert_true("myapp://" not in body,
                "A custom app scheme is refused too — deployments must use an https universal/app link")
