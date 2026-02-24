# Rate Limiting & Endpoint Metrics — Django Developer Reference

Three decorators in `mojo.decorators` handle rate limiting and usage tracking:

| Decorator | Algorithm | Use for |
|---|---|---|
| `@md.rate_limit` | Fixed-window | General API throughput limits |
| `@md.strict_rate_limit` | Sliding-window | Security-sensitive endpoints |
| `@md.endpoint_metrics` | Metrics recording | Per-endpoint usage tracking |

All are available via the standard import:

```python
import mojo.decorators as md
```

---

## `@md.rate_limit` — Fixed-Window

Counts requests in fixed time buckets. Fast and cheap (one Redis INCR per check). The right choice for general throughput limits where a small burst across a window boundary doesn't matter.

```python
def rate_limit(key, ip_limit, duid_limit=None, apikey_limit=None,
               ip_window=60, duid_window=60, apikey_window=60,
               min_granularity="hours")
```

### Parameters

| Param | Description |
|---|---|
| `key` | Bucket name — must be unique per endpoint (e.g. `"assess"`, `"feed"`) |
| `ip_limit` | Max requests per `ip_window` seconds per IP |
| `duid_limit` | Max requests per `duid_window` seconds per device UUID (optional) |
| `apikey_limit` | Default max requests per `apikey_window` per API key group (optional) |
| `ip_window` | Window in seconds for IP counter (default `60`) |
| `duid_window` | Window in seconds for duid counter (default `60`) |
| `apikey_window` | Default window in seconds for API key counter (default `60`) |
| `min_granularity` | Granularity for violation metrics (default `"hours"`) |

### Examples

```python
# IP-only limit: 60 requests per minute
@md.POST("feed")
@md.rate_limit("feed", ip_limit=60)
def on_feed(request):
    ...

# IP + duid: 20/min IP, 10 per 5 min per device
@md.POST("search")
@md.rate_limit("search", ip_limit=20, duid_limit=10, duid_window=300)
def on_search(request):
    ...

# With API key limits: 60/min IP, 1000/hr per API key group
@md.POST("assess")
@md.rate_limit("assess", ip_limit=60, apikey_limit=1000, apikey_window=3600)
def on_assess(request):
    ...
```

### How it works

Each request increments a counter in Redis keyed by `rl:{key}:{dimension}:{id}:{window_start}`.

`window_start` is the current timestamp floored to the nearest `window` seconds — so all requests within the same bucket hit the same key. At the next boundary, a new key is created and the count starts from zero.

```
window = 60s, now = 14:32:47

window_start = 14:32:00   ← all requests from 14:32:00–14:32:59 share this key
window_start = 14:33:00   ← new key, count resets
```

---

## `@md.strict_rate_limit` — Sliding-Window

Counts requests within a true rolling window measured backwards from *now*. Correctly catches bursts that straddle window boundaries. Use this for any endpoint where the limit has a security meaning.

Same signature as `rate_limit`:

```python
def strict_rate_limit(key, ip_limit, duid_limit=None, apikey_limit=None,
                      ip_window=60, duid_window=60, apikey_window=60,
                      min_granularity="hours")
```

### Examples

```python
# Login: 10 attempts per minute per IP, 5 per 5 min per device
@md.POST("login")
@md.strict_rate_limit("login", ip_limit=10, duid_limit=5, duid_window=300)
def on_login(request):
    ...

# Password reset: 5 attempts per 5 minutes per IP
@md.POST("password/reset")
@md.strict_rate_limit("password_reset", ip_limit=5, ip_window=300)
def on_password_reset(request):
    ...

# Registration: 3 per hour per IP
@md.POST("register")
@md.strict_rate_limit("register", ip_limit=3, ip_window=3600)
def on_register(request):
    ...
```

### Fixed vs sliding — which to use?

With `limit=3, window=60s` and fixed-window, this sequence is allowed:

```
0:55  request 1  →  allow   (bucket 0:00–0:59, count=1)
0:58  request 2  →  allow   (bucket 0:00–0:59, count=2)
1:02  request 3  →  allow   (bucket 1:00–1:59, count=1  ← new bucket)
1:04  request 4  →  allow   (bucket 1:00–1:59, count=2)
```

4 requests in 9 seconds. With sliding-window, requests 1–3 fill the window and request 4 is blocked until request 1 is older than 60 seconds.

