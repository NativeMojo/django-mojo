# Publishing Jobs — Django Developer Reference

## Import

```python
from mojo.apps import jobs
```

## publish()

Enqueue a job for async execution by a runner.

```python
job_id = jobs.publish("myapp.services.email.send_welcome", {"user_id": 42})
```

### Full Signature

```python
jobs.publish(
    func,                    # str module path (preferred) or callable
    payload=None,            # dict — persisted to PostgreSQL, not Redis
    *,
    channel="default",       # queue channel name
    delay=None,              # int seconds from now
    run_at=None,             # datetime — schedule for specific time
    broadcast=False,         # True = all runners execute this job
    max_retries=None,        # int — override default (0)
    backoff_base=None,       # float — override default (2.0)
    backoff_max=None,        # int seconds — override default (3600)
    expires_in=None,         # int seconds until expiration
    expires_at=None,         # datetime — specific expiration time
    max_exec_seconds=None,   # int — execution time limit (advisory)
    idempotency_key=None,    # str — prevent duplicate execution
)
```

**Returns**: Job ID string (32-char UUID without dashes).

**Raises**: `ValueError` for invalid params, `RuntimeError` on publish failure.

### Parameter Reference

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `func` | str or callable | *required* | Dotted module path to job function |
| `payload` | dict | `None` | Input data (stored in DB, passed as `job.payload`) |
| `channel` | str | `"default"` | Queue channel — routes to specific workers |
| `delay` | int | `None` | Seconds from now to execute |
| `run_at` | datetime | `None` | Specific UTC time to execute |
| `broadcast` | bool | `False` | Execute on ALL runners for this channel |
| `max_retries` | int | `0` | Max retry attempts on failure |
| `backoff_base` | float | `2.0` | Exponential backoff base (delay = base^attempt) |
| `backoff_max` | int | `3600` | Max seconds between retries |
| `expires_in` | int | `None` | Seconds until job expires if not executed |
| `expires_at` | datetime | `None` | Specific expiration time |
| `max_exec_seconds` | int | `None` | Execution time limit (advisory — not enforced by engine) |
| `idempotency_key` | str | `None` | Unique key — duplicate publishes are silently ignored |

### String Path (Preferred)

Always use string paths for the `func` argument:

```python
jobs.publish("myapp.services.email.send_welcome", {"user_id": 42})
jobs.publish("myapp.services.export.generate_report", {"report_id": 7})
jobs.publish("myapp.services.cleanup.purge_expired", {"days_old": 30})
```

String paths are cleaner, avoid circular imports, and make it obvious where the code lives.

### Examples

```python
# Basic job
jobs.publish("myapp.services.email.send_welcome", {"user_id": 42})

# Delayed job (run in 5 minutes)
jobs.publish("myapp.services.reminder.send", {"user_id": 42}, delay=300)

# Scheduled job (specific time)
from mojo.helpers import dates
jobs.publish(
    "myapp.services.report.generate",
    {"report_id": 7},
    run_at=dates.add(dates.utcnow(), hours=1),
)

# With retries and backoff
jobs.publish(
    "myapp.services.payment.charge",
    {"payment_id": 55},
    max_retries=5,
    backoff_base=2.0,
    backoff_max=3600,
)

# Idempotent (won't create duplicate)
jobs.publish(
    "myapp.services.billing.invoice",
    {"user_id": 42, "month": "2026-03"},
    idempotency_key="invoice_42_2026_03",
)

# Broadcast to all runners
jobs.publish(
    "myapp.services.cache.clear_local",
    {"prefix": "user_*"},
    broadcast=True,
)

# Specific channel with expiration
jobs.publish(
    "myapp.services.export.generate_csv",
    {"export_id": 12},
    channel="heavy",
    expires_in=1800,  # expire if not picked up in 30 min
)
```

## publish_local()

Execute a job in a thread in the current process. No runner needed — useful for dev/testing.

```python
jobs.publish_local("myapp.services.email.send_welcome", {"user_id": 42})
```

### Signature

```python
jobs.publish_local(
    func,           # str module path or callable
    *args,          # positional args (payload dict)
    run_at=None,    # datetime — sleep until this time
    delay=None,     # int seconds — sleep before executing
    **kwargs,       # additional keyword args
)
```

**Returns**: Pseudo job ID string (for compatibility).

