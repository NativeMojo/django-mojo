# User-Scheduled Tasks

**Type**: request
**Status**: open
**Date**: 2026-04-06
**Priority**: high

## Description

Add a user-facing scheduled task system that lets users (and the Assistant) create simple recurring or one-off jobs. Users pick 1-2 times of day and optional days of week — not full cron syntax. Task types: LLM prompt, webhook, or generic job. Results stored in a TaskResult model, with opt-in notifications via existing channels (email, in_app, sms, push).

## Context

The existing `@schedule` decorator is code-level only. There is no way for end users or the Assistant to create, modify, or delete recurring jobs at runtime. Users want to schedule simple daily tasks like:

- "Send me an email every Sunday with merchants who haven't transacted in 4 days"
- "Check once a day for any merchant that has more than 4 transactions and 50% declined rate"

This is for simple once-or-twice-a-day tasks, not high-frequency scheduling.

## Design

### How It Works

1. User or Assistant creates a `ScheduledTask` — pick 1-2 times of day, optional days of week, task type + config
2. Existing system cron calls `run_now()` every minute → `@schedule(minutes="0")` function fires at top of each hour
3. That function queries enabled tasks, checks if any `run_times` fall in the current hour (converted from user's group timezone to UTC)
4. For matches, `jobs.publish(func="run_scheduled_task", run_at=<time>, idempotency_key=...)`
5. At execution time, job function loads task, confirms still enabled, executes, stores `TaskResult`, notifies user if configured
6. For `run_once` tasks, auto-disables after execution

### Edge Cases

- **Task created mid-hour**: Waits for next matching hour. Acceptable.
- **Task edited/disabled mid-hour**: Job checks `enabled` at execution time, skips if disabled.
- **Missed hour (restart)**: Just wait for next one.
- **Timezone**: Pull from `user.org.timezone` (defaults `America/Los_Angeles`). Convert run_times to UTC at dispatch.
- **Idempotency**: Key = `schtask:{task_id}:{run_at_timestamp}`. Safe to re-run hourly dispatch.

## Acceptance Criteria

- [ ] `ScheduledTask` model with owner, times, days, task type, config, enabled flag, run tracking
- [ ] `TaskResult` model stores execution output, owner-scoped read-only
- [ ] Hourly `@schedule(minutes="0")` cron function dispatches due tasks via `jobs.publish(run_at=...)`
- [ ] Job function checks task still enabled, executes by type, stores result, sends opt-in notifications
- [ ] `run_once` tasks auto-disable after first execution
- [ ] CRUD REST endpoints (owner-scoped)
- [ ] Assistant tools to create, list, update, delete scheduled tasks
- [ ] Timezone conversion using `user.org.timezone`

## Models

### ScheduledTask (`mojo/apps/jobs/models/scheduled_task.py`)

- `id` — CharField, UUID hex, PK
- `owner` — FK to User
- `name` — CharField, label
- `description` — TextField, blank
- `enabled` — BooleanField, default True
- `run_once` — BooleanField, default False (auto-disable after first run)
- `task_type` — CharField, choices: `job`, `webhook`, `llm`
- `run_times` — JSONField, list of `"HH:MM"` strings, max 2 (e.g. `["09:00", "17:00"]`)
- `run_days` — JSONField, list of weekday ints 0-6 (Mon=0), default `[]` = every day (Mon-Sun)
- `job_config` — JSONField:
  - `job`: `{"func": "...", "payload": {...}}`
  - `webhook`: `{"url": "...", "data": {...}}`
  - `llm`: `{"system_prompt": "...", "user_prompt": "..."}`
- `notify` — JSONField, default `[]`, opt-in list of channels: `"email"`, `"in_app"`, `"sms"`, `"push"`
- `channel` — CharField, default `"default"`
- `max_retries` — IntegerField, default 0
- `last_run` — DateTimeField, null
- `run_count` — IntegerField, default 0
- `last_error` — TextField, blank
- `created`, `modified`

RestMeta: `OWNER_FIELD = "owner"`, owner-scoped

### TaskResult (`mojo/apps/jobs/models/task_result.py`)

- `id` — CharField, UUID hex, PK
- `task` — FK to ScheduledTask
- `owner` — FK to User (denormalized)
- `job` — FK to Job, null
- `status` — CharField: `success`, `error`
- `output` — TextField
- `error` — TextField, blank
- `created`

RestMeta: owner-scoped, read-only

## Key Code

### Hourly Dispatch (`mojo/apps/jobs/cronjobs.py`)

- `@schedule(minutes="0")` function
- Queries `ScheduledTask.objects.filter(enabled=True)`
- For each task, gets user timezone via `task.owner.org.timezone`
- Converts each `run_times` entry to UTC, checks if it falls in the current hour
- Publishes with `jobs.publish(run_at=..., idempotency_key=...)`

### Job Function (`mojo/apps/jobs/asyncjobs.py`)

- `run_scheduled_task(job)` — loads task, checks enabled, dispatches by type
- For `llm` type: calls LLM with system/user prompts, stores result
- For `job` type: publishes the configured func+payload
- For `webhook` type: calls `jobs.publish_webhook()`
- Stores `TaskResult` on completion
- If `notify` has channels: delivers via existing methods (`user.notify()`, `user.send_email()`, `SMS.send()`, `user.push_notification()`)
- If `run_once`: sets `task.enabled = False`

### CRUD REST (`mojo/apps/jobs/rest/scheduled_task.py`)

Standard RestMeta CRUD. Validation: run_times format, max 2 entries, max tasks per user.

### Assistant Tools (`mojo/apps/assistant/services/tools/jobs.py`)

Add: `create_scheduled_task`, `list_scheduled_tasks`, `update_scheduled_task`, `delete_scheduled_task`

## Endpoints

| Method | Path | Description | Permission |
|---|---|---|---|
| GET | /api/scheduled_task | List user's tasks | jobs, view_scheduled_tasks, owner |
| POST | /api/scheduled_task | Create task | jobs, manage_scheduled_tasks |
| GET | /api/scheduled_task/\<pk\> | Task detail | jobs, view_scheduled_tasks, owner |
| POST | /api/scheduled_task/\<pk\> | Update task | jobs, manage_scheduled_tasks |
| DELETE | /api/scheduled_task/\<pk\> | Delete task | jobs, manage_scheduled_tasks |
| GET | /api/task_result | List user's results | jobs, view_scheduled_tasks, owner |
| GET | /api/task_result/\<pk\> | Result detail | jobs, view_scheduled_tasks, owner |

## Settings

| Setting | Default | Purpose |
|---|---|---|
| SCHEDULED_TASK_MAX_PER_USER | 10 | Max tasks per user |

## Files to Create/Modify

| File | Action |
|---|---|
| `mojo/apps/jobs/models/scheduled_task.py` | Create |
| `mojo/apps/jobs/models/task_result.py` | Create |
| `mojo/apps/jobs/models/__init__.py` | Modify — imports |
| `mojo/apps/jobs/cronjobs.py` | Modify — add dispatch_scheduled_tasks |
| `mojo/apps/jobs/asyncjobs.py` | Modify — add run_scheduled_task |
| `mojo/apps/jobs/rest/scheduled_task.py` | Create |
| `mojo/apps/jobs/rest/__init__.py` | Modify — imports |
| `mojo/apps/assistant/services/tools/jobs.py` | Modify — add tools |
| Tests | `tests/test_jobs/test_scheduled_task.py` |
| Docs | Both tracks |

## Existing Code Reused

- `mojo/decorators/cron.py` — `@schedule` decorator
- `mojo/helpers/cron.py` — `run_now()` called by system cron every minute
- `mojo/apps/jobs/__init__.py` — `jobs.publish(run_at=..., idempotency_key=...)`
- `mojo/apps/jobs/scheduler.py` — existing Redis ZSET scheduler handles `run_at` jobs
- `mojo/apps/account/models/user.py` — `user.notify()`, `user.send_email()`, `user.push_notification()`
- `mojo/apps/phonehub/models/sms.py` — `SMS.send()` for text delivery
- `mojo/apps/account/models/group.py` — `group.timezone` property for user timezone

## Tests Required

- CRUD: create, read, update, delete via REST
- Owner scoping: user A cannot see/edit user B's tasks
- Dispatch logic: hourly function correctly identifies matching tasks
- Timezone conversion: run_times in user timezone dispatched at correct UTC time
- run_once: auto-disables after execution
- Notification delivery: respects opt-in channels
- Idempotency: dispatch does not double-publish
- Task disabled check: job skips execution if task disabled mid-hour
- Max tasks per user enforced
- Assistant tools: create/list/update/delete

## Out of Scope

- Full cron syntax (just times + days)
- High-frequency scheduling (min once per day)
- UI/frontend (REST + assistant tools only)
- Task dependencies / DAG workflows
- Catch-up on missed hours
- Immediate execution on task create (waits for next matching hour)
