from mojo import decorators as md
from mojo.helpers.response import JsonResponse
from mojo.apps.jobs.models import Job, JobEvent
from mojo.apps.jobs.manager import get_manager
from mojo.apps.jobs import publish, cancel, status
from django.utils import timezone
from django.db.models import Q
import json


# Basic CRUD for Jobs (with RestMeta permissions)
@md.URL('jobs/job')
@md.URL('jobs/job/<int:pk>')
def on_job(request, pk=None):
    """Standard CRUD operations for jobs with automatic permission handling."""
    return Job.on_rest_request(request, pk)


# Basic CRUD for Job Events
@md.URL('jobs/event')
@md.URL('jobs/event/<int:pk>')
def on_job_event(request, pk=None):
    """Standard CRUD operations for job events."""
    return JobEvent.on_rest_request(request, pk)


# Publish a new job
@md.POST('jobs/publish')
@md.requires_perms('manage_jobs')
@md.requires_params('func', 'payload')
def on_publish_job(request):
    """Publish a new job for asynchronous execution."""
    try:
        from datetime import datetime

        # Extract parameters
        func = request.DATA['func']
        payload = request.DATA['payload']
        channel = request.DATA.get('channel', 'default')
        delay = request.DATA.get('delay')
        run_at = request.DATA.get('run_at')
        broadcast = request.DATA.get('broadcast', False)
        max_retries = request.DATA.get('max_retries')
        backoff_base = request.DATA.get('backoff_base')
        backoff_max = request.DATA.get('backoff_max')
        expires_in = request.DATA.get('expires_in')
        expires_at = request.DATA.get('expires_at')
        max_exec_seconds = request.DATA.get('max_exec_seconds')
        idempotency_key = request.DATA.get('idempotency_key')

        # Parse run_at if provided as string
        if run_at and isinstance(run_at, str):
            run_at = datetime.fromisoformat(run_at)

        # Parse expires_at if provided as string
        if expires_at and isinstance(expires_at, str):
            expires_at = datetime.fromisoformat(expires_at)

        # Build kwargs
        kwargs = {
            'func': func,
            'payload': payload,
            'channel': channel,
            'broadcast': broadcast
        }

        # Add optional parameters
        if delay is not None:
            kwargs['delay'] = int(delay)
        if run_at is not None:
            kwargs['run_at'] = run_at
        if max_retries is not None:
            kwargs['max_retries'] = int(max_retries)
        if backoff_base is not None:
            kwargs['backoff_base'] = float(backoff_base)
        if backoff_max is not None:
            kwargs['backoff_max'] = int(backoff_max)
        if expires_in is not None:
            kwargs['expires_in'] = int(expires_in)
        if expires_at is not None:
            kwargs['expires_at'] = expires_at
        if max_exec_seconds is not None:
            kwargs['max_exec_seconds'] = int(max_exec_seconds)
        if idempotency_key:
            kwargs['idempotency_key'] = idempotency_key

        # Publish the job
        job_id = publish(**kwargs)

        # Get job details for response
        job = Job.objects.get(id=job_id)

        return JsonResponse({
            'status': True,
            'job_id': job_id,
            'data': {
                'id': job.id,
                'func': job.func,
                'channel': job.channel,
                'status': job.status,
                'run_at': job.run_at.isoformat() if job.run_at else None,
                'expires_at': job.expires_at.isoformat() if job.expires_at else None,
                'created': job.created.isoformat()
            }
        })

    except Exception as e:
        return JsonResponse({
            'status': False,
            'error': str(e)
        }, status=400)


# Get job status
@md.GET('jobs/status/<str:job_id>')
@md.requires_perms('manage_jobs', 'view_jobs')
def on_get_job_status(request, job_id):
    """Get the current status of a job."""
    try:
        job_status = status(job_id)

        if job_status is None:
            return JsonResponse({
                'status': False,
                'error': 'Job not found'
            }, status=404)

        return JsonResponse({
            'status': True,
            'data': job_status
        })

    except Exception as e:
        return JsonResponse({
            'status': False,
            'error': str(e)
        }, status=400)


