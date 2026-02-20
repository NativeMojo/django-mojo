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
    timezone=None               # str — e.g., "America/Los_Angeles"
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
metrics.record("page_views", account="group_123")
metrics.record("page_views", account="group_456")
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
metrics.set_write_perms("group_123", "manage_metrics")
```
