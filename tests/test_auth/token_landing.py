"""
Human confirmation landings for the ev: / ec: / dv: emailed links (#3257).

The bug: the two link flows that reached a working URL applied their change the
instant the URL was OPENED, so a mail scanner, a link preview or a browser
prefetch silently burned the single-use token and changed the account with
nobody present. Deactivation had no human page at all.

Contract this file enforces:

  - Opening a landing NEVER validates or consumes the token and NEVER touches
    account state. The same token still works afterwards.
  - Only an explicit POST — the button press — commits anything.
  - The ev: confirm is verify-only: no JWT, no last_login, no login event.
  - Emailed links point at the landings; /auth?token=<prefix>:... still reaches
    them through a server-side compatibility redirect.
  - The token lives only in inert page state; the page is self-contained, sends
    no referrer, and never auto-navigates.
  - The landing GET has its own rate bucket, and a throttled landing files a
    diagnostic that carries no token, path or query string.
"""
import uuid as _uuid

from testit import helpers as th
from testit.helpers import assert_eq, assert_true

TL_PREFIX = "token_landing"
TL_PWORD = "tokenland##mojo99"

READY_MARKER = 'id="mojo-landing-ready"'
INVALID_MARKER = 'id="mojo-landing-invalid"'


def _fresh_user(username, email=None):
    """Delete-then-create — the suite runs against a long-lived database."""
    from mojo.apps.account.models import User
    email = email or f"{username}@example.com"
    User.objects.filter(username=username).delete()
    User.objects.filter(email=email).delete()
    user = User(username=username, email=email)
    user.save()
    user.is_active = True
    user.is_email_verified = False
    user.requires_mfa = False
    user.save_password(TL_PWORD)
    user.save()
    return user


@th.django_unit_setup()
def setup_token_landing(opts):
    from mojo.decorators.limits import clear_rate_limits
    clear_rate_limits(ip="127.0.0.1")

    opts.ec_user_id = _fresh_user(f"{TL_PREFIX}_ec").pk
    opts.ev_user_id = _fresh_user(f"{TL_PREFIX}_ev").pk
    opts.dv_user_id = _fresh_user(f"{TL_PREFIX}_dv").pk
    opts.misc_user_id = _fresh_user(f"{TL_PREFIX}_misc").pk


def _header(opts, name):
    """One response header, case-insensitively — the client stores the raw
    lowercase wire names in a plain dict."""
    headers = opts.client.last_response.headers or {}
    lowered = {str(k).lower(): v for k, v in headers.items()}
    return lowered.get(name.lower())


def _get(opts, path, **params):
    from mojo.decorators.limits import clear_rate_limits
    clear_rate_limits(ip="127.0.0.1")
    resp = opts.client.get(path, params=params)
    return resp, (resp.get("text") or "")


# ===========================================================================
# The three no-mutation contracts — the bug itself
# ===========================================================================

@th.tier("core")
@th.django_unit_test("ec: landing GET renders without consuming the token")
def test_ec_landing_get_does_not_consume_token(opts):
    from mojo.apps.account.models import User
    from mojo.apps.account.utils import tokens
    from mojo.decorators.limits import clear_rate_limits

    user = User.objects.get(pk=opts.ec_user_id)
    original_email = str(user.email)
    new_email = f"{TL_PREFIX}_ec_new@example.com"
    User.objects.filter(email=new_email).delete()
    token = tokens.generate_email_change_token(user, new_email)

    resp, body = _get(opts, "/api/auth/email/change/confirm", token=token)

    assert_eq(resp.status_code, 200,
              f"The ec: landing must render 200, got {resp.status_code}")
    assert_true(READY_MARKER in body,
                f"Expected the ready state to render, got: {body[:400]!r}")

    user.refresh_from_db()
    assert_eq(str(user.email), original_email,
              "Opening the ec: landing must NOT commit the email change")
    assert_eq(user.get_secret("pending_email"), new_email,
              "Opening the ec: landing must leave the pending change intact")

    # The very same token must still work — the GET consumed nothing.
    clear_rate_limits(ip="127.0.0.1")
    confirm = opts.client.post("/api/auth/email/change/confirm", {"token": token})
    assert_eq(confirm.status_code, 200,
              f"The token the GET rendered must still confirm, got {confirm.status_code}")
    user.refresh_from_db()
    assert_eq(str(user.email), new_email,
              "The explicit POST is what commits the email change")