The function is imported and called directly in a new thread. If `delay` or `run_at` is set, the thread sleeps first.

## publish_webhook()

Publish an HTTP POST webhook as a job with automatic retries.

```python
from mojo.apps.jobs import publish_webhook

job_id = publish_webhook(
    url="https://api.partner.com/webhooks/order",
    data={"order_id": 99, "event": "created"},
)
```

Full reference: [webhooks.md](webhooks.md)

### Signature

```python
publish_webhook(
    url,                     # str — target URL (must start with http:// or https://)
    data,                    # dict — JSON data to POST
    *,
    headers=None,            # dict — additional HTTP headers
    channel="webhooks",      # str — job channel
    delay=None,              # int seconds
    run_at=None,             # datetime
    timeout=30,              # int seconds — HTTP request timeout
    max_retries=None,        # int — default 5 for webhooks
    backoff_base=None,       # float — default 2.0
    backoff_max=None,        # int — default 3600
    expires_in=None,         # int seconds
    expires_at=None,         # datetime
    idempotency_key=None,    # str
    webhook_id=None,         # str — custom identifier for tracking
)
```

**Returns**: Job ID string.

**Raises**: `ValueError` for invalid URL or non-serializable data, `RuntimeError` on failure.

Internally creates a job that calls `mojo.apps.jobs.handlers.webhook.post_webhook`.

## broadcast_execute()

Execute a function on ALL active runners without creating a Job record. This is a real-time control-channel operation.

```python
results = jobs.broadcast_execute(
    "myapp.services.cache.clear_all",
    data={"prefix": "user_*"},
    collect_replies=True,
    timeout=5.0,
)
```

### Signature

```python
jobs.broadcast_execute(
    func_path,              # str — dotted path to function
    data=None,              # dict — passed to the function
    timeout=2.0,            # float seconds — wait for responses
    collect_replies=False,  # bool — True to gather return values
)
```

**Returns**: List of dicts, one per responding runner:
```python
[
    {"runner_id": "runner-host1-abc", "func": "...", "status": "success", "result": {...}},
    {"runner_id": "runner-host2-def", "func": "...", "status": "error", "error": "..."},
]
```

Empty list if no runners respond.

### Use Cases

- Cache invalidation across all runners
- Config reload
- Collecting system info (`jobs.get_sysinfo()` uses this internally)

## Channels

Channels route jobs to specific worker pools. Run separate engine processes per channel:

```bash
# Email worker pool
python manage.py jobs_engine --channels emails --max-workers 20

# Heavy processing pool
python manage.py jobs_engine --channels heavy --max-workers 5

# Default catches everything else
python manage.py jobs_engine --channels default --max-workers 10
```

Configure available channels in settings:

```python
JOBS_CHANNELS = ['default', 'emails', 'webhooks', 'heavy', 'maintenance']
```

Default channel is `"default"`.

## Payload Best Practices

Payloads are persisted to PostgreSQL. Max size is **16KB** by default (`JOBS_PAYLOAD_MAX_BYTES`).

```python
# Good — pass IDs, fetch in job
jobs.publish("myapp.services.order.process", {"order_id": 42})

# Bad — large objects in payload
jobs.publish("myapp.services.order.process", {"order": huge_dict})
```

Always pass identifiers and fetch the data inside the job function.

## Other Functions

### status()

```python
info = jobs.status(job_id)
# Returns dict: {id, status, channel, func, created, started_at, finished_at, attempt, last_error, metadata}
# Returns None if not found
```

### cancel()

```python
success = jobs.cancel(job_id)
# Returns True if cancel requested, False if not found or already terminal
```

Sets `cancel_requested=True` on the job. The running function must check via `job.check_cancel_requested()`.

### get_runners()

```python
runners = jobs.get_runners(channel=None)
# Returns list of dicts with runner info and heartbeat data
```

### get_sysinfo()

```python
info = jobs.get_sysinfo(runner_id=None, timeout=5.0)
# Returns list of dicts with CPU, memory, disk, network info per runner
```

Requires `psutil` installed on runners.

### broadcast_command()

```python
responses = jobs.broadcast_command("status", timeout=2.0)
# Commands: "status", "shutdown", "pause", "resume"
```

### ping()

```python
alive = jobs.ping("runner-host1-abc123", timeout=2.0)
# Returns True/False
```
