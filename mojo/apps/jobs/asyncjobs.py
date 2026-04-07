from mojo.apps.jobs.models import Job
from django.utils import timezone
from datetime import timedelta
from mojo.helpers.settings import settings
from mojo.helpers import logit


def prune_jobs(job):
    qset = Job.objects.filter(
        created__lt=timezone.now() - timedelta(days=7))
    qset.delete()


def run_scheduled_task(job):
    """
    Execute a user-defined scheduled task.

    Called by the job engine when a scheduled task's run_at time arrives.
    Loads the ScheduledTask, checks it's still enabled, dispatches by type,
    stores a TaskResult, and sends opt-in notifications.
    """
    from mojo.apps.jobs.models import ScheduledTask, TaskResult
    from mojo.apps import jobs

    task_id = job.payload.get("task_id")
    if not task_id:
        logit.error("run_scheduled_task: missing task_id in payload")
        return

    try:
        task = ScheduledTask.objects.select_related("user").get(id=task_id)
    except ScheduledTask.DoesNotExist:
        logit.error("run_scheduled_task: task %s not found", task_id)
        return

    # Check task is still enabled (force flag bypasses for on-demand runs)
    if not task.enabled and not job.payload.get("force"):
        logit.info("run_scheduled_task: task %s is disabled, skipping", task_id)
        return

    output = ""
    error = ""
    status = "success"

    try:
        if task.task_type == "llm":
            output = _run_llm_task(task)
        elif task.task_type == "webhook":
            output = _run_webhook_task(task)
        elif task.task_type == "job":
            output = _run_job_task(task)
        else:
            error = f"Unknown task_type: {task.task_type}"
            status = "error"
    except Exception as exc:
        error = str(exc)[:2000]
        status = "error"
        logit.error("run_scheduled_task: task %s failed: %s", task_id, exc)

    # Store result
    TaskResult.objects.create(
        task=task,
        user=task.user,
        job=job,
        status=status,
        output=output[:50000] if output else "",
        error=error,
    )

    # Update task tracking
    now = timezone.now()
    task.last_run = now
    task.run_count += 1
    task.last_error = error
    update_fields = ["last_run", "run_count", "last_error", "modified"]

    # Auto-disable run_once tasks
    if task.run_once and status == "success":
        task.enabled = False
        update_fields.append("enabled")

    task.save(update_fields=update_fields)

    # Send notifications if configured
    if task.notify and status == "success":
        _send_notifications(task, output)


def _run_llm_task(task):
    """Run an LLM prompt and return the response text."""
    from mojo.helpers import llm

    config = task.job_config
    system_prompt = config.get("system_prompt", "")
    user_prompt = config.get("user_prompt", "")

    if not user_prompt:
        raise ValueError("LLM task requires a user_prompt in job_config")

    return llm.ask(user_prompt, system=system_prompt or None)


def _run_webhook_task(task):
    """Publish a webhook job and return confirmation."""
    from mojo.apps import jobs

    config = task.job_config
    url = config.get("url")
    data = config.get("data", {})

    if not url:
        raise ValueError("Webhook task requires a url in job_config")

    job_id = jobs.publish_webhook(url=url, data=data)
    return f"Webhook published: job_id={job_id}"


def _run_job_task(task):
    """Publish a generic job and return confirmation."""
    from mojo.apps import jobs

    config = task.job_config
    func = config.get("func")
    payload = config.get("payload", {})

    if not func:
        raise ValueError("Job task requires a func in job_config")

    job_id = jobs.publish(func=func, payload=payload)
    return f"Job published: job_id={job_id}"


def _send_notifications(task, output):
    """Send opt-in notifications to the task owner."""
    user = task.user
    title = f"Scheduled Task: {task.name}"
    body = output[:500] if output else "Task completed successfully."

    for channel in task.notify:
        try:
            if channel == "in_app":
                user.notify(title=title, body=body, kind="scheduled_task")
            elif channel == "email":
                user.send_email(
                    subject=title,
                    body_text=body,
                )
            elif channel == "push":
                user.push_notification(title=title, body=body, kind="scheduled_task")
            elif channel == "sms":
                from mojo.apps.phonehub.models import SMS
                if hasattr(user, "phone") and user.phone:
                    SMS.send(body=body, to_number=user.phone, user=user)
        except Exception as exc:
            logit.error("_send_notifications: %s notify via %s failed: %s",
                        task.id, channel, exc)
