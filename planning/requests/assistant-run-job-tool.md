# Assistant Run Job & Run Scheduled Task Tools

**Type**: request
**Status**: planned
**Date**: 2026-04-07
**Priority**: medium

## Description
Add two assistant tools to fill gaps in the jobs domain:
1. `run_job` — publish a new job (fresh by func+payload, or rerun from existing job as template)
2. `run_scheduled_task_now` — immediately execute a scheduled task regardless of schedule or enabled state

## Context
The assistant has 11 job tools covering query, stats, health, cancel, retry, and scheduled task CRUD. But there's no way to actually *run* a job or trigger a scheduled task on demand.

- **`retry_job`** only works on failed/canceled/expired jobs, resets the same record
- **`create_scheduled_task`** is indirect/heavyweight for "run this now"
- **`publish_job_from_template`** exists in the service layer and REST but has no assistant tool
- **`run_scheduled_task`** asyncjob exists but only fires via hourly cron, and skips disabled tasks

User story: "Create a task that runs now that generates a CSV of most recent transactions and emails it to me." Today this requires creating a scheduled task and waiting for the cron to pick it up.

## Acceptance Criteria
- `run_job` tool: publish a job by `func`+`payload` (fresh) or `job_id` (rerun from template)
- `run_scheduled_task_now` tool: immediately publish a job that executes a scheduled task
- Both require `manage_jobs`, both `mutates=True`
- `run_job` validates `func` is importable via `load_job_function` before publishing
- `run_scheduled_task_now` works even if the task is disabled (`force` flag)
- Both return the new job ID

## Investigation
**What exists**:
- `jobs.publish(func, payload, channel, ...)` — core publish API (`mojo/apps/jobs/__init__.py`)
- `JobActionsService.publish_job_from_template(job, overrides)` — rerun logic (`mojo/apps/jobs/services/job_actions.py:406`)
- `load_job_function(func_path)` — dynamic import + validation (`mojo/apps/jobs/job_engine.py:46`)
- `run_scheduled_task(job)` — asyncjob that dispatches by task type (`mojo/apps/jobs/asyncjobs.py:14`)
- 11 existing tools in `mojo/apps/assistant/services/tools/jobs.py`
- Scheduled task tools (list, create, update, delete) exist in code but are missing from docs

**What changes**:
- `mojo/apps/assistant/services/tools/jobs.py` — add 2 tool definitions
- `mojo/apps/jobs/asyncjobs.py` — add `force` flag to bypass enabled check
- `docs/django_developer/assistant/README.md` — update jobs domain table (add 6 missing tools)
- `docs/web_developer/assistant/README.md` — same if matching table exists

**Constraints**:
- `func` must be validated via `load_job_function` at tool call time — fail fast, don't create a broken job
- `force` flag only applies when explicitly set in payload — normal cron path unchanged
- Must require `manage_jobs` + `mutates=True` on both tools

**Related files**:
- `mojo/apps/assistant/services/tools/jobs.py`
- `mojo/apps/jobs/__init__.py`
- `mojo/apps/jobs/asyncjobs.py`
- `mojo/apps/jobs/job_engine.py` (load_job_function)
- `mojo/apps/jobs/services/job_actions.py` (publish_job_from_template)
- `mojo/apps/jobs/models/scheduled_task.py`
- `docs/django_developer/assistant/README.md`

## Tests Required
- `run_job` with `func` + `payload` publishes a new job, returns job ID
- `run_job` with `job_id` creates new job from template
- `run_job` with `job_id` + `payload` override merges payload correctly
- `run_job` with invalid `func` (not importable) returns validation error
- `run_job` with both `func` and `job_id` returns error
- `run_job` with neither `func` nor `job_id` returns error
- `run_job` with nonexistent `job_id` returns error
- `run_scheduled_task_now` publishes job for enabled task
- `run_scheduled_task_now` publishes job for disabled task (force bypass)
- `run_scheduled_task_now` with nonexistent `task_id` returns error
- `run_scheduled_task_now` returns trackable job ID

## Out of Scope
- Modifying existing `retry_job` tool behavior
- Job function registry (formal registration system) — validation uses dynamic import
- Changes to scheduled task cron dispatch logic

## Plan

**Status**: planned
**Planned**: 2026-04-07

### Objective
Add `run_job` and `run_scheduled_task_now` assistant tools so users can publish jobs and trigger scheduled tasks on demand.

### Steps
1. `mojo/apps/jobs/asyncjobs.py` — Add `force` flag check before the `enabled` gate at line 37. If `job.payload.get("force")` is truthy, skip the enabled check. Normal cron path unchanged.

2. `mojo/apps/assistant/services/tools/jobs.py` — Add `run_job` tool:
   - Input: `func` (optional string), `payload` (optional object), `channel` (optional string, default "default"), `delay` (optional int seconds), `job_id` (optional string). Exactly one of `func` or `job_id` required.
   - If `func`: validate via `load_job_function(func)`, then `jobs.publish(func, payload, channel, delay)`.
   - If `job_id`: load Job, call `JobActionsService.publish_job_from_template(job, overrides)` with any provided payload/channel/delay.
   - Permission: `manage_jobs`, `mutates=True`.

3. `mojo/apps/assistant/services/tools/jobs.py` — Add `run_scheduled_task_now` tool:
   - Input: `task_id` (required string).
   - Load ScheduledTask by ID, scoped to user (`user=user`).
   - Publish: `jobs.publish(func="mojo.apps.jobs.asyncjobs.run_scheduled_task", payload={"task_id": task.id, "force": True})`.
   - Return the new job ID.
   - Permission: `manage_jobs`, `mutates=True`.

4. `docs/django_developer/assistant/README.md` — Update the Jobs Domain table to include all 13 tools (add the 4 existing scheduled task tools + 2 new tools).

5. `docs/web_developer/assistant/README.md` — Mirror the jobs domain table update if a matching section exists.

6. `tests/test_assistant/` — Add test file for the new tools covering all scenarios from "Tests Required" above.

### Design Decisions
- **Validate func at call time**: Use `load_job_function` to try the import before publishing. Fail fast with a clear error rather than creating a pending job that will fail at execution.
- **`force` flag in payload, not a new asyncjob**: Reuses the existing `run_scheduled_task` function. One small conditional addition vs. duplicating the entire dispatch/result/notification flow.
- **User-scoped task lookup**: `run_scheduled_task_now` filters by `user=user` so users can only trigger their own tasks. Admins with `manage_jobs` still need to own the task.
- **Exactly one of `func` or `job_id`**: Cleaner than merging both — avoids ambiguity about which `func` wins.

### Edge Cases
- **Bad func path**: `load_job_function` raises `ImportError` — caught and returned as `{"ok": False, "error": "..."}`.
- **Both `func` and `job_id` provided**: Rejected with validation error before any work.
- **Neither provided**: Same.
- **Disabled task with `run_now`**: Runs anyway — `force=True` bypasses the enabled check. Task's `run_count` and `last_run` still update normally.
- **`run_once` task triggered manually**: If task is `run_once` and already disabled from a previous run, `force` still runs it. The `run_once` auto-disable logic fires again on success (no-op since already disabled).

### Testing
- New tool scenarios -> `tests/test_assistant/test_job_tools.py` (or add to existing test file if one covers job tools)

### Docs
- `docs/django_developer/assistant/README.md` — Add 6 missing tools to Jobs Domain table
- `docs/web_developer/assistant/README.md` — Mirror if applicable
