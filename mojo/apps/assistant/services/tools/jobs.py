"""Jobs domain tools — query jobs, stats, queue health, cancel, retry."""
from mojo.apps.assistant import tool
from mojo.helpers import dates


MAX_RESULTS = 50
MAX_MINUTES = 43200  # 30 days


@tool(
    name="query_jobs",
    domain="jobs",
    permission="view_jobs",
    description="Filter jobs by status, channel, function name, date range. Returns up to 50 jobs.",
    input_schema={
        "type": "object",
        "properties": {
            "status": {"type": "string", "description": "Filter by status (pending, running, completed, failed, canceled)"},
            "channel": {"type": "string", "description": "Filter by channel name"},
            "func": {"type": "string", "description": "Filter by function name (partial match)"},
            "minutes": {"type": "integer", "description": "Look back N minutes (default 1440 = 24h)", "default": 1440},
            "limit": {"type": "integer", "description": "Max results (default 50)", "default": 50},
        },
    },
)
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


@tool(
    name="query_job_events",
    domain="jobs",
    permission="view_jobs",
    description="Get the event log for a specific job.",
    input_schema={
        "type": "object",
        "properties": {
            "job_id": {"type": "string", "description": "The job ID"},
            "limit": {"type": "integer", "description": "Max results (default 50)", "default": 50},
        },
        "required": ["job_id"],
    },
)
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


@tool(
    name="query_job_logs",
    domain="jobs",
    permission="view_jobs",
    description="Get structured logs for a specific job.",
    input_schema={
        "type": "object",
        "properties": {
            "job_id": {"type": "string", "description": "The job ID"},
            "limit": {"type": "integer", "description": "Max results (default 50)", "default": 50},
        },
        "required": ["job_id"],
    },
)
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


@tool(
    name="get_job_stats",
    domain="jobs",
    permission="view_jobs",
    description="Get job statistics: counts by status, average duration, failure rate.",
    input_schema={
        "type": "object",
        "properties": {
            "channel": {"type": "string", "description": "Filter by channel (optional)"},
            "minutes": {"type": "integer", "description": "Look back N minutes (default 1440 = 24h)", "default": 1440},
        },
    },
)
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


@tool(
    name="get_queue_health",
    domain="jobs",
    permission="view_jobs",
    description="Get pending/running job counts per channel and number of stuck jobs.",
    input_schema={
        "type": "object",
        "properties": {
            "channel": {"type": "string", "description": "Filter by channel (optional)"},
        },
    },
)
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


@tool(
    name="cancel_job",
    domain="jobs",
    permission="manage_jobs",
    description="Request cancellation of a job. IMPORTANT: Confirm with the user before executing.",
    input_schema={
        "type": "object",
        "properties": {
            "job_id": {"type": "string", "description": "The job ID to cancel"},
        },
        "required": ["job_id"],
    },
    mutates=True,
)
def _tool_cancel_job(params, user):
    from mojo.apps.jobs import cancel

    job_id = params["job_id"]
    result = cancel(job_id)
    return {
        "ok": result,
        "job_id": job_id,
        "message": f"Job {job_id} cancellation {'requested' if result else 'failed'}",
    }


# ── Scheduled Tasks ──────────────────────────────────────────────


@tool(
    name="list_scheduled_tasks",
    domain="jobs",
    permission="view_jobs",
    description="List the current user's scheduled tasks. Returns up to 50 tasks.",
    input_schema={
        "type": "object",
        "properties": {
            "enabled_only": {"type": "boolean", "description": "Only show enabled tasks (default true)", "default": True},
            "limit": {"type": "integer", "description": "Max results (default 50)", "default": 50},
        },
    },
)
def _tool_list_scheduled_tasks(params, user):
    from mojo.apps.jobs.models import ScheduledTask

    criteria = {"user": user}
    if params.get("enabled_only", True):
        criteria["enabled"] = True

    limit = min(params.get("limit", MAX_RESULTS), MAX_RESULTS)
    tasks = ScheduledTask.objects.filter(**criteria).order_by("-created")[:limit]

    return [
        {
            "id": t.id,
            "name": t.name,
            "enabled": t.enabled,
            "run_once": t.run_once,
            "task_type": t.task_type,
            "run_times": t.run_times,
            "run_days": t.run_days,
            "notify": t.notify,
            "last_run": str(t.last_run) if t.last_run else None,
            "run_count": t.run_count,
            "created": str(t.created),
        }
        for t in tasks
    ]


