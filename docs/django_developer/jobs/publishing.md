# Publishing Jobs — Django Developer Reference

## Import

```python
from mojo.apps import jobs
```

## Enqueue a Job

Pass a module path string or callable, plus a payload dict:

```python
# By module path string (preferred — works across distributed workers)
jobs.publish("myapp.services.email.send_welcome", {"user_id": 42})

# By callable (module path is auto-derived)
from myapp.services.email import send_welcome
jobs.publish(send_welcome, {"user_id": 42})
```

## publish() Signature

```python
jobs.publish(
    func,              # str module path or callable
    payload,           # dict — stored in DB only, never Redis
    channel="default", # queue channel name
    run_at=None,       # datetime — schedule for future execution
    **options
)
```

Returns the `job_id` string.

## Scheduled Jobs

```python
from mojo.helpers import dates

# Run in 1 hour
jobs.publish(
    "myapp.services.cleanup.purge_expired",
    {"days_old": 30},
    run_at=dates.add(dates.utcnow(), hours=1)
)
```

Scheduled jobs are stored in a Redis ZSET and dequeued when their time arrives.

## Channels

Channels allow routing different job types to different worker pools:

```python
jobs.publish("myapp.tasks.send_email", payload, channel="emails")
jobs.publish("myapp.tasks.process_image", payload, channel="heavy")
```

Configure channel workers via Django management commands. Default channel is `"default"`.

## Payload Limits

Payloads are stored in the database (not Redis). Keep payloads lean:

```python
# Good — pass IDs, fetch in job
jobs.publish("myapp.tasks.process_order", {"order_id": 42})

# Avoid — don't pass large objects
jobs.publish("myapp.tasks.process_order", {"order": huge_dict})  # bad
```

## Metrics

Each `publish()` call automatically records metrics:
- `jobs.published` (global)
- `jobs.channel.<channel>.published` (per channel)