@th.tier("core")
@th.django_unit_test("ev: landing GET renders without verifying the email")
def test_ev_landing_get_does_not_verify(opts):
    from mojo.apps.account.models import User
    from mojo.apps.account.utils import tokens
    from mojo.decorators.limits import clear_rate_limits

    User.objects.filter(pk=opts.ev_user_id).update(is_email_verified=False)
    user = User.objects.get(pk=opts.ev_user_id)
    token = tokens.generate_email_verify_token(user)

    resp, body = _get(opts, "/api/auth/verify/email/confirm", token=token)

    assert_eq(resp.status_code, 200,
              f"The ev: landing must render 200, got {resp.status_code}")
    assert_true(READY_MARKER in body,
                f"Expected the ready state to render, got: {body[:400]!r}")

    user.refresh_from_db()
    assert_true(not user.is_email_verified,
                "Opening the ev: landing must NOT mark the email verified")

    clear_rate_limits(ip="127.0.0.1")
    confirm = opts.client.post("/api/auth/email/verify/confirm", {"token": token})
    assert_eq(confirm.status_code, 200,
              f"The token the GET rendered must still verify, got {confirm.status_code}")
    user.refresh_from_db()
    assert_true(user.is_email_verified,
                "The explicit POST is what verifies the email")


@th.tier("core")
@th.django_unit_test("dv: landing GET renders without deactivating the account")
def test_dv_landing_get_does_not_deactivate(opts):
    from mojo.apps.account.models import User
    from mojo.apps.account.utils import tokens
    from mojo.decorators.limits import clear_rate_limits

    User.objects.filter(pk=opts.dv_user_id).update(is_active=True)
    user = User.objects.get(pk=opts.dv_user_id)
    token = tokens.generate_deactivate_token(user)

    resp, body = _get(opts, "/api/account/deactivate/confirm", token=token)

    assert_eq(resp.status_code, 200,
              f"The dv: landing must render 200, got {resp.status_code}")
    assert_true(READY_MARKER in body,
                f"Expected the ready state to render, got: {body[:400]!r}")

    user.refresh_from_db()
    assert_true(user.is_active,
                "Opening the dv: landing must NOT close the account")

    clear_rate_limits(ip="127.0.0.1")
    confirm = opts.client.post("/api/account/deactivate/confirm", {"token": token})
    assert_eq(confirm.status_code, 200,
              f"The token the GET rendered must still deactivate, got {confirm.status_code}")
    user.refresh_from_db()
    assert_true(not user.is_active,
                "The explicit POST is what closes the account")


# ===========================================================================
# The verify-only confirm — a confirmation page must not be a login
# ===========================================================================

@th.django_unit_test("ev: confirm verifies the email and issues no session")
def test_ev_confirm_is_verify_only(opts):
    from mojo.apps.account.models import User
    from mojo.apps.account.models.login_event import UserLoginEvent
    from mojo.apps.account.utils import tokens
    from mojo.decorators.limits import clear_rate_limits

    User.objects.filter(pk=opts.ev_user_id).update(
        is_email_verified=False, is_active=True, last_login=None)
    user = User.objects.get(pk=opts.ev_user_id)
    logins_before = UserLoginEvent.objects.filter(user=user).count()
    token = tokens.generate_email_verify_token(user)

    clear_rate_limits(ip="127.0.0.1")
    resp = opts.client.post("/api/auth/email/verify/confirm", {"token": token})

    assert_eq(resp.status_code, 200,
              f"The verify-only confirm must succeed, got {resp.status_code}")
    body = resp.get("response") or {}
    assert_true(body.get("status") is True,
                f"Expected status true, got {body!r}")
    data = body.get("data") or {}
    for key in ("access_token", "refresh_token", "access", "refresh"):
        assert_true(key not in data,
                    f"The verify-only confirm must issue NO token package — found {key} in {data!r}")

    user.refresh_from_db()
    assert_true(user.is_email_verified,
                "The verify-only confirm must mark the address verified")
    assert_true(user.last_login is None,
                f"Clicking a verification link is not signing in — last_login must be "
                f"untouched, got {user.last_login!r}")
    assert_eq(UserLoginEvent.objects.filter(user=user).count(), logins_before,
              "The verify-only confirm must record no login event — #3329 adds one in "
              "jwt_login, and this path must never reach it")


