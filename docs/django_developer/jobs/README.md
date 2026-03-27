# Jobs — Django Developer Reference

Async job processing with Redis transport and PostgreSQL persistence. Publish a job from any Django code, and a background runner executes it.

## Quick Start

```python
from mojo.apps import jobs

# Publish a job (string path preferred)
jobs.publish("myapp.services.email.send_welcome", {"user_id": 42})
```

Write the job function — it receives the `Job` model instance:

```python
# myapp/services/email.py

def send_welcome(job):
    user_id = job.payload["user_id"]
    user = User.objects.get(pk=user_id)
    mailbox = Mailbox.get_system_default()
    mailbox.send_template_email("welcome", user.email, {"display_name": user.display_name})
```

That's it. The job is persisted to PostgreSQL, queued via Redis, and executed by a runner.

## Publishing Methods

| Method | Use case |
|--------|----------|
| `jobs.publish()` | Standard async job — most common |
| `jobs.publish_webhook()` | HTTP POST to external URL with retries |
| `jobs.publish_local()` | In-process thread execution (dev/testing) |
| `jobs.broadcast_execute()` | Run function on ALL runners (no job record) |

### publish()

```python
jobs.publish(
    "myapp.services.cleanup.purge_expired",
    {"days_old": 30},
    channel="maintenance",
    delay=3600,              # seconds from now
    max_retries=3,
    idempotency_key="daily_purge_2026_03_26",
)
```

Full signature and all options: [publishing.md](publishing.md)

### publish_webhook()

```python
from mojo.apps.jobs import publish_webhook

publish_webhook(
    url="https://api.partner.com/webhooks/order",
    data={"order_id": 99, "event": "created"},
    headers={"Authorization": "Bearer sk_live_xxx"},
    max_retries=5,
)
```

Full reference: [webhooks.md](webhooks.md)

### publish_local()

Runs the job in a thread in the current process. Useful for dev/testing when you don't have a runner:

```python
jobs.publish_local("myapp.services.email.send_welcome", {"user_id": 42})

# With delay
jobs.publish_local("myapp.services.cleanup.run", delay=60)
```

### broadcast_execute()

Execute a function on every active runner. No Job record is created — this is fire-and-collect:

```python
# Collect results from all runners
results = jobs.broadcast_execute(
    "myapp.services.cache.clear_all",
    data={"prefix": "user_*"},
    collect_replies=True,
    timeout=5.0,
)
```

## Writing Job Functions

```python
def process_order(job):
    order_id = job.payload["order_id"]
    order = Order.objects.get(pk=order_id)

    # Track progress via metadata
    job.metadata["step"] = "processing"
    job.save()

    process(order)

    # Log inside the job
    job.add_log("Order processed successfully", kind="info")
```

### Return Values

Job functions can return a status string to control the final state:

| Return value | Job status |
|-------------|------------|
| `None` or `"success"` | `completed` |
| `"failed"` | `failed` (triggers retry if configured) |
| `"cancelled"` | `canceled` |

### Cancellation

Long-running jobs should check for cancellation:

```python
def export_all_users(job):
    users = User.objects.all().iterator()
    for i, user in enumerate(users):
        if job.check_cancel_requested():
            return "cancelled"
        export_user(user)
        if i % 100 == 0:
            job.metadata["exported"] = i
            job.save()
```

Full guide: [writing_jobs.md](writing_jobs.md)

## Scheduling

```python
from mojo.helpers import dates

# Delay by seconds
jobs.publish("myapp.services.report.generate", payload, delay=3600)

# Specific time
jobs.publish("myapp.services.report.generate", payload,
    run_at=dates.add(dates.utcnow(), hours=1))
```

Scheduled jobs are stored in a Redis ZSET and moved to the queue when due.

## Retries

```python
jobs.publish(
    "myapp.services.payment.charge",
    {"payment_id": 55},
    max_retries=5,
    backoff_base=2.0,     # delay = 2^attempt seconds
    backoff_max=3600,     # cap at 1 hour between retries
)
```

The engine retries on unhandled exceptions or when the job returns `"failed"`. Backoff is exponential: 2s, 4s, 8s, 16s, ...

## Idempotency

Prevent duplicate job execution with `idempotency_key`:

```python
jobs.publish(
    "myapp.services.billing.invoice_user",
    {"user_id": 42, "month": "2026-03"},
    idempotency_key="invoice_42_2026_03",
)
```

A second publish with the same key is silently ignored.

## Broadcast

Send a job to ALL runners on a channel:

```python
jobs.publish(
    "myapp.services.cache.rebuild_local",
    {"version": 5},
    broadcast=True,
)
```

Every runner executes the job independently. Useful for cache invalidation, config reload, etc.

## Job Logging

```python
def import_data(job):
    job.add_log("Starting import", kind="info")
    try:
        count = do_import(job.payload["file_id"])
        job.add_log(f"Imported {count} records", kind="info", meta={"count": count})
    except Exception as e:
        job.add_log(f"Import failed: {e}", kind="error")
        raise
```

Logs are stored in the `JobLog` model and visible via REST API.

## Monitoring

```python
from mojo.apps import jobs

# Check job status
info = jobs.status(job_id)  # returns dict or None

# Cancel a job
jobs.cancel(job_id)

# List active runners
runners = jobs.get_runners()

# System info from runners
sysinfo = jobs.get_sysinfo()
```

## Deep Dives

| Page | What it covers |
|------|---------------|
| [publishing.md](publishing.md) | Full `publish()` signature, all methods, channels |
| [writing_jobs.md](writing_jobs.md) | Job function patterns, error handling, logging, cancellation |
| [job_model.md](job_model.md) | Job, JobEvent, JobLog model reference — all fields |
| [webhooks.md](webhooks.md) | `publish_webhook()`, retry behavior, security, monitoring |
| [settings.md](settings.md) | All settings with correct defaults |
| [admin.md](admin.md) | REST control endpoints for ops |
