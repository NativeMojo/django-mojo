---
id:
type: bug
title: "test_jobs.test_scheduled_task flakes when the suite runs during the :59 minute (fallback picks a past minute)"
priority: P3
owner: backend
opened: 2026-07-18
depends_on: []
related: [DM-051]
links: []
---

# test_scheduled_task minute-:59 boundary flake

## What & Why
Two tests in `tests/test_jobs/test_scheduled_task.py` are time-of-day
dependent and fail if the suite happens to execute during the `:59` minute of
any hour:

- `dispatch_scheduled_tasks` — `tests/test_jobs/test_scheduled_task.py:352`
- `dispatch_idempotency` — `tests/test_jobs/test_scheduled_task.py:408`

Both compute a target minute with:

```python
target_minute = 59 if current_minute < 59 else 58   # :375 and :424
```

When the test runs at minute `:59`, the fallback picks minute `58`, which is
already **in the past** within the current hour. `mojo/apps/jobs/cronjobs.py:61`
(`if run_at_utc < now: continue`) then correctly skips publishing a job whose
scheduled time has already passed — so the test's own "a job was published"
assertion fails. The production `dispatch_scheduled_tasks` is behaving
correctly; the bug is in the test's minute-boundary fallback arithmetic.

Surfaced by the DM-051 post-build test-runner: the default suite showed these 2
red only because that run happened to cross the `:59` minute. Re-running
`test_jobs` alone immediately after (a minute later) was 54/54 green, and the
same suite run before/after the window is fully green — a reproducibly
non-failing-outside-that-one-minute flake, unrelated to DM-051 (both files
untouched since `481a76e8`, well before DM-051).

## Acceptance Criteria
- [ ] The two tests pass deterministically regardless of the wall-clock minute
      they run at (including `:59`).
- [ ] Fix the test's target-minute selection so it never picks a minute that is
      already in the past for the current hour (e.g. schedule into the next
      hour, or anchor to a controlled/frozen clock rather than `now`).
- [ ] Do not weaken the production `run_at_utc < now` skip in
      `mojo/apps/jobs/cronjobs.py` — that behavior is correct.

## Repro — bugs only
1. Run `bin/run_tests --agent -t test_jobs.test_scheduled_task` during the `:59`
   minute of any hour (or mock `now` to `HH:59`).
- Expected: both dispatch tests pass.
- Actual: both fail — no job published, because the fallback target minute (58)
  is already in the past and is correctly skipped by the dispatcher.

## Plan
<!-- PLAN PENDING — /scope fills this section. While this marker is present the item
is UNPLANNED and /build MUST refuse it. Delete this comment when the plan is complete. -->

## Notes
- Pure test-logic bug; no production behavior change expected.

## Resolution
- closed: YYYY-MM-DD
- branch:
- files changed:
- tests added:
