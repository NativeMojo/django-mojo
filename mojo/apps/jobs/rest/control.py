from mojo import decorators as md
from mojo.helpers.response import JsonResponse
from mojo.apps.jobs.models import Job, JobEvent
from mojo.apps.jobs.manager import get_manager
from mojo.apps.jobs.adapters import get_adapter
from mojo.apps.jobs.keys import JobKeys
from django.utils import timezone
from django.db.models import Q
from datetime import datetime, timedelta


# Get runtime configuration
@md.GET('jobs/control/config')
@md.requires_perms('manage_jobs')
def on_get_config(request):
    """Get current jobs system configuration."""
    from django.conf import settings

    config = {
        'redis_url': getattr(settings, 'JOBS_REDIS_URL', 'redis://localhost:6379/0'),
        'redis_prefix': getattr(settings, 'JOBS_REDIS_PREFIX', 'mojo:jobs'),
        'engine': {
            'max_workers': getattr(settings, 'JOBS_ENGINE_MAX_WORKERS', 10),
            'claim_buffer': getattr(settings, 'JOBS_ENGINE_CLAIM_BUFFER', 2),
            'claim_batch': getattr(settings, 'JOBS_ENGINE_CLAIM_BATCH', 5),
            'read_timeout': getattr(settings, 'JOBS_ENGINE_READ_TIMEOUT', 100),
        },
        'defaults': {
            'channel': getattr(settings, 'JOBS_DEFAULT_CHANNEL', 'default'),
            'expires_sec': getattr(settings, 'JOBS_DEFAULT_EXPIRES_SEC', 900),
            'max_retries': getattr(settings, 'JOBS_DEFAULT_MAX_RETRIES', 3),
            'backoff_base': getattr(settings, 'JOBS_DEFAULT_BACKOFF_BASE', 2.0),
            'backoff_max': getattr(settings, 'JOBS_DEFAULT_BACKOFF_MAX', 3600),
        },
        'limits': {
            'payload_max_bytes': getattr(settings, 'JOBS_PAYLOAD_MAX_BYTES', 1048576),
            'stream_maxlen': getattr(settings, 'JOBS_STREAM_MAXLEN', 100000),
            'local_queue_maxsize': getattr(settings, 'JOBS_LOCAL_QUEUE_MAXSIZE', 1000),
        },
        'timeouts': {
            'idle_timeout_ms': getattr(settings, 'JOBS_IDLE_TIMEOUT_MS', 60000),
            'xpending_idle_ms': getattr(settings, 'JOBS_XPENDING_IDLE_MS', 60000),
            'runner_heartbeat_sec': getattr(settings, 'JOBS_RUNNER_HEARTBEAT_SEC', 5),
            'scheduler_lock_ttl_ms': getattr(settings, 'JOBS_SCHEDULER_LOCK_TTL_MS', 5000),
        },
        'channels': getattr(settings, 'JOBS_CHANNELS', ['default'])
    }

    return JsonResponse({
        'status': True,
        'data': config
    })


# Clear stuck jobs
@md.POST('jobs/control/clear-stuck')
@md.requires_perms('manage_jobs')
@md.requires_params('channel')
def on_clear_stuck_jobs(request):
    """
    Clear stuck jobs from a channel.

    Params:
        channel: Channel to clear stuck jobs from
        idle_threshold_ms: Consider stuck if idle longer than this (default: 60000)
    """
    try:
        channel = request.DATA['channel']
        idle_threshold_ms = int(request.DATA.get('idle_threshold_ms', 60000))

        manager = get_manager()
        redis = get_adapter()
        keys = JobKeys()

        # Find stuck jobs
        stuck = manager._find_stuck_jobs(channel, idle_threshold_ms)

        if not stuck:
            return JsonResponse({
                'status': True,
                'message': 'No stuck jobs found',
                'cleared': 0
            })

        # Reclaim stuck jobs
        stream_key = keys.stream(channel)
        group_key = keys.group_workers(channel)
        cleared = 0

        for job_info in stuck:
            try:
                # Try to claim the message
                claimed = redis.xclaim(
                    stream_key,
                    group_key,
                    'reclaimer',
                    idle_threshold_ms,
                    job_info['message_id']
                )

                if claimed:
                    # ACK to remove from pending
                    redis.xack(stream_key, group_key, job_info['message_id'])
                    cleared += 1

                    # Update job status in DB
                    # Extract job_id from message if possible
                    for msg_id, data in claimed:
                        job_id = data.get(b'job_id', b'').decode('utf-8')
                        if job_id:
                            try:
                                job = Job.objects.get(id=job_id)
                                if job.status == 'running':
                                    job.status = 'failed'
                                    job.last_error = f'Job was stuck (idle for {idle_threshold_ms}ms)'
                                    job.finished_at = timezone.now()
                                    job.save(update_fields=['status', 'last_error', 'finished_at'])

                                    JobEvent.objects.create(
                                        job=job,
                                        channel=channel,
                                        event='failed',
                                        details={'reason': 'stuck', 'idle_ms': idle_threshold_ms}
                                    )
                            except Job.DoesNotExist:
                                pass

            except Exception as e:
                # Log but continue with other stuck jobs
                pass

        return JsonResponse({
            'status': True,
            'message': f'Cleared {cleared} stuck jobs from {channel}',
            'cleared': cleared,
            'total_stuck': len(stuck)
        })

    except Exception as e:
        return JsonResponse({
            'status': False,
            'error': str(e)
        }, status=400)


