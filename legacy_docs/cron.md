# Mojo Cron (clean, simple)

Use cron to trigger small, fast functions that live in YOUR_APP/asyncjobs.py.
Those functions should finish in a few seconds. If they need to do real work,
publish a background job instead (recommended).

See also: docs/jobs.md

## Philosophy
- Cron: just a timer.
- Jobs: do the heavy lifting.
- Pattern: cron publishes a job → worker runs it.

## Where to put cron functions
In each Django app, define cron functions in asyncjobs.py.

Example layout:
    myapp/
    ├─ __init__.py
    ├─ models.py
    ├─ views.py
    └─ asyncjobs.py   # your scheduled functions live here

## Scheduling API (quick reference)
Decorate a function with a cron-like schedule.

    from mojo.decorators.cron import schedule

    @schedule(minutes="*/5")                # every 5 minutes
    @schedule(minutes="0", hours="2")       # daily at 02:00
    @schedule(minutes="0", hours="9", weekdays="1-5")  # weekdays 09:00

Accepted fields: minutes, hours, days, months, weekdays
Accepted patterns: "*", "5", "1,15,30", "1-5", "*/15"

Tip: keep the function itself fast and idempotent.

## Cron that publishes jobs (recommended)
Let the cron function push work to the jobs system.

    # myapp/asyncjobs.py
    from mojo.decorators.cron import schedule
    from mojo.apps.jobs import publish  # See docs/jobs.md

    @schedule(minutes="*/5")
    def sync_billing():
        # Do not sync here. Publish a job and return quickly.
        publish("myapp.jobs.sync_billing", {"since": "5m"})

    @schedule(minutes="0", hours="2")
    def cleanup_old_data():
        publish("myapp.jobs.cleanup", {"older_than_days": 30})

    @schedule(minutes="0", hours="9", weekdays="1-5")
    def send_daily_digest():
        publish("myapp.jobs.send_daily_digest", {"tz": "America/Los_Angeles"})

Why?
- Fast cron runs are reliable and cheap.
- Jobs can be retried, monitored, and parallelized.

## Loading and running cron
Load all app asyncjobs and run matching functions for “now”.

    from mojo.helpers.cron import load_app_cron, run_now

    # Usually called during startup
    load_app_cron()  # imports YOUR_APP.asyncjobs across installed apps

    # Called every minute by your scheduler/runner
    run_now()

Notes:
- Ensure your deployment has a small runner that calls run_now() each minute.
- load_app_cron() expects asyncjobs.py per app.

## Inline work (when absolutely minimal)
If the work is trivial (a few DB rows, a quick ping), it can be done inline:

    @schedule(minutes="30")
    def ping_healthcheck():
        # Keep it very short (<1–2s)
        pass

If it grows, switch to publish().

## Best practices
- Idempotent cron functions (safe to re-run)
- Prefer publish() for anything non-trivial
- Add small guards (time limits, batch sizes)
- Keep schedules simple and readable
- Log minimally; avoid noisy output

## References
- Jobs system and publish(): docs/jobs.md
- Cron helper usage (advanced): mojo.helpers.cron