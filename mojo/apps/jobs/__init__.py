"""
Django-MOJO Jobs System - Public API

A reliable background job system for Django with Redis fast path and Postgres truth.
"""
import uuid
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, Optional, Union

from django.utils import timezone
from django.db import transaction

from mojo.helpers import logit
from mojo.helpers.settings import settings
from mojo.apps import metrics
from .keys import JobKeys
from .adapters import get_adapter

# Module-level settings for readability
JOB_CHANNELS = settings.get('JOBS_CHANNELS', ['default'])
JOBS_PAYLOAD_MAX_BYTES = settings.get('JOBS_PAYLOAD_MAX_BYTES', 16384)
JOBS_DEFAULT_EXPIRES_SEC = settings.get('JOBS_DEFAULT_EXPIRES_SEC', 900)
JOBS_DEFAULT_MAX_RETRIES = settings.get('JOBS_DEFAULT_MAX_RETRIES', 0)
JOBS_DEFAULT_BACKOFF_BASE = settings.get('JOBS_DEFAULT_BACKOFF_BASE', 2.0)
JOBS_DEFAULT_BACKOFF_MAX = settings.get('JOBS_DEFAULT_BACKOFF_MAX', 3600)
JOBS_STREAM_MAXLEN = settings.get('JOBS_STREAM_MAXLEN', 100000)


__all__ = [
    'publish',
    'publish_local',
    'publish_webhook',
    'cancel',
    'status',
]


