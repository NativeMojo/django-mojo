"""
JobManager for control and inspection of the jobs system.

Provides high-level management operations for monitoring and controlling
job runners, queues, and individual jobs.
"""
import json
import uuid
import time
from typing import Any, Dict, List, Optional, Tuple
from datetime import datetime, timedelta

from django.conf import settings
from django.utils import timezone

from mojo.helpers import logit
from .keys import JobKeys
from .adapters import get_adapter
from .models import Job, JobEvent


class JobManager:
    """
    Management interface for the jobs system.

    Provides methods for inspecting queue state, controlling runners,
    and managing jobs.
    """

    def __init__(self):
        """Initialize the JobManager."""
        self.redis = get_adapter()
        self.keys = JobKeys()

    def get_runners(self, channel: Optional[str] = None) -> List[Dict[str, Any]]:
        """
        Get list of active runners.

        Args:
            channel: Filter by channel (None for all runners)

        Returns:
            List of runner info dicts with keys:
                - runner_id: Runner identifier
                - channels: List of channels served
                - jobs_processed: Number of jobs completed
                - jobs_failed: Number of jobs failed
                - started: When runner started
                - last_heartbeat: Last heartbeat time
                - alive: Whether runner is considered alive
        """
        runners = []

        try:
            # Find all runner heartbeat keys
            pattern = self.keys.runner_hb('*')

            # Use scan_iter for better performance and cluster compatibility
            # scan_iter handles both standalone and cluster modes automatically
            all_keys = list(self.redis.get_client().scan_iter(match=pattern, count=100))

            # Check each runner
            for key in all_keys:
                try:
                    # Get heartbeat data
                    key_str = key.decode('utf-8') if isinstance(key, bytes) else str(key)
                    data = self.redis.get(key_str)
                    if not data:
                        continue

                    runner_info = json.loads(data)

                    # Filter by channel if specified
                    if channel and channel not in runner_info.get('channels', []):
                        continue

                    # Check if alive (heartbeat within 3x interval)
                    last_hb = runner_info.get('last_heartbeat')
                    if last_hb:
                        last_hb_time = datetime.fromisoformat(last_hb)
                        if timezone.is_naive(last_hb_time):
                            last_hb_time = timezone.make_aware(last_hb_time)

                        age = (timezone.now() - last_hb_time).total_seconds()
                        alive = age < (getattr(settings, 'JOBS_RUNNER_HEARTBEAT_SEC', 5) * 3)
                    else:
                        alive = False

                    runner_info['alive'] = alive
                    runners.append(runner_info)

                except Exception as e:
                    logit.warn(f"Failed to parse runner heartbeat: {e}")

        except Exception as e:
            logit.exception(f"Failed to get runners: {e}")

        # Sort by runner_id for consistency
        runners.sort(key=lambda r: r.get('runner_id', ''))

        return runners

    def get_queue_state(self, channel: str) -> Dict[str, Any]:
        """
        Get queue state for a channel.

        Args:
            channel: Channel name

        Returns:
            Dict with queue statistics (Plan B):
                - queued_count: Number of messages waiting in the list queue (LLEN)
                - inflight_count: Number of in-flight messages (ZCARD of processing)
                - scheduled_count: Number of scheduled/delayed jobs (ZCARD of sched + sched_broadcast)
                - runners: Number of active runners
        """
        state = {
            'channel': channel,
            'queued_count': 0,
            'inflight_count': 0,
            'scheduled_count': 0,
            'runners': 0,
        }

        try:
            # Plan B counts: List + ZSET
            queue_key = self.keys.queue(channel)
            processing_key = self.keys.processing(channel)
            sched_key = self.keys.sched(channel)
            sched_b_key = self.keys.sched_broadcast(channel)
            # Exact counts
            state['queued_count'] = self.redis.llen(queue_key) or 0
            state['inflight_count'] = self.redis.zcard(processing_key) or 0
            state['scheduled_count'] = (self.redis.zcard(sched_key) or 0) + (self.redis.zcard(sched_b_key) or 0)
            # Active runners for this channel
            runners = self.get_runners(channel)
            state['runners'] = len([r for r in runners if r.get('alive')])
            # Add metrics (DB-derived)
            state['metrics'] = self._get_channel_metrics(channel)
        except Exception as e:
            logit.error(f"Failed to get queue state for {channel}: {e}")
        return state

    def get_channel_health(self, channel: str) -> Dict[str, Any]:
        """
        Get comprehensive health metrics for a channel.

        Args:
            channel: Channel name

        Returns:
            Dict with health status including unclaimed jobs, stuck jobs, and alerts
        """
        stream_key = self.keys.stream(channel)
        group_key = self.keys.group_workers(channel)
        sched_key = self.keys.sched(channel)

        # Get basic queue state
        state = self.get_queue_state(channel)

        # Calculate unclaimed (waiting to be picked up)
        total_messages = state['stream_length']
        pending_count = state['pending_count']
        unclaimed = max(0, total_messages - pending_count)

        # Find stuck jobs
        stuck = self._find_stuck_jobs(channel)

        # Get active runners
        runners = self.get_runners(channel)
        active_runners = [r for r in runners if r.get('alive')]

        # Build health status
        health = {
            'channel': channel,
            'status': 'healthy',  # Will update based on checks
            'messages': {
                'total': total_messages,
                'unclaimed': unclaimed,
                'pending': pending_count,
                'scheduled': state['scheduled_count'],
                'stuck': len(stuck)
            },
            'runners': {
                'active': len(active_runners),
                'total': len(runners)
            },
            'stuck_jobs': stuck[:10],  # First 10 stuck jobs
            'alerts': []
        }

        # Health checks
        if unclaimed > 100:
            health['alerts'].append(f"High unclaimed count: {unclaimed}")
            health['status'] = 'warning'

        if unclaimed > 500:
            health['status'] = 'critical'

        if len(stuck) > 0:
            health['alerts'].append(f"Stuck jobs detected: {len(stuck)}")
            health['status'] = 'warning'

        if len(stuck) > 10:
            health['status'] = 'critical'

        if len(active_runners) == 0 and total_messages > 0:
            health['alerts'].append("No active runners for channel with pending jobs")
            health['status'] = 'critical'

        # Add metrics if available
        if 'metrics' in state:
            health['metrics'] = state['metrics']

        return health

    def _find_stuck_jobs(self, channel: str, idle_threshold_ms: int = 60000) -> List[Dict]:
        """
        Plan B: Find jobs that are in-flight (processing ZSET) longer than the idle threshold.

        Args:
            channel: Channel name
            idle_threshold_ms: Consider stuck if idle longer than this (default 1 minute)

        Returns:
            List of stuck job details
        """
        stuck: List[Dict] = []
        try:
            now_ms = int(time.time() * 1000)
            cutoff = now_ms - max(0, int(idle_threshold_ms))
            processing_key = self.keys.processing(channel)

            if idle_threshold_ms <= 0:
                # Return all in-flight entries
                ids = self.redis.zrangebyscore(processing_key, float("-inf"), float("inf"))
                for jid in ids:
                    stuck.append({'job_id': jid, 'idle_ms': None})
                return stuck

            # Return entries older than cutoff
            ids = self.redis.zrangebyscore(processing_key, float("-inf"), cutoff)
            for jid in ids:
                # We don't store claim timestamp in the member by default; idle_ms calculation is approximate
                stuck.append({'job_id': jid, 'idle_ms': idle_threshold_ms})
        except Exception as e:
            logit.error(f"Failed to check stuck jobs for channel {channel}: {e}")

        return stuck

    def clear_stuck_jobs(self, channel: str, idle_threshold_ms: int = 60000) -> Dict[str, Any]:
        """
        Plan B: Clear stuck in-flight jobs from a channel by re-queueing or removing
        entries from the processing ZSET based on an idle threshold.

        Args:
            channel: Channel name to clear
            idle_threshold_ms: Consider stuck if older than this many ms (0 to clear all)

        Returns:
            Dict with results: {'cleared': int, 'details': [...], 'errors': [...]}
        """
        results = {
            'channel': channel,
            'cleared': 0,
            'details': [],
            'errors': []
        }

        try:
            now_ms = int(time.time() * 1000)
            processing_key = self.keys.processing(channel)
            queue_key = self.keys.queue(channel)

            # Determine score range to clear
            if idle_threshold_ms and idle_threshold_ms > 0:
                cutoff = now_ms - int(idle_threshold_ms)
                candidates = self.redis.zrangebyscore(processing_key, float("-inf"), cutoff)
            else:
                candidates = self.redis.zrangebyscore(processing_key, float("-inf"), float("inf"))

            if not candidates:
                results['message'] = f"No in-flight jobs found in {channel} matching threshold"
                return results

            for jid in candidates:
                try:
                    # Remove from processing and requeue
                    self.redis.zrem(processing_key, jid)
                    self.redis.rpush(queue_key, jid)
                    results['cleared'] += 1
                    results['details'].append({'job_id': jid, 'requeued': True})
                    # Write event trail (best effort)
                    try:
                        job = Job.objects.get(id=jid)
                        JobEvent.objects.create(
                            job=job,
                            channel=channel,
                            event='retry',
                            details={'reason': 'manual_clear_stuck'}
                        )
                    except Exception:
                        pass
                except Exception as e:
                    results['errors'].append(f"{jid}: {e}")

            results['message'] = f"Requeued {results['cleared']} in-flight jobs from {channel}"

        except Exception as e:
            import traceback
            results['errors'].append(str(e))
            results['stack_trace'] = traceback.format_exc()
            logit.error(f"Failed to clear stuck jobs from {channel}: {e}")

        return results

    def broadcast_command(self, command: str, data: Dict = None,
                         timeout: float = 2.0) -> List[Dict]:
        """
        Send command to all runners and collect responses.

        Args:
            command: Command to send (status, shutdown, pause, resume)
            data: Additional command data
            timeout: Time to wait for responses

        Returns:
            List of responses from runners
        """
        import uuid as uuid_module
        reply_channel = f"mojo:jobs:replies:{uuid_module.uuid4().hex[:8]}"

        # Subscribe to replies before sending
        pubsub = self.redis.pubsub()
        pubsub.subscribe(reply_channel)

        # Send broadcast command
        message = {
            'command': command,
            'data': data or {},
            'reply_channel': reply_channel,
            'timestamp': timezone.now().isoformat()
        }

        self.redis.publish("mojo:jobs:runners:broadcast", json.dumps(message))

        # Collect responses
        responses = []
        start_time = time.time()

        while time.time() - start_time < timeout:
            msg = pubsub.get_message(timeout=0.1)
            if msg and msg['type'] == 'message':
                try:
                    response_data = msg['data']
                    if isinstance(response_data, bytes):
                        response_data = response_data.decode('utf-8')
                    response = json.loads(response_data)
                    responses.append(response)
                except Exception as e:
                    logit.debug(f"Failed to parse response: {e}")

        pubsub.close()
        return responses

    def broadcast_execute(self, func_path: str, data: Dict = None,
                         timeout: float = 2.0, collect_replies: bool = False) -> List[Dict]:
        """
        Execute a function on all active runners.

        This is a fire-and-forget broadcast execution mechanism that runs
        a function on every active runner simultaneously. Unlike regular jobs,
        this does not create Job records, has no retry mechanism, and provides
        no persistence. Use for operations like cache clearing, configuration
        reloading, or local metrics collection.

        Args:
            func_path: Module path to function (e.g., 'myapp.jobs.cleanup_cache')
            data: Data dictionary to pass to the function
            timeout: Time to wait for responses if collect_replies=True
            collect_replies: Whether to wait for and collect responses

        Returns:
            List of responses from runners (if collect_replies=True), empty list otherwise

        Example:
            # Clear all runner caches
            manager.broadcast_execute('mojo.apps.jobs.tasks.clear_local_cache')

            # Reload configuration on all runners
            manager.broadcast_execute('myapp.tasks.reload_config')

            # Warm up caches with specific data
            manager.broadcast_execute(
                'myapp.tasks.warmup_cache',
                data={'keys': ['user_data', 'settings']}
            )

            # Collect results from all runners
            results = manager.broadcast_execute(
                'myapp.tasks.get_local_stats',
                collect_replies=True,
                timeout=5.0
            )
        """
        import uuid as uuid_module

        message = {
            'command': 'execute',
            'func': func_path,
            'data': data or {},
            'timestamp': timezone.now().isoformat()
        }

        if collect_replies:
            reply_channel = f"mojo:jobs:replies:{uuid_module.uuid4().hex[:8]}"
            message['reply_channel'] = reply_channel

            # Subscribe to replies before sending
            pubsub = self.redis.pubsub()
            pubsub.subscribe(reply_channel)

            # Send broadcast
            self.redis.publish("mojo:jobs:runners:broadcast", json.dumps(message))

            # Collect responses
            responses = []
            start_time = time.time()

            while time.time() - start_time < timeout:
                msg = pubsub.get_message(timeout=0.1)
                if msg and msg['type'] == 'message':
                    try:
                        response_data = msg['data']
                        if isinstance(response_data, bytes):
                            response_data = response_data.decode('utf-8')
                        response = json.loads(response_data)
                        responses.append(response)
                    except Exception as e:
                        logit.debug(f"Failed to parse execute reply: {e}")

            pubsub.close()
            return responses
        else:
            # Fire-and-forget
            self.redis.publish("mojo:jobs:runners:broadcast", json.dumps(message))
            return []

    def ping(self, runner_id: str, timeout: float = 2.0) -> bool:
        """
        Ping a runner to check if it's responsive.

        Args:
            runner_id: Runner identifier
            timeout: Maximum time to wait for response (seconds)

        Returns:
            True if runner responded, False otherwise
        """
        try:
            import uuid as uuid_module
            reply_channel = f"mojo:jobs:ping:{uuid_module.uuid4().hex[:8]}"

            # Subscribe to reply channel before sending
            pubsub = self.redis.pubsub()
            pubsub.subscribe(reply_channel)

            # Send ping command
            control_key = self.keys.runner_ctl(runner_id)
            message = json.dumps({
                'command': 'ping',
                'reply_channel': reply_channel
            })

            self.redis.publish(control_key, message)

            # Wait for response
            start_time = time.time()
            while time.time() - start_time < timeout:
                msg = pubsub.get_message(timeout=0.1)
                if msg and msg['type'] == 'message':
                    try:
                        response_data = msg['data']
                        if isinstance(response_data, bytes):
                            response_data = response_data.decode('utf-8')
                        if response_data == 'pong':
                            pubsub.close()
                            return True
                    except Exception as e:
                        logit.debug(f"Failed to parse ping response: {e}")

            # Timeout
            pubsub.close()
            return False

        except Exception as e:
            logit.error(f"Failed to ping runner {runner_id}: {e}")
            return False

    def shutdown(self, runner_id: str, graceful: bool = True) -> None:
        """
        Request a runner to shutdown.

        Args:
            runner_id: Runner identifier
            graceful: If True, wait for current job to finish
        """
        try:
            control_key = self.keys.runner_ctl(runner_id)
            message = json.dumps({
                'command': 'shutdown',
                'graceful': graceful
            })

            self.redis.publish(control_key, message)
            logit.info(f"Sent shutdown command to runner {runner_id} (graceful={graceful})")

        except Exception as e:
            logit.error(f"Failed to shutdown runner {runner_id}: {e}")

    def broadcast(self, channel: str, func: str, payload: Dict[str, Any],
                 **options) -> str:
        """
        Publish a broadcast job to a channel.

        Args:
            channel: Channel to broadcast on
            func: Job function module path
            payload: Job payload
            **options: Additional job options

        Returns:
            Job ID
        """
        from . import publish

        return publish(
            func=func,
            payload=payload,
            channel=channel,
            broadcast=True,
            **options
        )

    def job_status(self, job_id: str) -> Optional[Dict[str, Any]]:
        """
        Get detailed status of a job.

        Args:
            job_id: Job identifier

        Returns:
            Job status dict or None if not found
        """
        from . import status

        # Get basic status
        job_info = status(job_id)
        if not job_info:
            return None

        # Enhance with additional info
        try:
            # Add events timeline
            job = Job.objects.get(id=job_id)
            events = JobEvent.objects.filter(job=job).order_by('at')[:20]

            job_info['events'] = [
                {
                    'event': e.event,
                    'at': e.at.isoformat(),
                    'runner_id': e.runner_id,
                    'attempt': e.attempt,
                    'details': e.details
                }
                for e in events
            ]

            # Add queue position if pending
            if job_info['status'] == 'pending' and job.run_at:
                # Check position in scheduled queue
                sched_key = self.keys.sched(job.channel)
                rank = self.redis.get_client().zrank(sched_key, job_id)
                if rank is not None:
                    job_info['queue_position'] = rank + 1

        except Exception as e:
            logit.debug(f"Failed to enhance job status: {e}")

        return job_info

    def cancel_job(self, job_id: str) -> bool:
        """
        Cancel a job.

        Args:
            job_id: Job identifier

        Returns:
            True if cancelled, False otherwise
        """
        from . import cancel
        return cancel(job_id)

    def retry_job(self, job_id: str, delay: Optional[int] = None) -> bool:
        """
        Retry a failed job.

        Args:
            job_id: Job identifier
            delay: Delay in seconds before retry (default: immediate)

        Returns:
            True if retry scheduled, False otherwise
        """
        try:
            job = Job.objects.get(id=job_id)

            if job.status not in ('failed', 'canceled'):
                logit.warn(f"Cannot retry job {job_id} in status {job.status}")
                return False

            # Reset job for retry
            job.status = 'pending'
            job.attempt = 0
            job.last_error = ''
            job.stack_trace = ''

            if delay:
                job.run_at = timezone.now() + timedelta(seconds=delay)
            else:
                job.run_at = None

            job.save()

            # Re-publish to Redis
            from . import publish

            return publish(
                func=job.func,
                payload=job.payload,
                channel=job.channel,
                run_at=job.run_at,
                broadcast=job.broadcast,
                max_retries=job.max_retries,
                expires_at=job.expires_at,
                max_exec_seconds=job.max_exec_seconds
            )

        except Job.DoesNotExist:
            logit.error(f"Job {job_id} not found")
            return False
        except Exception as e:
            logit.error(f"Failed to retry job {job_id}: {e}")
            return False

    def _get_channel_metrics(self, channel: str) -> Dict[str, Any]:
        """Get recent metrics for a channel."""
        metrics = {
            'jobs_per_minute': 0,
            'success_rate': 0,
            'avg_duration_ms': 0
        }

        try:
            # Get recent job counts from database
            now = timezone.now()
            last_hour = now - timedelta(hours=1)

            # Jobs completed in last hour
            completed = Job.objects.filter(
                channel=channel,
                status='completed',
                finished_at__gte=last_hour
            ).count()

            # Jobs failed in last hour
            failed = Job.objects.filter(
                channel=channel,
                status='failed',
                finished_at__gte=last_hour
            ).count()

            total = completed + failed
            if total > 0:
                metrics['jobs_per_minute'] = round(total / 60, 2)
                metrics['success_rate'] = round(completed / total * 100, 1)

            # Average duration of recent completed jobs
            from django.db.models import Avg, F
            avg_duration = Job.objects.filter(
                channel=channel,
                status='completed',
                finished_at__gte=last_hour,
                started_at__isnull=False
            ).aggregate(
                avg_ms=Avg(F('finished_at') - F('started_at'))
            )

            if avg_duration['avg_ms']:
                metrics['avg_duration_ms'] = int(avg_duration['avg_ms'].total_seconds() * 1000)

        except Exception as e:
            logit.debug(f"Failed to get channel metrics: {e}")

        return metrics

    def get_stats(self) -> Dict[str, Any]:
        """
        Get overall system statistics.

        Returns:
            System-wide statistics
        """
        stats = {
            'channels': {},
            'runners': [],
            'totals': {
                'pending': 0,
                'queued': 0,
                'inflight': 0,
                'running': 0,
                'completed': 0,
                'failed': 0,
                'scheduled': 0,
                'runners_active': 0
            },
            'scheduler': {
                'active': False,
                'lock_holder': None
            }
        }

        try:
            # Get stats for each configured channel
            channels = getattr(settings, 'JOBS_CHANNELS', ['default', 'email', 'webhooks', 'priority'])
            for channel in channels:
                    state = self.get_queue_state(channel)

                    # Include DB running count per channel for better visibility
                    try:
                        state['db_running'] = Job.objects.filter(channel=channel, status='running').count()
                    except Exception:
                        state['db_running'] = 0

                    stats['channels'][channel] = state

                    # Aggregate totals
                    stats['totals']['scheduled'] += state['scheduled_count']
                    # queued_count = unclaimed_count; inflight_count = pending_count
                    queued = state.get('queued_count', state.get('unclaimed_count', max(0, state.get('stream_length', 0) - state.get('pending_count', 0))))
                    inflight = state.get('inflight_count', state.get('pending_count', 0))
                    stats['totals']['queued'] += queued
                    stats['totals']['inflight'] += inflight
                    # Keep 'pending' as alias for queued for backward compatibility
                    stats['totals']['pending'] += queued

            # Get all runners
            all_runners = self.get_runners()
            stats['runners'] = all_runners
            alive_runners = [r for r in all_runners if r.get('alive')]
            alive_ids = [r.get('runner_id') for r in alive_runners if r.get('runner_id')]
            stats['totals']['runners_active'] = len(alive_runners)

            # Database totals with active vs stale running split
            running_total = Job.objects.filter(status='running').count()
            if alive_ids:
                running_active = Job.objects.filter(status='running', runner_id__in=alive_ids).count()
            else:
                running_active = 0
            running_stale = max(0, running_total - running_active)

            stats['totals']['running'] = running_total
            stats['totals']['running_active'] = running_active
            stats['totals']['running_stale'] = running_stale
            stats['totals']['completed'] = Job.objects.filter(status='completed').count()
            stats['totals']['failed'] = Job.objects.filter(status='failed').count()

            # Check scheduler lock
            lock_value = self.redis.get(self.keys.scheduler_lock())
            if lock_value:
                stats['scheduler']['active'] = True
                stats['scheduler']['lock_holder'] = lock_value

        except Exception as e:
            logit.error(f"Failed to get system stats: {e}")

        return stats

    def pause_channel(self, channel: str) -> bool:
        """
        Pause a channel by setting a pause flag in Redis.
        Runners and scheduler should respect this flag.
        """
        try:
            self.redis.set(self.keys.channel_pause(channel), '1')
            logit.info(f"Paused channel {channel}")
            return True
        except Exception as e:
            logit.error(f"Failed to pause channel {channel}: {e}")
            return False

    def resume_channel(self, channel: str) -> bool:
        """
        Resume a channel by clearing the pause flag in Redis.
        """
        try:
            self.redis.delete(self.keys.channel_pause(channel))
            logit.info(f"Resumed channel {channel}")
            return True
        except Exception as e:
            logit.error(f"Failed to resume channel {channel}: {e}")
            return False

    def clear_channel(self, channel: str, cancel_db_pending: bool = True) -> Dict[str, Any]:
        """
        Completely clear a channel’s Redis queues and optionally cancel DB-pending jobs.

        Steps:
          1) Pause channel
          2) Delete main stream, broadcast stream, scheduled and scheduled_broadcast ZSETs
          3) Optionally mark DB pending jobs as canceled
          4) Resume channel
        """
        result: Dict[str, Any] = {
            'channel': channel,
            'deleted': {},
            'db_pending_canceled': 0,
            'status': True,
            'errors': []
        }
        try:
            self.pause_channel(channel)

            # Delete Plan B keys and legacy streams
            stream_key = self.keys.stream(channel)  # legacy
            broadcast_key = self.keys.stream_broadcast(channel)  # legacy
            sched_key = self.keys.sched(channel)
            sched_b_key = self.keys.sched_broadcast(channel)
            queue_key = self.keys.queue(channel)
            processing_key = self.keys.processing(channel)

            deleted_stream = self.redis.delete(stream_key)
            deleted_broadcast = self.redis.delete(broadcast_key)
            deleted_sched = self.redis.delete(sched_key)
            deleted_sched_b = self.redis.delete(sched_b_key)
            deleted_queue = self.redis.delete(queue_key)
            deleted_processing = self.redis.delete(processing_key)

            result['deleted'] = {
                'stream': bool(deleted_stream),
                'broadcast': bool(deleted_broadcast),
                'scheduled': bool(deleted_sched),
                'scheduled_broadcast': bool(deleted_sched_b),
                'queue': bool(deleted_queue),
                'processing': bool(deleted_processing),
            }

            if cancel_db_pending:
                try:
                    count = Job.objects.filter(
                        channel=channel,
                        status='pending'
                    ).update(
                        status='canceled',
                        finished_at=timezone.now()
                    )
                    result['db_pending_canceled'] = count
                except Exception as e:
                    result['errors'].append(f"DB cancel pending failed: {e}")
                    result['status'] = False

        except Exception as e:
            result['errors'].append(str(e))
            result['status'] = False
        finally:
            # Always attempt to resume to avoid leaving the channel paused
            self.resume_channel(channel)

        return result

    def requeue_db_pending(self, channel: str, limit: Optional[int] = None) -> Dict[str, Any]:
        """
        Requeue DB 'pending' jobs for a channel back into Redis streams.
        Useful after a clear to rebuild the stream from DB truth.
        """
        try:
            qs = Job.objects.filter(channel=channel, status='pending').order_by('created')
            if limit is not None:
                qs = qs[:int(limit)]

            requeued = 0
            for job in qs:
                stream_key = self.keys.stream_broadcast(channel) if job.broadcast else self.keys.stream(channel)
                try:
                    self.redis.xadd(stream_key, {
                        'job_id': job.id,
                        'func': job.func,
                        'created': timezone.now().isoformat()
                    })
                    try:
                        JobEvent.objects.create(
                            job=job,
                            channel=channel,
                            event='queued',
                            details={'requeued': True}
                        )
                    except Exception:
                        pass
                    requeued += 1
                except Exception as e:
                    logit.warn(f"Failed to requeue job {job.id} on {channel}: {e}")

            return {'status': True, 'requeued': requeued, 'channel': channel}
        except Exception as e:
            return {'status': False, 'error': str(e), 'channel': channel}

    def purge_old_jobs(self, days_old: int, status: Optional[str] = None, dry_run: bool = False) -> Dict[str, Any]:
        """
        Purge old jobs (and their events via cascade) from the database.

        Args:
            days_old: Delete jobs older than this many days
            status: Optional status filter to narrow deletion
            dry_run: If true, only count and do not delete

        Returns:
            dict with status and either count (dry_run) or delete details
        """
        try:
            cutoff = timezone.now() - timedelta(days=int(days_old))
            from django.db.models import Q
            query = Q(created__lt=cutoff)
            if status:
                query &= Q(status=status)
            qs = Job.objects.filter(query)
            count = qs.count()
            if dry_run:
                return {
                    'status': True,
                    'dry_run': True,
                    'count': count,
                    'cutoff': cutoff.isoformat(),
                    'status_filter': status
                }
            deleted, details = qs.delete()
            return {
                'status': True,
                'deleted': deleted,
                'details': details,
                'cutoff': cutoff.isoformat(),
                'status_filter': status
            }
        except Exception as e:
            return {'status': False, 'error': str(e)}

    def get_registered_channels(self) -> List[str]:
        """
        Discover registered channels by scanning Redis for main stream keys.
        Returns a sorted, de-duplicated list of channel names.
        """
        channels: List[str] = []
        try:
            pattern = f"{self.keys.prefix}:stream:*"
            client = self.redis.get_client()
            found = set()
            # Use scan_iter for cluster compatibility (handles both standalone and cluster modes)
            for k in client.scan_iter(match=pattern, count=200):
                key_str = k.decode('utf-8') if isinstance(k, (bytes, bytearray)) else k
                parts = key_str.split(":stream:")
                if len(parts) == 2 and parts[1]:
                    channel = parts[1]
                    # ignore broadcast suffix if present
                    if channel.endswith(":broadcast"):
                        channel = channel.rsplit(":broadcast", 1)[0]
                    if channel:
                        found.add(channel)
            channels = sorted(found)
        except Exception as e:
            logit.debug(f"Failed to discover channels via Redis scan: {e}")
            channels = []
        return channels

    def get_queue_sizes(self, channels: Optional[List[str]] = None) -> Dict[str, Any]:
        """
        Get current queue sizes for channels including DB status counts.

        Args:
            channels: Optional list of channels. Defaults to discovered streams or settings.JOBS_CHANNELS

        Returns:
            dict with per-channel sizes and DB status counts
        """
        try:
            from django.conf import settings as dj_settings
            channels = channels or self.get_registered_channels() or getattr(dj_settings, 'JOBS_CHANNELS', ['default'])
            sizes: Dict[str, Any] = {}
            for channel in channels:
                stream_key = self.keys.stream(channel)
                sched_key = self.keys.sched(channel)
                sched_b_key = self.keys.sched_broadcast(channel)

                # Stream length
                try:
                    info = self.redis.xinfo_stream(stream_key)
                    stream_len = info.get('length', 0)
                except Exception:
                    stream_len = 0

                # Scheduled counts (both ZSETs)
                scheduled = (self.redis.zcard(sched_key) or 0) + (self.redis.zcard(sched_b_key) or 0)

                # DB status counts
                from django.db.models import Count
                db_counts_qs = Job.objects.filter(channel=channel).values('status').annotate(count=Count('id'))
                status_counts = {row['status']: row['count'] for row in db_counts_qs}

                sizes[channel] = {
                    'stream': stream_len,
                    'scheduled': scheduled,
                    'db_pending': status_counts.get('pending', 0),
                    'db_running': status_counts.get('running', 0),
                    'db_completed': status_counts.get('completed', 0),
                    'db_failed': status_counts.get('failed', 0),
                    'db_canceled': status_counts.get('canceled', 0),
                    'db_expired': status_counts.get('expired', 0),
                }

            return {'status': True, 'data': sizes}
        except Exception as e:
            return {'status': False, 'error': str(e)}


