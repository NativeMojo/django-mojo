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
        api_key = getattr(request, "api_key", None)
        engage_identity = f"apikey:{api_key.pk}" if api_key is not None else f"ip:{ip}"
        first_engage = r.set(f"rlb:{key}:{engage_identity}", 1, nx=True, ex=60)
    except Exception:
        first_engage = False
    if first_engage:
        from mojo.apps import incident
        try:
            metrics.record(f"rate_limit:{key}", category="rate_limits", min_granularity=min_granularity)
            event_kwargs = {}
            if api_key is not None:
                event_kwargs.update({
                    "model_name": "traffic:apikey",
                    "model_id": api_key.pk,
                    "identity": f"apikey:{api_key.pk}",
                })
            incident.report_event(
                f"Rate limit exceeded: {key}",
                category=f"rate_limit:{key}",
                scope="api",
                level=5,
                request=request,
                **event_kwargs,
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


def _log_invalid_apikey_limit(api_key, key, reason):
    """Log malformed per-key configuration at most once per key per hour."""
    try:
        r = get_connection()
        marker = f"rl:invalid:apikey:{api_key.pk}:{_hash_key(str(key))}"
        if r.set(marker, 1, nx=True, ex=3600):
            logger.error(
                f"invalid ApiKey limit for api_key={api_key.pk} key={key!r}: {reason}"
            )
    except Exception:
        pass


def _positive_int(value):
    if isinstance(value, bool):
        return None
    if isinstance(value, float) and not value.is_integer():
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _get_apikey_limits(request, key, default_limit=None, default_window=60):
    """
    Resolve the effective limit and window for an api_key check.

    Looks up request.api_key.limits[key] for a per-key hard limit. Falls back
    to a positive developer/deployment default when no entry is present.

    Returns (api_key_pk, limit_or_none, window_or_none, explicit) or None when
    the request is not ApiKey-authenticated. Per-key windows are minutes and
    are converted to seconds. Malformed/non-positive explicit entries fail
    open rather than becoming an accidental denial-of-service control.
    """
    api_key = getattr(request, "api_key", None)
    if api_key is None:
        return None

    try:
        all_limits = getattr(api_key, "limits", None) or {}
        if not isinstance(all_limits, dict):
            _log_invalid_apikey_limit(api_key, key, "limits must be an object")
            return api_key.pk, None, None, True
        if key in all_limits:
            override = all_limits.get(key)
            if not isinstance(override, dict):
                _log_invalid_apikey_limit(api_key, key, "entry must be an object")
                return api_key.pk, None, None, True
            limit = _positive_int(override.get("limit"))
            if limit is None:
                _log_invalid_apikey_limit(api_key, key, "limit must be a positive integer")
                return api_key.pk, None, None, True
            if "window" in override:
                window_minutes = _positive_int(override.get("window"))
                if window_minutes is None:
                    _log_invalid_apikey_limit(api_key, key, "window must be positive minutes")
                    return api_key.pk, None, None, True
                window = window_minutes * 60
            else:
                window = _positive_int(default_window) or 60
            return api_key.pk, limit, window, True
    except Exception as err:
        _log_invalid_apikey_limit(api_key, key, str(err))
        return api_key.pk, None, None, True

    limit = _positive_int(default_limit)
    window = _positive_int(default_window)
    if limit is None or window is None:
        return api_key.pk, None, None, False
    return api_key.pk, limit, window, False


def _resolve_apikey_observation(ip_limit, ip_window, duid_limit, duid_window,
                                muid_limit, muid_window, apikey_observe_limit,
                                apikey_window):
    """Return the ApiKey shadow threshold/window for an ordinary endpoint."""
    explicit = _positive_int(apikey_observe_limit)
    if explicit is not None:
        return explicit, (_positive_int(apikey_window) or 60)
    candidates = []
    for limit, window in (
        (ip_limit, ip_window),
        (duid_limit, duid_window),
        (muid_limit, muid_window),
    ):
        parsed_limit = _positive_int(limit)
        parsed_window = _positive_int(window)
        if parsed_limit is not None and parsed_window is not None:
            candidates.append((parsed_limit, parsed_window))
    return min(candidates, key=lambda item: item[0]) if candidates else (None, None)


def _report_apikey_threshold(request, redis, api_key_pk, source, count, threshold,
                             window, event_window=None, event_budget=None,
                             report=None):
    """Best-effort, bounded evidence for an allowed ApiKey threshold crossing."""
    category = "traffic:apikey_threshold"
    identity = f"apikey:{api_key_pk}"
    try:
        api_key = getattr(request, "api_key", None)
        group = getattr(api_key, "group", None)
        account = f"group-{group.pk}" if group is not None else "global"
        metrics.record(
            f"apikey_threshold:{source}:apikey:{api_key_pk}",
            category="traffic_apikey_threshold",
            account=account,
            min_granularity="hours",
        )
    except Exception:
        pass

    try:
        if event_window is None:
            event_window = settings.get(
                "API_THROTTLE_APIKEY_EVENT_WINDOW", 3600, kind="int")
        if event_budget is None:
            event_budget = settings.get(
                "API_THROTTLE_APIKEY_EVENT_BUDGET", 100, kind="int")
        event_window = _positive_int(event_window) or 3600
        event_budget = _positive_int(event_budget) or 100
        if report is None:
            from mojo.apps import incident
            report = incident.report_event_suppressed
        report(
            (
                f"ApiKey observation threshold crossed: {identity} made "
                f"{count} requests against {source} (threshold {threshold}/{window}s); "
                "traffic was allowed"
            ),
            key=f"{api_key_pk}:{source}",
            title=f"ApiKey traffic threshold: {source}",
            category=category,
            scope="api",
            level=5,
            request=request,
            window=event_window,
            budget=event_budget,
            fail_open=False,
            connection=redis,
            model_name="traffic:apikey",
            model_id=api_key_pk,
            identity=identity,
            source=source,
            count=count,
            threshold=threshold,
            threshold_window=window,
        )
    except Exception:
        pass


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
        # A key-backed session buckets on the KEY, not on the member it acts
        # as (ApiKey.override_user) — otherwise several keys linked to the same
        # member would share one budget, and a key's traffic would eat the
        # human's. The "api_key" dimension above is per-key by pk; this keeps
        # the "user" dimension consistent with it rather than merging the two.
        api_key = getattr(request, "api_key", None)
        if api_key is not None:
            return f"apikey-{api_key.pk}"
        user = getattr(request, "user", None)
        if user and getattr(user, "is_authenticated", False):
            return str(user.id)
    if dimension == "group":
        group = request.group
        if group is None:
            # Same reasoning: fall back to the key's OWN group before the
            # acting member's default org, which may be a different tenant.
            api_key = getattr(request, "api_key", None)
            if api_key is not None:
                group = getattr(api_key, "group", None)
            elif request.user:
                group = request.user.org
        return str(group.id) if group else None
    return None


def rate_limit(key, ip_limit, duid_limit=None, muid_limit=None, apikey_limit=None,
               ip_window=60, duid_window=60, muid_window=60, apikey_window=60,
               min_granularity="hours", apikey_observe_limit=None):
    """
    Fixed-window rate limiting decorator.

    Suitable for general API throughput limits where a small burst across a
    window boundary is acceptable. For security-sensitive endpoints (login,
    password reset, MFA) use strict_rate_limit instead.

    Ordinary ApiKey traffic skips the consumer IP/duid/muid gates. Positive
    per-key limits and apikey_limit remain hard; otherwise the consumer
    threshold is shadow-counted per ApiKey and crossing it records bounded
    evidence without blocking. Use strict_rate_limit for security boundaries.

    Usage:
        @md.POST("feed")
        @md.rate_limit("feed", ip_limit=60)

        @md.POST("assess")
        @md.rate_limit("assess", ip_limit=20, apikey_limit=100, apikey_window=3600)

    Args:
        key:             Rate limit bucket name (e.g. "assess", "feed")
        ip_limit:        Max requests per ip_window seconds per IP
        duid_limit:      Max requests per duid_window seconds per device UUID (optional)
        apikey_limit:    Hard max requests per apikey_window per ApiKey (optional)
        apikey_observe_limit: Non-blocking ApiKey threshold; defaults to the
                          lowest positive consumer limit (optional)
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

                api_key = getattr(request, "api_key", None)
                if api_key is not None:
                    resolved = _get_apikey_limits(
                        request, key, apikey_limit, apikey_window)
                    api_key_pk, ak_limit, ak_window, _ = resolved
                    if ak_limit is not None:
                        ak_window_start = now // ak_window * ak_window
                        ak_key = f"rl:{key}:apikey:{api_key_pk}:{ak_window_start}"
                        if _incr_fixed(r, ak_key, ak_window) > ak_limit:
                            return _block(key, request, _retry_after_fixed(ak_window_start, ak_window), min_granularity)
                    else:
                        observe_limit, observe_window = _resolve_apikey_observation(
                            ip_limit, ip_window, duid_limit, duid_window,
                            muid_limit, muid_window, apikey_observe_limit,
                            apikey_window,
                        )
                        if observe_limit is not None:
                            observe_start = now // observe_window * observe_window
                            observe_key = (
                                f"rl:{key}:observe:apikey:{api_key_pk}:{observe_start}")
                            count = _incr_fixed(r, observe_key, observe_window)
                            if count == observe_limit + 1:
                                _report_apikey_threshold(
                                    request, r, api_key_pk, f"endpoint:{key}",
                                    count, observe_limit, observe_window)
                else:
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

    ApiKey callers do not bypass this decorator's IP/duid/muid gates. Positive
    hard limits resolve from request.api_key.limits[key] when present, falling
    back to apikey_limit / apikey_window, and are keyed by the ApiKey pk.
    Per-key window overrides are in minutes.

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
        apikey_limit:    Hard max requests per apikey_window per ApiKey (optional)
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

                # --- api_key check (optional developer or explicit per-key hard limit) ---
                resolved = _get_apikey_limits(request, key, apikey_limit, apikey_window)
                if resolved:
                    api_key_pk, ak_limit, ak_window, _ = resolved
                    if ak_limit is not None:
                        ak_key = f"srl:{key}:apikey:{api_key_pk}"
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
# Hot-path cost: one pipelined Redis round-trip. Every request increments the
# current identity and traffic-accounting buckets so a burst that stops before
# the next identity window is still visible to the concentration detector.
# Fail-open on any Redis error.
# ---------------------------------------------------------------------------

TRAFFIC_BUCKET_SECONDS = 300   # accounting bucket the concentration detector reads
TRAFFIC_KEY_TTL = 3600         # keep accounting keys around long enough to inspect
TRAFFIC_IP_MEMBER_LIMIT = 1000  # informational attribution must have bounded cardinality

_throttle_config_cache = None
_throttle_config_ts = 0.0


def _build_throttle_config(get_setting):
    """Build config through an injectable setting reader for isolated tests."""
    return {
        "enabled": get_setting("API_THROTTLE_ENABLED", True, kind="bool"),
        "user_limit": get_setting("API_THROTTLE_USER", 240, kind="int"),
        # ApiKeys are unlimited by default. A positive deployment value keeps
        # the legacy global hard ceiling; positive per-key `limits["api"]`
        # values remain hard regardless of this default.
        "apikey_limit": get_setting("API_THROTTLE_APIKEY", 0, kind="int"),
        "apikey_observe_limit": get_setting(
            "API_THROTTLE_APIKEY_OBSERVE", 600, kind="int"),
        "window": get_setting("API_THROTTLE_WINDOW", 60, kind="int"),
        "exempt_prefixes": get_setting(
            "API_THROTTLE_EXEMPT_PREFIXES", [], kind="list") or [],
        "report_floor": get_setting(
            "API_THROTTLE_REPORT_FLOOR", 60, kind="int"),
        "config_ttl": get_setting(
            "API_THROTTLE_CONFIG_TTL", 30, kind="int"),
    }


def _get_throttle_config():
    """Resolve throttle config, cached in-process for API_THROTTLE_CONFIG_TTL
    seconds. settings.get consults the DB Setting plane (Redis-backed) — fine
    once per TTL, never per request."""
    global _throttle_config_cache, _throttle_config_ts
    now = time.monotonic()
    cached = _throttle_config_cache
    if cached is not None and (now - _throttle_config_ts) < cached["config_ttl"]:
        return cached
    cfg = _build_throttle_config(settings.get)
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
    for name in (
        "enabled", "user_limit", "apikey_limit", "apikey_observe_limit",
        "window", "report_floor", "exempt_prefixes",
    ):
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
            event_kwargs = {}
            if kind == "apikey":
                event_kwargs.update({
                    "model_name": "traffic:apikey",
                    "model_id": pk,
                    "identity": f"apikey:{pk}",
                })
            incident.report_event(
                f"API throttle engaged: {kind}:{pk} exceeded {limit}/{window}s",
                category="rate_limit:api",
                scope="api",
                level=5,
                request=request,
                **event_kwargs,
            )
    except Exception:
        pass
    resp = JsonResponse({"error": "Rate limit exceeded", "code": 429, "status": False}, status=429)
    resp["Retry-After"] = str(retry_after)
    return resp


def check_api_throttle(request, now=None, config=None, connection=None):
    """Global per-identity throttle + traffic accounting for every dispatched
    REST request. Returns a 429 HttpResponse when the identity is over budget
    and enforcement is enabled, else None.

    - Anonymous requests: immediate None, zero Redis cost.
    - Accounting (identity counter + bucket total + top talkers) always
      runs for authenticated identities, even when enforcement is disabled —
      the concentration detector must see traffic regardless of 429 posture.
    - Per-key hard override: request.api_key.limits["api"] = {"limit": N,
      "window": minutes} (same convention as the rate_limit decorators).
    - ApiKeys have no built-in hard ceiling; their default 600/window
      threshold records bounded review evidence while allowing traffic.
    - Fail-open: any Redis/config error logs and allows the request.
    """
    try:
        kind, pk = _resolve_throttle_identity(request)
        if kind is None:
            return None
        cfg = _get_throttle_config() if config is None else dict(config)
        cfg = _test_mode_throttle_config(request, cfg)
        window = _positive_int(cfg["window"]) or 60
        explicit = False
        if kind == "apikey":
            limit = _positive_int(cfg["apikey_limit"])
            resolved = _get_apikey_limits(request, "api", limit, window)
            if resolved:
                _, limit, resolved_window, explicit = resolved
                if resolved_window is not None:
                    window = resolved_window
        else:
            limit = _positive_int(cfg["user_limit"])

        exempt = any(
            _matches_prefix_rule(request, prefix)
            for prefix in cfg["exempt_prefixes"]
        )
        enforcement_active = bool(cfg["enabled"]) and not exempt and limit is not None

        now = int(time.time()) if now is None else int(now)
        window_start = now // window * window
        bucket = now // TRAFFIC_BUCKET_SECONDS * TRAFFIC_BUCKET_SECONDS
        ident_key = f"rl:api:{kind}:{pk}:{window_start}"
        top_key = f"traffic:top:{bucket}"
        top_ip_key = f"traffic:top_ip:{bucket}"

        r = get_connection() if connection is None else connection
        p = r.pipeline(transaction=False)
        p.incr(ident_key)
        p.expire(ident_key, window * 2)
        p.incr(f"traffic:total:{bucket}")
        p.expire(f"traffic:total:{bucket}", TRAFFIC_KEY_TTL)
        p.zincrby(top_key, 1, f"{kind}:{pk}")
        p.expire(top_key, TRAFFIC_KEY_TTL)
        ip = getattr(request, "ip", None)
        if ip:
            p.zincrby(top_ip_key, 1, f"ip:{ip}")
            # Keep only the highest-scoring source IPs. IP attribution is
            # informational; it must never let rotating sources grow Redis
            # without bound or crowd authenticated identities out of top-K.
            p.zremrangebyrank(top_ip_key, 0, -(TRAFFIC_IP_MEMBER_LIMIT + 1))
            p.expire(top_ip_key, TRAFFIC_KEY_TTL)
        count = p.execute()[0]

        if kind == "apikey" and not enforcement_active:
            observe_limit = _positive_int(cfg["apikey_observe_limit"])
            if observe_limit is not None and count == observe_limit + 1:
                source = "global:explicit-bypass" if explicit else "global"
                _report_apikey_threshold(
                    request, r, pk, source, count, observe_limit, window)

        if enforcement_active and count > limit:
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
    if apikey_id is not None:
        endpoint_patterns = (
            f"rl:*:apikey:{apikey_id}:*",
            f"rl:*:observe:apikey:{apikey_id}:*",
            f"srl:*:apikey:{apikey_id}",
            f"rl:invalid:apikey:{apikey_id}:*",
        )
        for pattern in endpoint_patterns:
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
                    # Prefer the key's own group over the acting member's org
                    # (ApiKey.override_user) so metrics land in the tenant the
                    # key belongs to, not the member's default one.
                    api_key = getattr(request, "api_key", None)
                    if api_key is not None:
                        group = getattr(api_key, "group", None)
                    elif request.user and request.user.org:
                        group = request.user.org
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