def publish(
    func: Union[str, Callable],
    payload: Dict[str, Any] = None,
    *,
    channel: str = "default",
    delay: Optional[int] = None,
    run_at: Optional[datetime] = None,
    broadcast: bool = False,
    max_retries: Optional[int] = None,
    backoff_base: Optional[float] = None,
    backoff_max: Optional[int] = None,
    expires_in: Optional[int] = None,
    expires_at: Optional[datetime] = None,
    max_exec_seconds: Optional[int] = None,
    idempotency_key: Optional[str] = None
) -> str:
    """
    Publish a job to be executed asynchronously.

    Args:
        func: Job function (registered name or callable with _job_name)
        payload: Data to pass to the job handler
        channel: Channel to publish to (default: "default")
        delay: Delay in seconds from now
        run_at: Specific time to run the job (overrides delay)
        broadcast: If True, all runners on the channel will execute
        max_retries: Maximum retry attempts (default from settings or 3)
        backoff_base: Base for exponential backoff (default 2.0)
        backoff_max: Maximum backoff in seconds (default 3600)
        expires_in: Seconds until job expires (default from settings)
        expires_at: Specific expiration time (overrides expires_in)
        max_exec_seconds: Maximum execution time before hard kill
        idempotency_key: Optional key for exactly-once semantics

    Returns:
        Job ID (UUID string without dashes)

    Raises:
        ValueError: If func is not registered or arguments are invalid
        RuntimeError: If publishing fails
    """
    from .models import Job, JobEvent

    # Convert callable to module path string
    if callable(func):
        func_path = f"{func.__module__}.{func.__name__}"
    else:
        func_path = func

    # Validate payload
    payload = payload or {}
    if not isinstance(payload, dict):
        raise ValueError("Payload must be a dictionary")

    # Check payload size
    import json
    payload_json = json.dumps(payload)
    max_bytes = JOBS_PAYLOAD_MAX_BYTES
    if len(payload_json.encode('utf-8')) > max_bytes:
        raise ValueError(f"Payload exceeds maximum size of {max_bytes} bytes")

    # Validate channel against configured channels
    configured_channels = JOB_CHANNELS if isinstance(JOB_CHANNELS, list) else [JOB_CHANNELS]
    if channel not in configured_channels:
        raise ValueError(f"Invalid jobs channel '{channel}'. Must be one of: {', '.join(configured_channels)}")

    # Generate job ID
    job_id = uuid.uuid4().hex  # UUID without dashes

    # Calculate run_at time
    now = timezone.now()
    if run_at:
        if timezone.is_naive(run_at):
            run_at = timezone.make_aware(run_at)
    elif delay:
        run_at = now + timedelta(seconds=delay)
    else:
        run_at = None  # Immediate execution

    # Calculate expiration
    if expires_at:
        if timezone.is_naive(expires_at):
            expires_at = timezone.make_aware(expires_at)
    elif expires_in:
        expires_at = now + timedelta(seconds=expires_in)
    else:
        default_expire = JOBS_DEFAULT_EXPIRES_SEC
        expires_at = now + timedelta(seconds=default_expire)

    # Apply defaults
    if max_retries is None:
        max_retries = JOBS_DEFAULT_MAX_RETRIES
    if backoff_base is None:
        backoff_base = JOBS_DEFAULT_BACKOFF_BASE
    if backoff_max is None:
        backoff_max = JOBS_DEFAULT_BACKOFF_MAX

    # Create job in database
    try:
        with transaction.atomic():
            job = Job.objects.create(
                id=job_id,
                channel=channel,
                func=func_path,
                payload=payload,
                status='pending',
                run_at=run_at,
                expires_at=expires_at,
                max_retries=max_retries,
                backoff_base=backoff_base,
                backoff_max_sec=backoff_max,
                broadcast=broadcast,
                max_exec_seconds=max_exec_seconds,
                idempotency_key=idempotency_key
            )

            # Create initial event
            JobEvent.objects.create(
                job=job,
                channel=channel,
                event='created',
                details={'func': func_path, 'channel': channel}
            )

    except Exception as e:
        if 'UNIQUE constraint' in str(e) and idempotency_key:
            # Idempotent request - return existing job ID
            try:
                existing = Job.objects.get(idempotency_key=idempotency_key)
                logit.info(f"Idempotent job request, returning existing: {existing.id}")
                return existing.id
            except Job.DoesNotExist:
                pass
        logit.error(f"Failed to create job in database: {e}")
        raise RuntimeError(f"Failed to create job: {e}")

    # Mirror to Redis (Plan B: List + ZSET + Scheduled ZSET)
    try:
        redis = get_adapter()
        keys = JobKeys()

        # No per-job Redis hash (KISS): DB is source of truth

        # Route based on scheduling (Plan B: List + ZSET for immediate/scheduled)
        if run_at and run_at > now:
            # Add to scheduled ZSET (two-ZSET routing remains)
            score = run_at.timestamp() * 1000  # milliseconds
            target_zset = keys.sched_broadcast(channel) if broadcast else keys.sched(channel)
            redis.zadd(target_zset, {job_id: score})

            # Record scheduled event
            JobEvent.objects.create(
                job=job,
                channel=channel,
                event='scheduled',
                details={'run_at': run_at.isoformat()}
            )

            logit.info(f"Scheduled job {job_id} on {channel} for {run_at} "
                       f"(zset={'sched_broadcast' if broadcast else 'sched'})")
        else:
            # Immediate execution: enqueue to List queue (Plan B)
            queue_key = keys.queue(channel)
            redis.rpush(queue_key, job_id)

            # Record queued event (for immediate queue)
            JobEvent.objects.create(
                job=job,
                channel=channel,
                event='queued',
                details={'queue': queue_key}
            )

            logit.info(f"Queued job {job_id} on {channel} (broadcast={broadcast}) to queue {queue_key}")

        # Emit metric

        metrics.record(
            slug="jobs.published",
            when=now,
            count=1,
            category="jobs"
        )

        metrics.record(
            slug=f"jobs.published.{channel}",
            when=now,
            count=1,
            category="jobs"
        )

    except Exception as e:
        logit.error(f"Failed to mirror job {job_id} to Redis: {e}")
        # Mark job as failed in DB since it couldn't be queued
        job.status = 'failed'
        job.last_error = f"Failed to queue: {e}"
        job.save(update_fields=['status', 'last_error', 'modified'])
        raise RuntimeError(f"Failed to queue job: {e}")

    return job_id


