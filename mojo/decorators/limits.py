import hashlib
import time
from functools import wraps
from mojo.helpers.redis import get_connection
from mojo.helpers.response import JsonResponse
from mojo.helpers.settings import settings
from mojo.helpers import logit
from mojo.apps import metrics

logger = logit.get_logger("error", "error.log")

__all__ = ["rate_limit", "strict_rate_limit", "endpoint_metrics", "clear_rate_limits"]


def _hash_key(value):
    """Hash an arbitrary string to a fixed 16-char hex identifier for use in Redis keys."""
    return hashlib.sha256(value.encode()).hexdigest()[:16]


def _retry_after_fixed(window_start, window):
    return max(1, window_start + window - int(time.time()))


def _retry_after_sliding(window):
    return max(1, window)


def _block(key, request, retry_after, min_granularity):
    from mojo.apps import incident
    metrics.record(f"rate_limit:{key}", category="rate_limits", min_granularity=min_granularity)
    try:
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


def rate_limit(key, ip_limit, duid_limit=None, apikey_limit=None,
               ip_window=60, duid_window=60, apikey_window=60,
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


def strict_rate_limit(key, ip_limit, duid_limit=None, apikey_limit=None,
                      ip_window=60, duid_window=60, apikey_window=60,
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


def clear_rate_limits(ip=None, key=None, duid=None):
    """
    Clear rate limit counters from Redis.

    Args:
        ip:   Clear all srl keys for this IP (optionally scoped to key)
        key:  Limit bucket name (e.g. "login") — required when clearing by duid
        duid: Clear the duid counter for this device UUID (requires key)

    Examples:
        clear_rate_limits(ip="1.2.3.4")           # clear all limits for an IP
        clear_rate_limits(ip="1.2.3.4", key="login")  # clear login limit for an IP
        clear_rate_limits(key="login", duid="abc123")  # clear login limit for a device
    """
    r = get_connection()
    if not r:
        return 0
    deleted = 0
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