@th.django_unit_test("ev: the legacy verify-then-login endpoint is untouched")
def test_legacy_email_verify_still_logs_in(opts):
    from mojo.apps.account.models import User
    from mojo.apps.account.utils import tokens
    from mojo.decorators.limits import clear_rate_limits

    User.objects.filter(pk=opts.misc_user_id).update(
        is_email_verified=False, is_active=True, requires_password_change=False)
    user = User.objects.get(pk=opts.misc_user_id)
    token = tokens.generate_email_verify_token(user)

    clear_rate_limits(ip="127.0.0.1")
    resp = opts.client.post("/api/auth/email/verify", {"token": token})

    assert_eq(resp.status_code, 200,
              f"The existing verify-then-login endpoint must keep working, got {resp.status_code}")
    data = (resp.get("response") or {}).get("data") or {}
    assert_true("access_token" in data,
                f"Existing clients still receive a token package here, got {sorted(data)!r}")
    user.refresh_from_db()
    assert_true(user.is_email_verified,
                "The legacy endpoint still verifies the address")


# ===========================================================================
# What the page is, and is not
# ===========================================================================

@th.django_unit_test("landing: a missing token renders the invalid-link state with no button")
def test_landing_missing_token_renders_invalid_link(opts):
    resp, body = _get(opts, "/api/auth/email/change/confirm")

    assert_eq(resp.status_code, 200, f"The landing must render 200, got {resp.status_code}")
    assert_true(INVALID_MARKER in body,
                f"Expected the invalid-link state, got: {body[:400]!r}")
    assert_true(READY_MARKER not in body,
                "A token-less landing must not render the ready state")
    assert_true('id="mojo-landing-submit"' not in body,
                "A token-less landing must offer NO submit control at all")


@th.django_unit_test("landing: a repeated or structured ?token= is treated as absent")
def test_landing_structured_token_is_treated_as_absent(opts):
    from mojo.decorators.limits import clear_rate_limits
    clear_rate_limits(ip="127.0.0.1")

    resp = opts.client.get(
        "/api/auth/email/change/confirm?token=ec%3Aone&token=ec%3Atwo")
    body = resp.get("text") or ""

    assert_eq(resp.status_code, 200, f"The landing must render 200, got {resp.status_code}")
    assert_true(INVALID_MARKER in body,
                f"A repeated ?token= yields a list, which cannot name one token — "
                f"expected the invalid-link state, got: {body[:400]!r}")
    assert_true('id="mojo-landing-submit"' not in body,
                "No submit control may be offered for an unusable token")


@th.django_unit_test("landing: the GET never names the account the token belongs to")
def test_landing_never_renders_account_identity(opts):
    from mojo.apps.account.models import User
    from mojo.apps.account.utils import tokens

    user = User.objects.get(pk=opts.misc_user_id)
    token = tokens.generate_email_verify_token(user)

    resp, body = _get(opts, "/api/auth/verify/email/confirm", token=token)

    assert_eq(resp.status_code, 200, f"The landing must render 200, got {resp.status_code}")
    assert_true(READY_MARKER in body,
                f"Expected the ready state to render, got: {body[:400]!r}")
    assert_true(str(user.email) not in body,
                "Nobody has proved they hold the token yet — the GET must not "
                "disclose the account's email address")
    assert_true(str(user.username) not in body,
                "Nor the username")


@th.django_unit_test("landing: the token lives only in inert page state")
def test_landing_token_is_inert_page_state(opts):
    from mojo.apps.account.models import User
    from mojo.apps.account.utils import tokens

    user = User.objects.get(pk=opts.misc_user_id)
    token = tokens.generate_email_verify_token(user)

    resp, body = _get(opts, "/api/auth/verify/email/confirm", token=token)
    assert_eq(resp.status_code, 200, f"The landing must render 200, got {resp.status_code}")

    marker = '<script id="mojo-landing-data" type="application/json">'
    assert_true(marker in body,
                f"Expected the json_script data block, got: {body[:400]!r}")
    start = body.index(marker)
    end = body.index("</script>", start)
    data_block = body[start:end]
    outside = body[:start] + body[end:]

    assert_true(token in data_block,
                "The token must be carried by the application/json data block")
    assert_true(token not in outside,
                "The token must appear NOWHERE else on the page — not in an href, "
                "not in executable script text")
    assert_true("localStorage" not in body and "sessionStorage" not in body,
                "The token must never be written to browser storage")
    assert_true("history.replaceState" in body,
                "The page must drop the query string from history after init")


@th.django_unit_test("landing: the page is self-contained")
def test_landing_is_self_contained(opts):
    resp, body = _get(opts, "/api/account/deactivate/confirm", token="dv:render")

    assert_eq(resp.status_code, 200, f"The landing must render 200, got {resp.status_code}")
    assert_true(READY_MARKER in body,
                f"Expected the ready state to render, got: {body[:400]!r}")
    assert_true("<script src" not in body and "<script  src" not in body,
                "A confirmation page must load no third-party script")
    assert_true('rel="stylesheet"' not in body,
                "A confirmation page must load no external stylesheet")
    assert_true("<img" not in body,
                "A confirmation page must load no remote image — a tracking pixel "
                "on this URL would leak the token in a Referer")