def _jobmanager_cleanup_consumer_groups(self, channel: Optional[str] = None, destroy_empty_groups: bool = True) -> Dict[str, Any]:
    """
    Clean up Redis Stream consumer groups and consumers.

    - If channel is provided, operates on that channel only.
    - Otherwise, iterates discovered channels (or settings fallback).
    - Removes consumers with no pending messages.
    - Optionally destroys empty groups after consumer cleanup.

    Returns:
        Dict with per-channel cleanup results and any errors.
    """
    results: Dict[str, Any] = {'status': True, 'channels': {}, 'errors': []}
    try:
        # Determine channels to process
        try:
            from django.conf import settings as dj_settings
        except Exception:
            dj_settings = None

        if channel:
            channels = [channel]
        else:
            channels = self.get_registered_channels()
            if not channels and dj_settings:
                channels = getattr(dj_settings, 'JOBS_CHANNELS', ['default'])

        client = self.redis.get_client()

        for ch in channels:
            channel_result: Dict[str, Any] = {
                'stream': self.keys.stream(ch),
                'groups_processed': 0,
                'consumers_removed': 0,
                'groups_destroyed': 0,
                'errors': []
            }
            stream_key = self.keys.stream(ch)

            # Fetch groups for this stream
            try:
                groups = client.xinfo_groups(stream_key)
            except Exception as e:
                # If stream doesn't exist, nothing to clean
                channel_result['errors'].append(f"xinfo_groups failed: {e}")
                results['channels'][ch] = channel_result
                continue

            # Normalize groups to dicts with string keys
            norm_groups = []
            try:
                for g in groups or []:
                    if isinstance(g, dict):
                        name = g.get('name')
                        if isinstance(name, bytes):
                            name = name.decode('utf-8')
                        consumers_count = g.get('consumers', 0)
                        pending = g.get('pending', 0)
                        last_id = g.get('last-delivered-id')
                        if isinstance(last_id, bytes):
                            last_id = last_id.decode('utf-8')
                        norm_groups.append({
                            'name': name,
                            'consumers': int(consumers_count or 0),
                            'pending': int(pending or 0),
                            'last_delivered_id': last_id or ''
                        })
            except Exception as e:
                channel_result['errors'].append(f"group normalization failed: {e}")
                results['channels'][ch] = channel_result
                continue

            # Process each group
            for g in norm_groups:
                group_name = g['name']
                channel_result['groups_processed'] += 1
                try:
                    consumers = client.xinfo_consumers(stream_key, group_name)
                except Exception as e:
                    channel_result['errors'].append(f"xinfo_consumers({group_name}) failed: {e}")
                    consumers = []

                # Remove consumers with no pending messages
                removed = 0
                try:
                    for c in consumers or []:
                        cname = c.get('name')
                        if isinstance(cname, bytes):
                            cname = cname.decode('utf-8')
                        pending_c = int(c.get('pending', 0) or 0)
                        if pending_c == 0 and cname:
                            try:
                                client.execute_command('XGROUP', 'DELCONSUMER', stream_key, group_name, cname)
                                removed += 1
                            except Exception as e:
                                channel_result['errors'].append(f"DELCONSUMER {group_name}/{cname} failed: {e}")
                    channel_result['consumers_removed'] += removed
                except Exception as e:
                    channel_result['errors'].append(f"consumer removal loop failed for {group_name}: {e}")

                # Optionally destroy empty group
                if destroy_empty_groups:
                    try:
                        # Refresh group info to check if any consumers remain
                        refreshed_groups = client.xinfo_groups(stream_key)
                        grp = None
                        for rg in refreshed_groups or []:
                            nm = rg.get('name')
                            if isinstance(nm, bytes):
                                nm = nm.decode('utf-8')
                            if nm == group_name:
                                grp = rg
                                break
                        remaining = int(grp.get('consumers', 0) or 0) if grp else 0
                        if remaining == 0:
                            try:
                                client.execute_command('XGROUP', 'DESTROY', stream_key, group_name)
                                channel_result['groups_destroyed'] += 1
                            except Exception as e:
                                channel_result['errors'].append(f"XGROUP DESTROY {group_name} failed: {e}")
                    except Exception as e:
                        channel_result['errors'].append(f"post-clean xinfo_groups failed: {e}")

            results['channels'][ch] = channel_result

    except Exception as e:
        results['status'] = False
        results['errors'].append(str(e))

    return results

