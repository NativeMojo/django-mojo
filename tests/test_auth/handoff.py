"""
Tests for the cross-origin auth handoff (authorization-code style) flow.

Service layer: create_handoff_code / consume_handoff_code (Redis-backed,
single-use, TTL-bounded).

REST surface:
  POST /api/auth/handoff   — authenticated, mints a code
  POST /api/auth/exchange  — public, swaps code for JWT, single-use, rate-limited
"""
from testit import helpers as th
from testit.helpers import assert_true, assert_eq

TEST_USER = "handoff_user"
TEST_PWORD = "handoff##mojo99"


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
    code = auth_handoff.create_handoff_code(user, ip="127.0.0.1")
    assert_true(isinstance(code, str) and len(code) == 32, f"code should be 32-hex, got {code!r}")

    data = auth_handoff.consume_handoff_code(code)
    assert_true(data is not None, "consume should succeed for a fresh code")
    assert_eq(data["uid"], user.pk, "stored uid should match user")
    assert_eq(data["ip"], "127.0.0.1", "stored ip should match issuing ip")


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

@th.unit_test("auth/handoff requires authentication")
def test_handoff_endpoint_requires_auth(opts):
    opts.client.logout()
    resp = opts.client.post("/api/auth/handoff", {})
    assert_true(resp.status_code in (401, 403),
                f"unauthenticated handoff should be rejected, got {resp.status_code}")


@th.unit_test("auth/handoff returns code + expires_in for authed user")
def test_handoff_endpoint_returns_code(opts):
    assert_true(opts.client.login(TEST_USER, TEST_PWORD), "login should succeed")
    resp = opts.client.post("/api/auth/handoff", {})
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
    resp = opts.client.post("/api/auth/handoff", {})
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
    code = opts.client.post("/api/auth/handoff", {}).response.data.code
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
    code = opts.client.post("/api/auth/handoff", {}).response.data.code
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
    code = opts.client.post("/api/auth/handoff", {}).response.data.code
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

    blocked = False
    for i in range(25):
        resp = opts.client.post("/api/auth/exchange", {"code": "ffffffffffffffffffffffffffffffff"})
        if resp.status_code == 429:
            blocked = True
            break
    assert_true(blocked, "rate limit must trigger before 25 attempts within the window")

    # Reset so subsequent tests in this module aren't affected.
    clear_rate_limits(ip="127.0.0.1", key="auth_exchange")
