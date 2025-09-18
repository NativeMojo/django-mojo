"""
Scheduler daemon for moving due jobs from ZSET to List queues (Plan B).

Runs as a single active instance using Redis leadership lock.
Continuously monitors scheduled jobs and enqueues them when due.
"""
import os
import sys
import signal
import time
import json
import uuid
import random
import threading
from datetime import datetime, timedelta
from typing import List, Optional, Set, Dict

from django.utils import timezone
from django.db import close_old_connections
from mojo.helpers.settings import settings

from mojo.helpers import logit
from .daemon import DaemonRunner
from .keys import JobKeys
from .adapters import get_adapter
from .models import Job, JobEvent

# Module-level settings (readability)
JOBS_CHANNELS = settings.get('JOBS_CHANNELS', ['default'])
JOBS_SCHEDULER_LOCK_TTL_MS = settings.get('JOBS_SCHEDULER_LOCK_TTL_MS', 5000)
JOBS_STREAM_MAXLEN = settings.get('JOBS_STREAM_MAXLEN', 100000)
JOBS_DEBUG = settings.get('JOBS_DEBUG', False)



logger = logit.get_logger("scheduler", "scheduler.log")

class Scheduler:
    """
    Scheduler daemon that moves due jobs from ZSET to Streams.

    Uses Redis lock for single-leader pattern to ensure only one
    scheduler is active across the cluster at any time.
    """

    def __init__(self, channels: Optional[List[str]] = None,
                 scheduler_id: Optional[str] = None):
        """
        Initialize the scheduler.

        Args:
            channels: List of channels to schedule for (default: all configured)
            scheduler_id: Unique scheduler identifier (auto-generated if not provided)
        """
        self.channels = channels or self._get_all_channels()
        self.scheduler_id = scheduler_id or self._generate_scheduler_id()
        self.redis = get_adapter()
        self.keys = JobKeys()

        # Lock configuration
        self.lock_key = self.keys.scheduler_lock()
        self.lock_ttl_ms = JOBS_SCHEDULER_LOCK_TTL_MS
        self.lock_renew_interval = self.lock_ttl_ms / 1000 / 2  # Renew at half TTL
        self.lock_value = uuid.uuid4().hex  # Unique value for this scheduler

        # Control flags
        self.running = False
        self.stop_event = threading.Event()
        self.has_lock = False

        # Stats
        self.jobs_scheduled = 0
        self.jobs_expired = 0
        self.start_time = None

        # Sleep configuration (with jitter)
        self.base_sleep_ms = 250
        self.max_sleep_ms = 500

        logger.info(f"Scheduler initialized: id={self.scheduler_id}, "
                  f"channels={self.channels}")

    def _get_all_channels(self) -> List[str]:
        """Get all configured channels from settings or discover from Redis."""
        # Try settings first
        configured = JOBS_CHANNELS
        if configured:
            return configured

        # Default channels
        return ['default']

    def _generate_scheduler_id(self) -> str:
        """Generate a consistent scheduler ID based on hostname."""
        import socket
        hostname = socket.gethostname()
        # Clean hostname for use in ID (remove dots, make lowercase)
        clean_hostname = hostname.lower().replace('.', '-').replace('_', '-')

        return f"{clean_hostname}-scheduler"

    def start(self):
        """
        Start the scheduler daemon.

        Acquires leadership lock and begins processing scheduled jobs.
        """
        if self.running:
            logger.warn("Scheduler already running")
            return

        logger.info(f"Starting Scheduler {self.scheduler_id}")
        self.running = True
        self.start_time = timezone.now()
        self.stop_event.clear()

        # Register signal handlers
        self._setup_signal_handlers()

        # Main loop with lock management
        try:
            self._main_loop_with_lock()
        except KeyboardInterrupt:
            logger.info("Scheduler interrupted by user")
        except Exception as e:
            logger.error(f"Scheduler crashed: {e}")
            raise
        finally:
            self.stop()

    def stop(self):
        """Stop the scheduler gracefully."""
        if not self.running:
            return

        logger.info(f"Stopping Scheduler {self.scheduler_id}...")
        self.running = False
        self.stop_event.set()

        # Release lock if held
        if self.has_lock:
            self._release_lock()

        logger.info(f"Scheduler {self.scheduler_id} stopped. "
                  f"Scheduled: {self.jobs_scheduled}, Expired: {self.jobs_expired}")

    def _setup_signal_handlers(self):
        """Register signal handlers for graceful shutdown."""
        def handle_signal(signum, frame):
            logger.info(f"Scheduler received signal {signum}, shutting down")
            self.stop()
            sys.exit(0)

        signal.signal(signal.SIGTERM, handle_signal)
        signal.signal(signal.SIGINT, handle_signal)

    def _acquire_lock(self) -> bool:
        """
        Try to acquire the scheduler lock.

        Returns:
            True if lock acquired, False otherwise
        """
        try:
            # SET key value NX PX milliseconds
            result = self.redis.set(
                self.lock_key,
                self.lock_value,
                nx=True,  # Only set if doesn't exist
                px=self.lock_ttl_ms  # Expire after milliseconds
            )

            if result:
                self.has_lock = True
                logger.info(f"Scheduler {self.scheduler_id} acquired lock")

                # Emit metric
                try:
                    from mojo.metrics.redis_metrics import record_metrics
                    record_metrics('jobs.scheduler.leader', timezone.now(), 1,
                                 category='jobs')
                except Exception:
                    pass

                return True

            return False

        except Exception as e:
            logger.error(f"Failed to acquire scheduler lock: {e}")
            return False

    def _renew_lock(self) -> bool:
        """
        Renew the scheduler lock if we still hold it.

        Returns:
            True if renewed, False if lost
        """
        if not self.has_lock:
            return False

        try:
            # Check if we still own the lock
            current_value = self.redis.get(self.lock_key)

            if current_value and current_value == self.lock_value:
                # We still own it, renew TTL
                self.redis.pexpire(self.lock_key, self.lock_ttl_ms)
                return True
            else:
                # Lock stolen or expired
                logger.warn(f"Scheduler {self.scheduler_id} lost lock")
                self.has_lock = False
                return False

        except Exception as e:
            logger.error(f"Failed to renew scheduler lock: {e}")
            self.has_lock = False
            return False

    def _release_lock(self):
        """Release the scheduler lock if we hold it."""
        if not self.has_lock:
            return

        try:
            # Only delete if we own it
            current_value = self.redis.get(self.lock_key)

            if current_value and current_value == self.lock_value:
                self.redis.delete(self.lock_key)
                logger.info(f"Scheduler {self.scheduler_id} released lock")

            self.has_lock = False

        except Exception as e:
            logger.error(f"Failed to release scheduler lock: {e}")

    def _main_loop_with_lock(self):
        """Main loop with lock acquisition and renewal."""
        last_renew = time.time()

        while self.running and not self.stop_event.is_set():
            try:
                # Try to acquire lock if we don't have it
                if not self.has_lock:
                    if not self._acquire_lock():
                        # Failed to acquire, sleep and retry
                        time.sleep(2)
                        continue

                # Renew lock if needed
                now = time.time()
                if now - last_renew >= self.lock_renew_interval:
                    if not self._renew_lock():
                        # Lost lock, go back to acquisition
                        continue
                    last_renew = now

                # Process scheduled jobs
                self._process_scheduled_jobs()

                # Sleep with jitter
                sleep_ms = random.randint(self.base_sleep_ms, self.max_sleep_ms)
                time.sleep(sleep_ms / 1000.0)

            except Exception as e:
                logger.error(f"Error in scheduler main loop: {e}")
                time.sleep(1)

    def _process_scheduled_jobs(self):
        """Process scheduled jobs for all channels."""
        now = timezone.now()
        now_ms = now.timestamp() * 1000

        # Close old DB connections at start
        close_old_connections()

        for channel in self.channels:
            try:
                self._process_channel(channel, now, now_ms)
            except Exception as e:
                logger.error(f"Failed to process channel {channel}: {e}")

    def _process_channel(self, channel: str, now: datetime, now_ms: float):
        """
        Process scheduled jobs for a single channel.

        Args:
            channel: Channel name
            now: Current datetime
            now_ms: Current time in milliseconds
        """
        # Skip channel if paused
        try:
            if self.redis.get(self.keys.channel_pause(channel)):
                return
        except Exception:
            pass
        # Process non-broadcast delayed jobs (Plan B: enqueue to List queue)
        sched_key = self.keys.sched(channel)
        while True:
            results = self.redis.zpopmin(sched_key, count=10)
            if not results:
                break
            not_due: Dict[str, float] = {}
            for job_id, score in results:
                if score > now_ms:
                    # Collect not-due items to reinsert after the loop
                    not_due[job_id] = score
                else:
                    queue_key = self.keys.queue(channel)
                    # Enqueue to List queue
                    try:
                        self.redis.rpush(queue_key, job_id)
                    except Exception as e:
                        logger.error(f"Failed to enqueue job {job_id} to queue {queue_key}: {e}")
                        # If enqueue fails, reinsert back to sched to avoid loss
                        not_due[job_id] = score
                        continue
                    # Record DB event
                    try:
                        job = Job.objects.get(id=job_id)
                        scheduled_at_dt = datetime.fromtimestamp(score / 1000.0)
                        if timezone.is_naive(scheduled_at_dt):
                            scheduled_at_dt = timezone.make_aware(scheduled_at_dt)
                        if JOBS_DEBUG:
                            print(f"Job {job_id} scheduled at {scheduled_at_dt}")
                        JobEvent.objects.create(
                            job=job,
                            channel=channel,
                            event='queued',
                            details={
                                'scheduler_id': self.scheduler_id,
                                'queue': queue_key,
                                'scheduled_at': scheduled_at_dt.isoformat()
                            }
                        )
                    except Exception as e:
                        logger.warn(f"Failed to record queued event for {job_id}: {e}")
                    self.jobs_scheduled += 1
            # Re-add all not-due jobs and break (remaining entries are ordered)
            if not_due:
                self.redis.zadd(sched_key, not_due)
                break

        # Process broadcast delayed jobs (Plan B: if broadcast retained, enqueue to same queue or a special one)
        sched_b_key = self.keys.sched_broadcast(channel)
        while True:
            results = self.redis.zpopmin(sched_b_key, count=10)
            if not results:
                break
            not_due_b: Dict[str, float] = {}
            for job_id, score in results:
                if score > now_ms:
                    not_due_b[job_id] = score
                else:
                    # For simplicity, enqueue broadcast to the same queue; adjust if broadcast logic changes
                    queue_key = self.keys.queue(channel)
                    try:
                        self.redis.rpush(queue_key, job_id)
                    except Exception as e:
                        logger.error(f"Failed to enqueue broadcast job {job_id} to queue {queue_key}: {e}")
                        not_due_b[job_id] = score
                        continue
                    try:
                        job = Job.objects.get(id=job_id)
                        scheduled_at_dt = datetime.fromtimestamp(score / 1000.0)
                        if timezone.is_naive(scheduled_at_dt):
                            scheduled_at_dt = timezone.make_aware(scheduled_at_dt)
                        if JOBS_DEBUG:
                            print(f"Delayed Job {job_id} scheduled at {scheduled_at_dt}")
                        JobEvent.objects.create(
                            job=job,
                            channel=channel,
                            event='queued',
                            details={
                                'scheduler_id': self.scheduler_id,
                                'queue': queue_key,
                                'scheduled_at': scheduled_at_dt.isoformat(),
                                'broadcast': True
                            }
                        )
                    except Exception as e:
                        logger.warn(f"Failed to record queued event for broadcast {job_id}: {e}")
                    self.jobs_scheduled += 1
            if not_due_b:
                self.redis.zadd(sched_b_key, not_due_b)
                break

    def _enqueue_job(self, job_id: str, channel: str, now: datetime, stream_key: str, scheduled_at_ms: float):
        """
        Legacy helper retained for compatibility. Not used in Plan B path.
        """
        try:
            queue_key = self.keys.queue(channel)
            self.redis.rpush(queue_key, job_id)
            try:
                job = Job.objects.get(id=job_id)
                scheduled_at_dt = datetime.fromtimestamp(scheduled_at_ms / 1000.0)
                if timezone.is_naive(scheduled_at_dt):
                    scheduled_at_dt = timezone.make_aware(scheduled_at_dt)
                JobEvent.objects.create(
                    job=job,
                    channel=channel,
                    event='queued',
                    details={
                        'scheduler_id': self.scheduler_id,
                        'queue': queue_key,
                        'scheduled_at': scheduled_at_dt.isoformat()
                    }
                )
            except Exception as e:
                logger.warn(f"Failed to record queued event for {job_id}: {e}")
            self.jobs_scheduled += 1
            logger.debug(f"Enqueued job {job_id} to {queue_key}")
        except Exception as e:
            logger.error(f"Failed to enqueue job {job_id}: {e}")

    def _load_job(self, job_id: str) -> Optional[dict]:
        """Load job data from Redis or database."""
        # DB-only (KISS): skip Redis per-job hash
        # Fall back to database
        try:
            job = Job.objects.get(id=job_id)
            return {
                'status': job.status,
                'channel': job.channel,
                'func': job.func,
                'expires_at': job.expires_at.isoformat() if job.expires_at else '',
                'broadcast': '1' if job.broadcast else '0'
            }
        except Job.DoesNotExist:
            return None

    def _is_expired(self, job_data: dict, now: datetime) -> bool:
        """Check if a job has expired."""
        expires_at = job_data.get('expires_at', '')
        if not expires_at:
            return False

        try:
            expiry = datetime.fromisoformat(expires_at)
            if timezone.is_naive(expiry):
                expiry = timezone.make_aware(expiry)
            return now > expiry
        except Exception:
            return False

    def _mark_expired(self, job_id: str, channel: str):
        """Mark a job as expired."""
        try:
            # Redis per-job hash removed (KISS): DB is source of truth

            # Update database
            job = Job.objects.get(id=job_id)
            job.status = 'expired'
            job.finished_at = timezone.now()
            job.save(update_fields=['status', 'finished_at', 'modified'])

            # Record event
            JobEvent.objects.create(
                job=job,
                channel=channel,
                event='expired',
                details={'scheduler_id': self.scheduler_id}
            )

            logger.info(f"Job {job_id} expired at scheduler")

            # Emit metric
            try:
                from mojo.metrics.redis_metrics import record_metrics
                record_metrics('jobs.expired', timezone.now(), 1, category='jobs')
            except Exception:
                pass

        except Exception as e:
            logger.error(f"Failed to mark job {job_id} as expired: {e}")


