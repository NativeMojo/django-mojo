# Writing Job Functions — Django Developer Reference

## Function Signature

Job functions receive the `Job` model instance:

```python
# myapp/services/email.py

def send_welcome(job):
    user_id = job.payload["user_id"]
    user = User.objects.get(pk=user_id)
    mailbox = Mailbox.get_system_default()
    mailbox.send_template_email("welcome", user.email, {"display_name": user.display_name})
```

## Accessing Payload

```python
def my_job(job):
    # All payload data is available as a dict
    order_id = job.payload["order_id"]
    priority = job.payload.get("priority", "normal")
```

## Metadata

```python
def my_job(job):
    # Write metadata for progress tracking
    job.metadata["processed_count"] = 0
    job.save()  # or job.atomic_save()

    for item in items:
        process(item)
        job.metadata["processed_count"] += 1
        job.atomic_save()
```

## Cancellation Check

```python
def long_running_job(job):
    for i, item in enumerate(items):
        if job.cancel_requested:
            return "cancelled"
        process(item)
```

## Return Value

Return a string or dict to store in `job.result`:

```python
def my_job(job):
    result = do_work(job.payload)
    return {"status": "ok", "count": result}
```

## Error Handling

Unhandled exceptions are caught by the job engine:
- Job status set to `failed`
- Exception stored in `job.error`
- Retried if retry policy is configured

Raise exceptions for hard failures. For soft failures (expected errors), handle gracefully and set status manually:

```python
def my_job(job):
    user = User.objects.filter(pk=job.payload["user_id"]).first()
    if not user:
        job.metadata["error"] = "User not found"
        # Return without raising — job completes as 'completed' (no retry)
        return

    do_work(user)
```

## Best Practices

- **Idempotent functions**: Jobs may be retried. Design functions to be safe to run multiple times.
- **Fetch by ID**: Pass IDs in payload, fetch objects inside the job.
- **Log progress**: Use `self.log()` or `logit.info()` for observability.
- **Keep jobs small**: One job = one unit of work. Chain jobs for multi-step workflows.

## File Location

Place job functions in `app/services/` or dedicated `app/jobs/` modules:

```
myapp/
  services/
    email.py      # contains send_welcome(job), send_notification(job)
    export.py     # contains generate_report(job)
```

Publish using the full module path:

```python
jobs.publish("myapp.services.email.send_welcome", {"user_id": 42})
```
