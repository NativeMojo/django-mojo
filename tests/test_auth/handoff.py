"""
Tests for the cross-origin auth handoff (authorization-code style) flow.

Service layer: create_handoff_code / consume_handoff_code (Redis-backed,
single-use, TTL-bounded), plus the destination allowlist that decides whether a
code may be minted at all.

REST surface:
  POST /api/auth/handoff   — authenticated, requires an ALLOWED redirect_uri
  POST /api/auth/exchange  — public, swaps code for JWT, single-use, rate-limited

The allowlist is enforced at ISSUANCE, never at exchange — exchange is a
server-to-server call whose headers an attacker holding the code also controls.

Destinations pinned in the test project settings (see bin/create_testproject):
  AUTH_HANDOFF_ALLOWED_URLS = ["https://example.com/",
                               "https://*.handoff.example.net/app"]

The in-process allowlist tests (which assign/delete the AUTH_HANDOFF_*
attributes on django.conf.settings) moved to
tests/test_auth_extended_serial/handoff.py (maestro item #1839).
"""
from testit import helpers as th
from testit.helpers import assert_true, assert_eq


TEST_USER = "handoff_user"
TEST_PWORD = "handoff##mojo99"

# Destinations the pinned test-project allowlist admits.
ALLOWED_DEST = "https://example.com/app"
ALLOWED_WILDCARD_DEST = "https://tenant.handoff.example.net/app/home"
# The allowlist ENTRIES that admit the two destinations above. Enforcement is
# opt-in, so the test server has no allowlist by default — the enforcement test
# installs these with th.server_settings().
ALLOWED_ENTRY = "https://example.com/"
ALLOWED_WILDCARD_ENTRY = "https://*.handoff.example.net/app"


@th.django_unit_setup()
def setup_handoff_user(opts):
    from mojo.apps.account.models import User
    from mojo.decorators.limits import clear_rate_limits
    clear_rate_limits(ip="127.0.0.1")

    User.objects.filter(username=TEST_USER).delete()
    user = User(username=TEST_USER, email=f"{TEST_USER}@example.com", display_name=TEST_USER)
    user.save()
    user.is_email_verified = True
    user.is_active = True
    user.save_password(TEST_PWORD)
    user.save()
    opts.user_id = user.pk


# ---------------------------------------------------------------------------
# Service-layer tests (no HTTP)
# ---------------------------------------------------------------------------

@th.django_unit_test("auth_handoff: create + consume round-trip")
def test_create_and_consume(opts):
    from mojo.apps.account.models import User
    from mojo.apps.account.services import auth_handoff

    user = User.objects.get(pk=opts.user_id)
    code = auth_handoff.create_handoff_code(user, destination=ALLOWED_DEST, ip="127.0.0.1")
    assert_true(isinstance(code, str) and len(code) == 32, f"code should be 32-hex, got {code!r}")

    data = auth_handoff.consume_handoff_code(code)
    assert_true(data is not None, "consume should succeed for a fresh code")
    assert_eq(data["uid"], user.pk, "stored uid should match user")
    assert_eq(data["ip"], "127.0.0.1", "stored ip should match issuing ip")
    assert_eq(data["dest"], ALLOWED_DEST, "stored dest should record the minted-for destination")


@th.django_unit_test("auth_handoff: code is single-use")
def test_consume_is_single_use(opts):
    from mojo.apps.account.models import User
    from mojo.apps.account.services import auth_handoff

    user = User.objects.get(pk=opts.user_id)
    code = auth_handoff.create_handoff_code(user)

    first = auth_handoff.consume_handoff_code(code)
    second = auth_handoff.consume_handoff_code(code)
    assert_true(first is not None, "first consume should succeed")
    assert_true(second is None, "second consume of the same code must return None")


@th.django_unit_test("auth_handoff: invalid code returns None")
def test_consume_invalid_code(opts):
    from mojo.apps.account.services import auth_handoff
    assert_true(auth_handoff.consume_handoff_code("not_a_real_code") is None,
                "random code should not resolve")
    assert_true(auth_handoff.consume_handoff_code("") is None,
                "empty code should not resolve")
    assert_true(auth_handoff.consume_handoff_code(None) is None,
                "None code should not resolve")


