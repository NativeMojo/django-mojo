# Django-MOJO Jobs: Webhook Support

Django-MOJO Jobs provides robust webhook support for sending HTTP POST requests to external APIs with automatic retry logic, exponential backoff, and comprehensive error handling.

## Table of Contents

- [Quick Start](#quick-start)
- [Publishing Webhooks](#publishing-webhooks)
- [Configuration](#configuration)
- [Error Handling & Retries](#error-handling--retries)
- [Monitoring & Metrics](#monitoring--metrics)
- [Security Considerations](#security-considerations)
- [Examples](#examples)
- [Troubleshooting](#troubleshooting)

## Quick Start

```python
from mojo.apps.jobs import publish_webhook

# Send a simple webhook
job_id = publish_webhook(
    url="https://api.example.com/webhooks/user-signup",
    data={
        "user_id": 123,
        "email": "user@example.com", 
        "event": "signup",
        "timestamp": "2024-01-15T10:30:00Z"
    }
)

print(f"Webhook queued with job ID: {job_id}")
```

That's it! The webhook will be processed asynchronously with automatic retries if it fails.

## Publishing Webhooks

### Basic Usage

```python
from mojo.apps.jobs import publish_webhook

job_id = publish_webhook(
    url="https://api.example.com/webhooks",
    data={"key": "value"}
)
```

### Full Parameter Reference

```python
job_id = publish_webhook(
    url="https://api.example.com/webhooks",           # Required: Target URL
    data={"event": "user_signup", "user_id": 123},    # Required: JSON data to POST
    
    # Optional parameters
    headers={"Authorization": "Bearer token123"},      # HTTP headers
    channel="webhooks",                                # Job channel (default: "webhooks")
    delay=300,                                        # Delay in seconds (None = immediate)
    run_at=datetime(2024, 1, 15, 14, 30),            # Specific execution time
    timeout=30,                                       # Request timeout in seconds
    max_retries=5,                                    # Maximum retry attempts
    backoff_base=2.0,                                 # Exponential backoff base
    backoff_max=3600,                                 # Maximum backoff in seconds
    expires_in=86400,                                 # Job expires after N seconds
    expires_at=datetime(2024, 1, 16, 10, 0),         # Specific expiration time
    idempotency_key="user_123_signup",                # Prevent duplicate jobs
    webhook_id="signup_notification"                   # Custom webhook identifier
)
```

### Parameter Details

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `url` | `str` | *Required* | Target webhook URL (must start with http:// or https://) |
| `data` | `Dict[str, Any]` | *Required* | Data to POST (must be JSON serializable) |
| `headers` | `Dict[str, str]` | `None` | Additional HTTP headers |
| `channel` | `str` | `"webhooks"` | Job queue channel |
| `delay` | `int` | `None` | Delay in seconds before execution |
| `run_at` | `datetime` | `None` | Specific execution time |
| `timeout` | `int` | `30` | HTTP request timeout in seconds |
| `max_retries` | `int` | `5` | Maximum retry attempts |
| `backoff_base` | `float` | `2.0` | Exponential backoff multiplier |
| `backoff_max` | `int` | `3600` | Maximum backoff between retries (seconds) |
| `expires_in` | `int` | `None` | Job expiration in seconds |
| `expires_at` | `datetime` | `None` | Specific job expiration time |
| `idempotency_key` | `str` | `None` | Key to prevent duplicate executions |
| `webhook_id` | `str` | `None` | Custom identifier for tracking |

## Configuration

Add these settings to your Django `settings.py`:

```python
# Webhook-specific settings
JOBS_WEBHOOK_MAX_RETRIES = 5          # Default max retries for webhooks
JOBS_WEBHOOK_DEFAULT_TIMEOUT = 30     # Default request timeout in seconds  
JOBS_WEBHOOK_MAX_TIMEOUT = 300        # Maximum allowed timeout
JOBS_WEBHOOK_USER_AGENT = "Django-MOJO-Webhook/1.0"  # Default User-Agent

# Make sure webhooks channel is included
JOBS_CHANNELS = [
    'default',
    'webhooks',  # <- Webhook jobs go here by default
    'emails',
    # ... other channels
]
```

### Environment-Specific Configuration

```python
# Production: More aggressive retries, longer timeouts
JOBS_WEBHOOK_MAX_RETRIES = 10
JOBS_WEBHOOK_DEFAULT_TIMEOUT = 60

# Development: Fewer retries, shorter timeouts
JOBS_WEBHOOK_MAX_RETRIES = 2
JOBS_WEBHOOK_DEFAULT_TIMEOUT = 15
```

## Error Handling & Retries

### Automatic Retry Logic

Webhooks are automatically retried for certain types of failures:

**Retried Errors:**
- Network connection errors
- Request timeouts
- HTTP 408 (Request Timeout)
- HTTP 429 (Too Many Requests) 
- HTTP 502, 503, 504 (Server Errors)
- HTTP 520-524 (Cloudflare Errors)

**Not Retried:**
- HTTP 4xx client errors (400, 401, 403, 404, etc.)
- Invalid URLs or malformed requests
- JSON serialization errors

### Exponential Backoff

Retry delays follow exponential backoff with jitter:

```
Attempt 1: immediate
Attempt 2: ~2 seconds + jitter
Attempt 3: ~4 seconds + jitter  
Attempt 4: ~8 seconds + jitter
Attempt 5: ~16 seconds + jitter
...
```

Jitter (0.8x - 1.2x) prevents thundering herd problems when many webhooks fail simultaneously.

### Custom Retry Configuration

```python
# Critical webhooks: More retries, slower backoff
publish_webhook(
    url="https://critical-api.example.com/webhook",
    data=payment_data,
    max_retries=10,         # Up to 10 attempts
    backoff_base=1.5,       # Slower growth (1.5^attempt)
    backoff_max=7200,       # Up to 2 hours between retries
    timeout=120             # Longer timeout
)

# Non-critical webhooks: Fewer retries, faster failure
publish_webhook(
    url="https://analytics.example.com/track",
    data=analytics_data,
    max_retries=2,          # Only 2 retries
    backoff_base=2.0,       # Standard growth
    backoff_max=300         # Max 5 minutes between retries
)
```

## Monitoring & Metrics

### Job Status Tracking

```python
from mojo.apps.jobs import status

job_id = publish_webhook(url="...", data={...})

# Check job status
job_status = status(job_id)
print(f"Status: {job_status['status']}")
print(f"Attempt: {job_status['attempt']}")
print(f"Last error: {job_status.get('last_error', 'None')}")
```

### Webhook Metadata

Completed webhook jobs include rich metadata:

```python
{
    "webhook_started_at": "2024-01-15T10:30:00Z",
    "webhook_completed_at": "2024-01-15T10:30:02Z", 
    "url": "https://api.example.com/webhook",
    "webhook_id": "user_signup_123",
    "attempt": 1,
    "timeout_seconds": 30,
    "response_status_code": 200,
    "response_headers": {"content-type": "application/json"},
    "response_size_bytes": 156,
    "duration_ms": 1245,
    "headers_sent": {"authorization": "Bear...123", "content-type": "application/json"}
}
```

### Metrics for Monitoring

The webhook system emits metrics for monitoring:

- `webhooks.success` - Successful webhook deliveries
- `webhooks.timeout` - Webhook timeouts  
- `webhooks.connection_error` - Network connection failures
- `webhooks.error_client` - HTTP 4xx client errors
- `webhooks.error_retriable` - HTTP 5xx server errors (will retry)
- `webhooks.duration_ms` - Request duration in milliseconds
- `webhooks.host.{hostname}.{outcome}` - Per-host metrics

Set up alerts on high error rates or slow response times.

## Security Considerations

### Header Sanitization

Sensitive headers are automatically masked in logs and metadata:

```python
publish_webhook(
    url="https://api.example.com/webhook",
    data={"event": "signup"},
    headers={
        "Authorization": "Bearer sk_live_1234567890abcdef",  
        "X-API-Key": "secret_api_key_here"
    }
)

# Stored in metadata as:
# "headers_sent": {
#     "authorization": "Bear...cdef",
#     "x-api-key": "secr...here"  
# }
```

### Best Practices

1. **Use HTTPS URLs** - Never send webhooks to HTTP endpoints in production
2. **Implement signature verification** on the receiving end
3. **Use short-lived tokens** in Authorization headers when possible
4. **Set reasonable timeouts** to prevent resource exhaustion
5. **Monitor for suspicious patterns** (repeated failures to same endpoint)

```python
# Good: Secure webhook with proper auth
publish_webhook(
    url="https://secure-api.example.com/webhook",  # HTTPS
    data=webhook_data,
    headers={
        "Authorization": f"Bearer {short_lived_token}",
        "X-Webhook-Signature": generate_signature(webhook_data),
        "X-Timestamp": str(int(time.time()))
    },
    timeout=30,  # Reasonable timeout
    webhook_id=f"event_{event.id}"  # Trackable ID
)
```

## Examples

### User Registration Webhook

```python
def notify_user_registered(user):
    """Send webhook when user registers."""
    return publish_webhook(
        url="https://analytics.yoursite.com/events/user-registered",
        data={
            "event": "user_registered",
            "user_id": user.id,
            "email": user.email,
            "registration_date": user.date_joined.isoformat(),
            "source": "web_signup"
        },
        webhook_id=f"user_registered_{user.id}",
        idempotency_key=f"user_reg_{user.id}"  # Prevent duplicates
    )
```

### Payment Success Webhook

```python
def notify_payment_success(payment):
    """Send webhook when payment succeeds.""" 
    return publish_webhook(
        url="https://fulfillment.yoursite.com/webhooks/payment-success",
        data={
            "event": "payment_success",
            "payment_id": payment.id,
            "order_id": payment.order_id,
            "amount": str(payment.amount),
            "currency": payment.currency,
            "customer_id": payment.customer_id,
            "processed_at": payment.processed_at.isoformat()
        },
        headers={
            "Authorization": f"Bearer {settings.FULFILLMENT_WEBHOOK_TOKEN}",
            "X-Idempotency-Key": f"payment_{payment.id}_success"
        },
        max_retries=8,  # Critical for payments
        timeout=60,     # Longer timeout for important webhooks
        webhook_id=f"payment_success_{payment.id}"
    )
```

### Batch Webhook Processing

```python
def send_order_confirmations(orders):
    """Send webhook for each order in a batch."""
    job_ids = []
    
    for order in orders:
        job_id = publish_webhook(
            url="https://crm.yoursite.com/webhooks/new-order",
            data={
                "event": "order_created",
                "order_id": order.id,
                "customer_email": order.customer.email,
                "total_amount": str(order.total),
                "items": [
                    {"sku": item.sku, "quantity": item.quantity}
                    for item in order.items.all()
                ]
            },
            headers={
                "Authorization": f"Bearer {settings.CRM_WEBHOOK_TOKEN}"
            },
            webhook_id=f"order_created_{order.id}",
            idempotency_key=f"order_{order.id}_created"
        )
        job_ids.append(job_id)
    
    return job_ids
```

### Delayed Notification Webhook

```python
def schedule_trial_ending_reminder(user, trial_end_date):
    """Send webhook 24 hours before trial ends."""
    reminder_time = trial_end_date - timedelta(days=1)
    
    return publish_webhook(
        url="https://notifications.yoursite.com/webhooks/trial-reminder",
        data={
            "event": "trial_ending_reminder", 
            "user_id": user.id,
            "email": user.email,
            "trial_ends_at": trial_end_date.isoformat(),
            "plan": user.subscription.plan_name
        },
        run_at=reminder_time,  # Schedule for specific time
        expires_in=86400,      # Expire if not sent within 24 hours
        webhook_id=f"trial_reminder_{user.id}_{trial_end_date.date()}"
    )
```

### Webhook with Custom Retry Logic

```python
def send_critical_system_alert(alert_data):
    """Send critical system alerts with aggressive retry policy."""
    return publish_webhook(
        url="https://alerts.yoursite.com/webhooks/critical",
        data={
            "alert_level": "critical",
            "service": alert_data["service"],
            "message": alert_data["message"], 
            "timestamp": datetime.utcnow().isoformat(),
            "affected_users": alert_data.get("affected_users", 0)
        },
        headers={
            "Authorization": f"Bearer {settings.ALERTS_WEBHOOK_TOKEN}",
            "X-Alert-Priority": "critical"
        },
        max_retries=15,        # Very aggressive retry
        backoff_base=1.3,      # Slower backoff growth  
        backoff_max=1800,      # Max 30 minutes between retries
        timeout=120,           # Longer timeout for critical alerts
        webhook_id=f"critical_alert_{alert_data['alert_id']}"
    )
```

## Troubleshooting

### Common Issues

**1. Webhook not being sent**
```bash
# Check if job was created
from mojo.apps.jobs import status
job_status = status(job_id)
print(job_status)

# Check if webhook channel is being processed
python manage.py jobs_engine --channels webhooks
```

**2. Webhook failing with timeout**
```python
# Increase timeout for slow endpoints
publish_webhook(
    url="https://slow-api.example.com/webhook",
    data=webhook_data,
    timeout=120  # 2 minutes instead of default 30 seconds
)
```

**3. Too many retries**
```python
# Check job metadata for error details
job_status = status(job_id)
print("Last error:", job_status.get('last_error'))
print("Attempt:", job_status.get('attempt'))
print("Metadata:", job_status.get('metadata'))
```

**4. Webhooks being duplicated**
```python
# Use idempotency keys to prevent duplicates
publish_webhook(
    url="https://api.example.com/webhook",
    data=webhook_data,
    idempotency_key=f"unique_event_{event.id}_{event.type}"
)
```

### Debugging Failed Webhooks

1. **Check job status and metadata:**
```python
from mojo.apps.jobs import status
job_info = status(job_id)
print("Status:", job_info['status'])
print("Attempts:", job_info['attempt']) 
print("Last error:", job_info.get('last_error'))
print("Metadata:", job_info.get('metadata'))
```

2. **Review webhook job logs:**
```bash
# Look for webhook-specific log entries
tail -f logs/jobs.log | grep webhook
```

3. **Test endpoint manually:**
```python
import requests

# Test the endpoint directly
response = requests.post(
    "https://api.example.com/webhook",
    json={"test": "data"},
    headers={"Authorization": "Bearer token"},
    timeout=30
)
print("Status:", response.status_code)
print("Response:", response.text)
```

4. **Use webhook testing services:**
```python
# Test with httpbin.org to see what's being sent
test_job_id = publish_webhook(
    url="https://httpbin.org/post",
    data={"test": "webhook"},
    headers={"X-Test": "true"}
)

# Check the response in job metadata after completion
test_status = status(test_job_id)
print(test_status['metadata']['response_sample'])
```

### Performance Considerations

**High-volume webhook sending:**
- Use dedicated webhook channels: `channel="webhooks-high-volume"`
- Run multiple webhook workers: `python manage.py jobs_engine --channels webhooks --max-workers 50`
- Consider webhook batching for the same endpoint
- Monitor queue lengths and processing times

**Memory usage with large payloads:**
- Keep webhook payloads under 1MB (configurable with `JOBS_PAYLOAD_MAX_BYTES`)
- For large data, consider sending just IDs and letting the receiver fetch details
- Use pagination for bulk data transfers

### Monitoring Dashboard Queries

**Webhook success rate (last 24 hours):**
```sql
SELECT 
    COUNT(CASE WHEN status = 'completed' THEN 1 END) * 100.0 / COUNT(*) as success_rate
FROM jobs_job 
WHERE channel = 'webhooks' 
    AND created >= NOW() - INTERVAL '24 hours';
```

**Top failing webhook endpoints:**
```sql
SELECT 
    JSON_EXTRACT(payload, '$.url') as webhook_url,
    COUNT(*) as failure_count,
    AVG(attempt) as avg_attempts
FROM jobs_job 
WHERE channel = 'webhooks' 
    AND status = 'failed'
    AND created >= NOW() - INTERVAL '7 days'
GROUP BY JSON_EXTRACT(payload, '$.url')
ORDER BY failure_count DESC
LIMIT 10;
```

**Average webhook processing time:**
```sql
SELECT 
    AVG(JSON_EXTRACT(metadata, '$.duration_ms')) as avg_duration_ms
FROM jobs_job 
WHERE channel = 'webhooks' 
    AND status = 'completed'
    AND JSON_EXTRACT(metadata, '$.duration_ms') IS NOT NULL
    AND created >= NOW() - INTERVAL '24 hours';
```

---

## Summary

Django-MOJO Jobs webhook support provides:

✅ **Simple API** - Just `publish_webhook(url, data)`  
✅ **Robust retry logic** - Exponential backoff with jitter  
✅ **Smart error handling** - Knows when to retry vs. fail  
✅ **Comprehensive monitoring** - Detailed metadata and metrics  
✅ **Security-conscious** - Header sanitization and HTTPS validation  
✅ **Production-ready** - Handles high-volume webhook sending  
✅ **Flexible scheduling** - Immediate, delayed, or scheduled webhooks  
✅ **Duplicate prevention** - Idempotency key support  

The webhook system leverages all the existing job infrastructure (Redis streams, Postgres persistence, worker management) while providing webhook-specific optimizations and features.