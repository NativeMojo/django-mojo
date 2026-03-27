# Job Model — Django Developer Reference

## Job

Primary model for all jobs. Stored in PostgreSQL as the source of truth.

```python
from mojo.apps.jobs.models import Job
```

### Fields

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `id` | CharField(32), PK | Auto UUID | 32-char UUID without dashes |
| `channel` | CharField(100) | `"default"` | Queue channel |
| `func` | CharField(255) | — | Dotted module path to job function |
| `payload` | JSONField | `{}` | Input data dict |
| `status` | CharField(16) | `"pending"` | Job lifecycle state |
| `run_at` | DateTimeField | `None` | Scheduled execution time (null = immediate) |
| `expires_at` | DateTimeField | `None` | Expiration time |
| `attempt` | IntegerField | `0` | Current attempt number |
| `max_retries` | IntegerField | `0` | Max retry attempts |
| `backoff_base` | FloatField | `2.0` | Exponential backoff base |
| `backoff_max_sec` | IntegerField | `3600` | Max backoff seconds |
| `broadcast` | BooleanField | `False` | Execute on all runners |
| `cancel_requested` | BooleanField | `False` | Cancellation signal |
| `max_exec_seconds` | IntegerField | `None` | Execution time limit (advisory) |
| `runner_id` | CharField(64) | `None` | Runner currently executing this job |
| `last_error` | TextField | `""` | Last exception message |
| `stack_trace` | TextField | `""` | Full traceback on failure |
| `metadata` | JSONField | `{}` | Arbitrary progress/tracking data |
| `created` | DateTimeField | auto | When job was created |
| `modified` | DateTimeField | auto | Last modified |
| `started_at` | DateTimeField | `None` | When execution began |
| `finished_at` | DateTimeField | `None` | When execution ended |
| `idempotency_key` | CharField(64), unique | `None` | Prevents duplicate execution |

### Status Values

| Status | Meaning |
|--------|---------|
| `pending` | Queued, waiting for a runner |
| `running` | Currently executing on a runner |
| `completed` | Finished successfully |
| `failed` | Raised exception or returned `"failed"` |
| `canceled` | Cancelled before or during execution |
| `expired` | Not executed before `expires_at` |

### Properties

| Property | Returns | Description |
|----------|---------|-------------|
| `is_terminal` | bool | True if status in (`completed`, `failed`, `canceled`, `expired`) |
| `is_retriable` | bool | True if `failed` and `attempt < max_retries` |
| `duration_ms` | int or None | Milliseconds between `started_at` and `finished_at` |
| `is_expired` | bool | True if `expires_at` is past |

### Methods

| Method | Description |
|--------|-------------|
| `check_cancel_requested()` | Refresh from DB, return `cancel_requested` value |
| `add_log(message, kind="info", meta=None)` | Create a JobLog entry for this job |
| `atomic_save()` | Concurrent-safe save |

### REST Actions (POST_SAVE_ACTIONS)

| Action | Description |
|--------|-------------|
| `cancel_request` | Request cancellation |
| `retry_request` | Reset and re-publish a failed job |
| `get_status` | Get detailed status with recent events |
| `publish_job` | Create a new job from this one as a template |

### RestMeta

```python
class RestMeta:
    VIEW_PERMS = ["view_jobs", "manage_jobs"]
    SAVE_PERMS = ["manage_jobs"]
    DELETE_PERMS = ["manage_jobs"]
    GRAPHS = {
        "default": {...},   # Core fields
        "detail": {...},    # All fields including payload, metadata, errors
        "status": {...},    # Status-focused subset
        "admin": {...},     # Full admin view
    }
```

### Querying

