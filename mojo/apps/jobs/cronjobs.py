from mojo.decorators.cron import schedule
from mojo.apps import jobs
from mojo.helpers import logit


# Runs daily at 10:30 AM to clean up old jobs
@schedule(minutes="30", hours="10")
def prune_jobs(force=False, verbose=False, now=None):
    jobs.publish(
        func="mojo.apps.jobs.asyncjobs.prune_jobs",
        channel="cleanup",
        payload={})


# Runs at the top of every hour to dispatch user-scheduled tasks
@schedule(minutes="0")
def dispatch_scheduled_tasks():
    """
    Query enabled ScheduledTasks and publish jobs for any that match
    the current hour. Uses jobs.publish(run_at=...) so the existing
    scheduler handles delayed dispatch.
    """
    from django.utils import timezone
    from mojo.apps.jobs.models import ScheduledTask
    import pytz

    now = timezone.now()
    current_hour_utc = now.hour
    current_weekday = now.weekday()

    tasks = ScheduledTask.objects.filter(enabled=True).select_related("user__org")
    published = 0

    for task in tasks:
        # Check if this task runs today
        if not task.matches_day(current_weekday):
            continue

        # Get user timezone
        try:
            user_tz_name = task.user.org.timezone if task.user.org else "America/Los_Angeles"
        except Exception:
            user_tz_name = "America/Los_Angeles"
        try:
            user_tz = pytz.timezone(user_tz_name)
        except Exception:
            user_tz = pytz.timezone("America/Los_Angeles")

        # Convert current UTC time to user's local time to find matching hour
        user_now = now.astimezone(user_tz)
        user_hour = user_now.hour

        # Find run_times that fall in the current hour (in user's timezone)
        for run_time in task.get_run_times_for_hour(user_hour):
            h, m = run_time
            # Build the run_at in user timezone, then convert to UTC
            run_at_local = user_now.replace(hour=h, minute=m, second=0, microsecond=0)
            run_at_utc = run_at_local.astimezone(pytz.utc)

            # Skip if run_at is in the past (e.g. task was just enabled)
            if run_at_utc < now:
                continue

            idem_key = f"schtask:{task.id}:{int(run_at_utc.timestamp())}"

            try:
                jobs.publish(
                    func="mojo.apps.jobs.asyncjobs.run_scheduled_task",
                    payload={"task_id": str(task.id)},
                    run_at=run_at_utc,
                    idempotency_key=idem_key,
                    channel=task.channel,
                    max_retries=task.max_retries,
                )
                published += 1
            except Exception as exc:
                logit.error("dispatch_scheduled_tasks: failed to publish task %s: %s", task.id, exc)

    if published:
        logit.info("dispatch_scheduled_tasks: published %d jobs", published)
