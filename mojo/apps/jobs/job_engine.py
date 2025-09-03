"""
JobEngine - The runner daemon for executing jobs.

This module implements the core job execution engine that consumes
jobs from Redis Streams and executes registered handlers.
"""
import os
import sys
import signal
import socket
import random
import time
import json
import subprocess
import threading
import traceback
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Set, Tuple

from django.conf import settings
from django.utils import timezone
from django.db import close_old_connections

from mojo.helpers import logit
from .daemon import DaemonRunner
from .keys import JobKeys
from .adapters import get_adapter
from .context import JobContext
from .registry import get_job_function
from .models import Job, JobEvent


class JobEngine:
    """
    Job execution engine that runs as a daemon process.

    Consumes jobs from Redis Streams and executes registered handlers
    with support for retries, cancellation, and subprocess execution.
    """

    def __init__(self, channels: Optional[List[str]] = None,
                 runner_id: Optional[str] = None):
        """
        Initialize the job engine.

        Args:
            channels: List of channels to consume from (default: ['default'])
            runner_id: Unique runner identifier (auto-generated if not provided)
        """
        self.channels = channels or ['default']
        self.runner_id = runner_id or self._generate_runner_id()
        self.redis = get_adapter()
        self.keys = JobKeys()

        # Control flags
        self.running = False
        self.stop_event = threading.Event()

        # Heartbeat thread
        self.heartbeat_thread = None
        self.heartbeat_interval = getattr(settings, 'JOBS_RUNNER_HEARTBEAT_SEC', 5)

        # Control channel listener
        self.control_thread = None

        # Stats
        self.jobs_processed = 0
        self.jobs_failed = 0
        self.start_time = None

        logit.info(f"JobEngine initialized: runner_id={self.runner_id}, "
                  f"channels={self.channels}")

    def _generate_runner_id(self) -> str:
        """Generate a unique runner ID."""
        hostname = socket.gethostname()
        pid = os.getpid()
        rand = random.randint(1000, 9999)
        return f"{hostname}-{pid}-{rand}"

    def start(self):
        """
        Start the job engine.

        Sets up consumer groups, starts heartbeat, and begins processing.
        """
        if self.running:
            logit.warn("JobEngine already running")
            return

        logit.info(f"Starting JobEngine {self.runner_id}")
        self.running = True
        self.start_time = timezone.now()
        self.stop_event.clear()

        # Ensure consumer groups exist
        self._setup_consumer_groups()

        # Start heartbeat thread
        self._start_heartbeat()

        # Start control listener thread
        self._start_control_listener()

        # Register signal handlers
        self._setup_signal_handlers()

        # Main processing loop
        try:
            self._main_loop()
        except KeyboardInterrupt:
            logit.info("JobEngine interrupted by user")
        except Exception as e:
            logit.error(f"JobEngine crashed: {e}")
            raise
        finally:
            self.stop()

    def stop(self, timeout: float = 10.0):
        """
        Stop the job engine gracefully.

        Args:
            timeout: Maximum time to wait for clean shutdown
        """
        if not self.running:
            return

        logit.info(f"Stopping JobEngine {self.runner_id}...")
        self.running = False
        self.stop_event.set()

        # Stop heartbeat
        if self.heartbeat_thread and self.heartbeat_thread.is_alive():
            self.heartbeat_thread.join(timeout=timeout/2)

        # Stop control listener
        if self.control_thread and self.control_thread.is_alive():
            self.control_thread.join(timeout=timeout/2)

        # Clean up Redis keys
        try:
            self.redis.delete(self.keys.runner_hb(self.runner_id))
        except Exception as e:
            logit.warn(f"Failed to clean up runner keys: {e}")

        logit.info(f"JobEngine {self.runner_id} stopped. "
                  f"Processed: {self.jobs_processed}, Failed: {self.jobs_failed}")

    def _setup_consumer_groups(self):
        """Ensure all required consumer groups exist."""
        for channel in self.channels:
            # Workers group for normal stream
            stream_key = self.keys.stream(channel)
            group_key = self.keys.group_workers(channel)
            self.redis.xgroup_create(stream_key, group_key, id='0', mkstream=True)

            # Per-runner group for broadcast stream
            broadcast_stream = self.keys.stream_broadcast(channel)
            runner_group = self.keys.group_runner(channel, self.runner_id)
            self.redis.xgroup_create(broadcast_stream, runner_group, id='0', mkstream=True)

            logit.info(f"Consumer groups ready for channel: {channel}")

    def _setup_signal_handlers(self):
        """Register signal handlers for graceful shutdown."""
        def handle_signal(signum, frame):
            logit.info(f"Received signal {signum}, initiating graceful shutdown")
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
                    'channels': self.channels,
                    'jobs_processed': self.jobs_processed,
                    'jobs_failed': self.jobs_failed,
                    'started': self.start_time.isoformat(),
                    'last_heartbeat': timezone.now().isoformat()
                }), ex=self.heartbeat_interval * 3)  # TTL = 3x interval

            except Exception as e:
                logit.warn(f"Heartbeat update failed: {e}")

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
        pubsub = self.redis.pubsub()
        pubsub.subscribe(control_key)

        try:
            while self.running and not self.stop_event.is_set():
                message = pubsub.get_message(timeout=1.0)
                if message and message['type'] == 'message':
                    self._handle_control_message(message['data'])
        finally:
            pubsub.close()

    def _handle_control_message(self, data: bytes):
        """Handle a control channel message."""
        try:
            message = json.loads(data.decode('utf-8'))
            command = message.get('command')

            if command == 'ping':
                # Respond with pong
                response_key = message.get('response_key')
                if response_key:
                    self.redis.set(response_key, 'pong', ex=5)
                logit.debug(f"Responded to ping from control channel")

            elif command == 'shutdown':
                logit.info("Received shutdown command from control channel")
                self.stop()

            else:
                logit.warn(f"Unknown control command: {command}")

        except Exception as e:
            logit.error(f"Failed to handle control message: {e}")

    def _main_loop(self):
        """Main processing loop."""
        logit.info(f"JobEngine {self.runner_id} entering main loop")

        # Build streams dict for XREADGROUP
        streams = {}
        for channel in self.channels:
            # Normal stream
            stream_key = self.keys.stream(channel)
            streams[stream_key] = '>'

            # Broadcast stream
            broadcast_key = self.keys.stream_broadcast(channel)
            streams[broadcast_key] = '>'

        while self.running and not self.stop_event.is_set():
            try:
                # Close old DB connections at loop start
                close_old_connections()

                # Read from streams
                messages = self._read_streams(streams)

                if not messages:
                    continue

                # Process each message
                for stream_key, stream_messages in messages:
                    for msg_id, msg_data in stream_messages:
                        self._process_message(stream_key, msg_id, msg_data)

            except Exception as e:
                logit.error(f"Error in main loop: {e}")
                time.sleep(1)  # Brief pause before retry

    def _read_streams(self, streams: Dict[str, str]) -> List[Tuple]:
        """
        Read from multiple streams with appropriate consumer groups.

        Args:
            streams: Dict of stream keys to read positions

        Returns:
            List of (stream_key, messages) tuples
        """
        results = []

        for stream_key, position in streams.items():
            # Determine which group to use
            if ':broadcast' in stream_key:
                # Broadcast stream - use runner-specific group
                channel = stream_key.split(':')[-2]  # Extract channel from key
                group = self.keys.group_runner(channel, self.runner_id)
            else:
                # Normal stream - use workers group
                channel = stream_key.split(':')[-1]  # Extract channel from key
                group = self.keys.group_workers(channel)

            try:
                messages = self.redis.xreadgroup(
                    group=group,
                    consumer=self.runner_id,
                    streams={stream_key: position},
                    count=1,  # Process one at a time for fairness
                    block=1000  # Block for 1 second
                )

                if messages:
                    results.extend(messages)

            except Exception as e:
                logit.error(f"Failed to read from {stream_key}: {e}")

        return results

    def _process_message(self, stream_key: str, msg_id: str,
                        msg_data: Dict[bytes, bytes]):
        """
        Process a single message from a stream.

        Args:
            stream_key: The stream the message came from
            msg_id: Message ID in the stream
            msg_data: Message data
        """
        job_id = None

        try:
            # Extract job ID
            job_id = msg_data.get(b'job_id', b'').decode('utf-8')
            if not job_id:
                logit.error(f"Message {msg_id} has no job_id")
                self._ack_message(stream_key, msg_id)
                return

            # Load job from Redis and/or DB
            job_data = self._load_job(job_id)
            if not job_data:
                logit.error(f"Job {job_id} not found")
                self._ack_message(stream_key, msg_id)
                return

            # Check expiration
            if self._is_expired(job_data):
                self._mark_expired(job_id)
                self._ack_message(stream_key, msg_id)
                return

            # Mark as running
            self._mark_running(job_id)

            # Execute the job
            success = self._execute_job(job_id, job_data)

            # Acknowledge message
            self._ack_message(stream_key, msg_id)

            # Handle result
            if success:
                self._mark_completed(job_id)
                self.jobs_processed += 1
            else:
                self._handle_failure(job_id, job_data)
                self.jobs_failed += 1

        except Exception as e:
            logit.error(f"Failed to process message {msg_id}: {e}")
            if job_id:
                self._handle_failure(job_id, {})

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

    def _load_job(self, job_id: str) -> Optional[Dict]:
        """Load job data from Redis and/or database."""
        # Try Redis first
        job_hash = self.redis.hgetall(self.keys.job(job_id))

        if job_hash:
            return job_hash

        # Fall back to database
        try:
            job = Job.objects.get(id=job_id)
            return {
                'status': job.status,
                'channel': job.channel,
                'func': job.func,
                'payload': json.dumps(job.payload),
                'expires_at': job.expires_at.isoformat() if job.expires_at else '',
                'attempt': str(job.attempt),
                'max_retries': str(job.max_retries),
                'max_exec_seconds': str(job.max_exec_seconds or ''),
                'cancel_requested': '1' if job.cancel_requested else '0'
            }
        except Job.DoesNotExist:
            return None

    def _is_expired(self, job_data: Dict) -> bool:
        """Check if a job has expired."""
        expires_at = job_data.get('expires_at', '')
        if not expires_at:
            return False

        try:
            expiry = datetime.fromisoformat(expires_at)
            if timezone.is_naive(expiry):
                expiry = timezone.make_aware(expiry)
            return timezone.now() > expiry
        except Exception:
            return False

    def _mark_expired(self, job_id: str):
        """Mark a job as expired."""
        try:
            # Update Redis
            self.redis.hset(self.keys.job(job_id), {'status': 'expired'})

            # Update database
            job = Job.objects.get(id=job_id)
            job.status = 'expired'
            job.finished_at = timezone.now()
            job.save(update_fields=['status', 'finished_at', 'modified'])

            # Record event
            JobEvent.objects.create(
                job=job,
                channel=job.channel,
                event='expired',
                runner_id=self.runner_id
            )

            logit.info(f"Job {job_id} expired")

            # Emit metric
            from mojo.metrics.redis_metrics import record_metrics
            record_metrics('jobs.expired', timezone.now(), 1, category='jobs')

        except Exception as e:
            logit.error(f"Failed to mark job {job_id} as expired: {e}")

    def _mark_running(self, job_id: str):
        """Mark a job as running."""
        now = timezone.now()

        try:
            # Update Redis
            self.redis.hset(self.keys.job(job_id), {
                'status': 'running',
                'runner_id': self.runner_id,
                'started_at': now.isoformat()
            })

            # Update database
            job = Job.objects.get(id=job_id)
            job.status = 'running'
            job.runner_id = self.runner_id
            job.started_at = now
            job.save(update_fields=['status', 'runner_id', 'started_at', 'modified'])

            # Record event
            JobEvent.objects.create(
                job=job,
                channel=job.channel,
                event='running',
                runner_id=self.runner_id,
                attempt=job.attempt
            )

        except Exception as e:
            logit.error(f"Failed to mark job {job_id} as running: {e}")

    def _execute_job(self, job_id: str, job_data: Dict) -> bool:
        """
        Execute a job handler.

        Returns:
            True if successful, False if failed
        """
        func_name = job_data.get('func', '')

        # Get the registered function
        func = get_job_function(func_name)
        if not func:
            logit.error(f"No handler registered for {func_name}")
            return False

        # Parse payload
        try:
            payload = json.loads(job_data.get('payload', '{}'))
        except Exception:
            payload = {}

        # Create context
        ctx = JobContext(
            job_id=job_id,
            channel=job_data.get('channel', 'unknown'),
            payload=payload,
            redis_adapter=self.redis,
            redis_keys=self.keys
        )

        # Check for hard execution limit
        max_exec = job_data.get('max_exec_seconds', '')
        if max_exec and max_exec.isdigit():
            return self._execute_with_timeout(func, ctx, int(max_exec))
        else:
            return self._execute_direct(func, ctx)

    def _execute_direct(self, func: Callable, ctx: JobContext) -> bool:
        """Execute a job directly in this process."""
        try:
            close_old_connections()

            # Execute the function
            result = func(ctx)

            close_old_connections()

            ctx.log(f"Job completed successfully", level='info')
            return True

        except Exception as e:
            ctx.log(f"Job failed: {e}", level='error')

            # Store error details
            try:
                job = Job.objects.get(id=ctx.job_id)
                job.last_error = str(e)
                job.stack_trace = traceback.format_exc()
                job.save(update_fields=['last_error', 'stack_trace', 'modified'])
            except Exception:
                pass

            return False

    def _execute_with_timeout(self, func: Callable, ctx: JobContext,
                            timeout_seconds: int) -> bool:
        """Execute a job in a subprocess with timeout."""
        # TODO: Implement subprocess execution with hard timeout
        # For now, fall back to direct execution
        logit.warn(f"Subprocess execution not yet implemented for job {ctx.job_id}")
        return self._execute_direct(func, ctx)

    def _mark_completed(self, job_id: str):
        """Mark a job as completed."""
        now = timezone.now()

        try:
            # Update Redis
            self.redis.hset(self.keys.job(job_id), {
                'status': 'completed',
                'finished_at': now.isoformat()
            })

            # Update database
            job = Job.objects.get(id=job_id)
            job.status = 'completed'
            job.finished_at = now
            job.save(update_fields=['status', 'finished_at', 'modified'])

            # Record event
            JobEvent.objects.create(
                job=job,
                channel=job.channel,
                event='completed',
                runner_id=self.runner_id
            )

            # Emit metrics
            from mojo.metrics.redis_metrics import record_metrics
            record_metrics('jobs.completed', now, 1, category='jobs')

            if job.started_at:
                duration_ms = int((now - job.started_at).total_seconds() * 1000)
                record_metrics('jobs.duration_ms', now, duration_ms,
                             category='jobs', args=[job.channel, job.func])

        except Exception as e:
            logit.error(f"Failed to mark job {job_id} as completed: {e}")

    def _handle_failure(self, job_id: str, job_data: Dict):
        """Handle a failed job - retry or mark as failed."""
        try:
            job = Job.objects.get(id=job_id)
            job.attempt += 1

            if job.attempt <= job.max_retries:
                # Calculate backoff
                backoff = min(
                    job.backoff_base ** job.attempt,
                    job.backoff_max_sec
                )
                # Add jitter
                backoff = backoff * (0.8 + random.random() * 0.4)

                # Schedule retry
                retry_at = timezone.now() + timedelta(seconds=backoff)

                # Update job
                job.run_at = retry_at
                job.status = 'pending'
                job.save(update_fields=['attempt', 'run_at', 'status', 'modified'])

                # Add to scheduled ZSET
                score = retry_at.timestamp() * 1000
                self.redis.zadd(self.keys.sched(job.channel), {job_id: score})

                # Record event
                JobEvent.objects.create(
                    job=job,
                    channel=job.channel,
                    event='retry',
                    runner_id=self.runner_id,
                    attempt=job.attempt,
                    details={'retry_at': retry_at.isoformat(), 'backoff': backoff}
                )

                logit.info(f"Job {job_id} scheduled for retry #{job.attempt} at {retry_at}")

                # Emit metric
                from mojo.metrics.redis_metrics import record_metrics
                record_metrics('jobs.retried', timezone.now(), 1, category='jobs')

            else:
                # Max retries exceeded - mark as failed
                job.status = 'failed'
                job.finished_at = timezone.now()
                job.save(update_fields=['attempt', 'status', 'finished_at', 'modified'])

                # Update Redis
                self.redis.hset(self.keys.job(job_id), {
                    'status': 'failed',
                    'finished_at': job.finished_at.isoformat()
                })

                # Record event
                JobEvent.objects.create(
                    job=job,
                    channel=job.channel,
                    event='failed',
                    runner_id=self.runner_id,
                    attempt=job.attempt,
                    details={'max_retries_exceeded': True}
                )

                logit.error(f"Job {job_id} failed after {job.attempt} attempts")

                # Emit metric
                from mojo.metrics.redis_metrics import record_metrics
                record_metrics('jobs.failed', timezone.now(), 1, category='jobs')

        except Exception as e:
            logit.error(f"Failed to handle failure for job {job_id}: {e}")