@tool(
    name="create_scheduled_task",
    domain="jobs",
    permission="manage_jobs",
    description=(
        "Create a scheduled task for the user. "
        "Requires: name, task_type (job/webhook/llm), run_times (list of HH:MM, max 2), job_config. "
        "Optional: run_days (list of weekday ints 0-6, empty=every day), notify (list of channels), run_once. "
        "IMPORTANT: Confirm with the user before executing."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "name": {"type": "string", "description": "Human-readable label"},
            "description": {"type": "string", "description": "Optional description"},
            "task_type": {"type": "string", "enum": ["job", "webhook", "llm"], "description": "Type of task"},
            "run_times": {
                "type": "array", "items": {"type": "string"},
                "description": 'List of "HH:MM" strings, max 2 (e.g. ["09:00"])',
            },
            "run_days": {
                "type": "array", "items": {"type": "integer"},
                "description": "Weekday ints 0-6 (Mon=0). Empty = every day.",
            },
            "job_config": {
                "type": "object",
                "description": "Config for task type: job={func, payload}, webhook={url, data}, llm={system_prompt, user_prompt}",
            },
            "notify": {
                "type": "array", "items": {"type": "string"},
                "description": 'Notification channels: ["email", "in_app", "sms", "push"]',
            },
            "run_once": {"type": "boolean", "description": "Auto-disable after first run (default false)"},
        },
        "required": ["name", "task_type", "run_times", "job_config"],
    },
    mutates=True,
)
def _tool_create_scheduled_task(params, user):
    from mojo.apps.jobs.models import ScheduledTask

    try:
        task = ScheduledTask(
            user=user,
            name=params["name"],
            description=params.get("description", ""),
            task_type=params["task_type"],
            run_times=params["run_times"],
            run_days=params.get("run_days", []),
            job_config=params["job_config"],
            notify=params.get("notify", []),
            run_once=params.get("run_once", False),
        )
        task.save()
        return {
            "ok": True,
            "id": task.id,
            "name": task.name,
            "message": f"Scheduled task '{task.name}' created",
        }
    except (ValueError, Exception) as e:
        return {"ok": False, "error": str(e)}


@tool(
    name="update_scheduled_task",
    domain="jobs",
    permission="manage_jobs",
    description=(
        "Update an existing scheduled task. Provide the task ID and any fields to change. "
        "IMPORTANT: Confirm with the user before executing."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "task_id": {"type": "string", "description": "The scheduled task ID"},
            "name": {"type": "string"},
            "description": {"type": "string"},
            "enabled": {"type": "boolean"},
            "run_once": {"type": "boolean"},
            "run_times": {"type": "array", "items": {"type": "string"}},
            "run_days": {"type": "array", "items": {"type": "integer"}},
            "job_config": {"type": "object"},
            "notify": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["task_id"],
    },
    mutates=True,
)
def _tool_update_scheduled_task(params, user):
    from mojo.apps.jobs.models import ScheduledTask

    try:
        task = ScheduledTask.objects.get(id=params["task_id"], user=user)
    except ScheduledTask.DoesNotExist:
        return {"ok": False, "error": "Task not found"}

    updatable = ["name", "description", "enabled", "run_once", "run_times",
                 "run_days", "job_config", "notify"]
    changed = []
    for field in updatable:
        if field in params:
            setattr(task, field, params[field])
            changed.append(field)

    if not changed:
        return {"ok": False, "error": "No fields to update"}

    try:
        task.save()
        return {"ok": True, "id": task.id, "updated": changed}
    except (ValueError, Exception) as e:
        return {"ok": False, "error": str(e)}


@tool(
    name="delete_scheduled_task",
    domain="jobs",
    permission="manage_jobs",
    description="Delete a scheduled task. IMPORTANT: Confirm with the user before executing.",
    input_schema={
        "type": "object",
        "properties": {
            "task_id": {"type": "string", "description": "The scheduled task ID to delete"},
        },
        "required": ["task_id"],
    },
    mutates=True,
)
def _tool_delete_scheduled_task(params, user):
    from mojo.apps.jobs.models import ScheduledTask

    try:
        task = ScheduledTask.objects.get(id=params["task_id"], user=user)
    except ScheduledTask.DoesNotExist:
        return {"ok": False, "error": "Task not found"}

    name = task.name
    task.delete()
    return {"ok": True, "message": f"Scheduled task '{name}' deleted"}


@tool(
    name="retry_job",
    domain="jobs",
    permission="manage_jobs",
    description="Retry a failed job. IMPORTANT: Confirm with the user before executing.",
    input_schema={
        "type": "object",
        "properties": {
            "job_id": {"type": "string", "description": "The job ID to retry"},
            "delay": {"type": "integer", "description": "Delay in seconds before retrying (optional)"},
        },
        "required": ["job_id"],
    },
    mutates=True,
)
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