@th.django_unit_test("auth_handoff: expired/manually-deleted code returns None")
def test_consume_expired(opts):
    from mojo.apps.account.models import User
    from mojo.apps.account.services import auth_handoff
    from mojo.helpers.redis import get_connection

    user = User.objects.get(pk=opts.user_id)
    code = auth_handoff.create_handoff_code(user)

    # Simulate TTL expiry by deleting the Redis key directly.
    get_connection().delete(f"auth:handoff:{code}")
    assert_true(auth_handoff.consume_handoff_code(code) is None,
                "expired code must not resolve")


# ---------------------------------------------------------------------------
# Endpoint tests
# ---------------------------------------------------------------------------

def _minted_code(resp):
    """Return the code from a handoff response body, or None when absent."""
    body = resp.response
    if not isinstance(body, dict):
        return None
    data = body.get("data")
    if not isinstance(data, dict):
        return None
    return data.get("code")


@th.unit_test("auth/handoff requires authentication")
def test_handoff_endpoint_requires_auth(opts):
    opts.client.logout()
    # Body deliberately empty: auth must be decided before the parameter check,
    # so this must not degrade into a 400.
    resp = opts.client.post("/api/auth/handoff", {})
    assert_true(resp.status_code in (401, 403),
                f"unauthenticated handoff should be rejected, got {resp.status_code}")


@th.unit_test("auth/handoff monitor mode mints anything and needs no redirect_uri")
def test_handoff_endpoint_monitor_mode_mints_anything(opts):
    """The shipped default: no allowlist configured means NOTHING changes.

    This is THE upgrade-safety test, and it runs against the test server's
    default state on purpose — no server_settings, no fixture. A deployment
    that upgrades without setting AUTH_HANDOFF_ALLOWED_URLS or
    AUTH_HANDOFF_RESOLVER must keep minting exactly as it did before this
    feature existed: for a request with no redirect_uri at all, and for a
    destination that enforcement would refuse.
    """
    from mojo.decorators.limits import clear_rate_limits
    clear_rate_limits(ip="127.0.0.1", key="auth_handoff")

    assert_true(opts.client.login(TEST_USER, TEST_PWORD), "login should succeed")

    resp = opts.client.post("/api/auth/handoff", {})
    assert_eq(resp.status_code, 200,
              f"monitor mode must mint with no redirect_uri (pre-feature behavior), "
              f"got {resp.status_code}: {resp.response}")
    assert_true(bool(_minted_code(resp)),
                f"monitor mode must return a code for an empty body, got {resp.response}")

    resp = opts.client.post("/api/auth/handoff", {"redirect_uri": "https://evil.tld/steal"})
    assert_eq(resp.status_code, 200,
              f"monitor mode must mint even for a destination enforcement would refuse — "
              f"that is the whole point of opt-in. Got {resp.status_code}: {resp.response}")
    assert_true(bool(_minted_code(resp)),
                f"monitor mode must return a code for a foreign host, got {resp.response}")


@th.unit_test("auth/handoff enforced: required redirect_uri, allowed mints, unlisted refused")
def test_handoff_endpoint_enforcement(opts):
    """Everything enforcement changes, inside ONE server reload.

    Enforcement is opt-in, so the test server does not run it by default —
    setting AUTH_HANDOFF_ALLOWED_URLS is what turns it on, exactly as a
    deployment would. Grouped into a single test because each
    th.server_settings block costs a uvicorn reload.
    """
    from mojo.decorators.limits import clear_rate_limits

    with th.server_settings(AUTH_HANDOFF_ALLOWED_URLS=[ALLOWED_ENTRY, ALLOWED_WILDCARD_ENTRY]):
        clear_rate_limits(ip="127.0.0.1", key="auth_handoff")
        assert_true(opts.client.login(TEST_USER, TEST_PWORD), "login should succeed")

        resp = opts.client.post("/api/auth/handoff", {})
        assert_eq(resp.status_code, 400,
                  f"with an allowlist configured, a missing redirect_uri must 400, "
                  f"got {resp.status_code}: {resp.response}")
        assert_true(_minted_code(resp) is None,
                    f"a refused handoff must not return a code, got {resp.response}")

        resp = opts.client.post("/api/auth/handoff", {"redirect_uri": ALLOWED_DEST})
        assert_eq(resp.status_code, 200,
                  f"an allowed destination must still mint under enforcement, "
                  f"got {resp.status_code}: {resp.response}")
        assert_true(bool(_minted_code(resp)),
                    f"an allowed destination must return a code, got {resp.response}")

        resp = opts.client.post("/api/auth/handoff", {"redirect_uri": ALLOWED_WILDCARD_DEST})
        assert_eq(resp.status_code, 200,
                  f"an allowed wildcard destination must mint, "
                  f"got {resp.status_code}: {resp.response}")
        assert_true(bool(_minted_code(resp)), f"a code should be returned, got {resp.response}")

        refused = [
            ("https://evil.tld/steal", "a foreign host"),
            ("https://example.com.evil.tld/", "a suffix-extended confusable host"),
            ("https://example.com@evil.tld/", "a userinfo confusable host"),
            ("https://a.b.handoff.example.net/app", "two labels under a one-label wildcard"),
            ("https://tenant.handoff.example.net/other", "a path outside the allowed prefix"),
            ("javascript:alert(1)", "a javascript: URL"),
            ("/relative/path", "a relative path"),
        ]
        for dest, why in refused:
            resp = opts.client.post("/api/auth/handoff", {"redirect_uri": dest})
            assert_eq(resp.status_code, 400,
                      f"handoff to {dest!r} ({why}) should 400 under enforcement, "
                      f"got {resp.status_code}: {resp.response}")
            assert_true(_minted_code(resp) is None,
                        f"handoff to {dest!r} ({why}) must not return a code, got {resp.response}")


