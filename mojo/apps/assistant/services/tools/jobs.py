"""Jobs domain tools — query jobs, stats, queue health, cancel, retry."""
from mojo.helpers import dates


MAX_RESULTS = 50
MAX_MINUTES = 43200  # 30 days


def _tool_query_jobs(params, user):
    from mojo.apps.jobs.models import Job

    criteria = {}
    if params.get("status"):
        criteria["status"] = params["status"]
    if params.get("channel"):
        criteria["channel"] = params["channel"]
    if params.get("func"):
        criteria["func__icontains"] = params["func"]

    minutes = min(params.get("minutes", 1440), MAX_MINUTES)
    criteria["created__gte"] = dates.subtract(minutes=minutes)

    limit = min(params.get("limit", MAX_RESULTS), MAX_RESULTS)
    jobs = Job.objects.filter(**criteria).order_by("-created")[:limit]

    return [
        {
            "id": j.id,
            "channel": j.channel,
            "func": j.func,
            "status": j.status,
            "attempt": j.attempt,
            "max_retries": j.max_retries,
            "last_error": (j.last_error or "")[:300],
            "created": str(j.created),
            "started_at": str(j.started_at) if j.started_at else None,
            "finished_at": str(j.finished_at) if j.finished_at else None,
        }
        for j in jobs
    ]


def _tool_query_job_events(params, user):
    from mojo.apps.jobs.models import JobEvent

    job_id = params["job_id"]
    limit = min(params.get("limit", MAX_RESULTS), MAX_RESULTS)
    events = JobEvent.objects.filter(job_id=job_id).order_by("-created")[:limit]

    return [
        {
            "id": e.pk,
            "job_id": e.job_id,
            "kind": e.kind,
            "message": (e.message or "")[:500],
            "created": str(e.created),
        }
        for e in events
    ]


def _tool_query_job_logs(params, user):
    from mojo.apps.jobs.models import JobLog

    job_id = params["job_id"]
    limit = min(params.get("limit", MAX_RESULTS), MAX_RESULTS)
    logs = JobLog.objects.filter(job_id=job_id).order_by("-created")[:limit]

    return [
        {
            "id": log.pk,
            "job_id": log.job_id,
            "level": log.level,
            "message": (log.message or "")[:500],
            "created": str(log.created),
        }
        for log in logs
    ]


def _tool_get_job_stats(params, user):
    from mojo.apps.jobs.models import Job
    from django.db.models import Count, Avg, F

    minutes = min(params.get("minutes", 1440), MAX_MINUTES)
    since = dates.subtract(minutes=minutes)

    qs = Job.objects.filter(created__gte=since)
    if params.get("channel"):
        qs = qs.filter(channel=params["channel"])

    by_status = dict(
        qs.values("status")
        .annotate(count=Count("id"))
        .values_list("status", "count")
    )

    completed = qs.filter(status="completed", started_at__isnull=False, finished_at__isnull=False)
    avg_duration = completed.aggregate(
        avg_ms=Avg(F("finished_at") - F("started_at"))
    )["avg_ms"]

    total = sum(by_status.values())
    failed = by_status.get("failed", 0)

    return {
        "period_minutes": minutes,
        "total": total,
        "by_status": by_status,
        "avg_duration_seconds": avg_duration.total_seconds() if avg_duration else None,
        "failure_rate": round(failed / total * 100, 1) if total else 0,
    }


def _tool_get_queue_health(params, user):
    from mojo.apps.jobs.models import Job
    from django.db.models import Count

    channel = params.get("channel")

    qs = Job.objects.filter(status__in=["pending", "running"])
    if channel:
        qs = qs.filter(channel=channel)

    by_channel_status = list(
        qs.values("channel", "status")
        .annotate(count=Count("id"))
        .order_by("channel", "status")
    )

    # Check for stuck jobs (running > 30 min)
    stuck_threshold = dates.subtract(minutes=30)
    stuck = Job.objects.filter(
        status="running", started_at__lt=stuck_threshold
    ).count()

    return {
        "queues": by_channel_status,
        "stuck_jobs": stuck,
    }


