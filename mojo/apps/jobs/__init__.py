"""
Django-MOJO Jobs System - Public API

A reliable background job system for Django with Redis fast path and Postgres truth.
"""
import re
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

# Channels the framework itself publishes to. This is the JOBS_CHANNELS
# default, so an out-of-the-box deployment consumes everything the framework
# can produce. Publishing does NOT consult this list — it exists purely so a
# box that never configures JOBS_CHANNELS still runs framework jobs now that
# publish() no longer reroutes unlisted channels to "default".
#
# "certs" covers the DEFAULT of DNSMAN_CERT_SYNC_CHANNEL; override that setting
# and you must add your channel to some consumer's list AND to
# JOBS_ALLOWED_CHANNELS (publish refuses undeclared channels).
DEFAULT_CHANNELS = [
    'default',
    'priority',
    'cleanup',
    'incident_handlers',
    'renditions',
    'certs',
    'webhooks',
    'webhook_fanout',
    # "edge" carries the broadcast that makes a node converge its nginx
    # generation. A deployment that sets JOBS_ALLOWED_CHANNELS must list it
    # THERE too — this list is the CONSUME default, and enforcement reads the
    # other one.
    'edge',
]

# Module-level settings for readability. JOB_CHANNELS is the box's CONSUME
# list; JOBS_ALLOWED_CHANNELS is the deployment's declared user channels.
# The setting doubles as the enforcement switch: None (unset — the default)
# means MONITOR mode, where an undeclared publish still routes as named but
# files a jobs:undeclared_channel incident; any list (even []) turns
# enforcement on and publish() refuses undeclared channels. Upgrading
# deployments therefore never hit a flag day — they get incidents naming the
# channels to declare, and opt into strictness by declaring them.
JOB_CHANNELS = settings.get_static('JOBS_CHANNELS', DEFAULT_CHANNELS)
JOBS_ALLOWED_CHANNELS = settings.get_static('JOBS_ALLOWED_CHANNELS', None)
JOBS_PAYLOAD_MAX_BYTES = settings.get_static('JOBS_PAYLOAD_MAX_BYTES', 16384)
JOBS_DEFAULT_EXPIRES_SEC = settings.get_static('JOBS_DEFAULT_EXPIRES_SEC', 900)
JOBS_DEFAULT_MAX_RETRIES = settings.get_static('JOBS_DEFAULT_MAX_RETRIES', 0)
JOBS_DEFAULT_BACKOFF_BASE = settings.get_static('JOBS_DEFAULT_BACKOFF_BASE', 2.0)
JOBS_DEFAULT_BACKOFF_MAX = settings.get_static('JOBS_DEFAULT_BACKOFF_MAX', 3600)
JOBS_STREAM_MAXLEN = settings.get_static('JOBS_STREAM_MAXLEN', 100000)
JOBS_WEBHOOK_MAX_RETRIES = settings.get_static("JOBS_WEBHOOK_MAX_RETRIES", 5, kind="int")

__all__ = [
    'publish',
    'publish_local',
    'publish_webhook',
    'cancel',
    'status',
    'get_sysinfo',
]

# register_sched_channel/get_sched_channels are framework plumbing — called by
# publish(), the engine's retry path, and the scheduler. Importable, but kept
# out of __all__ because app code has no reason to touch them.


# A channel name becomes a Redis key segment, a metric slug, a log line, and an
# incident title. Publishing no longer clamps unknown channels to "default", so
# this is the one place that keeps those downstream uses safe. Letters, digits,
# underscore, dot and dash only — notably NO colons (the engine recovers the
# channel by splitting the queue key on ":", job_engine.py) and no whitespace or
# control characters (they would forge log lines and skip the unconsumed-channel
# scan). 100 chars matches Job.channel's column width.
CHANNEL_NAME_RE = re.compile(r'^[A-Za-z0-9_.\-]{1,100}$')


