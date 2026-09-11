"""
Job Actions Service - Business logic for job operations.

Handles cancel, retry, status and other job actions separately from the model.
"""
from typing import Any, Dict, Optional
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
        Retry a failed, canceled or expired job by publishing a REPLACEMENT.

        The original row is left as the terminal record it is — status,
        attempt, diagnostics and finished_at untouched — and gains
        metadata['retried_as'] naming the replacement; the replacement carries
        metadata['retried_from'], and a 'retry' event on the original links
        the two ids. A second retry is refused while that replacement is still
        pending/running/completed, so a double-click cannot run the work twice.

        The replacement gets a fresh lifetime: the larger of the publish
        default and the window the original was published with, extended by
        `delay` so a delayed retry is still alive when the scheduler promotes
        it. The original's expires_at is never reused — an expired source would
        otherwise produce a replacement that expires before it runs.

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

        from mojo.apps.jobs.models import Job, JobEvent

        # Once-only: a live replacement (a list of them for a fanned-out
        # broadcast) blocks another retry. One that itself ended terminal
        # does not — the newest id overwrites retried_as below.
        prior = job.metadata.get('retried_as')
        if prior:
            prior_ids = prior if isinstance(prior, list) else [prior]
            if Job.objects.filter(pk__in=prior_ids).exclude(
                    status__in=('failed', 'canceled', 'expired')).exists():
                return {
                    'status': False,
                    'error': f'Job already retried as {prior} — retry that job instead',
                    'new_job_id': prior
                }

        try:
            from mojo.apps.jobs import publish, JOBS_DEFAULT_EXPIRES_SEC

            delay_seconds = int(delay) if delay else 0
            if delay_seconds < 0:
                return {'status': False, 'error': 'delay must be zero or more seconds'}

            # publish() derived the original's expires_at from the same `now`
            # as `created`, so their difference is the expires_in the publisher
            # chose — publishers size that window to their max_retries, and
            # the replacement inherits that budget.
            lifetime = JOBS_DEFAULT_EXPIRES_SEC
            if job.expires_at:
                lifetime = max(lifetime, int((job.expires_at - job.created).total_seconds()))

            # Immediate mirror on purpose: inside a transaction publish() would
            # defer the mirror and swallow its failure, and a manual retry that
            # reports success for a job that never queued is the worst outcome.
            new_job_id = publish(
                func=job.func,
                payload=job.payload,
                channel=job.channel,
                delay=delay_seconds or None,
                broadcast=job.broadcast,
                max_retries=job.max_retries,
                backoff_base=job.backoff_base,
                backoff_max=job.backoff_max_sec,
                expires_in=lifetime + delay_seconds,
                max_exec_seconds=job.max_exec_seconds
            )
            new_ids = new_job_id if isinstance(new_job_id, list) else [new_job_id]

            # Back-link, only while the replacement's metadata is still empty:
            # a fast runner may already have claimed it, and the engine saves
            # the handler's in-memory metadata on completion — an unconditional
            # update here could erase handler output. Losing the back-link in
            # that race is fine; the retry event below is the authoritative link.
            Job.objects.filter(pk__in=new_ids, metadata={}).update(
                metadata={'retried_from': job.id}, modified=timezone.now())

            job.metadata['retried_as'] = new_job_id
            job.save(update_fields=['metadata', 'modified'])

            # Record event
            JobEvent.objects.create(
                job=job,
                channel=job.channel,
                event='retry',
                details={
                    'retry_requested': True,
                    'new_job_id': new_job_id,
                    'delay': delay,
                    'previous_status': job.status
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
