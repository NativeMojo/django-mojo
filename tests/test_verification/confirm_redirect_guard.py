"""
Regression tests for the ?redirect= scheme guard on the email-verify confirm page.

GET /api/auth/verify/email/confirm renders account/email_verify_confirm.html and
puts the caller-supplied ?redirect= value on the template context, where it is
used in three places: the <meta http-equiv=refresh>, the success "Continue"
anchor, and the error "Go back" anchor. An unvalidated javascript: value is
therefore a one-click script-execution sink on the auth origin.

Contract this file enforces:
  - A non-http(s) scheme never reaches the rendered page — on the error page
    (Go back anchor) or the success page (Continue anchor + meta refresh)
  - A refused value OMITS the link rather than rendering it dead — the
    "close this tab" fallback takes over
  - http(s) and relative values render byte-identically — the host is
    deliberately NOT allowlisted and relative paths keep working

Every test asserts a POSITIVE page marker before its negative assertion so it
can never pass vacuously against a 404 or a 500.
"""
from testit import helpers as th
from testit.helpers import assert_eq, assert_true

TESTIT_TIER = "bug"

CRG_USER = "confirm_redirect_guard_user"
CRG_PWORD = "crguard##mojo99"
XSS_REDIRECT = "javascript:alert(1)"


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
    """GET the verify-confirm page with a token and a ?redirect= value."""
    from mojo.decorators.limits import clear_rate_limits
    clear_rate_limits(ip="127.0.0.1")
    resp = opts.client.get(
        "/api/auth/verify/email/confirm",
        params={"token": token, "redirect": redirect},
    )
    return resp, (resp.get("text") or "")


@th.django_unit_test("verify confirm (error page): javascript: redirect is dropped, no link rendered")
def test_verify_confirm_error_page_omits_javascript_redirect(opts):
    resp, body = _confirm_get(opts, XSS_REDIRECT)

    assert_eq(resp.status_code, 200,
              f"Confirm page must render 200 even for an invalid token, got {resp.status_code}")
    assert_true("Link invalid" in body,
                f"Expected the error card to render (positive marker 'Link invalid'), got: {body[:400]!r}")
    assert_true("javascript:" not in body,
                "A javascript: ?redirect= must never reach the rendered page — "
                "it lands in the 'Go back' anchor href and executes on click")
    assert_true("Go back" not in body,
                "A refused ?redirect= must OMIT the button entirely, not render it dead")


@th.django_unit_test("verify confirm (success page): javascript: redirect is dropped from anchor and meta refresh")
def test_verify_confirm_success_page_omits_javascript_redirect(opts):
    from mojo.apps.account.models import User
    from mojo.apps.account.utils import tokens

    User.objects.filter(pk=opts.crg_user_id).update(is_email_verified=False, is_active=True)
    user = User.objects.get(pk=opts.crg_user_id)
    token = tokens.generate_email_verify_token(user)

    resp, body = _confirm_get(opts, XSS_REDIRECT, token=token)

    assert_eq(resp.status_code, 200, f"Confirm page must render 200 on success, got {resp.status_code}")
    assert_true("Email verified" in body,
                f"Expected the success card to render (positive marker 'Email verified'), got: {body[:400]!r}")
    assert_true("javascript:" not in body,
                "A javascript: ?redirect= must reach neither the 'Continue' anchor "
                "nor the <meta http-equiv=refresh> on the success page")
    assert_true("http-equiv=\"refresh\"" not in body,
                "No meta refresh may be emitted for a refused ?redirect=")
    assert_true("You can close this tab" in body,
                "A refused ?redirect= must fall through to the 'close this tab' copy — "
                "proving the link was omitted, not merely left dead")

    user.refresh_from_db()
    assert_true(user.is_email_verified,
                "The guard must not change the outcome of the flow — the email is still verified")


@th.django_unit_test("verify confirm: an https redirect is preserved byte-for-byte")
def test_verify_confirm_keeps_https_redirect(opts):
    destination = "https://example.com/app?next=1"
    resp, body = _confirm_get(opts, destination)

    assert_eq(resp.status_code, 200, f"Confirm page must render 200, got {resp.status_code}")
    assert_true("Link invalid" in body,
                f"Expected the error card to render (positive marker 'Link invalid'), got: {body[:400]!r}")
    assert_true(f'href="{destination}"' in body,
                f"A cross-origin https destination must render unchanged (host is not allowlisted); "
                f"expected href=\"{destination}\" in the page")
    assert_true("Go back" in body,
                "The 'Go back' button must still be rendered for an accepted destination")


@th.django_unit_test("verify confirm: a relative redirect is preserved, not normalized to absolute")
def test_verify_confirm_keeps_relative_redirect(opts):
    destination = "/dashboard?next=1"
    resp, body = _confirm_get(opts, destination)

    assert_eq(resp.status_code, 200, f"Confirm page must render 200, got {resp.status_code}")
    assert_true("Link invalid" in body,
                f"Expected the error card to render (positive marker 'Link invalid'), got: {body[:400]!r}")
    assert_true(f'href="{destination}"' in body,
                f"A relative destination must render unchanged — no normalization to an absolute URL; "
                f"expected href=\"{destination}\" in the page")


@th.django_unit_test("verify confirm: non-web app schemes are refused the same way")
def test_verify_confirm_refuses_custom_app_scheme(opts):
    resp, body = _confirm_get(opts, "myapp://home")

    assert_eq(resp.status_code, 200, f"Confirm page must render 200, got {resp.status_code}")
    assert_true("Link invalid" in body,
                f"Expected the error card to render (positive marker 'Link invalid'), got: {body[:400]!r}")
    assert_true("myapp://" not in body,
                "A custom app scheme is refused too — deployments must use an https universal/app link")
