# Writing Job Functions — Django Developer Reference

## Function Signature

Every job function receives a single argument: the `Job` model instance.

```python
# myapp/services/email.py

def send_welcome(job):
    user_id = job.payload["user_id"]
    user = User.objects.get(pk=user_id)
    mailbox = Mailbox.get_system_default()
    mailbox.send_template_email("welcome", user.email, {"display_name": user.display_name})
```

Publish it:

```python
from mojo.apps import jobs
jobs.publish("myapp.services.email.send_welcome", {"user_id": 42})
```

## Accessing Payload

The payload dict is available as `job.payload`:

```python
def process_order(job):
    order_id = job.payload["order_id"]
    priority = job.payload.get("priority", "normal")
    options = job.payload.get("options", {})
```

Payload is stored in PostgreSQL as JSON. Keep it small — pass IDs and fetch objects inside the job.

## Return Values

Job functions can return a status string to control the final job state:

| Return value | Job status set to |
|-------------|-------------------|
| `None` | `completed` |
| `"success"` | `completed` |
| `"failed"` | `failed` (triggers retry if `max_retries > 0`) |
| `"cancelled"` | `canceled` |

```python
def charge_payment(job):
    payment = Payment.objects.get(pk=job.payload["payment_id"])
    result = payment_gateway.charge(payment)

    if result.success:
        return "success"

    if result.declined:
        # Don't retry declined cards
        job.metadata["decline_reason"] = result.reason
        job.save()
        return  # completes normally

    # Transient failure — retry
    return "failed"
```

## Metadata

Use `job.metadata` to store progress, results, or tracking data:

```python
def export_users(job):
    job.metadata["step"] = "querying"
    job.save()

    users = User.objects.filter(group_id=job.payload["group_id"])
    total = users.count()
    job.metadata["total"] = total

    for i, user in enumerate(users):
        export_user(user)
        if i % 100 == 0:
            job.metadata["exported"] = i
            job.save()

    job.metadata["step"] = "done"
    job.metadata["exported"] = total
    job.save()
```

`metadata` is a JSONField — store any JSON-serializable data.

### atomic_save()

For long-running jobs where concurrent saves could conflict:

```python
job.metadata["processed"] = count
job.atomic_save()
```

## Cancellation

Jobs support cooperative cancellation. The engine sets `cancel_requested=True` on the DB record — your function must check for it.

### check_cancel_requested()

Refreshes the job from the database and returns the current `cancel_requested` value:

```python
def process_large_batch(job):
    items = Item.objects.filter(batch_id=job.payload["batch_id"])

    for i, item in enumerate(items):
        if job.check_cancel_requested():
            job.add_log(f"Cancelled after {i} items", kind="info")
            return "cancelled"

        process(item)

        if i % 50 == 0:
            job.metadata["processed"] = i
            job.save()
```

Check periodically in loops — every N iterations, not every iteration (each check hits the database).

### Requesting Cancellation

From outside the job:

```python
from mojo.apps import jobs
jobs.cancel(job_id)
```

Or via REST API: `POST /api/jobs/cancel` with `{"job_id": "..."}`

## Job Logging

Use `job.add_log()` to create structured log entries attached to the job:

```python
def import_data(job):
    job.add_log("Starting import", kind="info")

    try:
        count = do_import(job.payload["file_id"])
        job.add_log(f"Imported {count} records", kind="info", meta={"count": count})
    except ValidationError as e:
        job.add_log(f"Validation failed: {e}", kind="warn")
        raise
    except Exception as e:
        job.add_log(f"Import failed: {e}", kind="error")
        raise
```

### add_log() Signature

```python
job.add_log(
    message,        # str — log message
    kind="info",    # str — "debug", "info", "warn", or "error"
    meta=None,      # dict — optional structured data
)
```

Creates a `JobLog` record linked to the job. Logs are queryable via REST API.

## Error Handling

### Unhandled Exceptions

If your function raises an exception:
- Job status → `failed`
- Exception message → `job.last_error`
- Full traceback → `job.stack_trace`
- If `max_retries > 0` and `attempt < max_retries`, the job is retried with backoff

```python
def risky_job(job):
    # If this raises, the engine catches it and marks the job failed
    result = external_api.call(job.payload["endpoint"])
    return "success"
```

### Soft Failures

For expected errors that shouldn't trigger retries, handle them and return normally:

```python
def send_notification(job):
    user = User.objects.filter(pk=job.payload["user_id"]).first()
    if not user:
        job.add_log("User not found, skipping", kind="warn")
        return  # completes as 'completed', no retry

    send_push(user, job.payload["message"])
```

### Retry vs No-Retry Decision

| Scenario | Approach |
|----------|----------|
| Transient API error | `raise` or `return "failed"` — let retry handle it |
| Missing data / invalid input | Handle gracefully, return `None` — no retry |
| Rate limited | `return "failed"` with backoff configured |
| Permanent external failure | Handle, log, return `None` |

## Long-Running Jobs

For jobs that take minutes or longer:

```python
def generate_report(job):
    job.add_log("Report generation started", kind="info")
    job.metadata["status"] = "generating"
    job.save()

    sections = job.payload.get("sections", ["summary", "detail", "charts"])
    for i, section in enumerate(sections):
        # Check cancellation between sections
        if job.check_cancel_requested():
            job.add_log("Report generation cancelled", kind="info")
            return "cancelled"

        generate_section(section, job.payload["report_id"])
        job.metadata["progress"] = f"{i + 1}/{len(sections)}"
        job.metadata["current_section"] = section
        job.save()

    job.metadata["status"] = "complete"
    job.save()
    job.add_log("Report generation complete", kind="info")
```

## Available Job Properties

The `job` argument is a full Django model instance. Key attributes:

| Attribute | Description |
|-----------|-------------|
| `job.id` | 32-char UUID string |
| `job.payload` | Input data dict |
| `job.metadata` | Read/write JSON dict for progress/results |
| `job.channel` | Queue channel name |
| `job.func` | Module path string |
| `job.status` | Current status string |
| `job.attempt` | Current attempt number (0-based) |
| `job.max_retries` | Max retries configured |
| `job.cancel_requested` | Boolean (check via `check_cancel_requested()`) |
| `job.runner_id` | ID of the runner executing this job |
| `job.created` | When the job was created |
| `job.started_at` | When execution began |
| `job.broadcast` | Whether this is a broadcast job |
| `job.idempotency_key` | Idempotency key if set |

## File Location

Place job functions in `app/services/` modules:

```
myapp/
  services/
    email.py      # send_welcome(job), send_notification(job)
    export.py     # generate_report(job), export_csv(job)
    cleanup.py    # purge_expired(job), archive_old(job)
```

Publish using the full dotted path:

```python
jobs.publish("myapp.services.email.send_welcome", {"user_id": 42})
jobs.publish("myapp.services.export.generate_report", {"report_id": 7})
```

## Best Practices

1. **Idempotent functions** — Jobs may be retried. Make functions safe to run multiple times.
2. **Pass IDs, fetch inside** — Don't put full objects in the payload.
3. **Check cancellation in loops** — Every N iterations, not every iteration.
4. **Log with `add_log()`** — Creates queryable, per-job log entries.
5. **Use `atomic_save()`** — For concurrent-safe metadata updates in long jobs.
6. **Keep jobs focused** — One job = one unit of work. Chain jobs for multi-step workflows.
7. **Handle expected errors gracefully** — Only `raise` for genuinely unexpected failures.