def validate_channel_name(channel):
    """
    Raise ValueError unless `channel` is a usable channel name.

    Deliberately a CHARSET check only. Membership — may this box target the
    channel at all — is is_channel_allowed(); publish() enforces both, in
    that order.
    """
    if not channel or not isinstance(channel, str):
        raise ValueError(f"Job channel must be a non-empty string, got {channel!r}")
    if not CHANNEL_NAME_RE.match(channel):
        raise ValueError(
            f"Invalid job channel {channel!r}: use only letters, digits, '_', '.' "
            f"and '-' (max 100 chars). Channel names become Redis keys and log "
            f"fields, so ':' and whitespace are not allowed."
        )
    return channel


# Channels ending in this suffix are box-direct: every engine consumes a
# channel named after its runner id (default "<hostname>-engine"), so
# publish(channel="web-01-engine") targets exactly that box. Hostnames vary
# per deployment and cannot live in a hand-written allowlist, so the suffix
# IS the allow rule — a typo'd host channel passes the gate and is caught by
# the unconsumed-channel incident instead.
ENGINE_CHANNEL_SUFFIX = '-engine'


def channels_enforced():
    """
    Whether this box refuses undeclared channels.

    Declaring JOBS_ALLOWED_CHANNELS (any list, even []) IS the opt-in: a
    deployment that has never heard of the setting keeps 906's route-as-named
    behavior and gets jobs:undeclared_channel incidents instead of failures —
    no flag day on upgrade.
    """
    return JOBS_ALLOWED_CHANNELS is not None


def is_channel_allowed(channel):
    """
    Whether `channel` is a declared publish target from this box.

    Declared: the framework's DEFAULT_CHANNELS, anything this box itself
    consumes (JOBS_CHANNELS), the deployment's declared user channels
    (JOBS_ALLOWED_CHANNELS — set it identically on every box), and box-direct
    channels ending in '-engine'. What happens to everything else depends on
    channels_enforced(): refused with a jobs:rejected_channel incident, or
    (monitor mode) routed anyway with a jobs:undeclared_channel incident.
    """
    return (
        channel in DEFAULT_CHANNELS
        or channel in JOB_CHANNELS
        or channel in (JOBS_ALLOWED_CHANNELS or [])
        or channel.endswith(ENGINE_CHANNEL_SUFFIX)
    )


# One undeclared/rejected-channel incident per channel per window — a code bug
# retrying publish() in a loop must not flood the incident log.
REJECTED_RENOTIFY_SEC = 3600


def _undeclared_notice_key(channel, enforced):
    kind = "rejected" if enforced else "undeclared"
    return f"{JobKeys().prefix}:alerted:{kind}:{channel}"


def _report_undeclared_channel(channel, func_path, enforced):
    """File a suppressed incident for an undeclared publish target. Never
    raises — in enforced mode the caller is already about to raise the
    ValueError that matters, and in monitor mode the publish must proceed."""
    try:
        redis = get_adapter()
        notice_key = _undeclared_notice_key(channel, enforced)
        if redis.get(notice_key) is not None:
            return
        redis.set(notice_key, "1", ex=REJECTED_RENOTIFY_SEC)
    except Exception as e:
        logit.warning(f"jobs: undeclared-channel suppression check failed: {e}")
    if enforced:
        body = (
            f"jobs.publish(channel='{channel}') was refused and the job was NOT "
            f"queued: the channel is not in DEFAULT_CHANNELS, this box's "
            f"JOBS_CHANNELS, or JOBS_ALLOWED_CHANNELS, and does not end in "
            f"'{ENGINE_CHANNEL_SUFFIX}'. Publisher func: {func_path}. If the "
            f"channel is real, add it to JOBS_ALLOWED_CHANNELS on every box."
        )
        title = f"Rejected job channel: {channel}"
        category = "jobs:rejected_channel"
    else:
        body = (
            f"jobs.publish(channel='{channel}') targets an undeclared channel. "
            f"The job WAS queued (JOBS_ALLOWED_CHANNELS is not set, so "
            f"enforcement is off). Publisher func: {func_path}. Declare your "
            f"user channels in JOBS_ALLOWED_CHANNELS on every box — once set, "
            f"undeclared publishes are refused instead of reported."
        )
        title = f"Undeclared job channel: {channel}"
        category = "jobs:undeclared_channel"
    try:
        from mojo.apps import incident
        incident.report_event(
            body,
            title=title,
            category=category,
            level=3,
            channel=channel,
            func=func_path)
    except Exception as e:
        logit.error(f"jobs: failed to report undeclared channel {channel}: {e}")