```python
from mojo.apps.jobs.models import Job
from mojo.helpers import dates

# Failed jobs in the last hour
failed = Job.objects.filter(
    status="failed",
    created__gte=dates.subtract(dates.utcnow(), hours=1),
)

# Jobs for a specific function
welcome_jobs = Job.objects.filter(func="myapp.services.email.send_welcome")

# Pending jobs in a channel
pending = Job.objects.filter(
    status="pending",
    channel="emails",
).order_by("created")

# Running jobs on a specific runner
running = Job.objects.filter(
    status="running",
    runner_id="runner-host1-abc123",
)

# Jobs with a specific idempotency key
job = Job.objects.filter(idempotency_key="invoice_42_2026_03").first()
```

---

## JobEvent

Append-only audit log of job state transitions. Created by the engine — not user-writable.

```python
from mojo.apps.jobs.models import JobEvent
```

### Fields

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `id` | AutoField, PK | Auto | |
| `job` | ForeignKey(Job) | — | Related job (related_name: `events`) |
| `channel` | CharField(100) | — | Channel at time of event |
| `event` | CharField(24) | — | Event type |
| `at` | DateTimeField | auto | When the event occurred |
| `runner_id` | CharField(64) | `None` | Runner that triggered the event |
| `attempt` | IntegerField | `0` | Attempt number at time of event |
| `details` | JSONField | `{}` | Additional event data |
| `created` | DateTimeField | auto | |
| `modified` | DateTimeField | auto | |

### Event Types

| Event | When it fires |
|-------|---------------|
| `created` | Job record created |
| `queued` | Job pushed to Redis queue |
| `scheduled` | Job added to scheduled ZSET |
| `claimed` | Runner claimed the job from queue |
| `running` | Job execution started |
| `completed` | Job finished successfully |
| `failed` | Job failed (exception or returned `"failed"`) |
| `retry` | Job scheduled for retry after failure |
| `canceled` | Job was cancelled |
| `expired` | Job expired before execution |
| `released` | Job released back to queue (runner died, visibility timeout) |

### Querying

```python
# Full timeline for a job
events = JobEvent.objects.filter(job_id=job_id).order_by("at")

# All failures in the last hour
failures = JobEvent.objects.filter(
    event="failed",
    at__gte=dates.subtract(dates.utcnow(), hours=1),
)
```

### RestMeta

```python
class RestMeta:
    VIEW_PERMS = ["manage_jobs", "view_jobs"]
    SAVE_PERMS = []  # System-created only
    DELETE_PERMS = ["manage_jobs"]
    GRAPHS = {
        "default": {...},
        "detail": {...},
        "timeline": {...},
    }
```

---

## JobLog

Per-job log entries created via `job.add_log()`. Append-only.

```python
from mojo.apps.jobs.models import JobLog
```

### Fields

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `id` | AutoField, PK | Auto | |
| `job` | ForeignKey(Job) | — | Related job (related_name: `logs`) |
| `channel` | CharField(100) | — | Channel at time of log |
| `created` | DateTimeField | auto | When the log was created |
| `kind` | CharField(16) | `"info"` | Log level |
| `message` | TextField | — | Log message |
| `meta` | JSONField | `{}` | Structured context data |

### Log Kinds

| Kind | Use for |
|------|---------|
| `debug` | Detailed diagnostic info |
| `info` | Normal progress updates |
| `warn` | Recoverable issues |
| `error` | Failures that need attention |

### Creating Logs

Always use `job.add_log()` inside a job function:

```python
def my_job(job):
    job.add_log("Starting processing", kind="info")
    job.add_log("Found 42 items", kind="info", meta={"count": 42})
    job.add_log("Skipped invalid record", kind="warn", meta={"record_id": 7})
```

### Querying

```python
# All logs for a job
logs = JobLog.objects.filter(job_id=job_id).order_by("created")

# Error logs across all jobs
errors = JobLog.objects.filter(
    kind="error",
    created__gte=dates.subtract(dates.utcnow(), hours=1),
)
```

### RestMeta

```python
class RestMeta:
    VIEW_PERMS = ["manage_jobs", "view_jobs"]
    SAVE_PERMS = []  # Use add_log() instead
    DELETE_PERMS = ["manage_jobs"]
```