# Purge old job data
@md.POST('jobs/control/purge')
@md.requires_perms('manage_jobs')
@md.requires_params('days_old')
def on_purge_old_jobs(request):
    """
    Purge old job data from the database.

    Params:
        days_old: Delete jobs older than this many days
        status: Optional status filter (completed, failed, cancelled)
        dry_run: If true, only count without deleting
    """
    try:
        days_old = int(request.DATA['days_old'])
        status_filter = request.DATA.get('status')
        dry_run = request.DATA.get('dry_run', False)

        # Calculate cutoff date
        cutoff = timezone.now() - timedelta(days=days_old)

        # Build query
        query = Q(created__lt=cutoff)
        if status_filter:
            query &= Q(status=status_filter)

        # Get jobs to delete
        jobs_to_delete = Job.objects.filter(query)
        count = jobs_to_delete.count()

        if dry_run:
            # Just return count
            return JsonResponse({
                'status': True,
                'message': f'Would delete {count} jobs older than {days_old} days',
                'count': count,
                'dry_run': True
            })

        # Actually delete (cascades to JobEvent)
        deleted, details = jobs_to_delete.delete()

        return JsonResponse({
            'status': True,
            'message': f'Deleted {deleted} records older than {days_old} days',
            'deleted': deleted,
            'details': details
        })

    except Exception as e:
        return JsonResponse({
            'status': False,
            'error': str(e)
        }, status=400)


# Reset failed jobs
@md.POST('jobs/control/reset-failed')
@md.requires_perms('manage_jobs')
def on_reset_failed_jobs(request):
    """
    Reset failed jobs to pending status for retry.

    Params:
        channel: Optional channel filter
        since: Optional datetime filter (ISO format)
        limit: Maximum number to reset (default: 100)
    """
    try:
        channel = request.DATA.get('channel')
        since = request.DATA.get('since')
        limit = int(request.DATA.get('limit', 100))

        # Build query
        query = Q(status='failed')
        if channel:
            query &= Q(channel=channel)
        if since:
            since_dt = datetime.fromisoformat(since)
            query &= Q(created__gte=since_dt)

        # Get failed jobs
        failed_jobs = Job.objects.filter(query).order_by('-created')[:limit]

        reset_count = 0
        redis = get_adapter()
        keys = JobKeys()

        for job in failed_jobs:
            # Reset to pending
            job.status = 'pending'
            job.attempt = 0
            job.last_error = ''
            job.stack_trace = ''
            job.run_at = None
            job.save(update_fields=['status', 'attempt', 'last_error', 'stack_trace', 'run_at'])

            # Re-queue in Redis
            stream_key = keys.stream(job.channel)
            redis.xadd(stream_key, {
                'job_id': job.id,
                'func': job.func,
                'created': timezone.now().isoformat()
            })

            # Add event
            JobEvent.objects.create(
                job=job,
                channel=job.channel,
                event='retry',
                details={'reset': True, 'original_status': 'failed'}
            )

            reset_count += 1

        return JsonResponse({
            'status': True,
            'message': f'Reset {reset_count} failed jobs to pending',
            'reset_count': reset_count
        })

    except Exception as e:
        return JsonResponse({
            'status': False,
            'error': str(e)
        }, status=400)