def _tool_cancel_job(params, user):
    from mojo.apps.jobs import cancel

    job_id = params["job_id"]
    result = cancel(job_id)
    return {
        "ok": result,
        "job_id": job_id,
        "message": f"Job {job_id} cancellation {'requested' if result else 'failed'}",
    }


def _tool_retry_job(params, user):
    from mojo.apps.jobs.models import Job

    job_id = params["job_id"]
    try:
        job = Job.objects.get(id=job_id)
    except Job.DoesNotExist:
        return {"ok": False, "error": "Job not found"}

    from mojo.apps.jobs.services import JobActionsService
    result = JobActionsService.retry_job(job, delay=params.get("delay"))
    return result


# ---------------------------------------------------------------------------
# Tool definitions
# ---------------------------------------------------------------------------

TOOLS = [
    {
        "name": "query_jobs",
        "description": "Filter jobs by status, channel, function name, date range. Returns up to 50 jobs.",
        "input_schema": {
            "type": "object",
            "properties": {
                "status": {"type": "string", "description": "Filter by status (pending, running, completed, failed, canceled)"},
                "channel": {"type": "string", "description": "Filter by channel name"},
                "func": {"type": "string", "description": "Filter by function name (partial match)"},
                "minutes": {"type": "integer", "description": "Look back N minutes (default 1440 = 24h)", "default": 1440},
                "limit": {"type": "integer", "description": "Max results (default 50)", "default": 50},
            },
        },
        "handler": _tool_query_jobs,
        "permission": "view_jobs",
    },
    {
        "name": "query_job_events",
        "description": "Get the event log for a specific job.",
        "input_schema": {
            "type": "object",
            "properties": {
                "job_id": {"type": "string", "description": "The job ID"},
                "limit": {"type": "integer", "description": "Max results (default 50)", "default": 50},
            },
            "required": ["job_id"],
        },
        "handler": _tool_query_job_events,
        "permission": "view_jobs",
    },
    {
        "name": "query_job_logs",
        "description": "Get structured logs for a specific job.",
        "input_schema": {
            "type": "object",
            "properties": {
                "job_id": {"type": "string", "description": "The job ID"},
                "limit": {"type": "integer", "description": "Max results (default 50)", "default": 50},
            },
            "required": ["job_id"],
        },
        "handler": _tool_query_job_logs,
        "permission": "view_jobs",
    },
    {
        "name": "get_job_stats",
        "description": "Get job statistics: counts by status, average duration, failure rate.",
        "input_schema": {
            "type": "object",
            "properties": {
                "channel": {"type": "string", "description": "Filter by channel (optional)"},
                "minutes": {"type": "integer", "description": "Look back N minutes (default 1440 = 24h)", "default": 1440},
            },
        },
        "handler": _tool_get_job_stats,
        "permission": "view_jobs",
    },
    {
        "name": "get_queue_health",
        "description": "Get pending/running job counts per channel and number of stuck jobs.",
        "input_schema": {
            "type": "object",
            "properties": {
                "channel": {"type": "string", "description": "Filter by channel (optional)"},
            },
        },
        "handler": _tool_get_queue_health,
        "permission": "view_jobs",
    },
    {
        "name": "cancel_job",
        "description": "Request cancellation of a job. IMPORTANT: Confirm with the user before executing.",
        "input_schema": {
            "type": "object",
            "properties": {
                "job_id": {"type": "string", "description": "The job ID to cancel"},
            },
            "required": ["job_id"],
        },
        "handler": _tool_cancel_job,
        "permission": "manage_jobs",
        "mutates": True,
    },
    {
        "name": "retry_job",
        "description": "Retry a failed job. IMPORTANT: Confirm with the user before executing.",
        "input_schema": {
            "type": "object",
            "properties": {
                "job_id": {"type": "string", "description": "The job ID to retry"},
                "delay": {"type": "integer", "description": "Delay in seconds before retrying (optional)"},
            },
            "required": ["job_id"],
        },
        "handler": _tool_retry_job,
        "permission": "manage_jobs",
        "mutates": True,
    },
]