@th.django_unit_test("monitor mode files an incident naming the unlisted destination")
def test_monitor_mode_reports_incident(opts):
    """Monitor mode is only useful if it TELLS you. The incident feed is what
    lets a deployment build AUTH_HANDOFF_ALLOWED_URLS before opting in."""
    from mojo.apps.incident.models import Event
    from mojo.apps.account.services import redirect_allowlist as ra
    from mojo.helpers.redis import get_connection

    dest = "https://monitor-probe.example.org/landing"
    category = "auth:handoff_destination_unlisted"
    Event.objects.filter(category=category).delete()
    # Suppression is per host per hour — clear it so this test is repeatable
    # against a long-lived Redis.
    try:
        get_connection().delete(f"{ra._NOTICE_PREFIX}:monitor:monitor-probe.example.org")
    except Exception:
        pass

    ra.report_unlisted_destination(dest, request=None, enforced=False)

    events = list(Event.objects.filter(category=category))
    assert_eq(len(events), 1,
              f"monitor mode must file exactly one {category} event, got {len(events)}")
    assert_true(dest in (events[0].details or ""),
                f"the incident body must name the destination so it can be "
                f"allowlisted, got {events[0].details!r}")

    # Suppressed: a second report for the same host inside the window is a no-op.
    ra.report_unlisted_destination(dest, request=None, enforced=False)
    assert_eq(Event.objects.filter(category=category).count(), 1,
              "a repeat destination inside the suppression window must not "
              "file a second incident — a crafted-link flood must not be able "
              "to spam the incident plane")

    Event.objects.filter(category=category).delete()


@th.unit_test("auth/handoff returns code + expires_in for an allowed destination")
def test_handoff_endpoint_returns_code(opts):
    assert_true(opts.client.login(TEST_USER, TEST_PWORD), "login should succeed")
    resp = opts.client.post("/api/auth/handoff", {"redirect_uri": ALLOWED_DEST})
    assert_eq(resp.status_code, 200, f"handoff should return 200, got {resp.status_code}: {resp.response}")
    data = resp.response.data
    assert_true(bool(data.code) and len(data.code) == 32,
                f"code should be 32-hex, got {data.code!r}")
    assert_true(data.expires_in > 0,
                f"expires_in should be positive, got {data.expires_in}")
    opts.handoff_code = data.code


@th.unit_test("auth/exchange returns JWT for valid code")
def test_exchange_endpoint_returns_jwt(opts):
    # Mint a fresh code via the authed endpoint, then drop the bearer to simulate
    # the consuming app calling exchange without prior auth.
    assert_true(opts.client.login(TEST_USER, TEST_PWORD), "login should succeed")
    resp = opts.client.post("/api/auth/handoff", {"redirect_uri": ALLOWED_DEST})
    code = resp.response.data.code
    opts.client.logout()

    resp = opts.client.post("/api/auth/exchange", {"code": code})
    assert_eq(resp.status_code, 200, f"exchange should return 200, got {resp.status_code}: {resp.response}")
    data = resp.response.data
    assert_true(bool(data.access_token), "access_token must be present")
    assert_true(bool(data.refresh_token), "refresh_token must be present")
    assert_true(data.user.id == opts.user_id, f"user.id should match, got {data.user.id}")


