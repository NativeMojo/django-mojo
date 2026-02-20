# Job Model — Django Developer Reference

## Model Fields

| Field | Type | Description |
|---|---|---|
| `id` | UUID | Job identifier |
| `func` | CharField | Module path of the job function |
| `payload` | JSONField | Input data (stored in DB only) |
| `channel` | CharField | Queue channel |
| `status` | CharField | `pending`, `running`, `completed`, `failed`, `cancelled` |
| `result` | JSONField | Return value from the job function |
| `error` | TextField | Exception message/traceback on failure |
| `metadata` | JSONField | Arbitrary progress/tracking data |
| `run_at` | DateTimeField | Scheduled execution time (null = immediate) |
| `started_at` | DateTimeField | When job execution began |
| `completed_at` | DateTimeField | When job finished |
| `created` | DateTimeField | When job was created |
| `cancel_requested` | BooleanField | Signal to the running job to stop |

## Job Status Values

| Status | Meaning |
|---|---|
| `pending` | Queued, not yet running |
| `running` | Currently executing |
| `completed` | Finished successfully |
| `failed` | Raised an unhandled exception |
| `cancelled` | Cancelled before or during execution |

## Querying Jobs

```python
from mojo.apps.jobs.models import Job

# All failed jobs in the last hour
from mojo.helpers import dates
failed = Job.objects.filter(
    status="failed",
    created__gte=dates.subtract(dates.utcnow(), hours=1)
)

# Jobs for a specific function
jobs = Job.objects.filter(func="myapp.services.email.send_welcome")

# Pending jobs in a channel
pending = Job.objects.filter(status="pending", channel="emails").order_by("created")
```

## Cancellation

```python
job = Job.objects.get(pk=job_id)
job.cancel_requested = True
job.atomic_save()
```

The running job function must check `job.cancel_requested` periodically to honour cancellations.

## RestMeta

```python
class RestMeta:
    VIEW_PERMS = ["view_taskqueue", "manage_users"]
    GRAPHS = {
        "list": {"fields": ["id", "func", "channel", "status", "created", "run_at"]},
        "default": {"fields": ["id", "func", "channel", "status", "created", "run_at",
                               "started_at", "completed_at", "result", "error", "metadata"]},
    }
```

## Management Commands

```bash
# Start job workers
python manage.py run_jobs --channel default --workers 4

# List pending jobs
python manage.py list_jobs --status pending

# Cancel a job
python manage.py cancel_job <job_id>
```