def _validate_job_func(func):
    """
    Validate that a job function path points to an installed Django app
    and is importable. Returns an error string or None if valid.
    """
    from django.apps import apps
    from mojo.apps.jobs.job_engine import load_job_function

    # Block internal framework functions that have dedicated tools
    if func == "mojo.apps.jobs.asyncjobs.run_scheduled_task":
        return "Use run_scheduled_task_now to trigger scheduled tasks"

    # Must have at least module.function
    if "." not in func:
        return f"Invalid function path: {func}"

    # Check that the function's root package is an installed Django app
    module_path = func.rsplit(".", 1)[0]
    installed = {cfg.name for cfg in apps.get_app_configs()}

    # Walk up the module path to find a matching installed app
    # e.g. "mojo.apps.incident.asyncjobs" checks:
    #   mojo.apps.incident.asyncjobs, mojo.apps.incident, mojo.apps, mojo
    parts = module_path.split(".")
    found = False
    for i in range(len(parts), 0, -1):
        candidate = ".".join(parts[:i])
        if candidate in installed:
            found = True
            break

    if not found:
        return f"Function must be inside an installed Django app, got: {func}"

    # Validate it actually imports
    try:
        load_job_function(func)
    except (ImportError, Exception) as e:
        return f"Invalid job function: {e}"

    return None


@tool(
    name="run_job",
    domain="jobs",
    permission="manage_jobs",
    description=(
        "Publish a new job. Two modes: "
        "(1) Fresh run — provide func (dotted path) and optional payload. "
        "(2) Rerun from template — provide job_id of an existing job to clone it. "
        "Exactly one of func or job_id is required. "
        "IMPORTANT: Confirm with the user before executing."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "func": {"type": "string", "description": "Dotted path to the job function (e.g. 'myapp.tasks.send_report')"},
            "payload": {"type": "object", "description": "Data to pass to the job (optional)"},
            "channel": {"type": "string", "description": "Job channel (default 'default')"},
            "delay": {"type": "integer", "description": "Delay in seconds before running (optional)"},
            "job_id": {"type": "string", "description": "ID of an existing job to rerun as a template"},
        },
    },
    mutates=True,
)
def _tool_run_job(params, user):
    func = params.get("func")
    job_id = params.get("job_id")

    if func and job_id:
        return {"ok": False, "error": "Provide either func or job_id, not both"}
    if not func and not job_id:
        return {"ok": False, "error": "Provide either func (fresh run) or job_id (rerun from template)"}

    if func:
        # Security: func must be inside an installed Django app
        err = _validate_job_func(func)
        if err:
            return {"ok": False, "error": err}

        from mojo.apps import jobs
        try:
            new_id = jobs.publish(
                func=func,
                payload=params.get("payload", {}),
                channel=params.get("channel", "default"),
                delay=params.get("delay"),
            )
            return {"ok": True, "job_id": new_id, "message": f"Job published: {func}"}
        except (ValueError, RuntimeError) as e:
            return {"ok": False, "error": str(e)}

    # Rerun from template
    from mojo.apps.jobs.models import Job
    try:
        job = Job.objects.get(id=job_id)
    except Job.DoesNotExist:
        return {"ok": False, "error": "Template job not found"}

    overrides = {}
    if params.get("payload"):
        overrides["payload"] = params["payload"]
    if params.get("channel"):
        overrides["channel"] = params["channel"]
    if params.get("delay"):
        overrides["delay"] = params["delay"]

    from mojo.apps.jobs.services import JobActionsService
    return JobActionsService.publish_job_from_template(job, overrides)


@tool(
    name="run_scheduled_task_now",
    domain="jobs",
    permission="manage_jobs",
    description=(
        "Immediately execute a scheduled task, regardless of its schedule or enabled state. "
        "Publishes a job that runs the task right now. "
        "IMPORTANT: Confirm with the user before executing."
    ),
    input_schema={
        "type": "object",
        "properties": {
            "task_id": {"type": "string", "description": "The scheduled task ID to run"},
        },
        "required": ["task_id"],
    },
    mutates=True,
)
def _tool_run_scheduled_task_now(params, user):
    from mojo.apps.jobs.models import ScheduledTask

    task_id = params["task_id"]
    try:
        task = ScheduledTask.objects.get(id=task_id, user=user)
    except ScheduledTask.DoesNotExist:
        return {"ok": False, "error": "Scheduled task not found"}

    from mojo.apps import jobs
    try:
        job_id = jobs.publish(
            func="mojo.apps.jobs.asyncjobs.run_scheduled_task",
            payload={"task_id": task.id, "force": True},
        )
        return {
            "ok": True,
            "job_id": job_id,
            "message": f"Scheduled task '{task.name}' published for immediate execution",
        }
    except (ValueError, RuntimeError) as e:
        return {"ok": False, "error": str(e)}