# Attach as a method on JobManager for runtime use
JobManager.cleanup_consumer_groups = _jobmanager_cleanup_consumer_groups

def _jobmanager_rebuild_scheduled(self, channel: Optional[str] = None, limit: Optional[int] = None) -> Dict[str, Any]:
    """
    Rebuild scheduled ZSETs from DB truth for pending jobs with future run_at.
    Useful if ZSETs were not populated during publish or after outages.

    Args:
        channel: Optional channel to restrict rebuild
        limit: Optional max number of jobs per channel

    Returns:
        Dict with per-channel counts and errors.
    """
    results: Dict[str, Any] = {'status': True, 'channels': {}, 'errors': []}
    try:
        from django.utils import timezone
        now = timezone.now()

        # Determine channels
        if channel:
            channels = [channel]
        else:
            channels = self.get_registered_channels()
            if not channels:
                try:
                    from django.conf import settings as dj_settings
                    channels = getattr(dj_settings, 'JOBS_CHANNELS', ['default'])
                except Exception:
                    channels = ['default']

        for ch in channels:
            ch_result = {'scheduled_added': 0, 'broadcast_added': 0, 'errors': []}
            try:
                # Query jobs pending with future run_at
                qs = Job.objects.filter(channel=ch, status='pending', run_at__gt=now).order_by('run_at')
                if limit is not None:
                    qs = qs[:int(limit)]

                sched_key = self.keys.sched(ch)
                sched_b_key = self.keys.sched_broadcast(ch)

                for job in qs:
                    try:
                        score = job.run_at.timestamp() * 1000.0
                        # Skip if already present
                        exists = self.redis.zscore(sched_b_key if job.broadcast else sched_key, job.id)
                        if exists is not None:
                            continue
                        # Insert into appropriate ZSET
                        if job.broadcast:
                            self.redis.zadd(sched_b_key, {job.id: score})
                            ch_result['broadcast_added'] += 1
                        else:
                            self.redis.zadd(sched_key, {job.id: score})
                            ch_result['scheduled_added'] += 1
                    except Exception as ie:
                        ch_result['errors'].append(f"{job.id}: {ie}")

            except Exception as ce:
                ch_result['errors'].append(str(ce))

            results['channels'][ch] = ch_result

    except Exception as e:
        results['status'] = False
        results['errors'].append(str(e))

    return results