def register_sched_channel(channel):
    """
    Record that `channel` has scheduled (delayed or retrying) work.

    The cluster runs ONE scheduler, but each box configures only the channels
    it consumes. Without this registry the lock-winning scheduler would never
    promote another box's delayed jobs. Called by every writer that puts a job
    into a sched ZSET.

    Never raises: losing a registry write degrades promotion latency, it must
    not fail the publish that triggered it.
    """
    try:
        get_adapter().get_client().sadd(JobKeys().sched_registry(), channel)
    except Exception as e:
        logit.warning(f"jobs: failed to register sched channel {channel}: {e}")


def get_sched_channels():
    """
    Channels known to have scheduled work — see register_sched_channel().

    Returns a sorted list; an empty list means the registry is genuinely empty.
    Redis errors PROPAGATE on purpose: a caller that merges this into its own
    channel list has to tell "nothing registered" apart from "could not read",
    or a transient blip would silently drop channels it was already serving.
    """
    members = get_adapter().get_client().smembers(JobKeys().sched_registry())
    return sorted(
        m.decode('utf-8') if isinstance(m, (bytes, bytearray)) else m
        for m in (members or [])
    )


# Dotted paths run when a runner daemon comes up — registered from an app's
# AppConfig.ready(), so registration happens in EVERY Django process and stays
# inert: hooks fire only from JobEngine.start(), the entry point unique to a
# real engine (never a shell, a management command, or the test runner).
ENGINE_STARTUP_HOOKS = []


def register_startup_hook(func_path):
    """
    Register a dotted function path to run when a job engine starts.

    The function is called with the JobEngine instance, on the engine's own
    worker pool: a hook that raises is logged and never stops the engine, and
    a slow one never delays job consumption. Exists so a node can reconcile
    itself because it booted, rather than depending on a broadcast it may
    have been down for (maestro #1772).
    """
    if func_path not in ENGINE_STARTUP_HOOKS:
        ENGINE_STARTUP_HOOKS.append(func_path)


