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
# installs these with Setting.set (read live via settings.get; maestro #2791).
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
    deployment would. The setting is written with Setting.set (read live via
    settings.get) rather than a server reload (maestro #2791).
    """
    from mojo.decorators.limits import clear_rate_limits
    from mojo.apps.account.models.setting import Setting

    # AUTH_HANDOFF_ALLOWED_URLS is read live via settings.get (redirect_allowlist
    # .py:260,610) and is not a protected key, so Setting.set turns enforcement
    # on with no server reload (maestro #2791). Cleaned up in finally so the
    # other handoff tests still see enforcement off.
    Setting.set("AUTH_HANDOFF_ALLOWED_URLS", [ALLOWED_ENTRY, ALLOWED_WILDCARD_ENTRY])
    try:
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
    finally:
        Setting.remove("AUTH_HANDOFF_ALLOWED_URLS")


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


# ---------------------------------------------------------------------------
# PKCE (#7397): a code minted with a challenge exchanges only with its secret
# ---------------------------------------------------------------------------

# RFC 7636 appendix B test vector.
PKCE_VERIFIER = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
PKCE_CHALLENGE = "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM"
PKCE_OTHER_VERIFIER = "aBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"


def _clear_handoff_limits():
    from mojo.decorators.limits import clear_rate_limits
    clear_rate_limits(ip="127.0.0.1", key="auth_handoff")
    clear_rate_limits(ip="127.0.0.1", key="auth_exchange")


def _mint_with_challenge(opts, **extra):
    """Mint a code as the test user and sign out. Returns the handoff response."""
    assert_true(opts.client.login(TEST_USER, TEST_PWORD), "login should succeed")
    body = {"redirect_uri": ALLOWED_DEST}
    body.update(extra)
    resp = opts.client.post("/api/auth/handoff", body)
    opts.client.logout()
    return resp


def _pkce_code(opts):
    resp = _mint_with_challenge(
        opts, code_challenge=PKCE_CHALLENGE, code_challenge_method="S256")
    assert_eq(resp.status_code, 200,
              f"a handoff with a valid S256 challenge must mint, "
              f"got {resp.status_code}: {resp.response}")
    code = _minted_code(resp)
    assert_true(bool(code), f"no code minted: {resp.response}")
    return code


@th.unit_test("#7397: a code minted with a challenge does not exchange without the secret")
def test_pkce_code_needs_the_secret(opts):
    """The regression. Before #7397 the challenge was ignored and the bare code
    returned an access and refresh token pair to whoever held it."""
    _clear_handoff_limits()
    code = _pkce_code(opts)

    resp = opts.client.post("/api/auth/exchange", {"code": code})
    assert_eq(resp.status_code, 401,
              f"a code minted with a challenge must NOT exchange without the "
              f"code_verifier, got {resp.status_code}: {resp.response}")

    resp = opts.client.post(
        "/api/auth/exchange", {"code": code, "code_verifier": PKCE_VERIFIER})
    assert_eq(resp.status_code, 401,
              f"the failed attempt must have spent the code, so the right "
              f"secret afterwards also answers 401, got {resp.status_code}: {resp.response}")


@th.unit_test("#7397: the matching secret exchanges; a wrong one is refused and spends the code")
def test_pkce_right_and_wrong_secret(opts):
    _clear_handoff_limits()
    code = _pkce_code(opts)
    resp = opts.client.post(
        "/api/auth/exchange", {"code": code, "code_verifier": PKCE_VERIFIER})
    assert_eq(resp.status_code, 200,
              f"the matching code_verifier must exchange, got {resp.status_code}: {resp.response}")
    data = resp.response.data
    assert_true(bool(data.access_token) and bool(data.refresh_token),
                "the exchange must return an access and refresh token pair")
    assert_true(data.user.id == opts.user_id, f"user.id should match, got {data.user.id}")

    code = _pkce_code(opts)
    resp = opts.client.post(
        "/api/auth/exchange", {"code": code, "code_verifier": PKCE_OTHER_VERIFIER})
    assert_eq(resp.status_code, 401,
              f"a wrong code_verifier must answer 401, got {resp.status_code}: {resp.response}")
    resp = opts.client.post(
        "/api/auth/exchange", {"code": code, "code_verifier": PKCE_VERIFIER})
    assert_eq(resp.status_code, 401,
              f"a wrong code_verifier must spend the code, got {resp.status_code}: {resp.response}")


@th.unit_test("#7397: a secret sent for a code that has no challenge is refused")
def test_pkce_secret_for_a_code_without_challenge(opts):
    """Otherwise an attacker mints a code for their OWN account and feeds it to
    the real app, which believes it is completing the flow it started."""
    _clear_handoff_limits()
    resp = _mint_with_challenge(opts)
    code = _minted_code(resp)
    assert_true(bool(code), f"no code minted: {resp.response}")

    resp = opts.client.post(
        "/api/auth/exchange", {"code": code, "code_verifier": PKCE_VERIFIER})
    assert_eq(resp.status_code, 401,
              f"a code_verifier for a code with no challenge must answer 401, "
              f"got {resp.status_code}: {resp.response}")
    resp = opts.client.post("/api/auth/exchange", {"code": code})
    assert_eq(resp.status_code, 401,
              f"that attempt must have spent the code, got {resp.status_code}: {resp.response}")


@th.unit_test("#7397: a malformed challenge is refused and no code is minted")
def test_pkce_malformed_challenge_is_refused(opts):
    _clear_handoff_limits()
    refused = [
        ({"code_challenge": PKCE_CHALLENGE, "code_challenge_method": "plain"}, "method plain"),
        ({"code_challenge": PKCE_CHALLENGE}, "no method"),
        ({"code_challenge": PKCE_CHALLENGE[:42], "code_challenge_method": "S256"},
         "a 42-character challenge"),
        ({"code_challenge": "", "code_challenge_method": "S256"}, "an empty challenge"),
        ({"code_challenge": ["x"], "code_challenge_method": "S256"}, "a list"),
        ({"code_challenge": 12345, "code_challenge_method": "S256"}, "a number"),
        ({"code_challenge_method": "S256"}, "a method with no challenge"),
    ]
    for extra, why in refused:
        resp = _mint_with_challenge(opts, **extra)
        assert_eq(resp.status_code, 400,
                  f"a handoff with {why} must answer 400, got {resp.status_code}: {resp.response}")
        assert_true(_minted_code(resp) is None,
                    f"a handoff with {why} must not return a code, got {resp.response}")


@th.unit_test("#7397: a non-string code_verifier is a 401, never a 500")
def test_pkce_non_string_verifier(opts):
    _clear_handoff_limits()
    for bad in (["x"], {"a": 1}, 12345, ""):
        code = _pkce_code(opts)
        resp = opts.client.post("/api/auth/exchange", {"code": code, "code_verifier": bad})
        assert_eq(resp.status_code, 401,
                  f"code_verifier={bad!r} must answer 401, got {resp.status_code}: {resp.response}")


@th.unit_test("#7397: a wrong secret cannot tell a disabled account from a bad code")
def test_pkce_check_runs_before_the_account_lookup(opts):
    from mojo.apps.account.models import User

    _clear_handoff_limits()
    code = _pkce_code(opts)
    User.objects.filter(pk=opts.user_id).update(is_active=False)
    try:
        resp = opts.client.post(
            "/api/auth/exchange", {"code": code, "code_verifier": PKCE_OTHER_VERIFIER})
        assert_eq(resp.status_code, 401,
                  f"a wrong code_verifier for a disabled account must answer 401, "
                  f"not 403, got {resp.status_code}: {resp.response}")
    finally:
        User.objects.filter(pk=opts.user_id).update(is_active=True)


@th.django_unit_test("#7397: the stored record carries the challenge and check_exchange reads it")
def test_pkce_service_layer(opts):
    from mojo.apps.account.models import User
    from mojo.apps.account.services import auth_handoff

    user = User.objects.get(pk=opts.user_id)
    code = auth_handoff.create_handoff_code(
        user, destination="myapp://auth", code_challenge=PKCE_CHALLENGE)
    data = auth_handoff.consume_handoff_code(code)
    assert_eq(data.get("cc"), PKCE_CHALLENGE, "the record must store the challenge as cc")
    assert_true(auth_handoff.check_exchange(data, PKCE_VERIFIER), "the pre-image must pass")
    assert_true(not auth_handoff.check_exchange(data, PKCE_OTHER_VERIFIER), "another secret must fail")
    assert_true(not auth_handoff.check_exchange(data), "no secret must fail")
    assert_true(not auth_handoff.check_exchange(data, None), "a null secret must fail")
    assert_true(not auth_handoff.check_exchange(data, ["x"]), "a non-string must fail, not raise")

    plain = auth_handoff.consume_handoff_code(auth_handoff.create_handoff_code(user))
    assert_true("cc" not in plain, "a code minted without a challenge stores no cc key")
    assert_true(auth_handoff.check_exchange(plain), "no challenge and no secret passes, as before")
    for sent in (None, "", ["x"], 12345):
        assert_true(not auth_handoff.check_exchange(plain, sent),
                    f"a code_verifier field sent as {sent!r} for a record with no "
                    f"challenge was sent, and must fail")
    assert_true(not auth_handoff.check_exchange(plain, PKCE_VERIFIER),
                "a secret for a record with no challenge must fail")


@th.unit_test("#7397: a challenge field sent as null is malformed, not absent")
def test_pkce_null_challenge_is_refused(opts):
    """A null field was sent. Reading it as absent minted an unbound code for a
    caller who believed it had asked for a bound one."""
    _clear_handoff_limits()
    refused = [
        ({"code_challenge": None}, "a null challenge"),
        ({"code_challenge": None, "code_challenge_method": None}, "a null challenge and method"),
        ({"code_challenge_method": None}, "a null method"),
        ({"code_challenge": None, "code_challenge_method": "S256"}, "a null challenge with S256"),
    ]
    for extra, why in refused:
        resp = _mint_with_challenge(opts, **extra)
        assert_eq(resp.status_code, 400,
                  f"a handoff with {why} must answer 400, got {resp.status_code}: {resp.response}")
        assert_true(_minted_code(resp) is None,
                    f"a handoff with {why} must not return a code, got {resp.response}")


@th.unit_test("#7397: a code_verifier field sent for a code with no challenge is refused, null included")
def test_pkce_any_verifier_field_for_a_code_without_challenge(opts):
    _clear_handoff_limits()
    for sent in (None, "", ["x"], 12345):
        resp = _mint_with_challenge(opts)
        code = _minted_code(resp)
        assert_true(bool(code), f"no code minted: {resp.response}")
        resp = opts.client.post("/api/auth/exchange", {"code": code, "code_verifier": sent})
        assert_eq(resp.status_code, 401,
                  f"code_verifier={sent!r} for a code with no challenge must answer "
                  f"401, got {resp.status_code}: {resp.response}")


@th.unit_test("#7397: the failed-exchange incident stores neither the code nor the secret")
def test_pkce_failed_incident_keeps_no_secret(opts):
    """Both fields are accepted in the query string, and the incident reporter
    stores the query string of any request it is handed."""
    import json
    from mojo.apps.incident.models import Event as IncidentEvent

    _clear_handoff_limits()
    code = _pkce_code(opts)
    before = set(IncidentEvent.objects.filter(
        category="auth:handoff_pkce_failed", uid=opts.user_id).values_list("pk", flat=True))
    resp = opts.client.post(
        f"/api/auth/exchange?code={code}&code_verifier={PKCE_OTHER_VERIFIER}", {})
    assert_eq(resp.status_code, 401,
              f"a wrong code_verifier in the query string must answer 401, "
              f"got {resp.status_code}: {resp.response}")
    events = [e for e in IncidentEvent.objects.filter(
        category="auth:handoff_pkce_failed", uid=opts.user_id) if e.pk not in before]
    assert_eq(len(events), 1, f"one auth:handoff_pkce_failed incident expected, got {len(events)}")
    event = events[0]
    stored = json.dumps(event.metadata, default=str) + str(event.details) + str(event.title)
    assert_true(code not in stored, "the incident must not store the handoff code")
    assert_true(PKCE_OTHER_VERIFIER not in stored, "the incident must not store the code_verifier")
    assert_true("http_query_string" not in (event.metadata or {}),
                "the incident must not keep the request's query string")
    assert_true(bool(event.source_ip), "the incident must still record the caller's address")


@th.tier("extended")  # per-IP rate-limit counter — cannot run in the parallel core/framework ring (#2789 -j sweep)
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