def publish_local(func: Union[str, Callable], *args,
                 run_at: Optional[datetime] = None,
                 delay: Optional[int] = None,
                 **kwargs) -> str:
    """
    Publish a job to the local in-process queue.

    Simple approach: spawns a thread that sleeps if delay is specified,
    then executes the function.

    Args:
        func: Job function (module path or callable)
        *args: Positional arguments for the job
        run_at: When to execute the job (None for immediate)
        delay: Delay in seconds before execution (ignored if run_at is provided)
        **kwargs: Keyword arguments for the job

    Returns:
        Job ID (for compatibility, though local jobs aren't persistent)

    Raises:
        ImportError: If function cannot be loaded
    """
    from .local_queue import get_local_queue
    import importlib

    # Resolve function
    if callable(func):
        func_path = f"{func.__module__}.{func.__name__}"
        func_obj = func
    else:
        # Dynamic import
        func_path = func
        try:
            module_path, func_name = func_path.rsplit('.', 1)
            module = importlib.import_module(module_path)
            func_obj = getattr(module, func_name)
        except (ImportError, AttributeError, ValueError) as e:
            raise ImportError(f"Cannot load local job function '{func_path}': {e}")

    # Generate a pseudo job ID
    job_id = f"local-{uuid.uuid4().hex[:8]}"

    # Calculate run_at time
    if run_at is None and delay is not None:
        from django.utils import timezone
        from datetime import timedelta
        run_at = timezone.now() + timedelta(seconds=delay)

    # Queue the job (always succeeds with new simple approach)
    queue = get_local_queue()
    queue.put(func_obj, args, kwargs, job_id, run_at=run_at)

    if run_at:
        logit.info(f"Scheduled local job {job_id} ({func_path}) for {run_at}")
    else:
        logit.info(f"Queued local job {job_id} ({func_path})")
    return job_id


def publish_webhook(
    url: str,
    data: Dict[str, Any],
    *,
    headers: Optional[Dict[str, str]] = None,
    channel: str = "webhooks",
    delay: Optional[int] = None,
    run_at: Optional[datetime] = None,
    timeout: Optional[int] = 30,
    max_retries: Optional[int] = None,
    backoff_base: Optional[float] = None,
    backoff_max: Optional[int] = None,
    expires_in: Optional[int] = None,
    expires_at: Optional[datetime] = None,
    idempotency_key: Optional[str] = None,
    webhook_id: Optional[str] = None
) -> str:
    """
    Publish a webhook job to POST data to an external URL.

    Args:
        url: Target webhook URL
        data: Data to POST (will be JSON encoded)
        headers: Optional HTTP headers (default includes Content-Type: application/json)
        channel: Channel to publish to (default: "webhooks")
        delay: Delay in seconds from now
        run_at: Specific time to run the webhook (overrides delay)
        timeout: Request timeout in seconds (default: 30)
        max_retries: Maximum retry attempts (default from settings or 5 for webhooks)
        backoff_base: Base for exponential backoff (default 2.0)
        backoff_max: Maximum backoff in seconds (default 3600)
        expires_in: Seconds until webhook expires (default from settings)
        expires_at: Specific expiration time (overrides expires_in)
        idempotency_key: Optional key for exactly-once semantics
        webhook_id: Optional webhook identifier for tracking

    Returns:
        Job ID (UUID string without dashes)

    Raises:
        ValueError: If URL is invalid or data cannot be serialized
        RuntimeError: If publishing fails

    Example:
        job_id = publish_webhook(
            url="https://api.example.com/webhooks/user-signup",
            data={"user_id": 123, "email": "user@example.com", "event": "signup"},
            headers={"Authorization": "Bearer secret"},
            max_retries=3
        )
    """
    # Validate URL
    if not url or not isinstance(url, str):
        raise ValueError("URL must be a non-empty string")

    if not url.startswith(('http://', 'https://')):
        raise ValueError("URL must start with http:// or https://")

    # Validate data can be JSON serialized
    import json
    try:
        json.dumps(data)
    except (TypeError, ValueError) as e:
        raise ValueError(f"Data must be JSON serializable: {e}")

    # Build headers with defaults
    webhook_headers = {
        'Content-Type': 'application/json',
        'User-Agent': 'Django-MOJO-Webhook/1.0'
    }
    if headers:
        webhook_headers.update(headers)

    # Build payload for webhook handler
    payload = {
        'url': url,
        'data': data,
        'headers': webhook_headers,
        'timeout': timeout or 30,
        'webhook_id': webhook_id
    }

    # Set webhook-specific defaults
    if max_retries is None:
        max_retries = getattr(settings, 'JOBS_WEBHOOK_MAX_RETRIES', 5)

    # Validate timeout limits
    max_allowed_timeout = getattr(settings, 'JOBS_WEBHOOK_MAX_TIMEOUT', 300)
    if timeout > max_allowed_timeout:
        raise ValueError(f"Timeout cannot exceed {max_allowed_timeout} seconds")

    # Use the main publish function with webhook handler
    return publish(
        func='mojo.apps.jobs.handlers.webhook.post_webhook',
        payload=payload,
        channel=channel,
        delay=delay,
        run_at=run_at,
        max_retries=max_retries,
        backoff_base=backoff_base,
        backoff_max=backoff_max,
        expires_in=expires_in,
        expires_at=expires_at,
        idempotency_key=idempotency_key
    )


