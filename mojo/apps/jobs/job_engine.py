"""
JobEngine - The runner daemon for executing jobs.

Plan B engine: consumes jobs from Redis Lists (per-channel queues),
tracks in-flight jobs in a ZSET with visibility timeout, and executes
registered handlers.
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
JOBS_VISIBILITY_TIMEOUT_MS = settings.get('JOBS_VISIBILITY_TIMEOUT_MS', 30000)
JOBS_DEBUG = settings.get('JOBS_DEBUG', False)


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

    Plan B: Consumes jobs from Redis List queues and executes handlers dynamically
    with support for retries, cancellation, and parallel execution. Tracks in-flight
    jobs in a ZSET to enable crash recovery via a reaper.
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

        # Limit claimed jobs to actual execution capacity
        # Don't claim more than we can execute - let other engines help
        self.max_claimed = self.max_workers
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
        """No-op in Plan B (List + ZSET)."""
        logger.info("Plan B mode: no consumer groups to set up.")

    def _setup_signal_handlers(self):
        """Register signal handlers for graceful shutdown."""
        def handle_signal(signum, frame):
            logger.info(f"Received signal {signum}, initiating graceful shutdown")
            self.stop()
            sys.exit(0)

        signal.signal(signal.SIGTERM, handle_signal)
        signal.signal(signal.SIGINT, handle_signal)

    def _start_heartbeat(self):
        """Start the heartbeat and reaper threads."""
        self.heartbeat_thread = threading.Thread(
            target=self._heartbeat_loop,
            name=f"Heartbeat-{self.runner_id}",
            daemon=True
        )
        self.heartbeat_thread.start()
        # Reaper thread for visibility timeout
        self.reaper_thread = threading.Thread(
            target=self._reaper_loop,
            name=f"Reaper-{self.runner_id}",
            daemon=True
        )
        self.reaper_thread.start()

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

                # Touch visibility timeout for active jobs to prevent premature reaping
                try:
                    now_ms = int(time.time() * 1000)
                    # Snapshot active jobs to minimize lock hold time
                    with self.active_lock:
                        active_snapshot = [(jid, meta.get('channel')) for jid, meta in self.active_jobs.items()]
                    for jid, ch in active_snapshot:
                        if not ch:
                            continue
                        # Update in-flight ZSET score to extend visibility timeout
                        self.redis.zadd(self.keys.processing(ch), {jid: now_ms})
                except Exception as te:
                    logger.debug(f"Heartbeat touch failed: {te}")

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

    def _remove_from_processing(self, channel: str, job_id: str, max_retries: int = 3):
        """Remove job from processing ZSET with retries to ensure cleanup."""
        for attempt in range(max_retries):
            try:
                result = self.redis.zrem(self.keys.processing(channel), job_id)
                if result or attempt == max_retries - 1:
                    return result
                time.sleep(0.1 * (attempt + 1))  # Exponential backoff
            except Exception as e:
                if attempt == max_retries - 1:
                    logger.error(f"Failed to remove job {job_id} from processing after {max_retries} attempts: {e}")
                else:
                    logger.debug(f"Retry {attempt + 1} removing job {job_id} from processing: {e}")
                time.sleep(0.1 * (attempt + 1))
        return False

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

            if command == 'execute':
                # Execute function on this runner
                func_path = message.get('func')
                if func_path:
                    try:
                        logger.info(f"Executing broadcast function {func_path}")
                        func = load_job_function(func_path)
                        # Execute with the message data as context
                        result = func(message.get('data', {}))
                        logger.info(f"Executed broadcast function {func_path}: {result}")

                        # Send reply if reply_channel is provided
                        reply_channel = message.get('reply_channel')
                        if reply_channel:
                            reply = {
                                'runner_id': self.runner_id,
                                'func': func_path,
                                'result': str(result) if result else None,
                                'status': 'success',
                                'timestamp': dates.utcnow().isoformat(),
                            }
                            try:
                                self.redis.publish(reply_channel, json.dumps(reply))
                            except Exception as e:
                                logger.warning(f"Failed to publish execute reply: {e}")
                    except Exception as e:
                        logger.error(f"Broadcast execution failed for {func_path}: {e}")
                        logger.error(traceback.format_exc())

                        # Send error reply if reply_channel is provided
                        reply_channel = message.get('reply_channel')
                        if reply_channel:
                            reply = {
                                'runner_id': self.runner_id,
                                'func': func_path,
                                'error': str(e),
                                'status': 'error',
                                'timestamp': dates.utcnow().isoformat(),
                            }
                            try:
                                self.redis.publish(reply_channel, json.dumps(reply))
                            except Exception:
                                pass
                else:
                    logger.warning("Execute command received without func path")

            elif command == 'ping':
                # Respond with pong
                # Support both old response_key (Redis key) and new reply_channel (pub/sub)
                response_key = message.get('response_key')
                reply_channel = message.get('reply_channel')

                if reply_channel:
                    # New pub/sub method
                    try:
                        self.redis.publish(reply_channel, 'pong')
                    except Exception as e:
                        logger.warning(f"Failed to publish ping reply: {e}")
                elif response_key:
                    # Legacy Redis key method
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
        """Main processing loop - claims jobs from List queues based on capacity."""
        logger.info(f"JobEngine {self.runner_id} entering main loop (Plan B)")

        while self.running and not self.stop_event.is_set():
            try:
                # Check available capacity
                with self.active_lock:
                    active_count = len(self.active_jobs)

                if active_count >= self.max_claimed:
                    time.sleep(0.1)
                    continue

                # Compose BRPOP order (priority first)
                channels_ordered = list(self.channels)
                if 'priority' in channels_ordered:
                    channels_ordered = ['priority'] + [c for c in channels_ordered if c != 'priority']
                queue_keys = [self.keys.queue(ch) for ch in channels_ordered]

                # Claim one job at a time to avoid over-claiming
                popped = self.redis.brpop(queue_keys, timeout=1)
                if not popped:
                    continue

                queue_key, job_id = popped
                # Determine channel from key
                channel = queue_key.split(':')[-1]

                # Track in-flight (visibility)
                try:
                    if JOBS_DEBUG:
                        logger.info(f"Claiming job {job_id} from channel {channel}")
                    self.redis.zadd(self.keys.processing(channel), {job_id: int(time.time() * 1000)})
                except Exception as e:
                    logger.warning(f"Failed to add job {job_id} to processing ZSET: {e}")

                # Submit to thread pool
                future = self.executor.submit(
                    self.execute_job,
                    channel, job_id
                )

                with self.active_lock:
                    self.active_jobs[job_id] = {
                        'future': future,
                        'started': dates.utcnow(),
                        'channel': channel
                    }

                future.add_done_callback(lambda f, jid=job_id: self._job_completed(jid))

            except Exception as e:
                logger.error(f"Error in main loop: {e}")
                time.sleep(0.5)

    def claim_jobs_by_channel(self, channel: str, count: int) -> List[Tuple[str, str, str]]:
        """Plan B: not used. Kept for compatibility."""
        return []

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
        """Plan B: not used. Kept for compatibility."""
        return

    def execute_job(self, channel: str, job_id: str):
        """Execute job and handle all state updates (Plan B)."""
        job = None
        try:
            # Load job from database
            if JOBS_DEBUG:
                logger.info(f"Loading job {job_id} from database...")
            close_old_connections()
            if JOBS_DEBUG:
                logger.info(f"Loading job {job_id} (no lock needed - Redis already claimed it)...")
            # No select_for_update needed: Redis BRPOP already ensures only one process gets each job
            job = Job.objects.get(id=job_id)
            if JOBS_DEBUG:
                logger.info(f"Successfully loaded job {job_id} from database")
        except Exception as e:
            logger.exception(f"Failed to load job {job_id}: {e}")
            # Remove from processing to avoid leak
            try:
                self.redis.zrem(self.keys.processing(channel), job_id)
            except Exception:
                pass
            return

        try:
            if JOBS_DEBUG:
                logger.info(f"Executing job {job_id} from channel {channel}")
            # Check if already processed or canceled
            if job.status in ('completed', 'canceled'):
                # Already finished; remove from processing if present
                self._remove_from_processing(channel, job_id)
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

                # Remove from processing after DB update
                self._remove_from_processing(channel, job_id)
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
                    details={'queue': self.keys.queue(channel)}
                )
            except Exception:
                pass

            # Load and execute function
            func = load_job_function(job.func)
            func(job)
            if JOBS_DEBUG:
                logger.info(f"Completed job {job_id} from channel {channel}")
            # Mark complete
            job.status = 'completed'
            job.finished_at = dates.utcnow()
            job.save(update_fields=['status', 'finished_at', 'metadata'])
            if JOBS_DEBUG:
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

            # Remove from processing after DB update with retries to prevent reaper issues
            self._remove_from_processing(channel, job_id)

            # Metrics
            metrics.record("jobs.completed", count=1)
            metrics.record(f"jobs.channel.{job.channel}.completed", category="jobs_channels", count=1)
            # metrics.record("jobs.duration_ms", count=job.duration_ms)

        except Exception as e:
            try:
                if job:
                    job.add_log(f"Failed to complete job: {e}", kind="error")
            except Exception:
                pass
            self._handle_job_failure(job_id, channel, e)

    def _handle_job_failure(self, job_id: str, channel: str, error: Exception):
        """Handle job failure with retries (Plan B)."""
        try:
            job = Job.objects.get(id=job_id)

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

            # Always remove from processing to prevent leaks - critical for reaper
            self._remove_from_processing(channel, job_id)

        except Exception as e:
            logger.exception(f"Failed to handle job failure: {e}")

    def _job_completed(self, job_id: str):
        """Callback when job future completes."""
        with self.active_lock:
            self.active_jobs.pop(job_id, None)
        self.jobs_processed += 1

    def _reaper_loop(self):
        """Requeue stale in-flight jobs based on visibility timeout (Plan B)."""
        while self.running and not self.stop_event.is_set():
            try:
                now_ms = int(time.time() * 1000)
                cutoff = now_ms - JOBS_VISIBILITY_TIMEOUT_MS
                for ch in self.channels:
                    # Acquire short-lived lock to avoid duplicate requeues across engines
                    acquired = False
                    try:
                        acquired = self.redis.set(self.keys.reaper_lock(ch), self.runner_id, nx=True, px=2000)
                    except Exception as le:
                        logger.debug(f"Reaper lock error for {ch}: {le}")
                        acquired = False
                    if not acquired:
                        # Another engine is handling this channel right now
                        continue
                    # Fetch stale entries: claimed earlier than cutoff
                    try:
                        stale_ids = self.redis.zrangebyscore(self.keys.processing(ch), float("-inf"), cutoff, limit=100)
                    except Exception as e:
                        logger.debug(f"Reaper fetch failed for {ch}: {e}")
                        stale_ids = []
                    for jid in stale_ids:
                        try:
                            # Check if job should be retried before requeuing
                            try:
                                job = Job.objects.get(id=jid)

                                # Check if job already completed or canceled - just clean up Redis
                                if job.status in ('completed', 'canceled'):
                                    logger.debug(f"Reaper: Job {jid} already {job.status}, removing from processing")
                                    self._remove_from_processing(ch, jid)
                                    continue

                                # Check if job is expired
                                if job.is_expired:
                                    logger.info(f"Reaper: Job {jid} expired, marking as expired instead of requeuing")
                                    self._remove_from_processing(ch, jid)
                                    job.status = 'expired'
                                    job.finished_at = dates.utcnow()
                                    job.save(update_fields=['status', 'finished_at'])
                                    JobEvent.objects.create(
                                        job=job, channel=ch, event='expired',
                                        runner_id=self.runner_id, attempt=job.attempt,
                                        details={'reason': 'reaper_expired'}
                                    )
                                    continue

                                # Check retry limits (prevent infinite requeuing)
                                if job.attempt >= job.max_retries:
                                    logger.info(f"Reaper: Job {jid} exceeded max retries ({job.attempt}/{job.max_retries}), marking as failed")
                                    self._remove_from_processing(ch, jid)
                                    job.status = 'failed'
                                    job.finished_at = dates.utcnow()
                                    job.last_error = f"Exceeded max retries after reaper timeout (attempt {job.attempt})"
                                    job.save(update_fields=['status', 'finished_at', 'last_error'])
                                    JobEvent.objects.create(
                                        job=job, channel=ch, event='failed',
                                        runner_id=self.runner_id, attempt=job.attempt,
                                        details={'reason': 'reaper_max_retries_exceeded'}
                                    )
                                    continue

                                # Job can be retried - requeue it
                                self._remove_from_processing(ch, jid)
                                self.redis.rpush(self.keys.queue(ch), jid)

                                # Increment attempt count to track reaper retries
                                job.attempt += 1
                                job.save(update_fields=['attempt'])

                                JobEvent.objects.create(
                                    job=job, channel=ch, event='retry',
                                    runner_id=self.runner_id, attempt=job.attempt,
                                    details={'reason': 'reaper_timeout'}
                                )
                                logger.info(f"Reaper requeued stale job {jid} on {ch} (attempt {job.attempt}/{job.max_retries})")

                            except Job.DoesNotExist:
                                # Job deleted from DB but still in Redis - clean it up
                                logger.info(f"Reaper: Job {jid} not found in DB, removing from processing")
                                self._remove_from_processing(ch, jid)

                        except Exception as e:
                            logger.warning(f"Reaper failed to handle job {jid} on {ch}: {e}")
                            # Remove from processing to prevent infinite retries on broken jobs
                            self._remove_from_processing(ch, jid)
            except Exception as e:
                logger.warning(f"Reaper loop error: {e}")
            # Sleep a bit before next pass
            for _ in range(5):
                if self.stop_event.is_set():
                    break
                time.sleep(1)