# Attach as a method on JobManager for runtime use
JobManager.rebuild_scheduled = _jobmanager_rebuild_scheduled

def _jobmanager_recover_stale_running(self, channel: Optional[str] = None, max_age_seconds: Optional[int] = None) -> Dict[str, Any]:
    """
    Recover stale running jobs (DB shows status='running' but no inflight messages in Redis).
    For each channel (or a specific channel), if inflight_count == 0, reset DB running jobs
    to pending and requeue them to the stream immediately.

    Args:
        channel: Optional channel to restrict recovery
        max_age_seconds: Optional age threshold (only recover jobs started before now - max_age_seconds)

    Returns:
        Dict with per-channel recovery results and any errors:
        {
          status: True/False,
          channels: {
            channel: {
              examined: N,
              recovered: M,
              errors: [...]
            },
            ...
          },
          errors: [...]
        }
    """
    results: Dict[str, Any] = {'status': True, 'channels': {}, 'errors': []}
    try:
        # Determine channels
        if channel:
            channels = [channel]
        else:
            try:
                channels = self.get_registered_channels()
            except Exception:
                channels = []
            if not channels:
                try:
                    from django.conf import settings as dj_settings
                    channels = getattr(dj_settings, 'JOBS_CHANNELS', ['default'])
                except Exception:
                    channels = ['default']

        now = timezone.now()
        for ch in channels:
            ch_result: Dict[str, Any] = {'examined': 0, 'recovered': 0, 'errors': []}
            try:
                # Check inflight (PEL) for this channel
                state = self.get_queue_state(ch)
                inflight = int(state.get('inflight_count', 0) or 0)

                # Only recover when no inflight (avoid racing real running work)
                if inflight > 0:
                    results['channels'][ch] = ch_result
                    continue

                # Build query for DB running jobs
                from django.db.models import Q
                q = Q(channel=ch, status='running')
                if max_age_seconds is not None and max_age_seconds > 0:
                    cutoff = now - timedelta(seconds=int(max_age_seconds))
                    q &= Q(started_at__lt=cutoff)

                running_qs = Job.objects.filter(q).order_by('started_at')
                ch_result['examined'] = running_qs.count()

                # Requeue each recovered job
                for job in running_qs:
                    try:
                        # Reset DB status to pending
                        job.status = 'pending'
                        job.runner_id = None
                        job.cancel_requested = False
                        job.started_at = None
                        job.finished_at = None
                        job.last_error = job.last_error or ''
                        job.stack_trace = job.stack_trace or ''
                        job.save(update_fields=['status', 'runner_id', 'cancel_requested', 'started_at', 'finished_at', 'last_error', 'stack_trace', 'modified'])

                        # Push to stream immediately
                        stream_key = self.keys.stream(job.channel) if not job.broadcast else self.keys.stream_broadcast(job.channel)
                        try:
                            self.redis.xadd(stream_key, {
                                'job_id': job.id,
                                'func': job.func,
                                'recovered': now.isoformat()
                            })
                        except Exception as xe:
                            ch_result['errors'].append(f"xadd failed for {job.id}: {xe}")

                        # Event
                        try:
                            JobEvent.objects.create(
                                job=job,
                                channel=job.channel,
                                event='retry',
                                details={'reason': 'recover_stale_running'}
                            )
                        except Exception as ee:
                            ch_result['errors'].append(f"event failed for {job.id}: {ee}")

                        ch_result['recovered'] += 1
                    except Exception as je:
                        ch_result['errors'].append(f"{job.id}: {je}")

            except Exception as ce:
                ch_result['errors'].append(str(ce))

            results['channels'][ch] = ch_result

    except Exception as e:
        results['status'] = False
        results['errors'].append(str(e))

    return results