# Cancel a job
@md.POST('jobs/cancel')
@md.requires_perms('manage_jobs')
@md.requires_params('job_id')
def on_cancel_job(request):
    """Request cancellation of a job."""
    try:
        job_id = request.DATA['job_id']
        result = cancel(job_id)

        return JsonResponse({
            'status': result,
            'message': f'Job {job_id} cancellation {"requested" if result else "failed"}'
        })

    except Exception as e:
        return JsonResponse({
            'status': False,
            'error': str(e)
        }, status=400)


# Retry a job
@md.POST('jobs/retry')
@md.requires_perms('manage_jobs')
@md.requires_params('job_id')
def on_retry_job(request):
    """Retry a failed or cancelled job."""
    try:
        job_id = request.DATA['job_id']
        delay = request.DATA.get('delay')

        # Get the job
        try:
            job = Job.objects.get(id=job_id)
        except Job.DoesNotExist:
            return JsonResponse({
                'status': False,
                'error': 'Job not found'
            }, status=404)

        # Use the service to retry
        from mojo.apps.jobs.services import JobActionsService
        result = JobActionsService.retry_job(job, delay=delay)

        return JsonResponse(result)

    except Exception as e:
        return JsonResponse({
            'status': False,
            'error': str(e)
        }, status=400)


# List jobs with filtering
@md.GET('jobs/list')
@md.requires_perms('manage_jobs', 'view_jobs')
def on_list_jobs(request):
    """Query jobs with filtering options."""
    try:
        from datetime import datetime

        # Get filter parameters
        channel = request.DATA.get('channel')
        status_filter = request.DATA.get('status')
        since = request.DATA.get('since')
        limit = int(request.DATA.get('limit', 100))

        # Build query
        query = Q()
        if channel:
            query &= Q(channel=channel)
        if status_filter:
            query &= Q(status=status_filter)
        if since:
            since_dt = datetime.fromisoformat(since)
            query &= Q(created__gte=since_dt)

        # Execute query
        jobs = Job.objects.filter(query).order_by('-created')[:limit]

        # Serialize results
        data = []
        for job in jobs:
            data.append({
                'id': job.id,
                'func': job.func,
                'channel': job.channel,
                'status': job.status,
                'attempt': job.attempt,
                'created': job.created.isoformat(),
                'started_at': job.started_at.isoformat() if job.started_at else None,
                'finished_at': job.finished_at.isoformat() if job.finished_at else None,
                'last_error': job.last_error
            })

        return JsonResponse({
            'status': True,
            'count': len(data),
            'data': data
        })

    except Exception as e:
        return JsonResponse({
            'status': False,
            'error': str(e)
        }, status=400)


# Get job events
@md.GET('jobs/job/<str:job_id>/events')
@md.requires_perms('manage_jobs', 'view_jobs')
def on_get_job_events(request, job_id):
    """Get the event history for a specific job."""
    try:
        # Verify job exists
        try:
            job = Job.objects.get(id=job_id)
        except Job.DoesNotExist:
            return JsonResponse({
                'status': False,
                'error': 'Job not found'
            }, status=404)

        # Get events
        events = JobEvent.objects.filter(job=job).order_by('at')

        # Serialize events
        data = []
        for i, event in enumerate(events):
            data.append({
                'id': i + 1,
                'event': event.event,
                'at': event.at.isoformat(),
                'runner_id': event.runner_id,
                'attempt': event.attempt,
                'details': event.details
            })

        return JsonResponse({
            'status': True,
            'job_id': job_id,
            'count': len(data),
            'data': data
        })

    except Exception as e:
        return JsonResponse({
            'status': False,
            'error': str(e)
        }, status=400)


# Get channel health
@md.GET('jobs/health/<str:channel>')
@md.requires_perms('manage_jobs', 'view_jobs')
def on_channel_health(request, channel):
    """Get comprehensive health metrics for a channel."""
    try:
        manager = get_manager()
        health = manager.get_channel_health(channel)

        return JsonResponse({
            'status': True,
            'data': health
        })

    except Exception as e:
        return JsonResponse({
            'status': False,
            'error': str(e)
        }, status=400)