def main():
    """
    Main entry point for running JobEngine as a daemon.

    This can be called directly or via Django management command.
    """
    import argparse

    parser = argparse.ArgumentParser(description='Django-MOJO Job Engine')
    parser.add_argument(
        '--channels',
        type=str,
        default='default',
        help='Comma-separated list of channels to serve'
    )
    parser.add_argument(
        '--runner-id',
        type=str,
        default=None,
        help='Explicit runner ID (auto-generated if not provided)'
    )
    parser.add_argument(
        '--daemon',
        action='store_true',
        help='Run as background daemon'
    )
    parser.add_argument(
        '--pidfile',
        type=str,
        default=None,
        help='PID file path (auto-generated if daemon mode and not specified)'
    )
    parser.add_argument(
        '--logfile',
        type=str,
        default=None,
        help='Log file path for daemon mode'
    )
    parser.add_argument(
        '--action',
        type=str,
        choices=['start', 'stop', 'restart', 'status'],
        default='start',
        help='Daemon control action (only with --daemon)'
    )

    args = parser.parse_args()

    # Parse channels
    channels = [c.strip() for c in args.channels.split(',')]

    # Create engine
    engine = JobEngine(channels=channels, runner_id=args.runner_id)

    # Auto-generate pidfile if daemon mode and not specified
    if args.daemon and not args.pidfile:
        runner_id = engine.runner_id
        args.pidfile = f"/tmp/job-engine-{runner_id}.pid"

    # Setup daemon runner
    runner = DaemonRunner(
        name="JobEngine",
        run_func=engine.start,
        stop_func=engine.stop,
        pidfile=args.pidfile,
        logfile=args.logfile,
        daemon=args.daemon
    )

    # Handle daemon actions
    if args.daemon and args.action != 'start':
        if args.action == 'stop':
            sys.exit(0 if runner.stop() else 1)
        elif args.action == 'restart':
            runner.restart()
            sys.exit(0)
        elif args.action == 'status':
            if runner.status():
                print(f"JobEngine is running (PID file: {args.pidfile})")
                sys.exit(0)
            else:
                print(f"JobEngine is not running")
                sys.exit(1)
    else:
        # Start the engine (foreground or background)
        try:
            runner.start()
        except Exception as e:
            logit.error(f"JobEngine failed: {e}")
            sys.exit(1)


if __name__ == '__main__':
    main()