# Attach as a method on JobManager for runtime use
JobManager.recover_stale_running = _jobmanager_recover_stale_running

# Module-level singleton

def _jobmanager_migrate_list_queues(self, channel: Optional[str] = None, dry_run: bool = True, max_items: Optional[int] = None) -> Dict[str, Any]:
    """
    Migrate items from old untagged list queues (prefix:queue:{channel}) to new tagged queues
    that include the cluster hash tag for BRPOP in Redis Cluster/Valkey Serverless.

    Args:
        channel: Optional single channel to migrate (default: all configured channels)
        dry_run: If True, do not move items—just report counts
        max_items: Optional cap on how many items to move per channel

    Returns:
        Dict with per-channel results and any errors.
    """
    results: Dict[str, Any] = {'status': True, 'channels': {}, 'errors': []}

    try:
        channels = [channel] if channel else getattr(settings, 'JOBS_CHANNELS', ['default'])
        client = self.redis.get_client()

        for ch in channels:
            old_key = f"{self.keys.prefix}:queue:{ch}"
            new_key = self.keys.queue(ch)

            ch_result: Dict[str, Any] = {
                'channel': ch,
                'old_key': old_key,
                'new_key': new_key,
                'migrated': 0,
                'old_len_before': 0,
                'old_len_after': 0
            }

            try:
                old_len = client.llen(old_key) or 0
                ch_result['old_len_before'] = int(old_len)

                if dry_run or old_len == 0:
                    results['channels'][ch] = ch_result
                    continue

                moved = 0
                while True:
                    if max_items is not None and moved >= int(max_items):
                        break
                    item = client.rpop(old_key)
                    if item is None:
                        break
                    client.lpush(new_key, item)
                    moved += 1

                ch_result['migrated'] = moved
                ch_result['old_len_after'] = int(client.llen(old_key) or 0)
                results['channels'][ch] = ch_result

            except Exception as ce:
                ch_result['error'] = str(ce)
                results['channels'][ch] = ch_result
                results['errors'].append(f"{ch}: {ce}")
                results['status'] = False

    except Exception as e:
        results['status'] = False
        results['errors'].append(str(e))

    return results

