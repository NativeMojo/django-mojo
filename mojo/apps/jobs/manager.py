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

            # Note: In production, use SCAN instead of KEYS for better performance
            # For now, using a simple approach
            all_keys = []
            cursor = 0
            while True:
                cursor, keys = self.redis.get_client().scan(
                    cursor, match=pattern, count=100
                )
                all_keys.extend(keys)
                if cursor == 0:
                    break

            # Check each runner
            for key in all_keys:
                try:
                    # Get heartbeat data
                    data = self.redis.get(key.decode('utf-8') if isinstance(key, bytes) else key)
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
            logit.error(f"Failed to get runners: {e}")

        # Sort by runner_id for consistency
        runners.sort(key=lambda r: r.get('runner_id', ''))

        return runners

    def get_queue_state(self, channel: str) -> Dict[str, Any]:
        """
        Get queue state for a channel.

        Args:
            channel: Channel name

        Returns:
            Dict with queue statistics:
                - stream_length: Number of messages in main stream
                - broadcast_length: Number of messages in broadcast stream
                - scheduled_count: Number of scheduled jobs
                - pending_count: Number of pending messages (being processed)
                - runners: Number of active runners
                - consumer_groups: List of consumer group info
        """
        state = {
            'channel': channel,
            'stream_length': 0,
            'broadcast_length': 0,
            'scheduled_count': 0,
            'pending_count': 0,
            'runners': 0,
            'consumer_groups': []
        }

        try:
            # Main stream info
            stream_key = self.keys.stream(channel)
            try:
                info = self.redis.xinfo_stream(stream_key)
                state['stream_length'] = info.get('length', 0)

                # Get pending count from consumer group
                group_key = self.keys.group_workers(channel)
                pending_info = self.redis.xpending(stream_key, group_key)
                if pending_info:
                    state['pending_count'] = pending_info.get('pending', 0)

            except Exception as e:
                logit.debug(f"Stream {stream_key} not found or has no data: {e}")

            # Broadcast stream info
            broadcast_key = self.keys.stream_broadcast(channel)
            try:
                info = self.redis.xinfo_stream(broadcast_key)
                state['broadcast_length'] = info.get('length', 0)
            except Exception as e:
                logit.debug(f"Broadcast stream {broadcast_key} not found: {e}")

            # Scheduled jobs count
            sched_key = self.keys.sched(channel)
            state['scheduled_count'] = self.redis.zcard(sched_key)

            # Active runners for this channel
            runners = self.get_runners(channel)
            state['runners'] = len([r for r in runners if r.get('alive')])

            # Consumer group details
            try:
                # Main stream consumer group
                groups_info = self.redis.get_client().xinfo_groups(stream_key)
                for group in groups_info:
                    state['consumer_groups'].append({
                        'name': group.get('name'),
                        'consumers': group.get('consumers', 0),
                        'pending': group.get('pending', 0),
                        'last_delivered_id': group.get('last-delivered-id')
                    })
            except Exception as e:
                logit.debug(f"Failed to get consumer group info: {e}")

            # Add metrics
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
        Find jobs that have been claimed but not processed.

        Args:
            channel: Channel name
            idle_threshold_ms: Consider stuck if idle longer than this (default 1 minute)

        Returns:
            List of stuck job details
        """
        stream_key = self.keys.stream(channel)
        group_key = self.keys.group_workers(channel)

        stuck = []
        try:
            # Get detailed pending info using xpending with range
            client = self.redis.get_client()
            # XPENDING key group [idle] start end count [consumer]
            pending_details = client.execute_command(
                'XPENDING', stream_key, group_key,
                idle_threshold_ms, '-', '+', '100'
            )

            if pending_details:
                for entry in pending_details:
                    # Entry format: [message_id, consumer, idle_time, delivery_count]
                    if len(entry) >= 4:
                        stuck.append({
                            'message_id': entry[0].decode('utf-8') if isinstance(entry[0], bytes) else entry[0],
                            'consumer': entry[1].decode('utf-8') if isinstance(entry[1], bytes) else entry[1],
                            'idle_ms': entry[2],
                            'delivery_count': entry[3]
                        })
        except Exception as e:
            logit.error(f"Failed to check stuck jobs: {e}")
            # Fallback to basic pending info
            try:
                pending_info = self.redis.xpending(stream_key, group_key)
                if pending_info and pending_info.get('pending', 0) > 0:
                    # Can't get details, but report that there are pending jobs
                    stuck.append({
                        'message_id': 'unknown',
                        'consumer': 'unknown',
                        'idle_ms': 0,
                        'delivery_count': 0,
                        'note': f"Total pending: {pending_info['pending']}"
                    })
            except:
                pass

        return stuck

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
            # Create a unique response key
            response_key = f"{self.keys.runner_ctl(runner_id)}:response:{uuid.uuid4().hex[:8]}"

            # Send ping command
            control_key = self.keys.runner_ctl(runner_id)
            message = json.dumps({
                'command': 'ping',
                'response_key': response_key
            })

            self.redis.publish(control_key, message)

            # Wait for response
            start_time = time.time()
            while time.time() - start_time < timeout:
                response = self.redis.get(response_key)
                if response == 'pong':
                    self.redis.delete(response_key)
                    return True
                time.sleep(0.1)

            # Timeout
            self.redis.delete(response_key)
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
            channels = getattr(settings, 'JOBS_CHANNELS', ['default'])
            for channel in channels:
                state = self.get_queue_state(channel)
                stats['channels'][channel] = state

                # Aggregate totals
                stats['totals']['scheduled'] += state['scheduled_count']
                stats['totals']['pending'] += state['stream_length']

            # Get all runners
            all_runners = self.get_runners()
            stats['runners'] = all_runners
            stats['totals']['runners_active'] = len([r for r in all_runners if r['alive']])

            # Database totals
            stats['totals']['running'] = Job.objects.filter(status='running').count()
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


# Module-level singleton
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
