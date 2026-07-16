import hashlib
import time
from functools import wraps
from mojo.helpers.redis import get_connection
from mojo.helpers.response import JsonResponse
from mojo.helpers.settings import settings
from mojo.helpers import logit
from mojo.apps import metrics

logger = logit.get_logger("error", "error.log")

__all__ = ["rate_limit", "strict_rate_limit", "endpoint_metrics", "clear_rate_limits",
           "check_account_attempt", "read_account_attempt", "check_api_throttle"]


def _hash_key(value):
    """Hash an arbitrary string to a fixed 16-char hex identifier for use in Redis keys."""
    return hashlib.sha256(value.encode()).hexdigest()[:16]


def _retry_after_fixed(window_start, window):
    return max(1, window_start + window - int(time.time()))


def _retry_after_sliding(window):
    return max(1, window)


def _block(key, request, retry_after, min_granularity):
    # Metric + incident event are gated to fire once per key+IP per minute
    # (SET NX). A retry storm that keeps hitting a limit must not turn every
    # rejected request into a synchronous Event INSERT + rule evaluation —
    # that makes a failed request cost MORE than a served one, the classic
    # self-amplifying failure loop. The 429 itself is always returned.
    try:
        r = get_connection()
        ip = getattr(request, "ip", None) or request.META.get("REMOTE_ADDR", "unknown")
        first_engage = r.set(f"rlb:{key}:{ip}", 1, nx=True, ex=60)
    except Exception:
        first_engage = False
    if first_engage:
        from mojo.apps import incident
        try:
            metrics.record(f"rate_limit:{key}", category="rate_limits", min_granularity=min_granularity)
            incident.report_event(
                f"Rate limit exceeded: {key}",
                category=f"rate_limit:{key}",
                scope="api",
                level=5,
                request=request,
            )
        except Exception:
            pass
    resp = JsonResponse({"error": "Rate limit exceeded", "code": 429, "status": False}, status=429)
    resp["Retry-After"] = str(retry_after)
    return resp


def _incr_fixed(r, redis_key, window):
    """
    Fixed-window counter. Increments and returns the count.
    Sets TTL on first write only (window * 2 as safety margin).
    """
    count = r.incr(redis_key)
    if count == 1:
        r.expire(redis_key, window * 2)
    return count


def _check_sliding(r, redis_key, window, limit):
    """
    Sliding-window counter using a Redis sorted set.
    Adds current timestamp, removes entries outside the window, returns current count.
    Returns (count, allowed).
    """
    now = time.time()
    cutoff = now - window
    p = r.pipeline(transaction=False)
    p.zremrangebyscore(redis_key, 0, cutoff)
    p.zadd(redis_key, {str(now): now})
    p.zcard(redis_key)
    p.expire(redis_key, window * 2)
    results = p.execute()
    count = results[2]
    return count, count <= limit


