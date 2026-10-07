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


def _synthetic_now():
    """A bucket-aligned instant ~3 years back, random per call, so in-process
    accounting keys never collide with live traffic or another test's keys."""
    base = (int(time.time()) - 94_608_000) // 300 * 300
    return base - (int(_uuid.uuid4().int % 10_000) * 300) + 1


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
def test_exempt_requests_do_not_spend_identity_budget(opts):
    """#6601: an exempt request is neither refused nor counted against the
    identity. A burst of exempt calls past the limit must leave the identity's
    next ordinary request served; ordinary requests still count and still get
    a 429 past the limit."""
    from mojo.decorators.limits import clear_rate_limits
    from mojo.helpers.redis import get_connection

    ok = opts.client.login(opts.email, opts.password)
    assert ok, f"login failed for throttle user: {opts.client.last_response.body}"
    clear_rate_limits(user_id=opts.user.pk)
    # The exempt burst and the ordinary requests must share one window, or the
    # ordinary ones would pass on a fresh budget whatever exempt calls cost.
    _wait_for_window_headroom(60, 15)

    # /api/user/me and /api/account/user/me are the same view; only the first
    # is exempt, so the ordinary request differs from the exempt one by path.
    headers = _throttle_header(
        enabled=True, user_limit=3, window=60,
        exempt_prefixes=["GET:/api/user/me"],
    )
    r = get_connection()
    member = f"user:{opts.user.pk}"
    bucket = int(time.time()) // 300 * 300
    top_before = float(r.zscore(f"traffic:top:{bucket}", member) or 0)
    for i in range(6):
        resp = opts.client.get("/api/user/me", headers=headers)
        assert resp.status_code == 200, (
            f"exempt request {i + 1}/6 (limit 3) must be served, got "
            f"{resp.status_code}: {resp.response}"
        )

    window_start = int(time.time()) // 60 * 60
    counted = sum(
        int(r.get(f"rl:api:user:{opts.user.pk}:{ws}") or 0)
        for ws in (window_start, window_start - 60)
    )
    assert counted == 0, (
        f"exempt requests must not be counted against the identity, got {counted} hits"
    )
    top_after = sum(
        float(r.zscore(f"traffic:top:{b}", member) or 0)
        for b in (bucket, bucket + 300)
    )
    assert top_after >= top_before + 6, (
        "exempt requests must still reach the top-talker set; "
        f"before={top_before}, after={top_after}"
    )

    for i in range(3):
        resp = opts.client.get("/api/account/user/me", headers=headers)
        assert resp.status_code == 200, (
            f"ordinary request {i + 1}/3 after 6 exempt ones must be served "
            f"(limit 3), got {resp.status_code}: {resp.response}"
        )
    resp = opts.client.get("/api/account/user/me", headers=headers)
    assert resp.status_code == 429, (
        "ordinary requests still count: the 4th must be throttled (limit 3), "
        f"got {resp.status_code}"
    )
    resp = opts.client.get("/api/user/me", headers=headers)
    assert resp.status_code == 200, (
        "an exempt request is never refused, even with the identity over "
        f"budget; got {resp.status_code}"
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
    r = get_connection()
    bucket = int(time.time()) // 300 * 300
    top_key = f"traffic:top:{bucket}"
    before = float(r.zscore(top_key, f"user:{opts.user.pk}") or 0)
    for i in range(4):
        resp = opts.client.get("/api/user/me", headers=headers)
        assert resp.status_code == 200, (
            f"enforcement is off — request {i + 1} must pass, got {resp.status_code}"
        )

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
    after = float(r.zscore(top_key, f"user:{opts.user.pk}") or 0)
    assert after >= before + 4, (
        "top-talker accounting must update on every allowed request, including "
        f"when enforcement is off; before={before}, after={after}"
    )
    clear_rate_limits(user_id=opts.user.pk)


@th.django_unit_test()
def test_exempt_requests_stay_in_traffic_accounting(opts):
    """#6601: only the identity's enforcement counter skips an exempt request.
    The bucket total and both top-talker sets still count it, so traffic views
    and the concentration detector see exempt traffic; the identity's next
    ordinary request is counted from one."""
    from mojo.decorators import limits
    from mojo.helpers.redis import get_connection

    marker = 870_000_000 + int(_uuid.uuid4().int % 5_000_000)

    class _FakeUser:
        pk = marker
        is_authenticated = True

        def is_request_user(self):
            return True

    class _FakeRequest:
        api_key = None
        user = _FakeUser()
        method = "GET"
        ip = "198.51.100.61"
        headers = {}
        META = {"REMOTE_ADDR": ip}

        def __init__(self, path):
            self.path = path

    config = {
        "enabled": True,
        "user_limit": 2,
        "apikey_limit": 0,
        "apikey_observe_limit": 600,
        "window": 60,
        "exempt_prefixes": ["GET:/api/exempt-accounting"],
        "report_floor": 60,
        "config_ttl": 30,
    }
    now = _synthetic_now()
    bucket = now // limits.TRAFFIC_BUCKET_SECONDS * limits.TRAFFIC_BUCKET_SECONDS
    member = f"user:{marker}"
    ident_key = f"rl:api:user:{marker}:{now // 60 * 60}"
    total_key = f"traffic:total:{bucket}"
    top_key = f"traffic:top:{bucket}"
    top_ip_key = f"traffic:top_ip:{bucket}"
    r = get_connection()
    r.delete(ident_key, total_key, top_key, top_ip_key)
    try:
        for i in range(5):
            blocked = limits.check_api_throttle(
                _FakeRequest("/api/exempt-accounting/beat"), now=now,
                config=config)
            assert blocked is None, (
                f"exempt request {i + 1}/5 (limit 2) must never be refused, got {blocked!r}"
            )
        assert not r.exists(ident_key), (
            "exempt requests must not create or increment the identity counter, "
            f"got {r.get(ident_key)!r}"
        )
        total = int(r.get(total_key) or 0)
        assert total == 5, f"exempt requests must count in the bucket total, got {total}"
        score = float(r.zscore(top_key, member) or 0)
        assert score == 5, f"exempt requests must count in the top-talker set, got {score}"
        ip_score = float(r.zscore(top_ip_key, "ip:198.51.100.61") or 0)
        assert ip_score == 5, f"exempt requests must count in the top-IP set, got {ip_score}"

        blocked = limits.check_api_throttle(
            _FakeRequest("/api/ordinary-accounting"), now=now, config=config)
        assert blocked is None, (
            "5 exempt requests past a limit of 2 must leave the next ordinary "
            f"request served, got {blocked!r}"
        )
        counted = int(r.get(ident_key) or 0)
        assert counted == 1, (
            "an ordinary request is counted against the identity and the exempt "
            f"ones before it were not: expected 1, got {counted}"
        )
        assert r.ttl(ident_key) > 0, "the identity counter must keep its expiry"
        total = int(r.get(total_key) or 0)
        assert total == 6, f"the bucket total must count every request, got {total}"
        score = float(r.zscore(top_key, member) or 0)
        assert score == 6, f"the top-talker set must count every request, got {score}"
    finally:
        r.delete(ident_key, total_key, top_key, top_ip_key)


@th.django_unit_test()
def test_exempt_apikey_requests_skip_observation_threshold(opts):
    """#6601: an exempt ApiKey request neither counts toward nor triggers the
    non-blocking observation threshold. Ordinary requests keep their behaviour:
    one bounded Event at threshold + 1, traffic never refused."""
    from mojo.apps.incident.models import Event
    from mojo.apps.incident.reporter import notice_key
    from mojo.decorators import limits
    from mojo.helpers.redis import get_connection

    marker = 875_000_000 + int(_uuid.uuid4().int % 5_000_000)

    class _FakeApiKey:
        pk = marker
        group = None
        limits = {}

    class _Anonymous:
        is_authenticated = False

    class _FakeRequest:
        api_key = _FakeApiKey()
        user = _Anonymous()
        group = None
        method = "GET"
        ip = "198.51.100.62"
        bearer = None
        headers = {}
        META = {"REMOTE_ADDR": ip}

        def __init__(self, path):
            self.path = path

    config = {
        "enabled": True,
        "user_limit": 240,
        "apikey_limit": 0,
        "apikey_observe_limit": 3,
        "window": 60,
        "exempt_prefixes": ["/api/exempt-observe"],
        "report_floor": 60,
        "config_ttl": 30,
    }
    now = _synthetic_now()
    bucket = now // limits.TRAFFIC_BUCKET_SECONDS * limits.TRAFFIC_BUCKET_SECONDS
    ident_key = f"rl:api:apikey:{marker}:{now // 60 * 60}"
    traffic_keys = (
        f"traffic:total:{bucket}", f"traffic:top:{bucket}",
        f"traffic:top_ip:{bucket}",
    )
    events = Event.objects.filter(
        category="traffic:apikey_threshold", model_id=marker)
    r = get_connection()

    def _cleanup():
        limits.clear_rate_limits(apikey_id=marker)
        r.delete(*traffic_keys)
        r.delete(notice_key("traffic:apikey_threshold", f"{marker}:global"))
        events.delete()

    _cleanup()
    try:
        for i in range(10):
            blocked = limits.check_api_throttle(
                _FakeRequest("/api/exempt-observe/beat"), now=now, config=config)
            assert blocked is None, (
                f"exempt ApiKey request {i + 1}/10 must never be refused, got {blocked!r}"
            )
        assert events.count() == 0, (
            "exempt requests past an observation threshold of 3 must not file "
            f"a threshold Event, got {events.count()}"
        )
        assert not r.exists(ident_key), (
            "exempt ApiKey requests must not move the identity counter, "
            f"got {r.get(ident_key)!r}"
        )
        score = float(r.zscore(f"traffic:top:{bucket}", f"apikey:{marker}") or 0)
        assert score == 10, (
            f"exempt ApiKey requests must still reach the top-talker set, got {score}"
        )

        for i in range(3):
            blocked = limits.check_api_throttle(
                _FakeRequest("/api/ordinary-observe"), now=now, config=config)
            assert blocked is None, (
                f"ordinary ApiKey request {i + 1}/3 must be allowed, got {blocked!r}"
            )
        assert events.count() == 0, (
            "3 ordinary requests reach, but do not cross, the threshold of 3; "
            f"got {events.count()} Events"
        )

        blocked = limits.check_api_throttle(
            _FakeRequest("/api/ordinary-observe"), now=now, config=config)
        assert blocked is None, (
            f"the observation threshold never refuses traffic, got {blocked!r}"
        )
        assert events.count() == 1, (
            "the 4th ordinary request crosses the threshold of 3 and must file "
            f"exactly one Event, got {events.count()}"
        )
        event = events.first()
        assert event.metadata.get("count") == 4, (
            "the Event counts ordinary requests only — the 10 exempt ones never "
            f"reached the identity counter; metadata={event.metadata!r}"
        )
        assert event.metadata.get("threshold") == 3, (
            f"the Event must name the configured threshold, metadata={event.metadata!r}"
        )
        assert event.metadata.get("source") == "global", (
            f"a key with no explicit limit reports source 'global', metadata={event.metadata!r}"
        )
    finally:
        _cleanup()


@th.django_unit_test()
def test_apikey_limits_override(opts):
    """A per-key ApiKey.limits['api'] override beats the global apikey default."""
    from mojo.apps.account.models import Group, ApiKey
    from mojo.apps.incident.models import Event
    from mojo.decorators.limits import clear_rate_limits

    group_name = f"dm042_throttle_{_uuid.uuid4().hex[:8]}"
    group = Group.objects.create(name=group_name, kind="organization")
    api_key, raw_token = ApiKey.create_for_group(
        group, "DM-042 throttle test",
        permissions={},
        limits={"api": {"limit": 2, "window": 1}},  # 2 requests / 1 minute
    )
    clear_rate_limits(apikey_id=api_key.pk)
    Event.objects.filter(category="rate_limit:api", model_id=api_key.pk).delete()

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
        retry_after = {
            key.lower(): value
            for key, value in opts.client.last_response.headers.items()
        }.get("retry-after")
        assert retry_after and int(retry_after) >= 1
        event = Event.objects.get(
            category="rate_limit:api", model_id=api_key.pk)
        assert event.model_name == "traffic:apikey"
        assert event.metadata.get("identity") == f"apikey:{api_key.pk}"
    finally:
        opts.client.logout()
        opts.client.bearer = "bearer"
        clear_rate_limits(apikey_id=api_key.pk)
        Event.objects.filter(category="rate_limit:api", model_id=api_key.pk).delete()
        api_key.delete()
        group.delete()


@th.django_unit_test()
def test_apikey_builtin_default_is_unlimited(opts):
    """With no deployment or per-key limit, the built-in ApiKey posture must
    never return 429. This exercises the real default values without relying
    on the test profile's enforcement override."""
    from mojo.decorators import limits
    from mojo.apps.incident.models import Event
    from mojo.helpers.redis import get_connection

    marker = 880_000_000 + int(_uuid.uuid4().int % 10_000_000)

    class _FakeGroup:
        pk = marker + 1

    class _FakeApiKey:
        pk = marker
        group = _FakeGroup()
        limits = {}

    class _FakeRequest:
        api_key = _FakeApiKey()
        class _Anonymous:
            is_authenticated = False
        user = _Anonymous()
        group = _FakeGroup()
        path = "/api/default-unlimited"
        method = "GET"
        ip = "198.51.100.44"
        bearer = None
        headers = {}
        META = {"REMOTE_ADDR": ip}

    limits.clear_rate_limits(apikey_id=marker)
    Event.objects.filter(
        category="traffic:apikey_threshold", model_id=marker).delete()
    try:
        # Returning each call's fallback value reproduces a deployment with no
        # API_THROTTLE_* settings at all through the production config builder.
        config = limits._build_throttle_config(
            lambda name, default=None, **kwargs: default)

        blocked = None
        for _ in range(601):
            blocked = limits.check_api_throttle(_FakeRequest(), config=config)
        assert blocked is None, (
            "an ApiKey with no explicit limit must remain allowed after the "
            f"old built-in 600-request ceiling, got {blocked!r}"
        )
        events = Event.objects.filter(
            category="traffic:apikey_threshold", model_id=marker)
        assert events.count() == 1, (
            "the built-in API_THROTTLE_APIKEY_OBSERVE=600 threshold should "
            f"produce one bounded event, got {events.count()}"
        )
        event = events.first()
        assert event.metadata.get("source") == "global"
        assert event.metadata.get("threshold") == 600
        assert event.metadata.get("identity") == f"apikey:{marker}"
        assert "token" not in event.metadata and "bearer" not in event.metadata

        bucket = int(time.time()) // 300 * 300
        r = get_connection()
        score = sum(
            float(r.zscore(f"traffic:top:{b}", f"apikey:{marker}") or 0)
            for b in (bucket, bucket - 300)
        )
        assert score >= 601, (
            f"unlimited ApiKey traffic must still feed concentration data, got {score}"
        )
    finally:
        limits.clear_rate_limits(apikey_id=marker)
        Event.objects.filter(
            category="traffic:apikey_threshold", model_id=marker).delete()


@th.django_unit_test()
def test_apikey_positive_deployment_limit_remains_hard(opts):
    from mojo.apps.account.models import Group, ApiKey
    from mojo.decorators.limits import clear_rate_limits

    group_name = f"dm042_deployment_{_uuid.uuid4().hex[:8]}"
    group = Group.objects.create(name=group_name, kind="organization")
    api_key, raw_token = ApiKey.create_for_group(
        group, "DM-042 deployment limit", permissions={}, limits={})
    clear_rate_limits(apikey_id=api_key.pk)

    opts.client.logout()
    opts.client.bearer = "apikey"
    opts.client.access_token = raw_token
    opts.client.is_authenticated = True
    headers = _throttle_header(
        enabled=True, apikey_limit=2, apikey_observe_limit=600, window=60)
    try:
        for i in range(2):
            resp = opts.client.get("/api/user/me", headers=headers)
            assert resp.status_code != 429, (
                f"request {i + 1}/2 is inside the deployment limit, got 429"
            )
        resp = opts.client.get("/api/user/me", headers=headers)
        assert resp.status_code == 429, (
            f"positive API_THROTTLE_APIKEY must remain a hard limit, got {resp.status_code}"
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

    result = limits.check_api_throttle(
        _FakeRequest(), connection=_BrokenConnection())
    assert result is None, (
        f"check_api_throttle must fail open on Redis errors, got {result!r}"
    )
