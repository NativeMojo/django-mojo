"""
Django-MOJO Jobs System - Public API

A reliable background job system for Django with Redis fast path and Postgres truth.
"""
import uuid
from datetime import datetime, timedelta
from typing import Any, Callable, Dict, Optional, Union

from django.conf import settings
from django.utils import timezone
from django.db import transaction

from mojo.helpers import logit
from mojo.helpers.settings import settings as mojo_settings

from .registry import async_job, local_async_job, get_job_function, list_jobs
from .keys import JobKeys
from .adapters import get_adapter
from .context import JobContext


__all__ = [
    'publish',
    'publish_local',
    'cancel',
    'status',
    'async_job',
    'local_async_job',
    'JobContext',
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

    # Resolve function name
    if callable(func):
        if hasattr(func, '_job_name'):
            func_name = func._job_name
        else:
            raise ValueError(f"Function {func} is not registered as a job")
    else:
        func_name = func
        if not get_job_function(func_name):
            raise ValueError(f"No job registered with name: {func_name}")

    # Validate payload
    payload = payload or {}
    if not isinstance(payload, dict):
        raise ValueError("Payload must be a dictionary")

    # Check payload size
    import json
    payload_json = json.dumps(payload)
    max_bytes = getattr(settings, 'JOBS_PAYLOAD_MAX_BYTES', 16384)
    if len(payload_json.encode('utf-8')) > max_bytes:
        raise ValueError(f"Payload exceeds maximum size of {max_bytes} bytes")

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
        default_expire = getattr(settings, 'JOBS_DEFAULT_EXPIRES_SEC', 900)
        expires_at = now + timedelta(seconds=default_expire)

    # Apply defaults
    if max_retries is None:
        max_retries = getattr(settings, 'JOBS_DEFAULT_MAX_RETRIES', 3)
    if backoff_base is None:
        backoff_base = getattr(settings, 'JOBS_DEFAULT_BACKOFF_BASE', 2.0)
    if backoff_max is None:
        backoff_max = getattr(settings, 'JOBS_DEFAULT_BACKOFF_MAX', 3600)

    # Create job in database
    try:
        with transaction.atomic():
            job = Job.objects.create(
                id=job_id,
                channel=channel,
                func=func_name,
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
                details={'func': func_name, 'channel': channel}
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

    # Mirror to Redis
    try:
        redis = get_adapter()
        keys = JobKeys()

        # Store job metadata in Redis hash
        redis.hset(keys.job(job_id), {
            'status': 'pending',
            'channel': channel,
            'func': func_name,
            'payload': json.dumps(payload),
            'expires_at': expires_at.isoformat() if expires_at else '',
            'run_at': run_at.isoformat() if run_at else '',
            'attempt': 0,
            'max_retries': max_retries,
            'broadcast': '1' if broadcast else '0',
            'max_exec_seconds': max_exec_seconds or '',
            'created_at': now.isoformat()
        })

        # Route based on scheduling
        if run_at and run_at > now:
            # Add to scheduled ZSET
            score = run_at.timestamp() * 1000  # milliseconds
            redis.zadd(keys.sched(channel), {job_id: score})

            # Record scheduled event
            JobEvent.objects.create(
                job=job,
                channel=channel,
                event='scheduled',
                details={'run_at': run_at.isoformat()}
            )

            logit.info(f"Scheduled job {job_id} on {channel} for {run_at}")
        else:
            # Add to stream for immediate execution
            stream_key = keys.stream_broadcast(channel) if broadcast else keys.stream(channel)
            redis.xadd(stream_key, {
                'job_id': job_id,
                'func': func_name,
                'created': now.isoformat()
            }, maxlen=getattr(settings, 'JOBS_STREAM_MAXLEN', 100000))

            # Record queued event
            JobEvent.objects.create(
                job=job,
                channel=channel,
                event='queued',
                details={'stream': stream_key}
            )

            logit.info(f"Queued job {job_id} on {channel} (broadcast={broadcast})")

        # Emit metric
        from mojo.metrics.redis_metrics import record_metrics
        record_metrics(
            slug="jobs.published",
            when=now,
            count=1,
            category="jobs",
            args=[channel, func_name]
        )

    except Exception as e:
        logit.error(f"Failed to mirror job {job_id} to Redis: {e}")
        # Mark job as failed in DB since it couldn't be queued
        job.status = 'failed'
        job.last_error = f"Failed to queue: {e}"
        job.save(update_fields=['status', 'last_error', 'modified'])
        raise RuntimeError(f"Failed to queue job: {e}")

    return job_id


def publish_local(func: Union[str, Callable], *args, **kwargs) -> str:
    """
    Publish a job to the local in-process queue.

    Args:
        func: Job function (registered name or callable)
        *args: Positional arguments for the job
        **kwargs: Keyword arguments for the job

    Returns:
        Job ID (for compatibility, though local jobs aren't persistent)

    Raises:
        ValueError: If func is not registered as a local job
        RuntimeError: If local queue is full
    """
    from .local_queue import get_local_queue

    # Resolve function
    if callable(func):
        if hasattr(func, '_job_name'):
            func_name = func._job_name
            func_obj = func
        else:
            raise ValueError(f"Function {func} is not registered as a local job")
    else:
        func_name = func
        from .registry import get_local_function
        func_obj = get_local_function(func_name)
        if not func_obj:
            raise ValueError(f"No local job registered with name: {func_name}")

    # Generate a pseudo job ID
    job_id = f"local-{uuid.uuid4().hex[:8]}"

    # Queue the job
    queue = get_local_queue()
    if not queue.put(func_obj, args, kwargs, job_id):
        raise RuntimeError("Local job queue is full")

    logit.info(f"Queued local job {job_id} ({func_name})")
    return job_id


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

        # Update Redis if job is active
        try:
            redis = get_adapter()
            keys = JobKeys()
            redis.hset(keys.job(job_id), {'cancel_requested': '1'})
        except Exception as e:
            logit.warn(f"Failed to set cancel flag in Redis for {job_id}: {e}")

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
    Get the current status of a job.

    Tries Redis first for speed, falls back to database.

    Args:
        job_id: Job ID to check

    Returns:
        Status dict with keys:
            - id: Job ID
            - status: Current status
            - channel: Job channel
            - func: Function name
            - created: Creation time
            - started_at: Execution start time (if started)
            - finished_at: Completion time (if finished)
            - attempt: Current attempt number
            - last_error: Last error message (if any)
            - metadata: Custom metadata
        Or None if job not found
    """
    # Try Redis first
    try:
        redis = get_adapter()
        keys = JobKeys()
        job_data = redis.hgetall(keys.job(job_id))

        if job_data:
            import json
            return {
                'id': job_id,
                'status': job_data.get('status', 'unknown'),
                'channel': job_data.get('channel', ''),
                'func': job_data.get('func', ''),
                'created': job_data.get('created_at', ''),
                'started_at': job_data.get('started_at', ''),
                'finished_at': job_data.get('finished_at', ''),
                'attempt': int(job_data.get('attempt', 0)),
                'last_error': job_data.get('last_error', ''),
                'metadata': json.loads(job_data.get('metadata', '{}'))
            }
    except Exception as e:
        logit.warn(f"Failed to get status from Redis for {job_id}: {e}")

    # Fall back to database
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