@th.django_unit_test("landing: a <noscript> notice explains the JavaScript requirement")
def test_landing_has_noscript_notice(opts):
    resp, body = _get(opts, "/api/auth/email/change/confirm", token="ec:render")

    assert_eq(resp.status_code, 200, f"The landing must render 200, got {resp.status_code}")
    assert_true("<noscript>" in body,
                "Confirming needs JavaScript; a no-JS visitor must be told so")
    noscript = body[body.index("<noscript>"):body.index("</noscript>")]
    assert_true("JavaScript" in noscript,
                f"The notice must name JavaScript, got: {noscript!r}")


@th.django_unit_test("landing: the response carries Referrer-Policy: no-referrer")
def test_landing_sends_no_referrer(opts):
    resp, body = _get(opts, "/api/auth/email/change/confirm", token="ec:render")

    assert_eq(resp.status_code, 200, f"The landing must render 200, got {resp.status_code}")
    assert_eq(_header(opts, "Referrer-Policy"), "no-referrer",
              f"The token is in this URL — it must not ride along in a Referer. "
              f"Headers were: {dict(opts.client.last_response.headers)!r}")
    assert_eq(_header(opts, "X-Frame-Options"), "DENY",
              f"A framed landing plus its token is a clickjacking primitive. "
              f"Headers were: {dict(opts.client.last_response.headers)!r}")


@th.django_unit_test("landing: nothing auto-navigates")
def test_landing_emits_no_meta_refresh(opts):
    resp, body = _get(opts, "/api/auth/email/change/confirm",
                      token="ec:render", redirect="https://example.com/app")

    assert_eq(resp.status_code, 200, f"The landing must render 200, got {resp.status_code}")
    assert_true("http-equiv" not in body,
                "No <meta http-equiv=refresh> may ever be emitted — a confirmation "
                "page that navigates on its own is the bug this item fixed")


# ===========================================================================
# Where the links point
# ===========================================================================

@th.django_unit_test("emailed links: the three confirmation flows target the landings")
def test_emailed_links_target_landings(opts):
    from mojo.apps.account.utils.webapp_url import build_token_url

    expected = {
        "email_verify": "/api/auth/verify/email/confirm",
        "email_change": "/api/auth/email/change/confirm",
        "account_deactivate": "/api/account/deactivate/confirm",
    }
    for flow, path in expected.items():
        url = build_token_url(flow, "xx:tok3n")
        assert_true(f"{path}?token=" in url,
                    f"{flow} must link to the API landing {path}, got {url!r}")
        assert_true("flow=" not in url,
                    f"{flow} must not carry the flow= parameter nothing reads, got {url!r}")

    for flow in ("password_reset", "magic_login", "invite"):
        url = build_token_url(flow, "xx:tok3n")
        assert_true(f"flow={flow}&token=" in url,
                    f"{flow} keeps its frontend URL — its consumer exists — got {url!r}")
        for path in expected.values():
            assert_true(path not in url,
                        f"{flow} must not be rerouted to a landing, got {url!r}")


@th.django_unit_test("emailed links: a token signature is percent-encoded")
def test_emailed_link_encodes_the_token(opts):
    from mojo.apps.account.utils.webapp_url import build_token_url

    url = build_token_url("email_verify", "ev:abc+def=")
    assert_true("ev:abc%2Bdef%3D" in url,
                f"A '+' in a signature decodes back as a space unless encoded; "
                f"the readable colon is kept. Got {url!r}")


@th.django_unit_test("/auth: a flow token is routed to its landing server-side")
def test_auth_page_routes_flow_tokens_to_landings(opts):
    cases = {
        "ec:legacylink": "/api/auth/email/change/confirm",
        "ev:legacylink": "/api/auth/verify/email/confirm",
        "dv:legacylink": "/api/account/deactivate/confirm",
    }
    for token, path in cases.items():
        resp = opts.client.get("/auth", params={"flow": "whatever", "token": token},
                               allow_redirects=False)
        assert_eq(resp.status_code, 302,
                  f"/auth?token={token} must redirect to the landing, got {resp.status_code}")
        location = _header(opts, "Location") or ""
        assert_true(location.startswith(f"{path}?token="),
                    f"Expected a redirect to {path}, got {location!r}")
        assert_true("flow=" not in location,
                    f"The redirect keys on the token prefix, not flow=; got {location!r}")


