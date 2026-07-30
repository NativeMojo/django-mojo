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


def find_unconsumed_channels():
    """
    Channels with queued jobs that no live engine is consuming.

    Returns a list of (channel, depth). A channel counts as consumed if any
    runner heartbeat (they carry the runner's channel list and expire on their
    own) names it — so a busy-but-served queue is never reported; that is a
    capacity question, not a routing mistake.
    """
    from mojo.apps.jobs.adapters import get_adapter
    from mojo.apps.jobs.keys import JobKeys
    import json

    redis = get_adapter()
    keys = JobKeys()
    client = redis.get_client()

    def _text(value):
        return value.decode('utf-8') if isinstance(value, (bytes, bytearray)) else value

    # Every channel some live runner claims to consume.
    consumed = set()
    for key in client.scan_iter(match=f"{keys.prefix}:runner:*:hb", count=200):
        try:
            raw = redis.get(_text(key))
            if not raw:
                continue
            consumed.update(json.loads(_text(raw)).get("channels") or [])
        except Exception as e:
            logit.warning(f"find_unconsumed_channels: unreadable heartbeat {key}: {e}")

    marker = ":queue:"
    orphans = []
    for key in client.scan_iter(match=f"{keys.prefix}:*{marker}*", count=200):
        key_str = _text(key)
        parts = key_str.split(marker)
        if len(parts) != 2 or not parts[1]:
            continue
        channel = parts[1]
        if channel in consumed:
            continue
        depth = redis.llen(key_str) or 0
        if depth > 0:
            orphans.append((channel, depth))
    return sorted(orphans)


# Every 5 minutes: jobs are only routed as named now, so a channel nobody
# consumes is the way a misconfiguration shows up. Say so loudly — queued jobs
# expire (JOBS_DEFAULT_EXPIRES_SEC), so the window to react is finite.
@schedule(minutes="*/5")
def check_unconsumed_channels(force=False, verbose=False, now=None):
    from mojo.apps import incident

    try:
        orphans = find_unconsumed_channels()
    except Exception as e:
        logit.error(f"check_unconsumed_channels: scan failed: {e}")
        return

    for channel, depth in orphans:
        logit.warning(
            f"jobs: channel '{channel}' has {depth} queued job(s) and no live consumer")
        incident.report_event(
            f"Job channel '{channel}' has {depth} queued job(s) but no engine is "
            f"consuming it. Add it to a worker's JOBS_CHANNELS (or start an engine "
            f"with --channels {channel}); queued jobs expire if left unclaimed.",
            title=f"Unconsumed job channel: {channel}",
            category="jobs:unconsumed_channel",
            level=3,
            channel=channel,
            queue_depth=depth)
    return orphans


# Runs at the top of every hour to dispatch user-scheduled tasks
@schedule(minutes="0")
def dispatch_scheduled_tasks(now=None):
    """
    Query enabled ScheduledTasks and publish jobs for any that match
    the current hour. Uses jobs.publish(run_at=...) so the existing
    scheduler handles delayed dispatch.

    `now` defaults to timezone.now(). Pass an aware UTC datetime to dispatch as
    of a simulated instant — the cron runner always calls this with no
    arguments, so the default path is unchanged.
    """
    from django.utils import timezone
    from mojo.apps.jobs.models import ScheduledTask
    import pytz

    if now is None:
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
