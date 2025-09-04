from mojo import decorators as md
from mojo.helpers.response import JsonResponse
from mojo.apps.jobs.models import Job
from mojo.apps.jobs.manager import get_manager
from django.utils import timezone
from django.db.models import Q
from mojo.apps.jobs.adapters import get_adapter
from mojo.apps.jobs.keys import JobKeys

from datetime import datetime


# Get runtime configuration
@md.GET('control/config')
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
@md.POST('control/clear-stuck')
@md.requires_perms('manage_jobs')
@md.requires_params('channel')
def on_clear_stuck_jobs(request):
    """
    Clear stuck jobs from a channel using JobManager methods.

    Params:
        channel: Channel to clear stuck jobs from
        idle_threshold_ms: Consider stuck if idle longer than this (default: 60000)
    """
    try:
        channel = request.DATA['channel']
        idle_threshold_ms = int(request.DATA.get('idle_threshold_ms', 60000))

        manager = get_manager()
        result = manager.clear_stuck_jobs(channel, idle_threshold_ms=idle_threshold_ms)

        return JsonResponse({
            'status': True,
            'message': result.get('message', f'Cleared {result.get("cleared", 0)} stuck jobs from {channel}'),
            'data': result
        })

    except Exception as e:
        return JsonResponse({
            'status': False,
            'error': str(e)
        }, status=400)


# Add a simpler manual reclaim endpoint
@md.POST('jobs/control/manual-reclaim')
@md.requires_perms('manage_jobs')
@md.requires_params('channel')
def on_manual_reclaim_jobs(request):
    """
    Manually reclaim all pending jobs in a channel.
    Uses the clear_stuck_jobs method from JobManager.
    """
    try:
        channel = request.DATA['channel']

        manager = get_manager()
        result = manager.clear_stuck_jobs(channel, idle_threshold_ms=0)  # Clear all pending jobs

        return JsonResponse({
            'status': True,
            'message': result.get('message', f'Manually reclaimed {result.get("cleared", 0)} jobs from {channel}'),
            'data': result
        })

    except Exception as e:
        return JsonResponse({
            'status': False,
            'error': str(e)
        }, status=400)


# Purge old job data
@md.POST('control/purge')
@md.requires_perms('manage_jobs')
@md.requires_params('days_old')
def on_purge_old_jobs(request):
    """
    Purge old job data via JobManager.
    """
    try:
        days_old = int(request.DATA['days_old'])
        status_filter = request.DATA.get('status')
        dry_run = bool(request.DATA.get('dry_run', False))

        manager = get_manager()
        result = manager.purge_old_jobs(days_old=days_old, status=status_filter, dry_run=dry_run)

        if result.get('status'):
            return JsonResponse({
                'status': True,
                'data': result
            })
        else:
            return JsonResponse({
                'status': False,
                'error': result.get('error', 'Unknown error')
            }, status=400)

    except Exception as e:
        return JsonResponse({
            'status': False,
            'error': str(e)
        }, status=400)


