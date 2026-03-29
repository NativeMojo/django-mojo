# Jobs Documentation Rewrite

**Type**: docs
**Status**: Done
**Priority**: High
**Date**: 2026-03-26

## Problem

The `mojo.apps.jobs` documentation is incomplete, inaccurate, and poorly organized. Django developers and LLM agents cannot effectively use the jobs system from the current docs. Key features are undocumented, documented behavior doesn't match the code, and there's no clear "how to use this" flow.

## Specific Issues Found

### Factual errors in current docs

1. **`result` field**: Docs reference a `result` field on the Job model — it doesn't exist. Only `metadata` is persisted.
2. **`JOBS_PAYLOAD_MAX_BYTES`**: Docs say 1MB (1048576), code default is 16KB (16384).
3. **Return values**: Docs don't explain that job functions can return status strings (`'success'`, `'failed'`, `'cancelled'`).

### Completely undocumented features

1. **`broadcast=True`** parameter on `publish()` — sends job to ALL runners. Zero docs.
2. **`publish_webhook()`** — exists in manager.py but not covered in the main publishing docs (separate webhooks.md doesn't connect well).
3. **`publish_local()`** — thread-based local execution for dev/testing. Not documented.
4. **`broadcast_execute()`** — execute a function on all runners without creating a job. Not documented.
5. **`JobEvent` model** — full audit trail of job lifecycle events. Not documented.
6. **`JobLog` model** and `job.add_log()` — per-job logging. Not documented.
7. **`job.check_cancel_requested()`** — cooperative cancellation pattern. Not documented.
8. **`job.atomic_save()`** — safe concurrent saves. Not documented.
9. **`stack_trace` field** on Job model. Not documented.
10. **`max_exec_seconds`** — exists on model but not enforced. Docs shouldn't advertise it without caveat.
11. **REST control endpoints** — purge, clear-stuck, reset-failed, rebuild-scheduled, etc. Barely mentioned.
12. **`idempotency_key`** — exactly-once semantics. Exists but not well explained.
13. **Visibility timeout and processing ZSET** — how in-flight jobs are tracked. Not documented.

### Organization problems

- No clear "Quick Start" showing the simplest publish → consume flow
- Publishing docs scattered across multiple files without clear hierarchy
- Job function signature (`def my_job(job):`) buried instead of prominent
- Settings scattered — no single reference table
- No clear distinction between "developer writing jobs" and "ops managing jobs"
- Webhook docs are separate but don't reference back to the main system
- No examples of real-world usage patterns

## Preferred Publishing Style

The **string path** approach is preferred:

```python
from mojo.apps import jobs
jobs.publish("myapp.services.email.send_welcome", {"user_id": 42})
```

NOT the callable approach:

```python
from mojo.apps import jobs
from myapp.services.email import send_welcome
jobs.publish(send_welcome, {"user_id": 42})  # avoid this
```

String paths are cleaner, avoid circular imports, and make it obvious where the code lives.

## Proposed New Doc Structure

### `docs/django_developer/jobs/README.md` — Complete rewrite

Should cover in this order:
1. **What it is** — one paragraph
2. **Quick Start** — publish a job in 30 seconds (string path, preferred way)
3. **Writing job functions** — signature, what `job` gives you, return values
4. **Publishing** — all methods: `publish()`, `publish_webhook()`, `publish_local()`, `broadcast_execute()`
5. **Scheduling** — `delay`, `run_at`, how the scheduler works
6. **Retries** — backoff, max_retries, how failures are handled
7. **Cancellation** — `check_cancel_requested()` pattern
8. **Idempotency** — `idempotency_key` for exactly-once
9. **Broadcast** — `broadcast=True` for cluster-wide ops
10. **Logging** — `job.add_log()` and JobLog
11. **Link to sub-pages** for deep dives

### `docs/django_developer/jobs/publishing.md` — Rewrite

Full API reference for all publish functions with complete signatures, examples, and the preferred string-path style.

### `docs/django_developer/jobs/writing_jobs.md` — Rewrite

Focus on:
- Job function signature
- Accessing payload, metadata
- Return values and what they mean
- Long-running jobs with progress tracking
- Cancellation pattern
- Error handling
- Logging with `add_log()`

### `docs/django_developer/jobs/job_model.md` — Rewrite

Accurate model reference:
- All fields with correct types and defaults
- Remove `result` field reference
- Add `stack_trace`, `broadcast`, `idempotency_key`
- Document JobEvent and JobLog models
- RestMeta graphs

### `docs/django_developer/jobs/webhooks.md` — Rewrite

Connect to main system, show `publish_webhook()` as the entry point, document retry behavior, response handling, metrics.

### `docs/django_developer/jobs/settings.md` — NEW

Single reference for ALL settings with correct defaults:
- Job defaults (channels, retries, backoff, payload limit)
- Engine config (workers, claim batch, visibility timeout)
- Redis config (URL, prefix)
- Webhook config (retries, timeout, user agent)
- Debug/logging

### `docs/django_developer/jobs/admin.md` — NEW

REST control endpoints for ops:
- Queue sizes, channel discovery
- Clear stuck, purge old, reset failed
- Rebuild scheduled, cleanup consumers
- Runner management, sysinfo

## Acceptance Criteria

- [x] README gives a developer everything they need to publish and write their first job in under 2 minutes
- [x] Every publish function is documented with full signature and examples
- [x] Every Job model field is accurate (no phantom `result` field)
- [x] All settings have correct defaults matching the code
- [x] `broadcast`, `publish_webhook`, `publish_local`, `broadcast_execute` are all documented
- [x] JobEvent and JobLog are documented
- [x] Preferred string-path publish style is shown first everywhere
- [x] No misleading information about unimplemented features
- [ ] Web developer docs updated if REST API docs change

## Files to Rewrite

| File | Action |
|------|--------|
| `docs/django_developer/jobs/README.md` | Complete rewrite |
| `docs/django_developer/jobs/publishing.md` | Complete rewrite |
| `docs/django_developer/jobs/writing_jobs.md` | Complete rewrite |
| `docs/django_developer/jobs/job_model.md` | Complete rewrite |
| `docs/django_developer/jobs/webhooks.md` | Rewrite |
| `docs/django_developer/jobs/settings.md` | NEW — settings reference |
| `docs/django_developer/jobs/admin.md` | NEW — control endpoints |
