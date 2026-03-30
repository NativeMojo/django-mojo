# execute_handler receives Job instance but expects dict payload

**Type**: bug
**Status**: open
**Date**: 2026-03-30
**Severity**: critical

## Description
`mojo.apps.incident.handlers.event_handlers.execute_handler` crashes at runtime with `'Job' object has no attribute 'get'`. The function signature expects a `payload` dict but the job engine passes a `Job` model instance.

## Context
Every incident handler (email, SMS, notify, block, ticket, LLM) dispatched via `RuleSet.run_handler()` fails silently because `execute_handler` crashes before it can parse the handler spec. This means **no incident handlers actually execute** in production when triggered via the job queue. The handlers work only when called directly (e.g., in tests).

The root cause of why testing didn't catch this: tests call `execute_handler({"handler_spec": ...})` directly with a dict, bypassing the job engine entirely. The real production path is `jobs.publish()` → Job DB row → job engine → `func(job)` where `job` is a Job model instance. Tests never exercise that path.

## Acceptance Criteria

### 1. Fix execute_handler signature
- `execute_handler` accepts a `Job` instance and reads `job.payload` to extract `handler_spec`, `event_id`, and `incident_id`
- All handler types (job, email, sms, notify, block, ticket, llm) execute successfully when dispatched via the job queue

### 2. Add `th.run_pending_jobs()` testit helper
- New helper in `testit/helpers.py` that simulates the job engine without Redis or a running engine process
- Queries `Job.objects.filter(status="pending")`, optionally filtered by channel
- For each job: imports the function via `load_job_function(job.func)`, calls `func(job)` — exactly like `job_engine.py:642`
- Marks the job completed on success, failed on exception
- Returns count of jobs executed
- This ensures tests exercise the **real calling convention** (`func(job)`) so signature mismatches like this bug are caught immediately

### 3. Update incident handler tests to use real job path
- Tests should call `jobs.publish(...)` then `th.run_pending_jobs()` instead of calling `execute_handler(dict)` directly
- This validates the full pipeline: publish → Job row → function dispatch → handler execution

## Investigation
**Likely root cause**: `execute_handler(payload)` at `event_handlers.py:471` treats its argument as a dict (`payload.get("handler_spec")` at line 482). But the job engine at `job_engine.py:642` calls `func(job)` where `job` is a `Job` model instance. The payload dict is stored in `job.payload`.

**Confidence**: confirmed

**Code path**:
1. `mojo/apps/incident/models/rule.py:162` — `jobs.publish(...)` creates a Job with `payload={"handler_spec": spec, "event_id": ..., "incident_id": ...}`
2. `mojo/apps/jobs/__init__.py:80-130` — `publish()` creates a `Job` row with the payload stored in `Job.payload` (JSONField)
3. `mojo/apps/jobs/job_engine.py:641-642` — `func = load_job_function(job.func); func(job)` passes the Job instance
4. `mojo/apps/incident/handlers/event_handlers.py:471-482` — `execute_handler(payload)` calls `payload.get("handler_spec")` which fails because `payload` is a `Job`, not a dict

**Regression test**: now feasible with `th.run_pending_jobs()` helper

**Related files**:
- `mojo/apps/incident/handlers/event_handlers.py` — `execute_handler()` needs to accept `job` and read `job.payload`
- `mojo/apps/jobs/job_engine.py:642` — confirms the calling convention: `func(job)`
- `mojo/apps/jobs/models/job.py:24` — `payload = models.JSONField(...)` confirms the data location
- `testit/helpers.py` — add `run_pending_jobs()` helper
- `tests/test_incident/rule_engine_comprehensive.py` — update handler tests to use job path

## Plan

### Step 1: Add `th.run_pending_jobs()` to testit/helpers.py
```python
def run_pending_jobs(channel=None, status="pending"):
    """
    Execute pending jobs from the DB the same way the job engine does.
    No Redis or running engine needed.
    Returns count of jobs executed.
    """
    from mojo.apps.jobs.models import Job
    from mojo.apps.jobs.job_engine import load_job_function

    qs = Job.objects.filter(status=status)
    if channel:
        qs = qs.filter(channel=channel)
    qs = qs.order_by("created")

    count = 0
    for job in qs:
        func = load_job_function(job.func)
        try:
            func(job)
            job.status = "completed"
            job.save(update_fields=["status", "modified"])
        except Exception:
            job.status = "failed"
            job.save(update_fields=["status", "modified"])
        count += 1
    return count
```

### Step 2: Fix execute_handler signature
```python
def execute_handler(job):
    payload = job.payload
    spec = payload.get("handler_spec")
    ...
```

### Step 3: Update tests
Replace direct `execute_handler(dict)` calls with `jobs.publish(...)` + `th.run_pending_jobs()` to exercise the real path.