@th.unit_test("auth/exchange code is single-use")
def test_exchange_single_use(opts):
    assert_true(opts.client.login(TEST_USER, TEST_PWORD), "login should succeed")
    code = opts.client.post("/api/auth/handoff", {"redirect_uri": ALLOWED_DEST}).response.data.code
    opts.client.logout()

    first = opts.client.post("/api/auth/exchange", {"code": code})
    assert_eq(first.status_code, 200, f"first exchange should succeed, got {first.status_code}")

    second = opts.client.post("/api/auth/exchange", {"code": code})
    assert_eq(second.status_code, 401,
              f"second exchange of consumed code must 401, got {second.status_code}: {second.response}")


@th.unit_test("auth/exchange invalid code is rejected")
def test_exchange_invalid_code(opts):
    opts.client.logout()
    resp = opts.client.post("/api/auth/exchange", {"code": "deadbeefdeadbeefdeadbeefdeadbeef"})
    assert_eq(resp.status_code, 401,
              f"invalid code should 401, got {resp.status_code}: {resp.response}")


@th.unit_test("auth/exchange rejects code for inactive user")
def test_exchange_inactive_user(opts):
    from mojo.apps.account.models import User

    assert_true(opts.client.login(TEST_USER, TEST_PWORD), "login should succeed")
    code = opts.client.post("/api/auth/handoff", {"redirect_uri": ALLOWED_DEST}).response.data.code
    opts.client.logout()

    User.objects.filter(pk=opts.user_id).update(is_active=False)
    try:
        resp = opts.client.post("/api/auth/exchange", {"code": code})
        assert_eq(resp.status_code, 403,
                  f"inactive user exchange should 403, got {resp.status_code}: {resp.response}")
    finally:
        User.objects.filter(pk=opts.user_id).update(is_active=True)


@th.unit_test("auth/exchange full round-trip yields a usable JWT")
def test_full_round_trip(opts):
    assert_true(opts.client.login(TEST_USER, TEST_PWORD), "login should succeed")
    code = opts.client.post("/api/auth/handoff", {"redirect_uri": ALLOWED_DEST}).response.data.code
    opts.client.logout()

    resp = opts.client.post("/api/auth/exchange", {"code": code})
    assert_eq(resp.status_code, 200, f"exchange should succeed, got {resp.status_code}")

    # Hand the new tokens to the client and call an authed endpoint to confirm.
    opts.client.is_authenticated = True
    opts.client.access_token = resp.response.data.access_token
    me = opts.client.get("/api/user/me")
    assert_eq(me.status_code, 200, f"/api/user/me with new JWT should 200, got {me.status_code}")
    assert_eq(me.response.data.id, opts.user_id, "JWT should resolve to the original user")


@th.unit_test("auth/exchange is rate-limited (20/min/IP)")
def test_exchange_rate_limit(opts):
    from mojo.decorators.limits import clear_rate_limits

    # Start from a clean slate so prior tests don't push us over the limit.
    clear_rate_limits(ip="127.0.0.1", key="auth_exchange")
    opts.client.logout()

    # Loop bound is well above the 20-req limit so the cap fires reliably
    # even under heavy parallel-suite load (strict_rate_limit fails open on
    # transient Redis errors — the test must tolerate occasional skipped
    # increments while still proving the cap reaches its threshold).
    blocked = False
    for i in range(60):
        resp = opts.client.post("/api/auth/exchange", {"code": "ffffffffffffffffffffffffffffffff"})
        if resp.status_code == 429:
            blocked = True
            break
    assert_true(blocked, "rate limit must trigger within 60 attempts within the window")

    # Reset so subsequent tests in this module aren't affected.
    clear_rate_limits(ip="127.0.0.1", key="auth_exchange")


# ---------------------------------------------------------------------------
# ?back= XSS regression
#
# The sink lives in client-side JS (auth_base.html reads location.search), so
# there is no server round-trip to assert against and no JS runtime in the
# suite. What IS checkable, and what actually regressed, is the template: the
# raw ?back= value must never reach an href, and the guard that replaces it
# must judge the RESOLVED protocol. This fails on the pre-fix template.
# ---------------------------------------------------------------------------

