"""
JobEngine - The runner daemon for executing jobs.

This module implements the core job execution engine that consumes
jobs from Redis Streams and executes registered handlers.
"""
import sys
import signal
import socket
import time
import json
import threading
import random
import traceback
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Set, Tuple

from django.db import close_old_connections

from mojo.helpers.settings import settings
from mojo.helpers import logit
from .keys import JobKeys
from .adapters import get_adapter
from .models import Job, JobEvent
import concurrent.futures
import importlib
from threading import Lock, Semaphore
from typing import Callable

from mojo.apps import metrics
from mojo.helpers import dates

logger = logit.get_logger("jobs", "jobs.log", debug=True)


JOBS_ENGINE_CLAIM_BATCH = settings.get('JOBS_ENGINE_CLAIM_BATCH', 5)
JOBS_CHANNELS = settings.get('JOBS_CHANNELS', ['default'])
JOBS_ENGINE_MAX_WORKERS = settings.get('JOBS_ENGINE_MAX_WORKERS', 10)
JOBS_ENGINE_CLAIM_BUFFER = settings.get('JOBS_ENGINE_CLAIM_BUFFER', 2)
JOBS_RUNNER_HEARTBEAT_SEC = settings.get('JOBS_RUNNER_HEARTBEAT_SEC', 5)


def load_job_function(func_path: str) -> Callable:
    """
    Dynamically import a job function.
    Example: 'mojo.apps.account.jobs.send_invite'
    """
    try:
        module_path, func_name = func_path.rsplit('.', 1)
        module = importlib.import_module(module_path)
        return getattr(module, func_name)
    except (ImportError, AttributeError, ValueError) as e:
        raise ImportError(f"Cannot load job function '{func_path}': {e}")