@th.django_unit_test("/auth: a sanitized ?redirect= rides along, a hostile one does not")
def test_auth_page_redirect_passenger_is_sanitized(opts):
    resp = opts.client.get(
        "/auth", params={"token": "ec:legacylink", "redirect": "javascript:alert(1)"},
        allow_redirects=False)
    assert_eq(resp.status_code, 302, f"Expected a redirect, got {resp.status_code}")
    location = _header(opts, "Location") or ""
    assert_true("javascript" not in location.lower(),
                f"A refused ?redirect= must be dropped, not forwarded; got {location!r}")

    resp = opts.client.get(
        "/auth", params={"token": "ec:legacylink", "redirect": "/dashboard"},
        allow_redirects=False)
    assert_eq(resp.status_code, 302, f"Expected a redirect, got {resp.status_code}")
    location = _header(opts, "Location") or ""
    assert_true("redirect=%2Fdashboard" in location,
                f"An accepted destination must ride along; got {location!r}")


@th.django_unit_test("/auth: ml:, pr: and token-less requests are untouched")
def test_auth_page_leaves_ml_and_pr_alone(opts):
    for token in ("ml:notalanding", "pr:notalanding", "iv:notalanding"):
        resp = opts.client.get("/auth", params={"token": token}, allow_redirects=False)
        location = _header(opts, "Location") or ""
        assert_true(resp.status_code != 302 or "confirm" not in location,
                    f"/auth?token={token} must fall through to the login page unchanged, "
                    f"got {resp.status_code} -> {location!r}")

    resp = opts.client.get("/auth", allow_redirects=False)
    location = _header(opts, "Location") or ""
    assert_true(resp.status_code != 302 or "confirm" not in location,
                f"A token-less /auth must be untouched, got {resp.status_code} -> {location!r}")


# ===========================================================================
# Rate limiting — a separate bucket, and a diagnostic that leaks nothing
# ===========================================================================

def _isolated_ip():
    """A per-test source IP so throttle counters cannot collide across the
    parallel suite. nginx sets X-Real-IP to the true client and the framework
    trusts exactly that header (mojo/helpers/request.py::get_remote_ip)."""
    octets = _uuid.uuid4().int
    return "10.%d.%d.%d" % ((octets >> 16) & 0xFF, (octets >> 8) & 0xFF, octets & 0xFF)


@th.django_unit_test("landing: the GET bucket is separate from the POST budget")
def test_landing_bucket_is_separate_from_post_bucket(opts):
    ip = _isolated_ip()
    headers = {"X-Real-IP": ip}

    blocked = None
    for attempt in range(11):
        resp = opts.client.get(
            "/api/auth/email/change/confirm",
            params={"token": "ec:preview"}, headers=headers)
        if resp.status_code == 429:
            blocked = resp
            break
    assert_true(blocked is not None,
                "11 landing views from one IP must trip the 10/hour landing bucket")
    assert_true(_header(opts, "Retry-After"),
                "A throttled landing must still carry Retry-After")

    # The confirmation budget is untouched — previews and reloads must not eat it.
    resp = opts.client.post(
        "/api/auth/email/change/confirm", {"token": "ec:notavalidtoken"},
        headers=headers)
    assert_true(resp.status_code != 429,
                f"The POST budget is separate from the landing's — a person who "
                f"reloaded the page must still be able to confirm; got {resp.status_code}")


@th.django_unit_test("landing: a throttle diagnostic carries no token, path or query")
def test_landing_throttle_incident_carries_no_token(opts):
    from mojo.apps.incident.models import Event

    ip = _isolated_ip()
    headers = {"X-Real-IP": ip}
    token = "ec:throttle-secret-token"

    for attempt in range(11):
        resp = opts.client.get(
            "/api/auth/email/change/confirm",
            params={"token": token}, headers=headers)
        if resp.status_code == 429:
            break
    assert_eq(resp.status_code, 429,
              f"The landing bucket must engage after 10 views, got {resp.status_code}")

    event = Event.objects.filter(
        category="rate_limit:email_change_landing", source_ip=ip).last()
    assert_true(event is not None,
                f"A throttled landing must still file its diagnostic for {ip}")
    meta = event.metadata or {}
    for field in ("http_path", "http_query_string", "http_user_agent", "request_ip"):
        assert_true(field not in meta,
                    f"{field} must not be recorded for a landing throttle; metadata was {meta!r}")
    blob = f"{event.details}{event.title}{meta!r}"
    assert_true("throttle-secret-token" not in blob,
                "The throttled request's own token must never reach the Event")
    assert_true("email_change_landing" in event.details,
                f"The diagnostic must still name the bucket, got {event.details!r}")