def main():
    """
    Main entry point for running Scheduler as a daemon.

    This can be called directly or via Django management command.
    """
    import argparse

    parser = argparse.ArgumentParser(description='Django-MOJO Job Scheduler')
    parser.add_argument(
        '--channels',
        type=str,
        default=None,
        help='Comma-separated list of channels to schedule (default: all)'
    )
    parser.add_argument(
        '--scheduler-id',
        type=str,
        default=None,
        help='Explicit scheduler ID (auto-generated if not provided)'
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

    # Parse channels if provided
    channels = None
    if args.channels:
        channels = [c.strip() for c in args.channels.split(',')]

    # Create scheduler
    scheduler = Scheduler(channels=channels, scheduler_id=args.scheduler_id)

    # Auto-generate pidfile if daemon mode and not specified
    if args.daemon and not args.pidfile:
        scheduler_id = scheduler.scheduler_id
        args.pidfile = f"/tmp/job-scheduler-{scheduler_id}.pid"

    # Setup daemon runner
    runner = DaemonRunner(
        name="Scheduler",
        run_func=scheduler.start,
        stop_func=scheduler.stop,
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
                print(f"Scheduler is running (PID file: {args.pidfile})")
                sys.exit(0)
            else:
                print(f"Scheduler is not running")
                sys.exit(1)
    else:
        # Start the scheduler (foreground or background)
        try:
            runner.start()
        except Exception as e:
            logger.exception(f"Scheduler failed: {e}")
            sys.exit(1)


if __name__ == '__main__':
    main()