class JobEngine:
    """
    Job execution engine that runs as a daemon process.

    Consumes jobs from Redis Streams and executes handlers dynamically
    with support for retries, cancellation, and parallel execution.
    """

    def __init__(self, channels: Optional[List[str]] = None,
                 runner_id: Optional[str] = None,
                 max_workers: Optional[int] = None):
        """
        Initialize the job engine.

        Args:
            channels: List of channels to consume from (default: from settings.JOBS_CHANNELS)
            runner_id: Unique runner identifier (auto-generated if not provided)
            max_workers: Maximum thread pool workers (default from settings)
        """
        self.channels = channels or JOBS_CHANNELS
        self.runner_id = runner_id or self._generate_runner_id()
        self.redis = get_adapter()
        self.keys = JobKeys()

        # Thread pool configuration
        self.max_workers = max_workers or JOBS_ENGINE_MAX_WORKERS
        self.executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=self.max_workers,
            thread_name_prefix=f"JobWorker-{self.runner_id}"
        )

        # Track active jobs
        self.active_jobs = {}
        self.active_lock = Lock()

        # Limit claimed jobs
        claim_buffer = JOBS_ENGINE_CLAIM_BUFFER
        self.max_claimed = self.max_workers * claim_buffer
        self.claim_semaphore = Semaphore(self.max_claimed)

        # Control flags
        self.running = False
        self.is_initialized = False
        self.stop_event = threading.Event()

        # Heartbeat thread
        self.heartbeat_thread = None
        self.heartbeat_interval = JOBS_RUNNER_HEARTBEAT_SEC

        # Control channel listener
        self.control_thread = None

        # Stats
        self.jobs_processed = 0
        self.jobs_failed = 0
        self.start_time = None

        logger.info(f"JobEngine initialized: runner_id={self.runner_id}, "
                  f"channels={self.channels}")

    def _generate_runner_id(self) -> str:
        """Generate a consistent runner ID based on hostname and channels."""
        hostname = socket.gethostname()
        # Clean hostname for use in ID (remove dots, make lowercase)
        clean_hostname = hostname.lower().replace('.', '-').replace('_', '-')

        # # Create a consistent suffix based on channels served
        # channels_hash = hash(tuple(sorted(self.channels))) % 10000

        return f"{clean_hostname}-engine"

    def initialize(self):
        if (self.is_initialized):
            logger.warning("JobEngine already initialized")
            return
        self.is_initialized = True

        logger.info(f"Initializing JobEngine {self.runner_id}")
        self.running = True
        self.start_time = dates.utcnow()
        self.stop_event.clear()

        # Ensure consumer groups exist
        self._setup_consumer_groups()

        # Start heartbeat thread
        self._start_heartbeat()

        # Start control listener thread
        self._start_control_listener()

        # Register signal handlers
        self._setup_signal_handlers()

    def start(self):
        """
        Start the job engine.

        Sets up consumer groups, starts heartbeat, and begins processing.
        """
        if self.running:
            logger.warning("JobEngine already running")
            return

        self.initialize()

        # Main processing loop
        try:
            self._main_loop()
        except KeyboardInterrupt:
            logger.info("JobEngine interrupted by user")
        except Exception as e:
            logger.error(f"JobEngine crashed: {e}")
            raise
        finally:
            self.stop()

    def stop(self, timeout: float = 30.0):
        """
        Stop the job engine gracefully.

        Args:
            timeout: Maximum time to wait for clean shutdown
        """
        if self.running:
            logger.info(f"Stopping JobEngine {self.runner_id}...")
            self.running = False
            self.stop_event.set()
            # Wait for active jobs
            with self.active_lock:
                active = list(self.active_jobs.values())
            if active:
                logger.info(f"Waiting for {len(active)} active jobs...")
                futures = [j['future'] for j in active]
                concurrent.futures.wait(futures, timeout=timeout/2)
            # Shutdown executor
            self.executor.shutdown(wait=True)

        # Stop heartbeat
        if self.heartbeat_thread and self.heartbeat_thread.is_alive():
            self.heartbeat_thread.join(timeout=5.0)

        # Stop control listener
        if self.control_thread and self.control_thread.is_alive():
            self.control_thread.join(timeout=5.0)

        # Clean up consumer registrations and reclaim pending jobs
        self._cleanup_consumer_groups()

        # Clean up Redis keys
        try:
            self.redis.delete(self.keys.runner_hb(self.runner_id))
        except Exception as e:
            logger.warning(f"Failed to clean up runner keys: {e}")

        logger.info(f"JobEngine {self.runner_id} stopped. "
                  f"Processed: {self.jobs_processed}, Failed: {self.jobs_failed}")

    def _cleanup_consumer_groups(self):
        """
        Clean up consumer group registrations on shutdown.
        This prevents accumulation of dead consumers.
        """
        logger.info(f"Cleaning up consumer registrations for {self.runner_id}")

        for channel in self.channels:
            try:
                stream_key = self.keys.stream(channel)
                group_key = self.keys.group_workers(channel)
                broadcast_stream = self.keys.stream_broadcast(channel)
                runner_group = self.keys.group_runner(channel, self.runner_id)

                client = self.redis.get_client()

                # For main stream: reclaim and ACK any pending jobs before deletion
                try:
                    pending_info = client.execute_command(
                        'XPENDING', stream_key, group_key, '-', '+', '100', self.runner_id
                    )

                    if pending_info:
                        message_ids = [msg[0] for msg in pending_info]
                        if message_ids:
                            # Reclaim and immediately ACK to clear them
                            try:
                                claimed = client.execute_command(
                                    'XCLAIM', stream_key, group_key, self.runner_id,
                                    '0', *message_ids
                                )
                                if claimed:
                                    client.execute_command('XACK', stream_key, group_key, *message_ids)
                                    logger.info(f"Cleared {len(message_ids)} pending jobs during cleanup for {channel}")
                            except Exception as e:
                                logger.warning(f"Failed to clear pending jobs during cleanup: {e}")

                except Exception as e:
                    logger.debug(f"No pending jobs to clean for {channel}: {e}")

                # Delete consumer from main group
                try:
                    client.execute_command('XGROUP', 'DELCONSUMER', stream_key, group_key, self.runner_id)
                    logger.debug(f"Removed consumer {self.runner_id} from group {group_key}")
                except Exception as e:
                    logger.debug(f"Consumer {self.runner_id} was not in group {group_key}: {e}")

                # Delete consumer from broadcast group
                try:
                    client.execute_command('XGROUP', 'DELCONSUMER', broadcast_stream, runner_group, self.runner_id)
                    logger.debug(f"Removed consumer {self.runner_id} from broadcast group {runner_group}")
                except Exception as e:
                    logger.debug(f"Consumer {self.runner_id} was not in broadcast group {runner_group}: {e}")

            except Exception as e:
                logger.warning(f"Failed to cleanup consumer groups for {channel}: {e}")

    def _setup_consumer_groups(self):
        """Ensure all required consumer groups exist."""
        for channel in self.channels:
            # Workers group for normal stream
            stream_key = self.keys.stream(channel)
            group_key = self.keys.group_workers(channel)
            try:
                self.redis.xgroup_create(stream_key, group_key, id='0', mkstream=True)
                logger.info(f"Created consumer group {group_key} for {stream_key}")
            except Exception as e:
                # Group likely already exists, which is fine
                logger.info(f"Consumer group {group_key} already exists: {e}")

            # Per-runner group for broadcast stream
            broadcast_stream = self.keys.stream_broadcast(channel)
            runner_group = self.keys.group_runner(channel, self.runner_id)
            try:
                self.redis.xgroup_create(broadcast_stream, runner_group, id='0', mkstream=True)
                logger.info(f"Created runner group {runner_group} for {broadcast_stream}")
            except Exception as e:
                # Group likely already exists, which is fine
                logger.info(f"Runner group {runner_group} already exists: {e}")

            logger.info(f"Consumer groups ready for channel: {channel}")

    def _setup_signal_handlers(self):
        """Register signal handlers for graceful shutdown."""
        def handle_signal(signum, frame):
            logger.info(f"Received signal {signum}, initiating graceful shutdown")
            self.stop()
            sys.exit(0)

        signal.signal(signal.SIGTERM, handle_signal)
        signal.signal(signal.SIGINT, handle_signal)

    def _start_heartbeat(self):
        """Start the heartbeat thread."""
        self.heartbeat_thread = threading.Thread(
            target=self._heartbeat_loop,
            name=f"Heartbeat-{self.runner_id}",
            daemon=True
        )
        self.heartbeat_thread.start()

    def _heartbeat_loop(self):
        """Heartbeat thread main loop."""
        hb_key = self.keys.runner_hb(self.runner_id)

        while self.running and not self.stop_event.is_set():
            try:
                # Update heartbeat with TTL
                self.redis.set(hb_key, json.dumps({
                    'runner_id': self.runner_id,
                    'hostname': socket.gethostname(),
                    'channels': self.channels,
                    'jobs_processed': self.jobs_processed,
                    'jobs_failed': self.jobs_failed,
                    'started': self.start_time.isoformat(),
                    'last_heartbeat': dates.utcnow().isoformat()
                }), ex=self.heartbeat_interval * 3)  # TTL = 3x interval

            except Exception as e:
                logger.warning(f"Heartbeat update failed: {e}")

            # Sleep with periodic wake for stop check
            for _ in range(self.heartbeat_interval):
                if self.stop_event.is_set():
                    break
                time.sleep(1)

    def _start_control_listener(self):
        """Start the control channel listener thread."""
        self.control_thread = threading.Thread(
            target=self._control_loop,
            name=f"Control-{self.runner_id}",
            daemon=True
        )
        self.control_thread.start()

    def _control_loop(self):
        """Control channel listener loop."""
        control_key = self.keys.runner_ctl(self.runner_id)
        broadcast_key = "mojo:jobs:runners:broadcast"
        pubsub = self.redis.pubsub()
        # Listen to runner-specific control and global broadcast control
        pubsub.subscribe(control_key, broadcast_key)

        try:
            while self.running and not self.stop_event.is_set():
                message = pubsub.get_message(timeout=5.0)
                if message and message.get('type') == 'message':
                    self._handle_control_message(message.get('data'), message.get('channel'))
        finally:
            pubsub.close()

    def _handle_control_message(self, data: bytes, channel: Optional[str] = None):
        """Handle a control channel message or broadcast command."""
        try:
            message = json.loads(data.decode('utf-8'))
            command = message.get('command')

            if command == 'ping':
                # Respond with pong (direct control)
                response_key = message.get('response_key')
                if response_key:
                    self.redis.set(response_key, 'pong', ex=5)
                logger.info("Responded to ping from control channel")

            elif command == 'status':
                # Broadcast status reply
                reply_channel = message.get('reply_channel')
                if reply_channel:
                    reply = {
                        'runner_id': self.runner_id,
                        'channels': self.channels,
                        'jobs_processed': self.jobs_processed,
                        'jobs_failed': self.jobs_failed,
                        'started': self.start_time.isoformat() if self.start_time else None,
                        'timestamp': dates.utcnow().isoformat(),
                    }
                    try:
                        self.redis.publish(reply_channel, json.dumps(reply))
                    except Exception as e:
                        logger.warning(f"Failed to publish status reply: {e}")

            elif command == 'shutdown':
                logger.info("Received shutdown command from control channel/broadcast")
                self.stop()

            else:
                logger.warning(f"Unknown control command: {command}")

        except Exception as e:
            logger.error(f"Failed to handle control message: {e}")

    def _main_loop(self):
        """Main processing loop - claims jobs based on capacity."""
        logger.info(f"JobEngine {self.runner_id} entering main loop")

        while self.running and not self.stop_event.is_set():
            try:
                # Check available capacity
                with self.active_lock:
                    active_count = len(self.active_jobs)

                if active_count >= self.max_claimed:
                    time.sleep(0.1)
                    continue

                # Claim jobs up to available capacity
                available = self.max_claimed - active_count
                claim_batch = JOBS_ENGINE_CLAIM_BATCH
                messages = self.claim_jobs(min(available, claim_batch))

                if messages:
                    logger.info(f"Claimed {len(messages)} jobs")
                # else:
                #     logger.info(f"No jobs claimed (active: {active_count}, max: {self.max_claimed})")

                for stream_key, msg_id, job_id in messages:
                    # Submit to thread pool
                    future = self.executor.submit(
                        self.execute_job,
                        stream_key, msg_id, job_id
                    )

                    # Track active job
                    with self.active_lock:
                        self.active_jobs[job_id] = {
                            'future': future,
                            'started': dates.utcnow(),
                            'stream': stream_key,
                            'msg_id': msg_id
                        }

                    # Cleanup callback
                    future.add_done_callback(
                        lambda f, jid=job_id: self._job_completed(jid)
                    )

                # If no jobs were claimed, sleep
                if not messages:
                    time.sleep(0.5)

            except Exception as e:
                logger.error(f"Error in main loop: {e}")
                time.sleep(1)  # Brief pause before retry

    def claim_jobs_by_channel(self, channel: str, count: int) -> List[Tuple[str, str, str]]:
        """
        Claim up to 'count' jobs from Redis streams.

        Args:
            channel: Channel to claim jobs from
            count: Maximum number of jobs to claim

        Returns:
            List of (stream_key, msg_id, job_id) tuples
        """
        claimed = []

        stream_key = self.keys.stream(channel)
        group = self.keys.group_workers(channel)

        # Respect paused channels
        try:
            if self.redis.get(self.keys.channel_pause(channel)):
                return claimed
        except Exception:
            pass

        try:
            # Check if stream exists and has messages first
            try:
                stream_info = self.redis.xinfo_stream(stream_key)
                stream_length = stream_info.get('length', 0)
                if stream_length == 0:
                    return claimed
            except Exception as e:
                logger.error(f"Channel {channel} stream doesn't exist or error: {e}")
                return claimed

            # Non-blocking read
            messages = self.redis.xreadgroup(
                group=group,
                consumer=self.runner_id,
                streams={stream_key: '>'},
                count=count,
                block=100  # 100ms timeout
            )
            if messages:
                stream_data = messages[0]  # Should be [stream_key, [(msg_id, data), ...]]
                message_list = stream_data[1]
                for msg_id, data in message_list:
                    # Try both string and bytes keys for job_id
                    job_id = data.get('job_id') or data.get(b'job_id', b'')
                    # Handle bytes conversion if needed
                    if isinstance(job_id, bytes):
                        job_id = job_id.decode('utf-8')
                    if job_id:
                        claimed.append((stream_key, msg_id, job_id))
                    else:
                        logger.warning(f"Message {msg_id} has no job_id: {data}")

        except Exception as e:
            logger.exception(f"Failed to claim jobs from {channel}: {e}")
        return claimed

    def claim_jobs(self, count: int) -> List[Tuple[str, str, str]]:
        """
        Claim up to 'count' jobs from Redis streams.

        Args:
            count: Maximum number of jobs to claim

        Returns:
            List of (stream_key, msg_id, job_id) tuples
        """
        claimed = []
        # Prioritize 'priority' channel first if present
        channels_ordered = list(self.channels)
        if 'priority' in channels_ordered:
            channels_ordered = ['priority'] + [c for c in channels_ordered if c != 'priority']
        for channel in channels_ordered:
            if len(claimed) >= count:
                break
            channel_messages = self.claim_jobs_by_channel(channel, count - len(claimed))
            claimed.extend(channel_messages)
        return claimed

    def _ack_message(self, stream_key: str, msg_id: str):
        """Acknowledge a message in the stream."""
        try:
            # Determine group based on stream type
            if ':broadcast' in stream_key:
                channel = stream_key.split(':')[-2]
                group = self.keys.group_runner(channel, self.runner_id)
            else:
                channel = stream_key.split(':')[-1]
                group = self.keys.group_workers(channel)

            self.redis.xack(stream_key, group, msg_id)
        except Exception as e:
            logit.error(f"Failed to ACK message {msg_id}: {e}")

    def execute_job(self, stream_key: str, msg_id: str, job_id: str):
        """Execute job and handle all state updates."""
        job = None
        try:
            # Load job from database
            close_old_connections()
            job = Job.objects.select_for_update().get(id=job_id)
        except Exception as e:
            logit.error(f"Failed to load job {job_id}: {e}")
            self._handle_job_failure(job_id, stream_key, msg_id, e)

        try:
            # Check if already processed or canceled
            if job.status in ('completed', 'canceled'):
                self._ack_message(stream_key, msg_id)
                return

            # Check expiration
            if job.is_expired:
                job.status = 'expired'
                job.finished_at = dates.utcnow()
                job.save(update_fields=['status', 'finished_at'])

                # Event: expired
                try:
                    JobEvent.objects.create(
                        job=job,
                        channel=job.channel,
                        event='expired',
                        runner_id=self.runner_id,
                        attempt=job.attempt,
                        details={'reason': 'job_expired_before_execution'}
                    )
                except Exception:
                    pass

                # ACK after DB update
                self._ack_message(stream_key, msg_id)
                metrics.record("jobs.expired")
                return

            # Mark as running
            job.status = 'running'
            job.started_at = dates.utcnow()
            job.runner_id = self.runner_id
            job.attempt += 1
            job.save(update_fields=['status', 'started_at', 'runner_id', 'attempt'])

            # Event: running
            try:
                JobEvent.objects.create(
                    job=job,
                    channel=job.channel,
                    event='running',
                    runner_id=self.runner_id,
                    attempt=job.attempt,
                    details={'stream': stream_key, 'msg_id': msg_id}
                )
            except Exception:
                pass

            # Load and execute function
            func = load_job_function(job.func)
            func(job)

            # Mark complete
            job.status = 'completed'
            job.finished_at = dates.utcnow()
            job.save(update_fields=['status', 'finished_at', 'metadata'])
            logger.info(f"Job {job.id} completed")
            # Event: completed
            try:
                JobEvent.objects.create(
                    job=job,
                    channel=job.channel,
                    event='completed',
                    runner_id=self.runner_id,
                    attempt=job.attempt,
                    details={}
                )
            except Exception:
                pass

            # ACK message (after DB update)
            self._ack_message(stream_key, msg_id)

            # Metrics
            metrics.record("jobs.completed", count=1)
            metrics.record(f"jobs.channel.{job.channel}.completed", count=1)
            metrics.record("jobs.duration_ms", count=job.duration_ms)

        except Exception as e:
            job.add_log(f"Failed to complete job: {e}", kind="error")
            self._handle_job_failure(job_id, stream_key, msg_id, e)

    def _handle_job_failure(self, job_id: str, stream_key: str,
                           msg_id: str, error: Exception):
        """Handle job failure with retries."""
        try:
            job = Job.objects.select_for_update().get(id=job_id)

            # Record error
            job.last_error = str(error)
            job.stack_trace = traceback.format_exc()

            # Check retry eligibility
            if job.attempt < job.max_retries:
                # Calculate backoff with jitter
                backoff = min(
                    job.backoff_base ** job.attempt,
                    job.backoff_max_sec
                )
                jitter = backoff * (0.8 + random.random() * 0.4)

                # Schedule retry
                job.run_at = dates.utcnow() + timedelta(seconds=jitter)
                job.status = 'pending'
                job.save(update_fields=[
                    'status', 'run_at', 'last_error', 'stack_trace'
                ])

                # Event: retry scheduled
                try:
                    JobEvent.objects.create(
                        job=job,
                        channel=job.channel,
                        event='retry',
                        runner_id=self.runner_id,
                        attempt=job.attempt,
                        details={'reason': 'failure', 'next_run_at': job.run_at.isoformat()}
                    )
                except Exception:
                    pass

                # Add to scheduled ZSET (route by broadcast)
                score = job.run_at.timestamp() * 1000
                target_zset = self.keys.sched_broadcast(job.channel) if job.broadcast else self.keys.sched(job.channel)
                self.redis.zadd(target_zset, {job_id: score})

                metrics.record("jobs.retried")
            else:
                # Max retries exceeded
                job.status = 'failed'
                job.finished_at = dates.utcnow()
                job.save(update_fields=[
                    'status', 'finished_at', 'last_error', 'stack_trace'
                ])

                # Event: failed
                try:
                    JobEvent.objects.create(
                        job=job,
                        channel=job.channel,
                        event='failed',
                        runner_id=self.runner_id,
                        attempt=job.attempt,
                        details={'error': job.last_error}
                    )
                except Exception:
                    pass

                metrics.record("jobs.failed")
                metrics.record(f"jobs.channel.{job.channel}.failed")

            # Always ACK to prevent redelivery
            self._ack_message(stream_key, msg_id)

        except Exception as e:
            logit.error(f"Failed to handle job failure: {e}")

    def _job_completed(self, job_id: str):
        """Callback when job future completes."""
        with self.active_lock:
            self.active_jobs.pop(job_id, None)
        self.jobs_processed += 1