@th.django_unit_test("?back= cannot carry a javascript: URL (XSS regression)")
def test_back_param_is_scheme_guarded(opts):
    from pathlib import Path
    import mojo

    tpl = (Path(mojo.__file__).resolve().parent
           / "apps" / "account" / "templates" / "account" / "auth_base.html")
    assert_true(tpl.exists(), f"auth_base.html should exist at {tpl}")
    src = tpl.read_text(encoding="utf-8")

    assert_true("backEl.href = paramBack" not in src,
                "the raw ?back= value must never be assigned to an href — "
                "a javascript: URL there executes on the auth origin when clicked")
    assert_true("backUrl: paramBack," not in src,
                "the raw ?back= value must not be published on _matConfig either — "
                "page scripts read backUrl straight into an href")
    assert_true("function safeNavUrl(" in src,
                "auth_base.html must define the safeNavUrl guard")
    assert_true('resolved.protocol !== "http:"' in src and 'resolved.protocol !== "https:"' in src,
                "safeNavUrl must decide on the RESOLVED protocol, so javascript:, "
                "data: and protocol-relative URLs are all refused")
    assert_eq(src.count("safeNavUrl(paramBack)"), 2,
              "both ?back= sinks (the hero link href and _matConfig.backUrl) must "
              "run through safeNavUrl")


# ---------------------------------------------------------------------------
# ?redirect= XSS regression
#
# Same shape as the ?back= test above, and for the same reason: the sink is
# client-side JS with no JS runtime in the suite, so the template source is
# what is checkable and what actually regressed. The difference is that the
# post-login destination reaches window.location.href at TWO sinks inside
# _mat.redirect() — direct navigation and post-handoff — so the assertions
# below pin that ONE guard runs before either of them, not two copies that
# can drift apart.
# ---------------------------------------------------------------------------

@th.django_unit_test("?redirect= cannot carry a javascript: URL (XSS regression)")
def test_redirect_param_is_scheme_guarded(opts):
    from pathlib import Path
    import mojo

    tpl = (Path(mojo.__file__).resolve().parent
           / "apps" / "account" / "templates" / "account" / "auth_base.html")
    assert_true(tpl.exists(), f"auth_base.html should exist at {tpl}")
    src = tpl.read_text(encoding="utf-8")

    assert_true("function safeNavUrl(" in src,
                "auth_base.html must define the shared safeNavUrl guard")
    assert_true("safeBackHref" not in src,
                "the ?back= guard must be generalized in place, not left behind "
                "alongside a second copy for ?redirect= to drift from")
    assert_eq(src.count('resolved.protocol !== "http:"'), 1,
              "there must be exactly ONE scheme check in the template — a second "
              "implementation is what drifts")
    assert_true('resolved.protocol !== "http:"' in src and 'resolved.protocol !== "https:"' in src,
                "safeNavUrl must decide on the RESOLVED protocol, so javascript:, "
                "data:, scheme-case and tab-padded variants are all refused")
    assert_true("var target = redirectTo;" not in src,
                "the raw ?redirect=/?next=/?returnTo= value must never become the "
                "navigation target — a javascript: URL there executes on the auth "
                "origin, whose localStorage holds the visitor's tokens")

    body = src.split("redirect: function ()", 1)[1].split("onAuthSuccess:", 1)[0]
    guard_at = body.find("safeNavUrl(redirectTo)")
    first_sink = body.find("window.location.href = ")
    assert_true(0 <= guard_at < first_sink,
                "the scheme guard must run BEFORE the first window.location.href "
                f"sink in _mat.redirect() (guard at {guard_at}, sink at {first_sink})")
    assert_true("showMessage" in body[guard_at:first_sink]
                and '"error"' in body[guard_at:first_sink],
                "a refused destination must show an error, not silently do nothing")
    assert_eq(body.count("window.location.href = target"),
              body.count("window.location.href = "),
              "EVERY navigation sink in _mat.redirect() must go to the guarded "
              "target — a new sink on the raw value reopens the hole")

    assert_true('params.get("redirect")' in src and 'params.get("next")' in src
                and 'params.get("returnTo")' in src,
                "all three destination aliases must still feed the guarded path")
