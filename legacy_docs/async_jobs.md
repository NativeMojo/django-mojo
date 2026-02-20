# Async Jobs and Cron (for Django + Mojo)

Cron is the timer; jobs do the work.

Per app, use two files:
- cronjobs.py — define “when to run” using cron schedules. Keep functions tiny (a few seconds max). Prefer publishing a job instead of doing real work here.
- asyncjobs.py — define job functions executed by the jobs engine. These do the heavy lifting, can retry, and can run in parallel.

Golden rule: cronjobs should almost always just publish jobs that point to functions in asyncjobs.py. Only do inline work if it is guaranteed to complete in a few seconds.

See also:
- docs/cron.md (scheduling details and runner)
- docs/jobs.md (jobs system and publish())

## Layout

Each Django app should provide both files:

    myapp/
    ├─ __init__.py
    ├─ models.py
    ├─ views.py
    ├─ asyncjobs.py   # job functions executed by the jobs engine
    └─ cronjobs.py    # scheduled functions (publish jobs, return fast)

The loader expects cron modules to be named cronjobs.py.

## Quickstart

1) Write job functions in asyncjobs.py (the work happens here)

```python
# myapp/asyncjobs.py
def send_daily_digest(job):
    tz = job.payload.get("tz", "UTC")
    # Build data and send the digest...
    job.metadata["status"] = "sent"
    return "completed"


def cleanup_old_data(job):
    days = int(job.payload.get("older_than_days", 30))
    # Delete/archival logic...
    job.metadata["deleted"] = 123
    return "completed"


def refresh_index(job):
    since = job.payload.get("since", "5m")
    # Reindex logic...
    job.metadata["since"] = since
    return "completed"
```

Notes:
- Jobs receive a Job model instance, can read job.payload and write job.metadata.
- Return a simple status string (e.g., "completed"); the engine updates status/timestamps.

2) Schedule tiny functions in cronjobs.py that publish jobs

```python
# myapp/cronjobs.py
from mojo.decorators.cron import schedule
from mojo.apps.jobs import publish  # See docs/jobs.md

@schedule(minutes="0", hours="9", weekdays="1-5")
def cron_send_daily_digest():
    # Publish job (recommended)
    publish("myapp.asyncjobs.send_daily_digest", {"tz": "America/Los_Angeles"})

@schedule(minutes="0", hours="2")
def cron_cleanup_old_data():
    publish("myapp.asyncjobs.cleanup_old_data", {"older_than_days": 30})

@schedule(minutes="*/5")
def cron_refresh_index():
    publish("myapp.asyncjobs.refresh_index", {"since": "5m"})
```

Why publish?
- Cron functions stay fast and reliable
- Jobs can retry, track progress, and run in parallel
- Workers can be scaled independently of the cron trigger

3) Load and run cron on a schedule

```python
from mojo.helpers.cron import load_app_cron, run_now

# Load all cron modules from installed apps (imports YOUR_APP.cronjobs)
load_app_cron()

# Invoke every minute (e.g., by a lightweight runner or management command)
run_now()
```

Typical deployment: a tiny process calls run_now() every minute. The cron system finds all scheduled functions that match “now” and executes them (your cron functions then publish jobs and return quickly).

## Scheduling syntax (cronjobs.py)

Use the `@schedule` decorator with crontab-like fields:
- minutes, hours, days, months, weekdays
- Patterns: "*", "5", "1,15,30", "1-5", "*/15"

Examples:
```python
@schedule(minutes="*/15")                  # every 15 minutes
@schedule(minutes="0", hours="2")          # daily at 02:00
@schedule(minutes="0", hours="9", weekdays="1-5")  # weekdays at 09:00
```

Tip: keep names clear (e.g., cron_send_daily_digest), and keep functions idempotent.

## Publishing jobs (cron → asyncjobs)

Use `publish()` to enqueue a job for the engine to execute:

```python
from mojo.apps.jobs import publish

publish("myapp.asyncjobs.some_job_function", {"param": "value"}, channel="default")
```

Recommendations:
- Use module path strings (e.g., "myapp.asyncjobs.send_daily_digest") to avoid import coupling.
- Keep payloads small and sufficient to reconstruct the work in the job function.
- Set channel if you use multiple queues.

Job function pattern (asyncjobs.py):
```python
def some_job_function(job):
    # Access input data
    data = job.payload
    # Update progress/metadata as needed
    job.metadata["progress"] = "started"
    # Do the work...
    return "completed"
```

## When is inline cron OK?

Inline work in cronjobs.py is OK only if:
- It’s guaranteed to finish in 1–2 seconds
- It doesn’t do significant I/O or CPU work
- It doesn’t need retries/parallelism/monitoring

Otherwise, publish a job.

Example (minimal inline):
```python
from mojo.decorators.cron import schedule

@schedule(minutes="30")
def cron_ping_healthcheck():
    # Very fast ping/check only; otherwise publish a job
    pass
```

## Best practices

- Keep cron functions idempotent and fast; prefer publish()
- Use clear, readable schedules (e.g., minutes="0", hours="2")
- Pass only necessary data in job payloads; compute inside the job
- In jobs, add guards (timeouts, batch sizes) and write progress to job.metadata
- Choose channels to isolate heavy workloads
- Log minimally in cron; do observability in job execution paths

## Troubleshooting

- Ensure your app is in INSTALLED_APPS
- Provide both files where needed:
  - cronjobs.py (schedules, calls publish)
  - asyncjobs.py (job functions)
- Call load_app_cron() on startup (it imports cronjobs.py per app)
- Ensure a runner calls run_now() every minute
- Publish with module path strings (e.g., "myapp.asyncjobs.fn") to decouple imports

## References

- Scheduling and runner: docs/cron.md
- Jobs engine and publish(): docs/jobs.md