def cancel(job_id: str) -> bool:
    """
    Request cancellation of a job.

    Sets a cooperative cancel flag that the job handler should check.
    The job will only stop if it checks the flag via context.should_cancel().

    Args:
        job_id: Job ID to cancel

    Returns:
        True if cancel was requested, False if job not found or already terminal

    Note:
        This is a cooperative cancel. Jobs must check should_cancel() to stop.
        For hard termination, use max_exec_seconds when publishing the job.
    """
    from .models import Job, JobEvent

    try:
        # Update database
        job = Job.objects.get(id=job_id)

        if job.is_terminal:
            logit.info(f"Job {job_id} already in terminal state: {job.status}")
            return False

        job.cancel_requested = True
        job.save(update_fields=['cancel_requested', 'modified'])

        # DB-only cancellation (KISS): handlers check DB flag

        # Record event
        JobEvent.objects.create(
            job=job,
            channel=job.channel,
            event='canceled',
            details={'requested_at': timezone.now().isoformat()}
        )

        logit.info(f"Requested cancellation of job {job_id}")
        return True

    except Job.DoesNotExist:
        logit.warn(f"Cannot cancel non-existent job: {job_id}")
        return False
    except Exception as e:
        logit.error(f"Failed to cancel job {job_id}: {e}")
        return False


def status(job_id: str) -> Optional[Dict[str, Any]]:
    """
    Get the current status of a job from the database (source of truth).

    Args:
        job_id: Job ID to check

    Returns:
        Status dict with keys: id, status, channel, func, created, started_at,
        finished_at, attempt, last_error, metadata; or None if not found.
    """
    try:
        from .models import Job
        job = Job.objects.get(id=job_id)

        return {
            'id': job.id,
            'status': job.status,
            'channel': job.channel,
            'func': job.func,
            'created': job.created.isoformat() if job.created else '',
            'started_at': job.started_at.isoformat() if job.started_at else '',
            'finished_at': job.finished_at.isoformat() if job.finished_at else '',
            'attempt': job.attempt,
            'last_error': job.last_error,
            'metadata': job.metadata
        }
    except Job.DoesNotExist:
        return None
    except Exception as e:
        logit.error(f"Failed to get status from DB for {job_id}: {e}")
        return None
