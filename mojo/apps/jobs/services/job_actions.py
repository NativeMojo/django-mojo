"""
Job Actions Service - Business logic for job operations.

Handles cancel, retry, status and other job actions separately from the model.
"""
from typing import Any, Dict, Optional
from datetime import timedelta
from django.utils import timezone
from mojo.helpers import logit


class JobActionsService:
    """
    Service class for job action business logic.

    Keeps models clean by handling complex operations here.
    """

    @staticmethod
    def cancel_job(job) -> Dict[str, Any]:
        """
        Cancel a job.

        Behavior:
          - If job is terminal: refuse
          - If job is running:
              - If runner heartbeat is not alive, force cancel (status='canceled')
              - If runner alive, set cancel_requested=True (cooperative cancel)
          - If job is not running (e.g., pending/scheduled/failed/expired): set status='canceled'

        Also attempts to remove from scheduled ZSETs when applicable.

        Args:
            job: Job model instance

        Returns:
            dict: Response with status and message
        """
        # Check terminal
        if job.is_terminal:
            return {
                'status': False,
                'error': f'Cannot cancel job in {job.status} state'
            }

        now = timezone.now()
        previous_status = job.status
        forced = False

        try:
            # Determine if runner is alive when job is marked running
            runner_alive = False
            if job.status == 'running' and job.runner_id:
                from mojo.apps.jobs.adapters import get_adapter
                from mojo.apps.jobs.keys import JobKeys
                redis = get_adapter()
                keys = JobKeys()
                hb = redis.get(keys.runner_hb(job.runner_id))
                runner_alive = bool(hb)

            if job.status == 'running':
                if runner_alive:
                    # Cooperative cancel for running job
                    job.cancel_requested = True
                    job.save(update_fields=['cancel_requested', 'modified'])
                else:
                    # Force cancel stale running job
                    job.status = 'canceled'
                    job.finished_at = now
                    job.cancel_requested = True
                    job.runner_id = None
                    job.save(update_fields=['status', 'finished_at', 'cancel_requested', 'runner_id', 'modified'])
                    forced = True
            else:
                # Not running: cancel immediately
                job.status = 'canceled'
                job.finished_at = now
                job.cancel_requested = True
                job.runner_id = None
                job.save(update_fields=['status', 'finished_at', 'cancel_requested', 'runner_id', 'modified'])

                # Best-effort: remove from scheduled ZSETs if it was scheduled
                try:
                    from mojo.apps.jobs.adapters import get_adapter
                    from mojo.apps.jobs.keys import JobKeys
                    redis = get_adapter()
                    keys = JobKeys()
                    # Remove from both sched sets; only one will match
                    redis.zadd  # touch to appease linters; real calls below
                    redis.zrem = redis.get_client().zrem  # ensure we have zrem via client
                    redis.get_client().zrem(keys.sched(job.channel), job.id)
                    redis.get_client().zrem(keys.sched_broadcast(job.channel), job.id)
                except Exception as e:
                    logit.debug(f"Cancel cleanup (sched zrem) failed for {job.id}: {e}")

            # Record event
            from mojo.apps.jobs.models import JobEvent
            JobEvent.objects.create(
                job=job,
                channel=job.channel,
                event='canceled',
                details={
                    'requested_at': now.isoformat(),
                    'forced': forced,
                    'previous_status': previous_status
                }
            )

            logit.info(f"Cancellation {'forced' if forced else 'requested'} for job {job.id} (prev={previous_status})")

            return {
                'status': True,
                'message': f"Job {job.id} {'canceled' if job.status == 'canceled' else 'cancellation requested'}",
                'job_id': job.id,
                'forced': forced
            }

        except Exception as e:
            logit.error(f"Failed to cancel job {job.id}: {e}")
            return {
                'status': False,
                'error': f'Failed to cancel job: {str(e)}'
            }

    @staticmethod
    def retry_job(job, delay: Optional[int] = None) -> Dict[str, Any]:
        """
        Retry a failed or canceled job.

        Args:
            job: Job model instance
            delay: Optional delay in seconds before retry

        Returns:
            dict: Response with status and new job ID
        """
        # Check if job can be retried
        if job.status not in ('failed', 'canceled', 'expired'):
            return {
                'status': False,
                'error': f'Cannot retry job in {job.status} state'
            }

        # Reset job for retry
        job.status = 'pending'
        job.attempt = 0
        job.last_error = ''
        job.stack_trace = ''
        job.cancel_requested = False
        job.runner_id = None
        job.started_at = None
        job.finished_at = None

        # Set run_at if delay specified
        if delay:
            job.run_at = timezone.now() + timedelta(seconds=int(delay))
        else:
            job.run_at = None

        job.save()

        # Re-publish to Redis
        try:
            from mojo.apps.jobs import publish

            # Re-publish the job
            new_job_id = publish(
                func=job.func,
                payload=job.payload,
                channel=job.channel,
                run_at=job.run_at,
                broadcast=job.broadcast,
                max_retries=job.max_retries,
                backoff_base=job.backoff_base,
                backoff_max=job.backoff_max_sec,
                expires_at=job.expires_at,
                max_exec_seconds=job.max_exec_seconds
            )

            # Record event
            from mojo.apps.jobs.models import JobEvent
            JobEvent.objects.create(
                job=job,
                channel=job.channel,
                event='retry',
                details={
                    'retry_requested': True,
                    'new_job_id': new_job_id,
                    'delay': delay
                }
            )

            logit.info(f"Job {job.id} retry scheduled as {new_job_id}")

            return {
                'status': True,
                'message': f'Job retry scheduled',
                'original_job_id': job.id,
                'new_job_id': new_job_id,
                'delayed': delay is not None
            }

        except Exception as e:
            logit.error(f"Failed to retry job {job.id}: {e}")
            return {
                'status': False,
                'error': f'Failed to retry job: {str(e)}'
            }

    @staticmethod
    def get_job_status(job) -> Dict[str, Any]:
        """
        Get detailed status of a job.

        Args:
            job: Job model instance

        Returns:
            dict: Detailed job status information
        """
        # Build detailed status response
        status_data = {
            'id': job.id,
            'status': job.status,
            'channel': job.channel,
            'func': job.func,
            'created': job.created.isoformat() if job.created else None,
            'started_at': job.started_at.isoformat() if job.started_at else None,
            'finished_at': job.finished_at.isoformat() if job.finished_at else None,
            'attempt': job.attempt,
            'max_retries': job.max_retries,
            'last_error': job.last_error,
            'metadata': job.metadata,
            'runner_id': job.runner_id,
            'cancel_requested': job.cancel_requested,
            'duration_ms': job.duration_ms,
            'is_terminal': job.is_terminal,
            'is_retriable': job.is_retriable
        }

        # Add recent events
        try:
            events = job.events.order_by('-at')[:10]
            status_data['recent_events'] = [
                {
                    'event': e.event,
                    'at': e.at.isoformat(),
                    'runner_id': e.runner_id,
                    'details': e.details
                }
                for e in events
            ]
        except Exception as e:
            logit.debug(f"Failed to get events for job {job.id}: {e}")
            status_data['recent_events'] = []

        # Check position in queue if pending and scheduled
        if job.status == 'pending' and job.run_at:
            try:
                from mojo.apps.jobs.adapters import get_adapter
                from mojo.apps.jobs.keys import JobKeys

                redis = get_adapter()
                keys = JobKeys()
                sched_key = keys.sched(job.channel)

                # Get position in scheduled queue
                rank = redis.get_client().zrank(sched_key, job.id)
                if rank is not None:
                    status_data['queue_position'] = rank + 1
            except Exception as e:
                logit.debug(f"Failed to get queue position for {job.id}: {e}")

        return {
            'status': True,
            'data': status_data
        }

    @staticmethod
    def pause_job(job) -> Dict[str, Any]:
        """
        Pause a pending job (remove from queue but keep in DB).

        Args:
            job: Job model instance

        Returns:
            dict: Response with status and message
        """
        if job.status != 'pending':
            return {
                'status': False,
                'error': f'Cannot pause job in {job.status} state'
            }

        # Update status to paused (using canceled state but with metadata)
        job.status = 'canceled'
        job.metadata['paused'] = True
        job.metadata['paused_at'] = timezone.now().isoformat()
        job.save(update_fields=['status', 'metadata', 'modified'])

        # Remove from Redis queue if present
        try:
            from mojo.apps.jobs.adapters import get_adapter
            from mojo.apps.jobs.keys import JobKeys

            redis = get_adapter()
            keys = JobKeys()

            # Remove from scheduled queue if scheduled
            if job.run_at:
                sched_key = keys.sched(job.channel)
                redis.get_client().zrem(sched_key, job.id)

            # Remove job hash
            redis.delete(keys.job(job.id))

        except Exception as e:
            logit.warn(f"Failed to remove job {job.id} from Redis: {e}")

        # Record event
        from mojo.apps.jobs.models import JobEvent
        JobEvent.objects.create(
            job=job,
            channel=job.channel,
            event='canceled',
            details={'paused': True}
        )

        logit.info(f"Job {job.id} paused")

        return {
            'status': True,
            'message': f'Job {job.id} paused',
            'job_id': job.id
        }

    @staticmethod
    def resume_job(job) -> Dict[str, Any]:
        """
        Resume a paused job.

        Args:
            job: Job model instance

        Returns:
            dict: Response with status and message
        """
        # Check if job is actually paused
        if job.status != 'canceled' or not job.metadata.get('paused'):
            return {
                'status': False,
                'error': 'Job is not paused'
            }

        # Reset to pending and clear pause metadata
        job.status = 'pending'
        job.metadata.pop('paused', None)
        job.metadata.pop('paused_at', None)
        job.metadata['resumed_at'] = timezone.now().isoformat()
        job.save(update_fields=['status', 'metadata', 'modified'])

        # Re-publish to Redis
        try:
            from mojo.apps.jobs import publish

            new_job_id = publish(
                func=job.func,
                payload=job.payload,
                channel=job.channel,
                run_at=job.run_at,
                broadcast=job.broadcast,
                max_retries=job.max_retries,
                backoff_base=job.backoff_base,
                backoff_max=job.backoff_max_sec,
                expires_at=job.expires_at,
                max_exec_seconds=job.max_exec_seconds
            )

            # Record event
            from mojo.apps.jobs.models import JobEvent
            JobEvent.objects.create(
                job=job,
                channel=job.channel,
                event='queued',
                details={'resumed': True, 'new_job_id': new_job_id}
            )

            logit.info(f"Job {job.id} resumed as {new_job_id}")

            return {
                'status': True,
                'message': f'Job resumed',
                'original_job_id': job.id,
                'new_job_id': new_job_id
            }

        except Exception as e:
            logit.error(f"Failed to resume job {job.id}: {e}")
            return {
                'status': False,
                'error': f'Failed to resume job: {str(e)}'
            }

    @staticmethod
    def publish_job_from_template(job, overrides: Dict[str, Any]) -> Dict[str, Any]:
        """
        Publish a new job using an existing job as a template.

        Args:
            job: Job model instance to use as template
            overrides: Dict with optional overrides for the new job

        Returns:
            dict: Response with new job ID
        """
        try:
            from mojo.apps.jobs import publish

            # Build parameters from template job
            params = {
                'func': overrides.get('func', job.func),
                'payload': overrides.get('payload', job.payload),
                'channel': overrides.get('channel', job.channel),
                'broadcast': overrides.get('broadcast', job.broadcast),
                'max_retries': overrides.get('max_retries', job.max_retries),
                'backoff_base': overrides.get('backoff_base', job.backoff_base),
                'backoff_max': overrides.get('backoff_max', job.backoff_max_sec),
                'max_exec_seconds': overrides.get('max_exec_seconds', job.max_exec_seconds),
            }

            # Handle scheduling
            if 'delay' in overrides:
                params['delay'] = overrides['delay']
            elif 'run_at' in overrides:
                params['run_at'] = overrides['run_at']
            elif job.run_at:
                params['run_at'] = job.run_at

            # Handle expiration
            if 'expires_in' in overrides:
                params['expires_in'] = overrides['expires_in']
            elif 'expires_at' in overrides:
                params['expires_at'] = overrides['expires_at']
            elif job.expires_at:
                params['expires_at'] = job.expires_at

            # Publish the new job
            new_job_id = publish(**params)

            logit.info(f"Published new job {new_job_id} from template {job.id}")

            return {
                'status': True,
                'message': 'Job published successfully',
                'job_id': new_job_id,
                'template_job_id': job.id
            }

        except Exception as e:
            logit.error(f"Failed to publish job from template {job.id}: {e}")
            return {
                'status': False,
                'error': f'Failed to publish job: {str(e)}'
            }