# Clear Redis queues
@md.POST('jobs/control/clear-queue')
@md.requires_perms('manage_jobs')
@md.requires_params('channel')
def on_clear_queue(request):
    """
    Clear all messages from a channel's Redis queue.
    WARNING: This will delete all pending jobs!

    Params:
        channel: Channel to clear
        confirm: Must be "yes" to confirm deletion
    """
    try:
        channel = request.DATA['channel']
        confirm = request.DATA.get('confirm')

        if confirm != 'yes':
            return JsonResponse({
                'status': False,
                'error': 'Must confirm with confirm="yes"'
            }, status=400)

        redis = get_adapter()
        keys = JobKeys()

        # Delete streams
        stream_key = keys.stream(channel)
        broadcast_key = keys.stream_broadcast(channel)
        sched_key = keys.sched(channel)

        deleted_stream = redis.delete(stream_key)
        deleted_broadcast = redis.delete(broadcast_key)
        deleted_sched = redis.delete(sched_key)

        # Update pending jobs in DB
        pending_count = Job.objects.filter(
            channel=channel,
            status='pending'
        ).update(
            status='cancelled',
            finished_at=timezone.now()
        )

        return JsonResponse({
            'status': True,
            'message': f'Cleared queue for channel {channel}',
            'deleted': {
                'stream': bool(deleted_stream),
                'broadcast': bool(deleted_broadcast),
                'scheduled': bool(deleted_sched),
                'pending_jobs': pending_count
            }
        })

    except Exception as e:
        return JsonResponse({
            'status': False,
            'error': str(e)
        }, status=400)


# Get queue sizes
@md.GET('jobs/control/queue-sizes')
@md.requires_perms('view_jobs', 'manage_jobs')
def on_get_queue_sizes(request):
    """Get current queue sizes for all channels."""
    try:
        from django.conf import settings
        redis = get_adapter()
        keys = JobKeys()

        channels = getattr(settings, 'JOBS_CHANNELS', ['default'])
        sizes = {}

        for channel in channels:
            stream_key = keys.stream(channel)
            sched_key = keys.sched(channel)

            try:
                # Get stream length
                stream_info = redis.xinfo_stream(stream_key)
                stream_len = stream_info.get('length', 0)
            except:
                stream_len = 0

            # Get scheduled count
            sched_count = redis.zcard(sched_key)

            # Get DB counts
            db_counts = Job.objects.filter(channel=channel).values('status').annotate(
                count=models.Count('id')
            )

            status_counts = {item['status']: item['count'] for item in db_counts}

            sizes[channel] = {
                'stream': stream_len,
                'scheduled': sched_count,
                'db_pending': status_counts.get('pending', 0),
                'db_running': status_counts.get('running', 0),
                'db_completed': status_counts.get('completed', 0),
                'db_failed': status_counts.get('failed', 0)
            }

        return JsonResponse({
            'status': True,
            'data': sizes
        })

    except Exception as e:
        return JsonResponse({
            'status': False,
            'error': str(e)
        }, status=400)


# Force scheduler leadership
@md.POST('jobs/control/force-scheduler-lead')
@md.requires_perms('manage_jobs')
def on_force_scheduler_lead(request):
    """
    Force release scheduler lock to allow a new leader.
    WARNING: Only use if scheduler is stuck!
    """
    try:
        redis = get_adapter()
        keys = JobKeys()

        lock_key = keys.scheduler_lock()

        # Check current lock
        current = redis.get(lock_key)

        if not current:
            return JsonResponse({
                'status': True,
                'message': 'No scheduler lock exists',
                'previous_holder': None
            })

        # Delete the lock
        redis.delete(lock_key)

        return JsonResponse({
            'status': True,
            'message': 'Scheduler lock released',
            'previous_holder': current
        })

    except Exception as e:
        return JsonResponse({
            'status': False,
            'error': str(e)
        }, status=400)


# Test job execution
@md.POST('jobs/control/test')
@md.requires_perms('manage_jobs')
def on_test_job(request):
    """
    Publish a test job to verify the system is working.

    Params:
        channel: Channel to test (default: "default")
        delay: Optional delay in seconds
    """
    try:
        from mojo.apps.jobs import publish

        channel = request.DATA.get('channel', 'default')
        delay = request.DATA.get('delay')

        # Define a simple test function module path
        # This assumes you have a test job function available
        test_func = 'mojo.apps.jobs.examples.sample_jobs.generate_report'

        # Publish test job
        job_id = publish(
            func=test_func,
            payload={
                'test': True,
                'timestamp': timezone.now().isoformat(),
                'channel': channel,
                'report_type': 'test',
                'start_date': timezone.now().date().isoformat(),
                'end_date': timezone.now().date().isoformat(),
                'format': 'pdf'
            },
            channel=channel,
            delay=int(delay) if delay else None
        )

        return JsonResponse({
            'status': True,
            'message': 'Test job published',
            'job_id': job_id,
            'channel': channel,
            'delayed': bool(delay)
        })

    except Exception as e:
        return JsonResponse({
            'status': False,
            'error': str(e)
        }, status=400)
