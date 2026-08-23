# Scheduled Tasks — Django Developer Reference

User-defined recurring or one-off tasks that run at specific times of day on specific days of the week. The system dispatches tasks via the existing job engine — no separate process required.

## Models

```python
from mojo.apps.jobs.models import ScheduledTask, TaskResult
```

### ScheduledTask

Represents one user-defined task. Owner-scoped: each task belongs to an `account.User`.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `id` | CharField(32), PK | Auto UUID | 32-char UUID without dashes |
| `user` | ForeignKey(User) | — | Owning user (CASCADE on delete) |
| `name` | CharField(255) | — | Human-readable label |
| `description` | TextField | `""` | Optional description |
| `enabled` | BooleanField | `True` | Whether the task is active |
| `run_once` | BooleanField | `False` | Auto-disable after first successful execution |
| `task_type` | CharField(16) | — | `"job"`, `"webhook"`, or `"llm"` |
| `run_times` | JSONField | `[]` | List of `"HH:MM"` strings (max 2) in user's local time |
| `run_days` | JSONField | `[]` | Weekday ints 0–6 (Mon=0). Empty = every day |
| `job_config` | JSONField | `{}` | Type-specific config (see below) |
| `notify` | JSONField | `[]` | Opt-in notification channels on success |
| `channel` | CharField(100) | `"default"` | Job engine channel for dispatched jobs |
| `max_retries` | IntegerField | `0` | Max retries for dispatched jobs |
| `last_run` | DateTimeField | `None` | When the task last ran |
| `run_count` | IntegerField | `0` | Total executions |
| `last_error` | TextField | `""` | Error message from last execution |
| `created` | DateTimeField | auto | Creation timestamp |
| `modified` | DateTimeField | auto | Last modification timestamp |

#### RestMeta

```
OWNER_FIELD  = "user"
VIEW_PERMS   = ["jobs", "view_scheduled_tasks", "owner"]
SAVE_PERMS   = ["jobs", "manage_scheduled_tasks", "owner"]
DELETE_PERMS = ["jobs", "manage_scheduled_tasks", "owner"]
```

Owners can create, read, update, and delete their own tasks without any elevated permission. Admin users with `view_scheduled_tasks` or `manage_scheduled_tasks` can access all tasks.

#### Graphs

| Graph | Fields |
|-------|--------|
| `list` | `id`, `name`, `enabled`, `run_once`, `task_type`, `run_times`, `run_days`, `last_run`, `run_count`, `created` |
| `default` | All fields including `job_config`, `notify`, `channel`, `max_retries`, `last_error`, `description`, `modified` |

### TaskResult

Read-only record of a single task execution. Created automatically by `run_scheduled_task`.

| Field | Type | Description |
|-------|------|-------------|
| `id` | CharField(32), PK | 32-char UUID |
| `task` | ForeignKey(ScheduledTask) | The task that ran (CASCADE) |
| `user` | ForeignKey(User) | Owner, denormalized from task (CASCADE) |
| `job` | ForeignKey(Job) | The job that executed the task (SET_NULL) |
| `status` | CharField(16) | `"success"` or `"error"` |
| `output` | TextField | Result content — LLM response text, webhook job ID, etc. (max 50 000 chars) |
| `error` | TextField | Error message if status is `"error"` (max 2 000 chars) |
| `created` | DateTimeField | When the result was recorded |

#### RestMeta

```
OWNER_FIELD  = "user"
VIEW_PERMS   = ["jobs", "view_scheduled_tasks", "owner"]
SAVE_PERMS   = []   # read-only via REST
DELETE_PERMS = ["jobs", "manage_scheduled_tasks"]
```

Results are never created or updated via REST. Deletion requires `manage_scheduled_tasks`.

---

## Schedule Format

`run_times` is a list of one or two `"HH:MM"` strings in the user's local timezone (resolved via `user.org.timezone`; falls back to `"America/Los_Angeles"`).

```python
run_times = ["09:00"]            # once a day at 9 AM
run_times = ["09:00", "17:00"]   # twice a day, 9 AM and 5 PM
```

`run_days` is a list of weekday integers (Monday = 0, Sunday = 6). An empty list means every day.

```python
run_days = []                      # every day
run_days = [0, 1, 2, 3, 4]        # weekdays only
run_days = [5, 6]                  # weekends only
```

### Validation rules

- `run_times` must have at most 2 entries.
- Each entry must match `"HH:MM"` exactly (24-hour clock).
- Each entry in `run_days` must be an int 0–6.
- `task_type` must be `"job"`, `"webhook"`, or `"llm"`.
- `notify` channels must be from `["email", "in_app", "sms", "push"]`.
- A user may have at most `SCHEDULED_TASK_MAX_PER_USER` tasks (default `10`).

---

## Task Types and job_config

### job

Publishes a job to the job engine.

```python
job_config = {
    "func": "myapp.services.report.generate",   # required
    "payload": {"user_id": 42},                  # optional
}
```

### webhook

Posts a webhook via `jobs.publish_webhook`.

```python
job_config = {
    "url": "https://api.partner.com/events",   # required
    "data": {"event": "daily_report"},          # optional
}
```

### llm

Runs an LLM prompt and stores the response in `TaskResult.output`.

```python
job_config = {
    "system_prompt": "You are a daily summary assistant.",   # optional
    "user_prompt": "Summarize today's headlines.",           # required
}
```

---

## Notification Channels

Set `notify` to a list of channels to receive a notification after a successful run. An empty list (the default) means no notifications.

| Channel | Delivery |
|---------|----------|
| `"in_app"` | `user.notify(...)` |
| `"email"` | `user.send_email(...)` |
| `"push"` | `user.push_notification(...)` |
| `"sms"` | `SMS.send(...)` via phonehub (only if user has a `phone`) |