Use `strict_rate_limit` wherever the limit is meant as a security control.

---

## API Key Rate Limiting

When `request.api_key` is set by middleware, both decorators support per-group rate limit overrides. The api_key object is expected to have this shape:

```python
request.api_key = {
    "group": <account.Group instance>,
    "limits": {
        "assess": {
            "limit": 500,
            "window": 60   # minutes
        }
    }
}
```

The decorator looks up `request.api_key.limits[key]` to resolve the effective limit and window for that group. If no override is present, the decorator's `apikey_limit` / `apikey_window` defaults apply.

The Redis key uses `group.pk` so all API keys belonging to the same group share a single counter:

```
rl:assess:apikey:42:1234567920
```

If `request.api_key` is `None` (unauthenticated request), the api_key check is skipped — IP limiting still applies.

Window values in `request.api_key.limits` are in **minutes**. The decorator converts them to seconds internally.

---

## `@md.endpoint_metrics` — Usage Tracking

Records per-endpoint metrics to the time-series metrics system. **Disabled entirely (zero overhead) when `API_METRICS=False`.**

```python
def endpoint_metrics(slug, by=None, min_granularity="hours")
```

### Parameters

| Param | Description |
|---|---|
| `slug` | Explicit metric name (e.g. `"login_attempts"`, `"assess_calls"`) |
| `by` | String or list — dimensions to break down by (see below) |
| `min_granularity` | Granularity passed to `metrics.record()` (default `"hours"`) |

### Supported dimensions

| Value | Tracks by |
|---|---|
| `"ip"` | Source IP address |
| `"duid"` | Device UUID from `request.DATA.get("duid")` |
| `"api_key"` | API key group PK (`request.api_key.group.pk`) |
| `"user"` | Authenticated user ID |
| `"group"` | Request group ID (`request.group.pk`) |

### Examples

```python
# Global count only
@md.POST("signup")
@md.endpoint_metrics("signup_total")
def on_signup(request):
    ...

# Global + IP breakdown
@md.POST("search")
@md.endpoint_metrics("search_calls", by="ip")
def on_search(request):
    ...

# Global + multiple breakdowns
@md.POST("login")
@md.endpoint_metrics("login_attempts", by=["ip", "duid"])
def on_login(request):
    ...

# API key usage tracking, daily granularity
@md.POST("assess")
@md.endpoint_metrics("assess_calls", by="api_key", min_granularity="days")
def on_assess(request):
    ...
```

Each resolved dimension produces an additional metric slug:

```
login_attempts              ← always recorded (global)
login_attempts:ip:1.2.3.4   ← per IP
login_attempts:duid:abc123  ← per device
```

Dimensions that are absent on the request (no duid, unauthenticated user, no group, no api_key) are skipped silently.

---

## On Violation

When a limit is exceeded, all three rate limiting decorators:

1. Record a violation metric: `rate_limit:{key}` in category `rate_limits`
2. Report to the incident system: `category="rate_limit:{key}"`, `level=5`
3. Return 429 with `Retry-After` header — the view is never called

```json
{"error": "Rate limit exceeded", "code": 429, "status": false}
```

This means violations are automatically visible in both the metrics dashboard and the incident system, with no extra code.

---

## Decorator Stacking Order

Routing decorator outermost, then rate limiting, then metrics, then auth/validation:

```python
@md.POST("login")
@md.strict_rate_limit("login", ip_limit=10, duid_limit=5, duid_window=300)
@md.endpoint_metrics("login_attempts", by=["ip", "duid"])
@md.requires_params("username", "password")
def on_login(request):
    ...

@md.POST("assess")
@md.rate_limit("assess", ip_limit=60, apikey_limit=1000, apikey_window=3600)
@md.endpoint_metrics("assess_calls", by=["api_key", "ip"])
def on_assess(request):
    ...
```

Rate limiting before metrics ensures that blocked requests are still counted (you want to see the full traffic volume, including rejected requests).

---

## Settings

| Setting | Default | Description |
|---|---|---|
| `API_METRICS` | `False` | Must be `True` for `endpoint_metrics` to record anything |

Redis connection uses the standard `REDIS_*` settings — see the Redis helper docs.

---

## Fail-Open Behaviour

If Redis is unavailable, all rate limit checks are skipped and the request is allowed through. A Redis outage will not take down the API. The error is logged to `error.log`.