def get_startup_hooks():
    """Registered startup hook paths — see register_startup_hook()."""
    return list(ENGINE_STARTUP_HOOKS)


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
        channel: Channel to publish to (default: "default"). Must be an
            allowed target: a framework DEFAULT_CHANNELS entry, a channel this
            box consumes (JOBS_CHANNELS), a declared user channel
            (JOBS_ALLOWED_CHANNELS), or a box-direct channel ending
            '-engine'. Routed verbatim — the job lands on this channel whether
            or not this box consumes it, which is how work is handed to
            another box. An unlisted channel raises ValueError and files a
            jobs:rejected_channel incident.
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

    # Fail before any DB write. A malformed channel would otherwise reach a
    # Redis key, a metric slug, and an incident title verbatim.
    validate_channel_name(channel)

    # Gate undeclared channels. The pre-1.2.62 reroute-to-default hid typos by
    # running the job in the wrong place; routing-as-named alone let a typo
    # strand the job on a queue nobody consumes. With JOBS_ALLOWED_CHANNELS
    # set, an undeclared channel is refused loudly at the call site; unset
    # (monitor mode) it still routes as named but files an incident naming the
    # channel and publisher, so upgrading deployments learn their channel list
    # instead of breaking.
    if not is_channel_allowed(channel):
        enforced = channels_enforced()
        _report_undeclared_channel(channel, func_path, enforced)
        if enforced:
            raise ValueError(
                f"Job channel {channel!r} is not a declared publish target. "
                f"Allowed: framework DEFAULT_CHANNELS, this box's JOBS_CHANNELS, "
                f"the deployment's JOBS_ALLOWED_CHANNELS, and box-direct channels "
                f"ending '{ENGINE_CHANNEL_SUFFIX}'. Add it to "
                f"JOBS_ALLOWED_CHANNELS to declare it."
            )

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

    # From here the channel is used verbatim — never rerouted. An allowed
    # channel this box does not consume is a supported target (that is how work
    # is handed to another box); an allowed channel nobody consumes is surfaced
    # by the jobs:unconsumed_channel incident (see cronjobs), not by silently
    # running the job somewhere it does not belong.

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
    mirror_existing_job = False
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
        if idempotency_key and ('UNIQUE constraint' in str(e) or 'duplicate key' in str(e)):
            # Idempotent request - return existing job ID
            try:
                existing = Job.objects.get(idempotency_key=idempotency_key)
                can_resume = (
                    existing.status == 'failed'
                    and existing.attempt == 0
                    and existing.last_error.startswith('Failed to queue:')
                )
                if not can_resume and existing.status != 'pending':
                    logit.info(f"Idempotent job request, returning existing: {existing.id}")
                    return existing.id
                if can_resume and (
                    existing.func != func_path
                    or existing.payload != payload
                    or existing.channel != channel
                    or existing.broadcast != broadcast
                ):
                    raise ValueError(
                        "Idempotency key belongs to different failed job content"
                    )
                with transaction.atomic():
                    existing = Job.objects.select_for_update().get(
                        idempotency_key=idempotency_key,
                    )
                    if (
                        existing.status == 'failed'
                        and existing.attempt == 0
                        and existing.last_error.startswith('Failed to queue:')
                    ):
                        existing.status = 'pending'
                        existing.last_error = ''
                        existing.stack_trace = ''
                        existing.finished_at = None
                        existing.save(update_fields=[
                            'status', 'last_error', 'stack_trace', 'finished_at',
                            'modified',
                        ])
                    elif existing.status != 'pending':
                        return existing.id
                job = existing
                job_id = existing.id
                channel = existing.channel
                run_at = existing.run_at
                broadcast = existing.broadcast
                now = timezone.now()
                mirror_existing_job = True
                logit.info(f"Mirroring existing idempotent job: {existing.id}")
            except Job.DoesNotExist:
                pass
        if not mirror_existing_job:
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

            # So the cluster's single scheduler promotes this even if the box it
            # runs on does not consume `channel`.
            register_sched_channel(channel)

            logit.info(f"Scheduled job {job_id} on {channel} for {run_at} "
                       f"(zset={'sched_broadcast' if broadcast else 'sched'})")
        else:
            # Immediate execution: enqueue to List queue (Plan B)
            queue_key = keys.queue(channel)
            redis.rpush(queue_key, job_id)

            logit.info(f"Queued job {job_id} on {channel} (broadcast={broadcast}) to queue {queue_key}")

    except Exception as e:
        logit.error(f"Failed to mirror job {job_id} to Redis: {e}")
        # Mark job as failed in DB since it couldn't be queued
        job.status = 'failed'
        job.last_error = f"Failed to queue: {e}"
        job.save(update_fields=['status', 'last_error', 'modified'])
        raise RuntimeError(f"Failed to queue job: {e}")

    try:
        JobEvent.objects.create(
            job=job,
            channel=channel,
            event='scheduled' if run_at and run_at > now else 'queued',
            details=(
                {'run_at': run_at.isoformat()}
                if run_at and run_at > now
                else {'queue': keys.queue(channel)}
            ),
        )
    except Exception as e:
        logit.warning(f"Failed to record publish event for job {job_id}: {e}")

    try:
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
        logit.warning(f"Failed to record publish metrics for job {job_id}: {e}")

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
    group=None,
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
        group: Optional account.Group (or int id). When provided, the job
               handler signs the outbound body with the Group's webhook secret
               and adds a signature header at delivery time — `X-Mojo-Signature`
               by default, or the WEBHOOK_SIGNATURE_HEADER setting if configured.
               The secret is never stored in the queue — only the group id.
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

        # Signed webhook — handler injects the signature header
        # (X-Mojo-Signature by default) at delivery
        job_id = publish_webhook(
            url=receiver_url,
            data={"event": "verification_complete", "customer_id": 42},
            group=customer.group,
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

    # Build headers with defaults. The User-Agent default is overridable via the
    # JOBS_WEBHOOK_USER_AGENT setting so operators need not advertise the
    # framework; a caller-supplied User-Agent in `headers` still wins (merge below).
    webhook_headers = {
        'Content-Type': 'application/json',
        'User-Agent': settings.get_static('JOBS_WEBHOOK_USER_AGENT', 'Django-MOJO-Webhook/1.0')
    }
    if headers:
        webhook_headers.update(headers)

    # Resolve sign_group_id without storing the secret in the queue payload
    sign_group_id = None
    if group is not None:
        sign_group_id = group.id if hasattr(group, "id") else int(group)

    # Build payload for webhook handler
    payload = {
        'url': url,
        'data': data,
        'headers': webhook_headers,
        'timeout': timeout or 30,
        'webhook_id': webhook_id,
        'sign_group_id': sign_group_id,
    }

    # Set webhook-specific defaults
    if max_retries is None:
        max_retries = JOBS_WEBHOOK_MAX_RETRIES

    # Validate timeout limits
    max_allowed_timeout = int(settings.get('JOBS_WEBHOOK_MAX_TIMEOUT', 300))
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


def broadcast_command(command, data=None, timeout=2.0):
    from .manager import get_manager
    manager = get_manager()
    return manager.broadcast_command(command, data, timeout)

def broadcast_execute(func_path, data=None, timeout=2.0, collect_replies=False):
    from .manager import get_manager
    manager = get_manager()
    return manager.broadcast_execute(func_path, data, timeout, collect_replies)

def ping(runnder_id, timeout=2.0):
    from .manager import get_manager
    manager = get_manager()
    return manager.ping(runnder_id, timeout)

def get_runners(channel=None):
    from .manager import get_manager
    manager = get_manager()
    return manager.get_runners(channel)


def get_sysinfo(runner_id=None, timeout=5.0):
    """
    Collect host system info from one or all active runners.

    Sends a broadcast-execute (or targeted execute) to runners, each of which
    calls sysinfo.get_host_info() in-process and replies with the result.

    Args:
        runner_id: Target a specific runner by ID. If None, collects from all
                   active runners via broadcast.
        timeout:   Seconds to wait for replies (default 5.0).

    Returns:
        List of reply dicts, one per responding runner. Each dict contains:
            - runner_id   (str)
            - func        (str)
            - status      ("success" | "error")
            - timestamp   (ISO string)
            - result      (dict from sysinfo.get_host_info(), on success)
            - error       (str, on failure)

    Example:
        from mojo.apps import jobs

        # All runners
        info = jobs.get_sysinfo()

        # Single runner
        info = jobs.get_sysinfo(runner_id='runner-host1-abc123')
    """
    from .manager import get_manager
    manager = get_manager()
    func_path = "mojo.apps.jobs.services.sysinfo_task.collect_sysinfo"
    if runner_id:
        result = manager.execute_on_runner(runner_id, func_path, timeout=timeout)
        return [result] if result else []
    return manager.broadcast_execute(func_path, collect_replies=True, timeout=timeout)