# Get all channels health
@md.GET('jobs/health')
@md.requires_perms('manage_jobs', 'view_jobs')
def on_health_overview(request):
    """Get health overview for all configured channels."""
    try:
        from django.conf import settings
        manager = get_manager()

        channels = getattr(settings, 'JOBS_CHANNELS', ['default'])
        health_data = {}

        for channel in channels:
            health_data[channel] = manager.get_channel_health(channel)

        # Calculate aggregate stats
        total_unclaimed = sum(h['messages']['unclaimed'] for h in health_data.values())
        total_pending = sum(h['messages']['pending'] for h in health_data.values())
        total_stuck = sum(h['messages']['stuck'] for h in health_data.values())
        total_runners = sum(h['runners']['active'] for h in health_data.values())

        # Determine overall status
        overall_status = 'healthy'
        if any(h['status'] == 'critical' for h in health_data.values()):
            overall_status = 'critical'
        elif any(h['status'] == 'warning' for h in health_data.values()):
            overall_status = 'warning'

        return JsonResponse({
            'status': True,
            'data': {
                'overall_status': overall_status,
                'totals': {
                    'unclaimed': total_unclaimed,
                    'pending': total_pending,
                    'stuck': total_stuck,
                    'runners': total_runners
                },
                'channels': health_data
            }
        })

    except Exception as e:
        return JsonResponse({
            'status': False,
            'error': str(e)
        }, status=400)


# Get active runners
@md.GET('jobs/runners')
@md.requires_perms('manage_jobs', 'view_jobs')
def on_list_runners(request):
    """List all active runners with their status."""
    try:
        manager = get_manager()

        # Optional channel filter
        channel = request.DATA.get('channel')
        runners = manager.get_runners(channel=channel)

        return JsonResponse({
            'status': True,
            'count': len(runners),
            'data': runners
        })

    except Exception as e:
        return JsonResponse({
            'status': False,
            'error': str(e)
        }, status=400)


# Ping a specific runner
@md.POST('jobs/runners/ping')
@md.requires_perms('manage_jobs')
@md.requires_params('runner_id')
def on_ping_runner(request):
    """Ping a specific runner to check if it's responsive."""
    try:
        manager = get_manager()
        runner_id = request.DATA['runner_id']
        timeout = float(request.DATA.get('timeout', 2.0))

        result = manager.ping(runner_id, timeout=timeout)

        return JsonResponse({
            'status': True,
            'runner_id': runner_id,
            'responsive': result
        })

    except Exception as e:
        return JsonResponse({
            'status': False,
            'error': str(e)
        }, status=400)


# Shutdown a runner
@md.POST('jobs/runners/shutdown')
@md.requires_perms('manage_jobs')
@md.requires_params('runner_id')
def on_shutdown_runner(request):
    """Request a runner to shutdown gracefully."""
    try:
        manager = get_manager()
        runner_id = request.DATA['runner_id']
        graceful = request.DATA.get('graceful', True)

        manager.shutdown(runner_id, graceful=bool(graceful))

        return JsonResponse({
            'status': True,
            'message': f'Shutdown command sent to runner {runner_id}'
        })

    except Exception as e:
        return JsonResponse({
            'status': False,
            'error': str(e)
        }, status=400)


# Broadcast command to all runners
@md.POST('jobs/runners/broadcast')
@md.requires_perms('manage_jobs')
@md.requires_params('command')
def on_broadcast_command(request):
    """Broadcast a command to all runners."""
    try:
        manager = get_manager()
        command = request.DATA['command']
        data = request.DATA.get('data', {})
        timeout = float(request.DATA.get('timeout', 2.0))

        # Validate command
        valid_commands = ['status', 'shutdown', 'pause', 'resume', 'reload']
        if command not in valid_commands:
            return JsonResponse({
                'status': False,
                'error': f'Invalid command. Must be one of: {", ".join(valid_commands)}'
            }, status=400)

        responses = manager.broadcast_command(command, data=data, timeout=timeout)

        return JsonResponse({
            'status': True,
            'command': command,
            'responses_count': len(responses),
            'responses': responses
        })

    except Exception as e:
        return JsonResponse({
            'status': False,
            'error': str(e)
        }, status=400)


# Get system stats
@md.GET('jobs/stats')
@md.requires_perms('manage_jobs', 'view_jobs')
def on_system_stats(request):
    """Get overall system statistics."""
    try:
        manager = get_manager()
        stats = manager.get_stats()

        return JsonResponse({
            'status': True,
            'data': stats
        })

    except Exception as e:
        return JsonResponse({
            'status': False,
            'error': str(e)
        }, status=400)