Notification failures are logged but do not affect the task result.

---

## Cron: dispatch_scheduled_tasks

### Fleet-once: one node runs each function, not all of them

Every node in a fleet installs the same per-minute cron entry, so without a
guard each `@schedule` function would run once per node — concurrently, since
the nodes tick within the same second.

`run_now()` therefore **claims each matched function for its wall-clock
minute** before running it:

```
SET mojo:cron:once:<module>.<function>:<YYYYMMDDHHMM> <node> NX EX 120
```

The node that wins the claim runs the function; the others skip it. This is
the default — a function says nothing and is fleet-once.

Opt out for work that is genuinely local to one machine:

```python
@schedule(minutes="*/10", per_node=True)
def converge_this_node():
    ...
```

**A function that only publishes a job is not per-node.** The job's own
delivery decides its fan-out: a normal publish is consumed once by one runner,
and `broadcast=True` reaches every runner from whichever node dispatched it.
Marking such a dispatcher `per_node=True` gives you N dispatches instead of
one — and with a broadcast, N² deliveries. None of the framework's own
scheduled functions are marked per-node for this reason.

Three properties worth knowing:

- **Redis failure runs the function.** Every failure path — including the
  connection being misconfigured — falls back to running it. Unreachable Redis
  degrades to single-node behavior, never to silence: a missed certificate
  renewal is worse than a duplicated sweep.
- **A dead winner cannot wedge later minutes.** The claim only expires; it is
  never released. The next minute is a different key.
- **The 120s TTL is also the fleet's clock-skew budget.** Nodes must share a
  timezone (the matcher uses local time) and stay well inside that skew — a
  node lagging more than the TTL reaches a minute whose claim already expired
  and runs it a second time. NTP keeps real fleets far inside this.

Two consequences for anything that calls `run_now()` by hand:

- Running it in the same minute as crond's tick **skips** the functions crond
  already claimed. That is correct, and the heartbeat's `skipped` list shows
  it. `aws-check`'s remediation command still works — if crond is dead, nobody
  holds the claims.
- A test or shell that calls `run_now()` repeatedly inside one minute against
  a real Redis will skip on the repeat. Pin it with the seams:
  `run_now(now=<a distinct minute>)` or `run_now(redis_client=<a stub>)`.

### Dispatcher heartbeat

`mojo.helpers.cron.run_now()` records an expiring per-run Redis heartbeat at
start and completion. Run IDs are isolated, so an older overlapping completion
cannot hide a newer failed/incomplete invocation. Redis heartbeat failures are
logged but never stop scheduled functions.

The record is `version: 2` and names the reporting node plus what it actually
did, so a fleet skip is never mistaken for a failure:

| Field | Meaning |
|-------|---------|
| `node` | Hostname of the node that recorded this run |
| `matched_count` | Functions whose schedule matched this minute |
| `completed_count` | Functions this node actually executed |
| `skipped_count` | Functions another node had already claimed |
| `executed` | Qualified names this node ran |
| `skipped` | Qualified names this node yielded to another node |

On an N-node fleet each node writes its own record every minute, and only one
of them lists a given fleet-once function under `executed`. A run whose
functions were all claimed elsewhere still ends `state: "completed"` with
`failed_count: 0` — it did its job by correctly doing nothing.

Audit the external dispatcher, jobs runners and scheduler together with:

```bash
python manage.py aws-check --section cron --check
```

The external scheduler should invoke
`cron.load_app_cron(); cron.run_now()` at least once per minute; `aws-check`
prints the exact shell command when no fresh heartbeat exists.

```python
# mojo/apps/jobs/cronjobs.py
@schedule(minutes="0")
def dispatch_scheduled_tasks(now=None):
    ...
```

`now` defaults to `timezone.now()`. Pass an aware UTC datetime to dispatch as of a simulated instant — the cron runner always calls it with no arguments, so the scheduled path is unchanged. Tests use this to drive dispatch deterministically instead of racing the wall clock.

Runs at the top of every hour. For each enabled `ScheduledTask`:

1. Checks `task.matches_day(weekday)` — skips if the task does not run today.
2. Converts the current UTC time to the user's local timezone.
3. Calls `task.get_run_times_for_hour(local_hour)` to find matching `run_times`.
4. Builds `run_at` in the user's timezone and converts to UTC.
5. Skips any `run_at` already in the past.
6. Publishes via `jobs.publish(func="...run_scheduled_task", run_at=run_at_utc, idempotency_key=...)`.

An idempotency key (`schtask:<task_id>:<unix_ts>`) prevents double-dispatch if the cron fires more than once in the same hour.

---

## Async Job: run_scheduled_task

```python
# mojo/apps/jobs/asyncjobs.py
def run_scheduled_task(job):
    ...
```

Called by the job engine when the scheduled time arrives. `job.payload` must contain `task_id`.

Execution flow:

1. Load `ScheduledTask` — abort if missing or disabled.
2. Dispatch by `task_type` (`_run_llm_task`, `_run_webhook_task`, `_run_job_task`).
3. Create a `TaskResult` with `status`, `output`, and `error`.
4. Update `task.last_run`, `task.run_count`, `task.last_error`.
5. If `run_once=True` and status is `"success"`, set `task.enabled = False`.
6. Send opt-in notifications if `task.notify` is set and status is `"success"`.

---

## Configuration

| Setting | Default | Description |
|---------|---------|-------------|
| `SCHEDULED_TASK_MAX_PER_USER` | `10` | Maximum tasks a single user can create |

Add to your `settings.py`:

```python
SCHEDULED_TASK_MAX_PER_USER = 25  # increase the per-user cap
```