def _get_apikey_limits(request, key, default_limit, default_window):
    """
    Resolve the effective limit and window for an api_key check.

    Looks up request.api_key.limits[key] for per-group overrides.
    Falls back to decorator defaults if not present.

    Returns (group_pk, limit, window_seconds) or None if no api_key on request.
    Window in request.api_key.limits is in minutes; converted to seconds here.
    """
    api_key = getattr(request, "api_key", None)
    if not api_key and not request.group:
        if request.user and request.user.org:
            return request.user.org.pk, default_limit, default_window
        return None
    group = getattr(api_key, "group", None)
    if not group:
        return None
    group_pk = group.pk

    limit = default_limit
    window = default_window
    try:
        key_limits = getattr(api_key, "limits", None)
        if key_limits:
            override = key_limits.get(key)
            if override:
                limit = override.get("limit", default_limit)
                window = override.get("window", default_window // 60) * 60  # minutes → seconds
    except Exception:
        pass

    return group_pk, limit, window


def _get_dimension(request, dimension):
    """Resolve the tracking value for a given dimension from the request."""
    if dimension == "ip":
        return getattr(request, "ip", None) or request.META.get("REMOTE_ADDR")
    if dimension == "duid":
        return request.DATA.get("duid")
    if dimension == "muid":
        return getattr(request, "muid", None) or None
    if dimension == "api_key":
        api_key = getattr(request, "api_key", None)
        if api_key:
            return str(api_key.pk)
    if dimension == "user":
        user = getattr(request, "user", None)
        if user and getattr(user, "is_authenticated", False):
            return str(user.id)
    if dimension == "group":
        group = request.group
        if request.group is None and request.user:
            group = request.user.org
        return str(group.id) if group else None
    return None


def rate_limit(key, ip_limit, duid_limit=None, muid_limit=None, apikey_limit=None,
               ip_window=60, duid_window=60, muid_window=60, apikey_window=60,
               min_granularity="hours"):
    """
    Fixed-window rate limiting decorator.

    Suitable for general API throughput limits where a small burst across a
    window boundary is acceptable. For security-sensitive endpoints (login,
    password reset, MFA) use strict_rate_limit instead.

    api_key limits are resolved from request.api_key.limits[key] if present,
    falling back to apikey_limit / apikey_window. Window overrides in
    request.api_key.limits are in minutes.

    Usage:
        @md.POST("feed")
        @md.rate_limit("feed", ip_limit=60)

        @md.POST("assess")
        @md.rate_limit("assess", ip_limit=20, apikey_limit=100, apikey_window=3600)

    Args:
        key:             Rate limit bucket name (e.g. "assess", "feed")
        ip_limit:        Max requests per ip_window seconds per IP
        duid_limit:      Max requests per duid_window seconds per device UUID (optional)
        apikey_limit:    Default max requests per apikey_window per API key group (optional)
        ip_window:       Window in seconds for IP counter (default 60)
        duid_window:     Window in seconds for duid counter (default 60)
        apikey_window:   Default window in seconds for API key counter (default 60)
        min_granularity: Granularity passed to metrics.record() (default "hours")
    """
    def decorator(func):
        @wraps(func)
        def wrapper(request, *args, **kwargs):
            try:
                r = get_connection()
                now = int(time.time())

                # --- IP check ---
                ip = getattr(request, "ip", None) or request.META.get("REMOTE_ADDR", "unknown")
                ip_window_start = now // ip_window * ip_window
                ip_key = f"rl:{key}:ip:{ip}:{ip_window_start}"
                if _incr_fixed(r, ip_key, ip_window) > ip_limit:
                    return _block(key, request, _retry_after_fixed(ip_window_start, ip_window), min_granularity)

                # --- duid check (optional) ---
                if duid_limit is not None:
                    duid = request.DATA.get("duid")
                    if duid:
                        duid_window_start = now // duid_window * duid_window
                        duid_key = f"rl:{key}:duid:{duid}:{duid_window_start}"
                        if _incr_fixed(r, duid_key, duid_window) > duid_limit:
                            return _block(key, request, _retry_after_fixed(duid_window_start, duid_window), min_granularity)

                # --- muid check (optional) — server-set cookie, bypass-resistant ---
                if muid_limit is not None:
                    muid = getattr(request, "muid", None)
                    if muid:
                        muid_window_start = now // muid_window * muid_window
                        muid_key = f"rl:{key}:muid:{muid}:{muid_window_start}"
                        if _incr_fixed(r, muid_key, muid_window) > muid_limit:
                            return _block(key, request, _retry_after_fixed(muid_window_start, muid_window), min_granularity)

                # --- api_key check (optional) ---
                if apikey_limit is not None:
                    resolved = _get_apikey_limits(request, key, apikey_limit, apikey_window)
                    if resolved:
                        group_pk, ak_limit, ak_window = resolved
                        ak_window_start = now // ak_window * ak_window
                        ak_key = f"rl:{key}:apikey:{group_pk}:{ak_window_start}"
                        if _incr_fixed(r, ak_key, ak_window) > ak_limit:
                            return _block(key, request, _retry_after_fixed(ak_window_start, ak_window), min_granularity)

            except Exception as err:
                logger.error(f"rate_limit: Redis error for key '{key}': {err}")

            return func(request, *args, **kwargs)
        return wrapper
    return decorator


def strict_rate_limit(key, ip_limit, duid_limit=None, muid_limit=None, apikey_limit=None,
                      ip_window=60, duid_window=60, muid_window=60, apikey_window=60,
                      min_granularity="hours"):
    """
    Sliding-window rate limiting decorator.

    Counts hits within a true rolling window so bursts straddling window
    boundaries are correctly caught. Use this for security-sensitive endpoints:
    login, password reset, MFA, registration.

    api_key limits are resolved from request.api_key.limits[key] if present,
    falling back to apikey_limit / apikey_window. Window overrides in
    request.api_key.limits are in minutes.

    Uses a Redis sorted set per key (slightly more memory than fixed-window
    but the only correct approach for tight limits).

    Usage:
        @md.POST("login")
        @md.strict_rate_limit("login", ip_limit=10, duid_limit=5, duid_window=300)

        @md.POST("password/reset")
        @md.strict_rate_limit("password_reset", ip_limit=5, ip_window=300)

    Args:
        key:             Rate limit bucket name (e.g. "login", "password_reset")
        ip_limit:        Max requests per ip_window seconds per IP
        duid_limit:      Max requests per duid_window seconds per device UUID (optional)
        apikey_limit:    Default max requests per apikey_window per API key group (optional)
        ip_window:       Window in seconds for IP sliding window (default 60)
        duid_window:     Window in seconds for duid sliding window (default 60)
        apikey_window:   Default window in seconds for API key sliding window (default 60)
        min_granularity: Granularity passed to metrics.record() (default "hours")
    """
    def decorator(func):
        @wraps(func)
        def wrapper(request, *args, **kwargs):
            try:
                r = get_connection()

                # --- IP check ---
                ip = getattr(request, "ip", None) or request.META.get("REMOTE_ADDR", "unknown")
                ip_key = f"srl:{key}:ip:{ip}"
                _, allowed = _check_sliding(r, ip_key, ip_window, ip_limit)
                if not allowed:
                    return _block(key, request, _retry_after_sliding(ip_window), min_granularity)

                # --- duid check (optional) ---
                if duid_limit is not None:
                    duid = request.DATA.get("duid")
                    if duid:
                        duid_key = f"srl:{key}:duid:{duid}"
                        _, allowed = _check_sliding(r, duid_key, duid_window, duid_limit)
                        if not allowed:
                            return _block(key, request, _retry_after_sliding(duid_window), min_granularity)

                # --- muid check (optional) — server-set cookie, bypass-resistant ---
                if muid_limit is not None:
                    muid = getattr(request, "muid", None)
                    if muid:
                        muid_key = f"srl:{key}:muid:{muid}"
                        _, allowed = _check_sliding(r, muid_key, muid_window, muid_limit)
                        if not allowed:
                            return _block(key, request, _retry_after_sliding(muid_window), min_granularity)

                # --- api_key check (optional) ---
                if apikey_limit is not None:
                    resolved = _get_apikey_limits(request, key, apikey_limit, apikey_window)
                    if resolved:
                        group_pk, ak_limit, ak_window = resolved
                        ak_key = f"srl:{key}:apikey:{group_pk}"
                        _, allowed = _check_sliding(r, ak_key, ak_window, ak_limit)
                        if not allowed:
                            return _block(key, request, _retry_after_sliding(ak_window), min_granularity)

            except Exception as err:
                logger.error(f"strict_rate_limit: Redis error for key '{key}': {err}")

            return func(request, *args, **kwargs)
        return wrapper
    return decorator


def check_account_attempt(key, account_id, limit, window, request=None,
                          min_granularity="hours"):
    """
    Per-account sliding-window check for failed-attempt counters.

    Used by views that have already resolved a user (or other account-scoped
    identity) and want to throttle attempts against that specific account
    independent of IP/duid/muid. Mirrors the response shape of
    strict_rate_limit so 429s look identical to the decorator's.

    Fail-open on Redis errors — same contract as strict_rate_limit. A Redis
    outage must never lock everyone out of authentication.

    Args:
        key:         Rate limit bucket name (e.g. "login")
        account_id:  Resolved user/account identifier (e.g. user.pk)
        limit:       Max attempts per window
        window:      Sliding window in seconds
        request:     Request object — used for the 429 response and incident
                     reporting. Optional; if None, the helper still tracks
                     the count but cannot produce a block response.
        min_granularity: Granularity passed to metrics on block.

    Returns:
        (count, response) — count is current attempts in window;
        response is a 429 JsonResponse if blocked, else None.
    """
    try:
        r = get_connection()
        redis_key = f"srl:{key}:account:{account_id}"
        count, allowed = _check_sliding(r, redis_key, window, limit)
        if not allowed and request is not None:
            return count, _block(key, request, _retry_after_sliding(window), min_granularity)
        return count, None
    except Exception as err:
        logger.error(f"check_account_attempt: Redis error for key '{key}' account '{account_id}': {err}")
        return 0, None


def read_account_attempt(key, account_id, limit=None, window=None):
    """Read the current per-account sliding-window attempt count from Redis.

    Pure read — does not increment, does not clean up old entries. Used by
    support tooling that wants to know whether a user is currently throttled
    without affecting their counter.

    Args:
        key:        Rate limit bucket name (e.g. "login")
        account_id: Resolved user/account identifier
        limit:      Caller-known limit (used to compute retry_after when over)
        window:     Caller-known window in seconds (required for any meaningful read)

    Returns:
        dict with keys: count, limit, window, retry_after_seconds.
        retry_after_seconds is 0 when the caller is under the limit or when
        window is None. Fail-open on Redis errors — returns count=0.
    """
    result = {
        "count": 0,
        "limit": limit,
        "window": window,
        "retry_after_seconds": 0,
    }
    if window is None:
        return result
    try:
        r = get_connection()
        if not r:
            return result
        redis_key = f"srl:{key}:account:{account_id}"
        now = time.time()
        cutoff = now - window
        count = r.zcount(redis_key, cutoff, "+inf")
        result["count"] = count
        if limit is not None and count >= limit:
            oldest = r.zrangebyscore(redis_key, cutoff, "+inf", start=0, num=1, withscores=True)
            if oldest:
                _, oldest_score = oldest[0]
                retry_after = int(oldest_score + window - now) + 1
                result["retry_after_seconds"] = max(1, retry_after)
            else:
                result["retry_after_seconds"] = max(1, int(window))
    except Exception as err:
        logger.error(f"read_account_attempt: Redis error for key '{key}' account '{account_id}': {err}")
    return result


# ---------------------------------------------------------------------------
# Global per-identity API throttle (DM-042)
#
# Called by the URL dispatcher for EVERY @md.URL route, before group
# resolution and the view. Keyed by authenticated identity only (user pk or
# api key pk) — never by IP: anonymous traffic is covered by the per-endpoint
# decorators above, and IP-keyed global limits punish CGNAT bystanders.
#
# Hot-path cost: one pipelined Redis round-trip (4 commands). Once per
# identity per window (~1/min) an extra small round-trip flushes the previous
# window's exact count into the traffic:top accounting zset that the
# concentration detector (incident cron) reads. Fail-open on any Redis error.
# ---------------------------------------------------------------------------

TRAFFIC_BUCKET_SECONDS = 300   # accounting bucket the concentration detector reads
TRAFFIC_KEY_TTL = 3600         # keep accounting keys around long enough to inspect

_throttle_config_cache = None
_throttle_config_ts = 0.0


def _get_throttle_config():
    """Resolve throttle config, cached in-process for API_THROTTLE_CONFIG_TTL
    seconds. settings.get consults the DB Setting plane (Redis-backed) — fine
    once per TTL, never per request."""
    global _throttle_config_cache, _throttle_config_ts
    now = time.monotonic()
    cached = _throttle_config_cache
    if cached is not None and (now - _throttle_config_ts) < cached["config_ttl"]:
        return cached
    cfg = {
        "enabled": settings.get("API_THROTTLE_ENABLED", True, kind="bool"),
        "user_limit": settings.get("API_THROTTLE_USER", 240, kind="int"),
        "apikey_limit": settings.get("API_THROTTLE_APIKEY", 600, kind="int"),
        "window": settings.get("API_THROTTLE_WINDOW", 60, kind="int"),
        "exempt_prefixes": settings.get("API_THROTTLE_EXEMPT_PREFIXES", [], kind="list") or [],
        "report_floor": settings.get("API_THROTTLE_REPORT_FLOOR", 60, kind="int"),
        "config_ttl": settings.get("API_THROTTLE_CONFIG_TTL", 30, kind="int"),
    }
    _throttle_config_cache = cfg
    _throttle_config_ts = now
    return cfg


def _matches_prefix_rule(request, prefix):
    """Same "METHOD:/path" | "/path" rule shape as the LOGIT_*_PREFIX settings."""
    method = None
    path_prefix = prefix
    if ":" in prefix:
        parts = prefix.split(":", 1)
        if len(parts) == 2 and parts[0].upper() in ("GET", "POST", "PUT", "DELETE", "PATCH", "HEAD", "OPTIONS"):
            method, path_prefix = parts[0].upper(), parts[1]
    if not request.path.startswith(path_prefix):
        return False
    if method and request.method != method:
        return False
    return True


def _resolve_throttle_identity(request):
    """Return ("apikey"|"user", pk) for an authenticated identity, else (None, None).

    hasattr(user, "is_request_user") is the framework's canonical "real request
    User" test — ANONYMOUS_USER and bare ApiKey identities don't have it.
    """
    api_key = getattr(request, "api_key", None)
    if api_key is not None:
        return "apikey", api_key.pk
    user = getattr(request, "user", None)
    if user is not None and hasattr(user, "is_request_user") and getattr(user, "is_authenticated", False):
        return "user", user.pk
    return None, None


def _test_mode_throttle_config(request, cfg):
    """Per-request override via X-Mojo-Test-Api-Throttle (JSON dict), gated by
    the standard test-mode gate. Lets throttle tests run in parallel without
    server reloads or poisoning global config."""
    header = request.headers.get("X-Mojo-Test-Api-Throttle")
    if not header:
        return cfg
    from mojo.helpers import test_mode
    if not test_mode.is_test_request(request):
        return cfg
    import json as _json
    try:
        overrides = _json.loads(header)
    except Exception:
        return cfg
    if not isinstance(overrides, dict):
        return cfg
    merged = dict(cfg)
    for name in ("enabled", "user_limit", "apikey_limit", "window", "report_floor", "exempt_prefixes"):
        if name in overrides:
            merged[name] = overrides[name]
    return merged


def _throttle_block(request, kind, pk, limit, window_start, window):
    """Cheap static 429. Metric + incident event fire only when the block
    FIRST engages for this identity+window (SET NX) — never per rejected
    request, so a machine-rate client can't turn its own rejections into
    load."""
    retry_after = _retry_after_fixed(window_start, window)
    try:
        r = get_connection()
        if r.set(f"rl:api:blocked:{kind}:{pk}:{window_start}", 1, nx=True, ex=window * 2):
            from mojo.apps import incident
            metrics.record("rate_limit:api", category="rate_limits", min_granularity="hours")
            incident.report_event(
                f"API throttle engaged: {kind}:{pk} exceeded {limit}/{window}s",
                category="rate_limit:api",
                scope="api",
                level=5,
                request=request,
            )
    except Exception:
        pass
    resp = JsonResponse({"error": "Rate limit exceeded", "code": 429, "status": False}, status=429)
    resp["Retry-After"] = str(retry_after)
    return resp


def check_api_throttle(request):
    """Global per-identity throttle + traffic accounting for every dispatched
    REST request. Returns a 429 HttpResponse when the identity is over budget
    and enforcement is enabled, else None.

    - Anonymous requests: immediate None, zero Redis cost.
    - Accounting (identity counter + bucket total + top-talkers flush) always
      runs for authenticated identities, even when enforcement is disabled —
      the concentration detector must see traffic regardless of 429 posture.
    - Per-key override: request.api_key.limits["api"] = {"limit": N,
      "window": minutes} (same convention as the rate_limit decorators).
    - Fail-open: any Redis/config error logs and allows the request.
    """
    try:
        kind, pk = _resolve_throttle_identity(request)
        if kind is None:
            return None
        cfg = _get_throttle_config()
        cfg = _test_mode_throttle_config(request, cfg)
        window = int(cfg["window"]) or 60
        if kind == "apikey":
            limit = int(cfg["apikey_limit"])
            resolved = _get_apikey_limits(request, "api", limit, window)
            if resolved:
                _, limit, window = resolved
        else:
            limit = int(cfg["user_limit"])
        if limit <= 0:
            return None  # explicit unlimited for this identity class
        for prefix in cfg["exempt_prefixes"]:
            if _matches_prefix_rule(request, prefix):
                return None

        now = int(time.time())
        window_start = now // window * window
        bucket = now // TRAFFIC_BUCKET_SECONDS * TRAFFIC_BUCKET_SECONDS
        ident_key = f"rl:api:{kind}:{pk}:{window_start}"

        r = get_connection()
        p = r.pipeline(transaction=False)
        p.incr(ident_key)
        p.expire(ident_key, window * 2)
        p.incr(f"traffic:total:{bucket}")
        p.expire(f"traffic:total:{bucket}", TRAFFIC_KEY_TTL)
        count = p.execute()[0]

        if count == 1:
            # First request of a new window: flush the previous window's exact
            # count into the accounting zset (once per identity per window).
            prev_count = r.get(f"rl:api:{kind}:{pk}:{window_start - window}")
            if prev_count and int(prev_count) >= int(cfg["report_floor"]):
                prev_bucket = (window_start - window) // TRAFFIC_BUCKET_SECONDS * TRAFFIC_BUCKET_SECONDS
                top_key = f"traffic:top:{prev_bucket}"
                p2 = r.pipeline(transaction=False)
                p2.zincrby(top_key, int(prev_count), f"{kind}:{pk}")
                ip = getattr(request, "ip", None)
                if ip:
                    # Approximate IP attribution — the identity's current IP is
                    # credited with its previous window. Informational only.
                    p2.zincrby(top_key, int(prev_count), f"ip:{ip}")
                p2.expire(top_key, TRAFFIC_KEY_TTL)
                p2.execute()

        if cfg["enabled"] and count > limit:
            return _throttle_block(request, kind, pk, limit, window_start, window)
    except Exception as err:
        logger.error(f"check_api_throttle: fail-open: {err}")
    return None


def clear_rate_limits(ip=None, key=None, duid=None, muid=None, account_id=None,
                      user_id=None, apikey_id=None):
    """
    Clear rate limit counters from Redis.

    Args:
        ip:         Clear all srl keys for this IP (optionally scoped to key)
        key:        Limit bucket name (e.g. "login") — required when clearing by duid/muid/account_id
        duid:       Clear the duid counter for this device UUID (requires key)
        muid:       Clear the muid counter for this client cookie (requires key)
        account_id: Clear the per-account counter for this resolved user (requires key)
        user_id:    Clear the global API throttle counters (rl:api:user:*) for this user
        apikey_id:  Clear the global API throttle counters (rl:api:apikey:*) for this api key

    Examples:
        clear_rate_limits(ip="1.2.3.4")                       # clear all limits for an IP
        clear_rate_limits(ip="1.2.3.4", key="login")          # clear login limit for an IP
        clear_rate_limits(key="login", duid="abc123")         # clear login limit for a device
        clear_rate_limits(key="login", account_id=42)         # clear per-account login counter
        clear_rate_limits(user_id=42)                         # clear API throttle for a user
    """
    r = get_connection()
    if not r:
        return 0
    deleted = 0
    for kind, ident in (("user", user_id), ("apikey", apikey_id)):
        if ident is not None:
            for pattern in (f"rl:api:{kind}:{ident}:*", f"rl:api:blocked:{kind}:{ident}:*"):
                for k in r.scan_iter(pattern):
                    r.delete(k)
                    deleted += 1
    if ip:
        # Clear both strict (srl:) and fixed-window (rl:) rate limit keys
        srl_pattern = f"srl:{key}:ip:{ip}" if key else f"srl:*:ip:{ip}"
        rl_pattern = f"rl:{key}:ip:{ip}:*" if key else f"rl:*:ip:{ip}:*"
        for pattern in (srl_pattern, rl_pattern):
            for k in r.scan_iter(pattern):
                r.delete(k)
                deleted += 1
    if duid and key:
        r.delete(f"srl:{key}:duid:{duid}")
        r.delete(f"rl:{key}:duid:{duid}")
        deleted += 1
    if muid and key:
        r.delete(f"srl:{key}:muid:{muid}")
        deleted += 1
        # rl: muid keys are window-suffixed; pattern-scan to clear them all
        for k in r.scan_iter(f"rl:{key}:muid:{muid}:*"):
            r.delete(k)
            deleted += 1
    if account_id is not None and key:
        r.delete(f"srl:{key}:account:{account_id}")
        r.delete(f"rl:{key}:account:{account_id}")
        deleted += 1
    return deleted


def endpoint_metrics(slug, by=None, min_granularity="hours", category="endpoint_metrics"):
    """
    Decorator to record per-endpoint usage metrics.

    Disabled entirely (zero overhead) when API_METRICS setting is falsy.
    Records on every hit before the view runs.
    Always records a global count for slug, plus one record per resolved dimension.

    Usage:
        @md.endpoint_metrics("login_attempts", by=["ip", "duid"])
        @md.endpoint_metrics("assess_calls", by="api_key", min_granularity="days")
        @md.endpoint_metrics("report_views", by=["user", "group"])
        @md.endpoint_metrics("signup_total")  # global count only

    Args:
        slug:            Metric name (e.g. "login_attempts", "assess_calls")
        by:              String or list of dimensions: "ip", "duid", "api_key", "user", "group"
        min_granularity: Granularity passed to metrics.record() (default "hours")
        category:        Category passed to metrics.record() (default "endpoint_metrics")
    """
    def decorator(func):
        if not settings.get("API_METRICS", False):
            return func  # no-op passthrough — no wrapper overhead

        by_list = [by] if isinstance(by, str) else (list(by) if by else [])

        @wraps(func)
        def wrapper(request, *args, **kwargs):
            try:
                logit.info(f"Recording metric {slug}  {category}", by_list)
                metrics.record(slug, category=category, min_granularity=min_granularity)
                group = request.group
                if not group:
                    group = request.user.org if request.user and request.user.org else None
                account = f"group-{group.pk}" if group else "global"
                for dimension in by_list:
                    value = _get_dimension(request, dimension)
                    if value:
                        if dimension == "group":
                            dslug = slug
                        else:
                            dslug = f"{slug}:{dimension}:{value}"
                        metrics.record(
                            dslug,
                            category=f"{category}_{dimension}",
                            min_granularity=min_granularity,
                            account=account
                        )
            except Exception:
                logit.exception(f"Failed to record metric {slug}")
            return func(request, *args, **kwargs)
        return wrapper
    return decorator