# Reset failed jobs
@md.POST('control/reset-failed')
@md.requires_perms('manage_jobs')
def on_reset_failed_jobs(request):
    """
    Reset failed jobs to pending status for retry and requeue via JobManager.

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

        # Capture affected channels for requeue
        affected_channels = list(
            Job.objects.filter(query).values_list('channel', flat=True).distinct()
        )

        # Reset to pending in bulk (select IDs first, then update)
        reset_ids = list(
            Job.objects.filter(query)
            .order_by('-created')
            .values_list('id', flat=True)[:limit]
        )
        reset_count = 0
        if reset_ids:
            reset_count = Job.objects.filter(id__in=reset_ids).update(
                status='pending',
                attempt=0,
                last_error='',
                stack_trace='',
                run_at=None
            )

        # Requeue using JobManager
        manager = get_manager()
        requeue_results = []

        if channel:
            requeue_results.append(manager.requeue_db_pending(channel, limit=reset_count))
        else:
            for ch in affected_channels:
                requeue_results.append(manager.requeue_db_pending(ch, limit=None))

        return JsonResponse({
            'status': True,
            'message': f'Reset {reset_count} failed jobs to pending',
            'reset_count': reset_count,
            'requeue': requeue_results
        })

    except Exception as e:
        return JsonResponse({
            'status': False,
            'error': str(e)
        }, status=400)


# Clear Redis queues
@md.POST('control/clear-queue')
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

        manager = get_manager()
        result = manager.clear_channel(channel, cancel_db_pending=True)

        return JsonResponse({
            'status': result.get('status', True),
            'message': f'Cleared queue for channel {channel}',
            'data': result
        })

    except Exception as e:
        return JsonResponse({
            'status': False,
            'error': str(e)
        }, status=400)


# Get queue sizes
@md.GET('control/queue-sizes')
@md.requires_perms('view_jobs', 'manage_jobs')
def on_get_queue_sizes(request):
    """Get current queue sizes for all channels via JobManager."""
    try:
        manager = get_manager()
        result = manager.get_queue_sizes()
        if result.get('status'):
            return JsonResponse({
                'status': True,
                'data': result.get('data', {})
            })
        else:
            return JsonResponse({
                'status': False,
                'error': result.get('error', 'Unknown error')
            }, status=400)
    except Exception as e:
        return JsonResponse({
            'status': False,
            'error': str(e)
        }, status=400)


# Rebuild scheduled ZSETs from DB truth
@md.POST('control/rebuild-scheduled')
@md.requires_perms('manage_jobs')
def on_rebuild_scheduled(request):
    """
    Rebuild Redis scheduled ZSETs from DB pending jobs with future run_at.

    Params:
        channel: Optional channel to restrict rebuild
        limit: Optional max number of jobs per channel
    """
    try:
        manager = get_manager()
        channel = request.DATA.get('channel')
        limit = request.DATA.get('limit')
        limit_val = int(limit) if limit is not None else None

        result = manager.rebuild_scheduled(channel=channel, limit=limit_val)

        if result.get('status', True):
            return JsonResponse({
                'status': True,
                'data': result
            })
        else:
            return JsonResponse({
                'status': False,
                'error': "; ".join(result.get('errors', [])) or 'Unknown error',
                'data': result
            }, status=400)
    except Exception as e:
        return JsonResponse({
            'status': False,
            'error': str(e)
        }, status=400)


# Cleanup consumer groups and stale consumers
@md.POST('control/cleanup-consumers')
@md.requires_perms('manage_jobs')
def on_cleanup_consumers(request):
    """
    Cleanup Redis Stream consumer groups and consumers.

    Optional params:
        channel: If provided, only clean this channel
        destroy_empty_groups: If true, destroys empty groups after cleanup (default: true)
    """
    try:
        manager = get_manager()
        channel = request.DATA.get('channel')
        destroy = request.DATA.get('destroy_empty_groups', True)
        destroy = bool(destroy) if isinstance(destroy, bool) else str(destroy).lower() in ('1', 'true', 'yes', 'on')
        result = manager.cleanup_consumer_groups(channel=channel, destroy_empty_groups=destroy)
        if result.get('status', True):
            return JsonResponse({
                'status': True,
                'data': result
            })
        else:
            return JsonResponse({
                'status': False,
                'error': "; ".join(result.get('errors', [])) or 'Unknown error',
                'data': result
            }, status=400)
    except Exception as e:
        return JsonResponse({
            'status': False,
            'error': str(e)
        }, status=400)


# List discovered channels (from registered streams)
@md.GET('control/channels')
@md.requires_perms('manage_jobs', 'view_jobs')
def on_get_channels(request):
    """
    Discover channels by scanning Redis for registered streams.
    """
    try:
        manager = get_manager()
        channels = manager.get_registered_channels()
        return JsonResponse({
            'status': True,
            'data': channels
        })
    except Exception as e:
        return JsonResponse({
            'status': False,
            'error': str(e)
        }, status=400)


# Force scheduler leadership
@md.POST('control/force-scheduler-lead')
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
@md.POST('control/test')
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