# Attach as a method on JobManager for runtime use
JobManager.migrate_list_queues = _jobmanager_migrate_list_queues

def migrate_list_queues(channel: Optional[str] = None, dry_run: bool = True, max_items: Optional[int] = None) -> Dict[str, Any]:
    """
    Convenience wrapper to migrate old list queues to new tagged queues.

    Example:
        from mojo.apps.jobs.manager import migrate_list_queues
        migrate_list_queues(channel="default", dry_run=False)
    """
    return get_manager().migrate_list_queues(channel=channel, dry_run=dry_run, max_items=max_items)
_manager = None


def get_manager() -> JobManager:
    """
    Get the JobManager singleton instance.

    Returns:
        JobManager instance
    """
    global _manager
    if not _manager:
        _manager = JobManager()
    return _manager


# Convenience functions for Django shell
def clear_stuck_jobs(channel: str, idle_threshold_ms: int = 60000) -> Dict[str, Any]:
    """
    Convenience function to clear stuck jobs from Django shell.

    Usage:
        from mojo.apps.jobs.manager import clear_stuck_jobs
        result = clear_stuck_jobs('email', idle_threshold_ms=60000)
        print(result)

    Args:
        channel: Channel name to clear
        idle_threshold_ms: Consider stuck if idle longer than this (0 to clear all)

    Returns:
        Dict with results
    """
    return get_manager().clear_stuck_jobs(channel, idle_threshold_ms=idle_threshold_ms)


def get_channel_health(channel: str) -> Dict[str, Any]:
    """
    Convenience function to check channel health from Django shell.

    Usage:
        from mojo.apps.jobs.manager import get_channel_health
        health = get_channel_health('email')
        print(f"Pending: {health['messages']['pending']}")

    Args:
        channel: Channel name to check

    Returns:
        Channel health dict
    """
    return get_manager().get_channel_health(channel)
