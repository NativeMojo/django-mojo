"""
Local in-process job queue for lightweight tasks.

No persistence, no retries, no distribution - just a simple
single worker thread with a queue for ultra-short work.
"""
import queue
import threading
import traceback
import time
from typing import Any, Callable, Optional
from dataclasses import dataclass
from datetime import datetime

from django.conf import settings
from django.utils import timezone

from mojo.helpers import logit


@dataclass
class LocalJob:
    """Container for a local job."""
    job_id: str
    func: Callable
    args: tuple
    kwargs: dict
    delay_seconds: float = 0.0


class LocalQueue:
    """
    Simple in-process job queue with single worker thread.

    For ultra-lightweight tasks that don't need persistence,
    retries, or distributed execution. Uses time.sleep() for delays.
    """

    def __init__(self, maxsize: Optional[int] = None):
        """
        Initialize the local queue.

        Args:
            maxsize: Maximum queue size (default from settings or 1000)
        """
        if maxsize is None:
            maxsize = getattr(settings, 'JOBS_LOCAL_QUEUE_MAXSIZE', 1000)

        self.queue = queue.Queue(maxsize=maxsize)
        self.worker_thread = None
        self.stop_event = threading.Event()
        self.started = False
        self._lock = threading.RLock()
        self._processed_count = 0
        self._error_count = 0

    def start(self):
        """Start the worker thread."""
        with self._lock:
            if self.started:
                return

            self.stop_event.clear()
            self.worker_thread = threading.Thread(
                target=self._worker,
                name="LocalJobWorker",
                daemon=True  # Dies with main process
            )
            self.worker_thread.start()
            self.started = True
            logit.info("Local job queue worker started")

    def stop(self, timeout: float = 5.0):
        """
        Stop the worker thread gracefully.

        Args:
            timeout: Maximum time to wait for thread to stop
        """
        with self._lock:
            if not self.started:
                return

            logit.info("Stopping local job queue worker...")
            self.stop_event.set()

            # Put a sentinel to unblock the worker if waiting
            try:
                self.queue.put_nowait(None)  # Sentinel value
            except queue.Full:
                pass

            if self.worker_thread and self.worker_thread.is_alive():
                self.worker_thread.join(timeout)
                if self.worker_thread.is_alive():
                    logit.warn("Local job worker thread did not stop cleanly")

            self.started = False
            logit.info(f"Local job queue stopped (processed={self._processed_count}, "
                      f"errors={self._error_count})")

    def put(self, func: Callable, args: tuple, kwargs: dict,
            job_id: str, run_at: Optional[datetime] = None) -> bool:
        """
        Add a job to the queue.

        Args:
            func: Function to execute
            args: Positional arguments
            kwargs: Keyword arguments
            job_id: Job identifier
            run_at: When to execute the job (None for immediate)

        Returns:
            True if queued, False if queue is full
        """
        if not self.started:
            self.start()

        # Calculate delay in seconds
        delay_seconds = 0.0
        if run_at:
            now = timezone.now()
            if run_at > now:
                delay_seconds = (run_at - now).total_seconds()

        job = LocalJob(
            job_id=job_id,
            func=func,
            args=args,
            kwargs=kwargs,
            delay_seconds=delay_seconds
        )

        try:
            self.queue.put_nowait(job)
            return True
        except queue.Full:
            logit.warn(f"Local job queue is full, rejecting job {job_id}")
            return False

    def size(self) -> int:
        """Get current queue size."""
        return self.queue.qsize()

    def is_empty(self) -> bool:
        """Check if queue is empty."""
        return self.queue.empty()

    def stats(self) -> dict:
        """
        Get queue statistics.

        Returns:
            Dict with queue stats
        """
        with self._lock:
            return {
                'size': self.size(),
                'maxsize': self.queue.maxsize,
                'processed': self._processed_count,
                'errors': self._error_count,
                'running': self.started,
                'worker_alive': self.worker_thread.is_alive() if self.worker_thread else False
            }

    def _worker(self):
        """
        Worker thread main loop.

        Continuously processes jobs from the queue until stopped.
        Simple approach: get job, sleep if needed, execute, repeat.
        """
        logit.info("Local job worker thread started")

        while not self.stop_event.is_set():
            try:
                # Get job from queue with timeout
                try:
                    job = self.queue.get(timeout=1.0)
                except queue.Empty:
                    continue

                # Check for shutdown sentinel
                if job is None:
                    logit.debug("Worker received shutdown sentinel")
                    break

                # Sleep if job has a delay
                if job.delay_seconds > 0:
                    logit.debug(f"Job {job.job_id} sleeping for {job.delay_seconds:.2f}s")

                    # Sleep in small chunks to allow for quick shutdown
                    sleep_remaining = job.delay_seconds
                    while sleep_remaining > 0 and not self.stop_event.is_set():
                        sleep_time = min(0.1, sleep_remaining)  # Sleep max 100ms at a time
                        time.sleep(sleep_time)
                        sleep_remaining -= sleep_time

                    # If we were asked to stop during sleep, break
                    if self.stop_event.is_set():
                        break

                # Execute the job
                self._execute_job(job)

                with self._lock:
                    self._processed_count += 1

                # Mark task as done for queue.join() if anyone uses it
                self.queue.task_done()

            except Exception as e:
                # This should never happen (caught in _execute_job)
                # but just in case...
                logit.error(f"Unexpected error in local job worker: {e}")
                with self._lock:
                    self._error_count += 1

        logit.info("Local job worker thread exiting")

    def _execute_job(self, job: LocalJob):
        """
        Execute a single job.

        Args:
            job: LocalJob to execute
        """
        start_time = timezone.now()

        try:
            # Log execution start
            logit.debug(f"Executing local job {job.job_id}")

            # Close old database connections before execution
            from django.db import close_old_connections
            close_old_connections()

            # Execute the function
            result = job.func(*job.args, **job.kwargs)

            # Close connections after execution
            close_old_connections()

            # Log success
            duration = (timezone.now() - start_time).total_seconds()
            logit.info(f"Local job {job.job_id} completed in {duration:.2f}s")

            # Emit metric
            try:
                from mojo.apps import metrics
                metrics.record(
                    slug="jobs.local.completed",
                    when=timezone.now(),
                    count=1,
                    category="jobs"
                )
                metrics.record(
                    slug="jobs.local.duration_ms",
                    when=timezone.now(),
                    count=int(duration * 1000),
                    category="jobs"
                )
            except Exception as e:
                logit.debug(f"Failed to record local job metrics: {e}")

            return result

        except Exception as e:
            # Log error
            with self._lock:
                self._error_count += 1
            duration = (timezone.now() - start_time).total_seconds()

            error_msg = str(e)
            stack = traceback.format_exc()

            logit.error(f"Local job {job.job_id} failed after {duration:.2f}s: {error_msg}")
            logit.debug(f"Stack trace for {job.job_id}:\n{stack}")

            # Emit error metric
            try:
                from mojo.apps import metrics
                metrics.record(
                    slug="jobs.local.failed",
                    when=timezone.now(),
                    count=1,
                    category="jobs"
                )
            except Exception as me:
                logit.debug(f"Failed to record local job error metrics: {me}")

            # Local jobs don't retry - just log and move on
            return None


class LocalQueueManager:
    """
    Manager for local queue singleton.

    Ensures only one queue instance exists per process.
    """

    def __init__(self):
        self._queue = None
        self._lock = threading.RLock()

    def get_queue(self) -> LocalQueue:
        """
        Get or create the local queue instance.

        Returns:
            LocalQueue instance
        """
        with self._lock:
            if self._queue is None:
                self._queue = LocalQueue()
            return self._queue

    def stop_queue(self, timeout: float = 5.0):
        """
        Stop the local queue if running.

        Args:
            timeout: Maximum time to wait for stop
        """
        with self._lock:
            if self._queue:
                self._queue.stop(timeout)
                self._queue = None

    def reset(self):
        """Reset the queue (useful for testing)."""
        self.stop_queue()


# Global manager instance
_manager = LocalQueueManager()


def get_local_queue() -> LocalQueue:
    """
    Get the local job queue instance.

    Returns:
        LocalQueue singleton
    """
    return _manager.get_queue()


def stop_local_queue(timeout: float = 5.0):
    """
    Stop the local job queue.

    Args:
        timeout: Maximum time to wait
    """
    _manager.stop_queue(timeout)


def reset_local_queue():
    """Reset the local queue (useful for testing)."""
    _manager.reset()
