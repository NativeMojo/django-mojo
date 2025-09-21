from mojo import decorators as md
from mojo.helpers.response import JsonResponse
from mojo.helpers import logit
from mojo.helpers.settings import settings
from mojo.apps.jobs.models import Job, JobEvent, JobLog
from mojo.apps.jobs.manager import get_manager
from mojo.apps.jobs import publish, cancel, status
from django.utils import timezone
from django.db.models import Q
import json


# Basic CRUD for Jobs (with RestMeta permissions)
@md.URL('job')
@md.URL('job/<str:pk>')
@md.uses_model_security(Job)
def on_job(request, pk=None):
    """Standard CRUD operations for jobs with automatic permission handling."""
    return Job.on_rest_request(request, pk)


# Basic CRUD for Job Events
@md.URL('event')
@md.URL('event/<int:pk>')
@md.uses_model_security(JobEvent)
def on_job_event(request, pk=None):
    """Standard CRUD operations for job events."""
    return JobEvent.on_rest_request(request, pk)


# Basic CRUD for Job Logs
@md.URL('logs')
@md.URL('logs/<int:pk>')
@md.uses_model_security(JobLog)
def on_job_logs(request, pk=None):
    """Standard CRUD operations for job logs."""
    return JobLog.on_rest_request(request, pk)


# Get job status
@md.GET('status/<str:job_id>')
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
@md.POST('cancel')
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
@md.POST('retry')
@md.requires_perms('manage_jobs')
@md.requires_params('job_id')
def on_retry_job(request):
    """Retry a failed or canceled job."""
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



# Get channel health
@md.GET('health/<str:channel>')
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
@md.GET('health')
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
@md.GET('runners')
@md.requires_perms('manage_jobs', 'view_jobs')
def on_list_runners(request):
    """List all active runners with their status."""
    try:
        manager = get_manager()

        # Optional channel filter
        channel = request.DATA.get('channel')
        runners = manager.get_runners(channel=channel)

        # Set id field for each runner
        for r in runners:
            r["id"] = r["runner_id"]

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
@md.POST('runners/ping')
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
@md.POST('runners/shutdown')
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
@md.POST('runners/broadcast')
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
@md.GET('stats')
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



@md.POST('test')
@md.requires_perms('manage_jobs')
def on_system_test(request):
    from mojo.apps import jobs
    jobs.publish(
        "mojo.apps.jobs.examples.sample_jobs.send_email",
        {
            "recipients": ["user@example.com"],
            "subject": "Test Email",
            "body": "This is a test email."
        },
        delay=30
    )

    jobs.publish(
        "mojo.apps.jobs.examples.sample_jobs.simulate_long_job",
        {
            "delay": 15
        },
        channel='priority'
    )
    return JsonResponse({
        'status': True,
        'message': 'Test job should be running.'
    })


@md.POST('tests')
@md.requires_perms('manage_jobs')
def on_system_tests(request):
    from mojo.apps import jobs
    import random

    base_job_list = [
        {
            "func": "mojo.apps.jobs.examples.sample_jobs.send_email",
            "payload": {
                "recipients": ["user@example.com"],
                "subject": "Test Email",
                "body": "This is a test email."
            },
            "channel": 'email'
        },
        {
            "func": "mojo.apps.jobs.examples.sample_jobs.process_file_upload",
            "payload": {
                "file_path": "/path/to/file"
            },
            "channel": 'priority'
        },
        {
            "func": "mojo.apps.jobs.examples.sample_jobs.process_file_upload",
            "payload": {
                "file_error_path": "/path/to/file"
            },
            "channel": 'default'
        }
    ]

    fetch_job = {
        "func": "mojo.apps.jobs.examples.sample_jobs.fetch_external_api",
        "payload": {
            "url": "https://nativemojo.com/"
        },
        "channel": 'webhooks'
    }

    job_list = []
    channels = settings.get("JOBS_CHANNELS", ["email"])
    for channel in channels:
        j = random.choice(base_job_list)
        j["channel"] = channel
        job_list.append(j)

    job_list.append(fetch_job)
    # lets schedule some jobs as well
    for i in range(10):
        j = random.choice(base_job_list)
        j = j.copy()
        j["delay"] = random.randint(30, 300)
        job_list.append(j)

    for i in range(50):
        j = random.choice(base_job_list)
        job_list.append(j.copy())




    for jd in job_list:
        jobs.publish(**jd)
    return JsonResponse({
        'status': True,
        'message': 'Test job should be running.'
    })
