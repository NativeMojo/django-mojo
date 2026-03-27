# Webhooks — Django Developer Reference

Send HTTP POST requests to external APIs as async jobs with automatic retries, exponential backoff, and monitoring.

## Quick Start

```python
from mojo.apps.jobs import publish_webhook

job_id = publish_webhook(
    url="https://api.partner.com/webhooks/order",
    data={"order_id": 99, "event": "created"},
)
```

The webhook is queued as a job and processed by a runner. If it fails, it retries automatically.

## Full Signature

```python
publish_webhook(
    url,                     # str — target URL (http:// or https://)
    data,                    # dict — JSON data to POST
    *,
    headers=None,            # dict — additional HTTP headers
    channel="webhooks",      # str — job channel
    delay=None,              # int seconds from now
    run_at=None,             # datetime — specific execution time
    timeout=30,              # int seconds — HTTP request timeout
    max_retries=None,        # int — default JOBS_WEBHOOK_MAX_RETRIES (5)
    backoff_base=None,       # float — default 2.0
    backoff_max=None,        # int — default 3600
    expires_in=None,         # int seconds
    expires_at=None,         # datetime
    idempotency_key=None,    # str — prevent duplicates
    webhook_id=None,         # str — custom identifier for tracking
)
```

**Returns**: Job ID string.

**Raises**: `ValueError` for invalid URL or non-serializable data.

Internally creates a job that calls `mojo.apps.jobs.handlers.webhook.post_webhook`.

## Parameter Reference

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `url` | str | *required* | Target URL (must start with `http://` or `https://`) |
| `data` | dict | *required* | JSON-serializable data to POST |
| `headers` | dict | `None` | Additional HTTP headers (sensitive values masked in logs) |
| `channel` | str | `"webhooks"` | Job channel — run a dedicated worker for webhooks |
| `delay` | int | `None` | Seconds before execution |
| `run_at` | datetime | `None` | Specific execution time |
| `timeout` | int | `30` | HTTP request timeout (max: `JOBS_WEBHOOK_MAX_TIMEOUT`, default 300) |
| `max_retries` | int | `5` | Max retry attempts (uses `JOBS_WEBHOOK_MAX_RETRIES`) |
| `backoff_base` | float | `2.0` | Exponential backoff multiplier |
| `backoff_max` | int | `3600` | Max seconds between retries |
| `expires_in` | int | `None` | Job expires after N seconds |
| `expires_at` | datetime | `None` | Specific expiration time |
| `idempotency_key` | str | `None` | Prevent duplicate webhook jobs |
| `webhook_id` | str | `None` | Custom ID stored in metadata for tracking |

## Retry Behavior

### What Gets Retried

| HTTP Status / Error | Retried? |
|---------------------|----------|
| Connection error | Yes |
| Timeout | Yes |
| 408 Request Timeout | Yes |
| 429 Too Many Requests | Yes |
| 502, 503, 504 | Yes |
| 520-524 (Cloudflare) | Yes |
| 400, 401, 403, 404 | No |
| Other 4xx | No |
| Invalid URL | No |

### Backoff Schedule

Retries use exponential backoff with jitter (0.8x–1.2x):

```
Attempt 1: immediate
Attempt 2: ~2s
Attempt 3: ~4s
Attempt 4: ~8s
Attempt 5: ~16s
```

The handler returns `"success"`, `"failed"`, or `"cancelled"`.

## Examples

### Basic Webhook

```python
publish_webhook(
    url="https://api.example.com/webhooks/user-signup",
    data={"user_id": 123, "email": "user@example.com", "event": "signup"},
)
```

### With Authentication

```python
publish_webhook(
    url="https://api.partner.com/webhooks/order",
    data={"order_id": 99, "event": "shipped"},
    headers={
        "Authorization": f"Bearer {settings.PARTNER_WEBHOOK_TOKEN}",
        "X-Webhook-Signature": generate_hmac(data),
    },
)
```

### Critical Webhook (More Retries)

```python
publish_webhook(
    url="https://fulfillment.example.com/webhooks/payment",
    data={"payment_id": 55, "status": "captured"},
    max_retries=10,
    backoff_base=1.5,
    backoff_max=7200,
    timeout=60,
    webhook_id=f"payment_{payment.id}",
)
```

### Scheduled Webhook

```python
from datetime import timedelta
from mojo.helpers import dates

publish_webhook(
    url="https://notifications.example.com/webhooks/reminder",
    data={"user_id": 42, "event": "trial_ending"},
    run_at=dates.subtract(trial_end_date, days=1),
    expires_in=86400,
    webhook_id=f"trial_reminder_{user.id}",
)
```

### Idempotent Webhook

```python
publish_webhook(
    url="https://analytics.example.com/events",
    data={"user_id": 42, "event": "signup"},
    idempotency_key=f"user_signup_{user.id}",
)
```

## Monitoring

### Check Status

```python
from mojo.apps.jobs import status

info = status(job_id)
# info["status"] — "pending", "running", "completed", "failed"
# info["attempt"] — current attempt number
# info["last_error"] — last error message if failed
# info["metadata"] — includes webhook-specific data
```

### Webhook Metadata

Completed webhook jobs store rich metadata:

```python
{
    "webhook_started_at": "2026-03-26T10:30:00Z",
    "webhook_completed_at": "2026-03-26T10:30:02Z",
    "url": "https://api.example.com/webhook",
    "webhook_id": "order_99",
    "attempt": 1,
    "timeout_seconds": 30,
    "response_status_code": 200,
    "response_headers": {"content-type": "application/json"},
    "response_size_bytes": 156,
    "duration_ms": 1245,
    "headers_sent": {"authorization": "Bear...xxx", "content-type": "application/json"},
}
```

### Metrics

The webhook handler emits these metrics:

| Metric | Description |
|--------|-------------|
| `webhooks.success` | Successful deliveries |
| `webhooks.timeout` | Timeouts |
| `webhooks.connection_error` | Network failures |
| `webhooks.error_client` | HTTP 4xx errors |
| `webhooks.error_retriable` | HTTP 5xx errors (will retry) |
| `webhooks.duration_ms` | Request duration |
| `webhooks.host.{hostname}.{outcome}` | Per-host metrics |

## Configuration

```python
# settings.py
JOBS_WEBHOOK_MAX_RETRIES = 5           # Default max retries for webhooks
JOBS_WEBHOOK_DEFAULT_TIMEOUT = 30      # Default HTTP timeout (seconds)
JOBS_WEBHOOK_MAX_TIMEOUT = 300         # Maximum allowed timeout (seconds)
JOBS_WEBHOOK_USER_AGENT = "Django-MOJO-Webhook/1.0"

# Include webhooks channel
JOBS_CHANNELS = ['default', 'webhooks']
```

Run a dedicated worker for webhooks:

```bash
python manage.py jobs_engine --channels webhooks --max-workers 20
```

## Security

- Sensitive headers (`Authorization`, `X-API-Key`, etc.) are automatically masked in logs and metadata
- Always use HTTPS URLs in production
- Implement signature verification on the receiving end
- Use short-lived tokens in Authorization headers
