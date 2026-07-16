"""DM-042: global per-identity API throttle (check_api_throttle in the dispatcher).

Enforcement is OFF suite-wide (API_THROTTLE_ENABLED=False in the test profile);
these tests opt in per-request with the X-Mojo-Test-Api-Throttle header so the
module stays parallel-safe and never poisons other modules' traffic.
"""
import json
import time
import uuid as _uuid

from testit import helpers as th


def _throttle_header(**overrides):
    return {"X-Mojo-Test-Api-Throttle": json.dumps(overrides)}


def _wait_for_window_headroom(window, needed):
    """Sleep past the window boundary if fewer than `needed` seconds remain,
    so a burst of requests never straddles two fixed windows mid-test."""
    now = time.time()
    remaining = window - (now % window)
    if remaining < needed:
        time.sleep(remaining + 0.2)


@th.django_unit_setup()
def setup_throttle_user(opts):
    from mojo.apps.account.models import User
    from mojo.decorators.limits import clear_rate_limits

    email = f"dm042_throttle_{_uuid.uuid4().hex[:8]}@limits.test"
    password = "Dm042##limits"
    User.objects.filter(username=email).delete()
    user = User.objects.create_user(username=email, email=email, password=password)
    user.is_active = True
    user.is_email_verified = True
    user.requires_mfa = False
    user.save()
    opts.user = user
    opts.email = email
    opts.password = password
    clear_rate_limits(user_id=user.pk)


@th.django_unit_test()
def test_throttle_blocks_over_limit(opts):
    from mojo.decorators.limits import clear_rate_limits

    ok = opts.client.login(opts.email, opts.password)
    assert ok, f"login failed for throttle user: {opts.client.last_response.body}"
    clear_rate_limits(user_id=opts.user.pk)
    _wait_for_window_headroom(60, 10)

    headers = _throttle_header(enabled=True, user_limit=5, window=60)
    for i in range(5):
        resp = opts.client.get("/api/user/me", headers=headers)
        assert resp.status_code == 200, (
            f"request {i + 1}/5 should be under the limit, got {resp.status_code}: {resp.response}"
        )
    resp = opts.client.get("/api/user/me", headers=headers)
    assert resp.status_code == 429, (
        f"6th request should be throttled (limit 5), got {resp.status_code}: {resp.response}"
    )
    resp_headers = {k.lower(): v for k, v in opts.client.last_response.headers.items()}
    retry_after = resp_headers.get("retry-after")
    assert retry_after and int(retry_after) >= 1, (
        f"429 must carry a Retry-After header, got {retry_after!r} in {sorted(resp_headers)}"
    )
    clear_rate_limits(user_id=opts.user.pk)


@th.django_unit_test()
def test_throttle_skips_anonymous(opts):
    opts.client.logout()
    headers = _throttle_header(enabled=True, user_limit=1, window=60)
    for i in range(3):
        resp = opts.client.get("/api/user/me", headers=headers)
        assert resp.status_code != 429, (
            f"anonymous request {i + 1} must never hit the identity throttle, got 429"
        )
        assert resp.status_code in (401, 403), (
            f"anonymous /api/user/me should be an auth error, got {resp.status_code}"
        )


@th.django_unit_test()
def test_throttle_exempt_prefix(opts):
    from mojo.decorators.limits import clear_rate_limits

    ok = opts.client.login(opts.email, opts.password)
    assert ok, f"login failed for throttle user: {opts.client.last_response.body}"
    clear_rate_limits(user_id=opts.user.pk)

    headers = _throttle_header(
        enabled=True, user_limit=1, window=60,
        exempt_prefixes=["GET:/api/user/me"],
    )
    for i in range(3):
        resp = opts.client.get("/api/user/me", headers=headers)
        assert resp.status_code == 200, (
            f"exempt-prefixed request {i + 1} must bypass the throttle, got {resp.status_code}"
        )
    clear_rate_limits(user_id=opts.user.pk)


@th.django_unit_test()
def test_accounting_runs_with_enforcement_off(opts):
    """Detection must not depend on 429 posture: counters increment even when
    enabled=false."""
    from mojo.decorators.limits import clear_rate_limits
    from mojo.helpers.redis import get_connection

    ok = opts.client.login(opts.email, opts.password)
    assert ok, f"login failed for throttle user: {opts.client.last_response.body}"
    clear_rate_limits(user_id=opts.user.pk)
    _wait_for_window_headroom(60, 10)

    headers = _throttle_header(enabled=False, user_limit=2, window=60)
    for i in range(4):
        resp = opts.client.get("/api/user/me", headers=headers)
        assert resp.status_code == 200, (
            f"enforcement is off — request {i + 1} must pass, got {resp.status_code}"
        )

    r = get_connection()
    now = int(time.time())
    window_start = now // 60 * 60
    total = 0
    for ws in (window_start, window_start - 60):
        val = r.get(f"rl:api:user:{opts.user.pk}:{ws}")
        if val:
            total += int(val)
    assert total >= 4, (
        f"accounting counter should be >= 4 with enforcement off, got {total}"
    )
    clear_rate_limits(user_id=opts.user.pk)


@th.django_unit_test()
def test_apikey_limits_override(opts):
    """A per-key ApiKey.limits['api'] override beats the global apikey default."""
    from mojo.apps.account.models import Group, ApiKey
    from mojo.decorators.limits import clear_rate_limits

    group_name = f"dm042_throttle_{_uuid.uuid4().hex[:8]}"
    group = Group.objects.create(name=group_name, kind="organization")
    api_key, raw_token = ApiKey.create_for_group(
        group, "DM-042 throttle test",
        permissions={},
        limits={"api": {"limit": 2, "window": 1}},  # 2 requests / 1 minute
    )
    clear_rate_limits(apikey_id=api_key.pk)

    opts.client.logout()
    opts.client.bearer = "apikey"
    opts.client.access_token = raw_token
    opts.client.is_authenticated = True
    _wait_for_window_headroom(60, 10)

    headers = _throttle_header(enabled=True, apikey_limit=1000, window=60)
    try:
        for i in range(2):
            resp = opts.client.get("/api/user/me", headers=headers)
            assert resp.status_code != 429, (
                f"apikey request {i + 1}/2 is inside its per-key limit, got 429"
            )
        resp = opts.client.get("/api/user/me", headers=headers)
        assert resp.status_code == 429, (
            f"3rd apikey request must hit the per-key limit of 2, got {resp.status_code}"
        )
    finally:
        opts.client.logout()
        opts.client.bearer = "bearer"
        clear_rate_limits(apikey_id=api_key.pk)
        api_key.delete()
        group.delete()


@th.django_unit_test()
def test_fail_open_on_redis_error(opts):
    """A Redis outage must never block traffic — check_api_throttle returns None."""
    from mojo.decorators import limits

    class _BrokenConnection:
        def __getattr__(self, name):
            raise RuntimeError("redis down (simulated)")

    class _FakeUser:
        pk = 999999901
        is_authenticated = True
        def is_request_user(self):
            return True

    class _FakeRequest:
        api_key = None
        user = _FakeUser()
        path = "/api/user/me"
        method = "GET"
        ip = "127.0.0.1"
        headers = {}
        META = {}

    original = limits.get_connection
    limits.get_connection = lambda: _BrokenConnection()
    try:
        result = limits.check_api_throttle(_FakeRequest())
        assert result is None, (
            f"check_api_throttle must fail open on Redis errors, got {result!r}"
        )
    finally:
        limits.get_connection = original
