# Recording Metrics — Django Developer Reference

## Import

```python
from mojo.apps import metrics
```

## record()

```python
metrics.record(
    slug,                       # str — metric name (e.g., "page_views")
    when=None,                  # datetime — defaults to now
    count=1,                    # int — increment amount
    category=None,              # str — group slugs by category
    account="global",           # str — namespace/tenant
    min_granularity="hours",    # finest time bucket to record
    max_granularity="years",    # coarsest time bucket to record
    timezone=None,              # str — e.g., "America/Los_Angeles"
    expires_at=None,            # int unix ts — override key expiry time
    disable_expiry=False        # bool — keep generated keys without TTL
)
```

## Basic Usage

```python
# Increment "page_views" by 1 now
metrics.record("page_views")

# Increment by a specific amount
metrics.record("bytes_processed", count=1024)

# Record at a specific time
metrics.record("user_signups", when=some_datetime, count=1)
```

## Granularity

Each `record()` call increments multiple time buckets simultaneously, bounded by `min_granularity` and `max_granularity`:

```python
# Track only at hours and days (not minutes or years)
metrics.record("api_calls", min_granularity="hours", max_granularity="days")

# Track at fine granularity for real-time monitoring
metrics.record("active_connections", min_granularity="minutes", max_granularity="hours")
```

Available granularities: `"minutes"`, `"hours"`, `"days"`, `"weeks"`, `"months"`, `"years"`

## Categories

Group related slugs under a category for batch fetching:

```python
metrics.record("user_login", category="auth")
metrics.record("user_logout", category="auth")
metrics.record("user_signup", category="auth")
```

## Multi-Tenant Accounts

Use `account` to namespace metrics by tenant/group:

```python
metrics.record("page_views", account="group-123")
metrics.record("page_views", account="group-456")
```

User-level namespaces follow the same pattern:

```python
metrics.record("sl:click:ABC123", account="user-42", category="shortlinks")
```

## Expiry Controls

By default, retention is based on granularity settings. You can override:

```python
# Force all recorded buckets for this call to expire at a specific unix timestamp
metrics.record("temp_metric", expires_at=1735689600)

# Disable expiry for this call (keys persist until manually deleted)
metrics.record("permanent_metric", disable_expiry=True)
```

## Simple Key/Value (Non-Time-Series)

```python
# Store a simple value (not time-series)
metrics.set_value("maintenance_mode", "off", account="public")
mode = metrics.get_value("maintenance_mode", account="public", default="off")
```

## Where to Record

Record metrics close to the event:

```python
# In a model's on_rest_created
def on_rest_created(self):
    metrics.record("orders_created", category="orders")

# In a service
def process_payment(order):
    result = stripe.charge(...)
    if result.success:
        metrics.record("payments_succeeded", count=order.total_cents)
    else:
        metrics.record("payments_failed")
```

## Account Permissions

```python
# Allow unauthenticated REST reads for public metrics
metrics.set_view_perms("public", "public")

# Restrict write access
metrics.set_write_perms("group-123", "manage_metrics")
```



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
