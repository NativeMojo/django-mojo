"""
Redis key builder for jobs system.
Centralized, prefix-aware key management.
"""
from typing import Optional
from django.conf import settings


QUEUE_TAG = "{jobs}"  # same slot for all queues (cluster-safe)

def queue_key(name: str) -> str:
    return f"{QUEUE_TAG}:queue:{name}"

def dlq_key(name: str) -> str:
    return f"{QUEUE_TAG}:dlq:{name}"


class JobKeys:
    """Centralized Redis key builder for the jobs system."""

    def __init__(self, prefix: Optional[str] = None):
        """
        Initialize with optional prefix override.

        Args:
            prefix: Override the default prefix from settings
        """
        self.prefix = prefix or getattr(settings, 'JOBS_REDIS_PREFIX', 'mojo:jobs')

    # ----------------------------
    # Streams (legacy/compat only)
    # ----------------------------
    def stream(self, channel: str) -> str:
        """
        Get the stream key for a channel.

        Args:
            channel: The channel name

        Returns:
            Redis key for the channel's main stream
        """
        return f"{self.prefix}:stream:{channel}"

    def stream_broadcast(self, channel: str) -> str:
        """
        Get the broadcast stream key for a channel.

        Args:
            channel: The channel name

        Returns:
            Redis key for the channel's broadcast stream
        """
        return f"{self.prefix}:stream:{channel}:broadcast"

    def group_workers(self, channel: str) -> str:
        """
        Get the consumer group key for workers on a channel.

        Args:
            channel: The channel name

        Returns:
            Redis consumer group name for workers
        """
        return f"{self.prefix}:cg:{channel}:workers"

    def group_runner(self, channel: str, runner_id: str) -> str:
        """
        Get the consumer group key for a specific runner on broadcast stream.

        Args:
            channel: The channel name
            runner_id: The runner's unique identifier

        Returns:
            Redis consumer group name for this runner
        """
        return f"{self.prefix}:cg:{channel}:runner:{runner_id}"

    # ----------------------------
    # Plan B: List + ZSET keys
    # ----------------------------
    def queue(self, channel: str) -> str:
        """
        Immediate jobs list (queue).
        RPUSH to enqueue, BRPOP to claim.
        """
        return f"{self.prefix}:{QUEUE_TAG}:queue:{channel}"

    def processing(self, channel: str) -> str:
        """
        In-flight tracking ZSET for visibility timeout.
        ZADD on claim, ZREM on completion.
        """
        return f"{self.prefix}:processing:{channel}"

    def sched(self, channel: str) -> str:
        """
        Get the scheduled jobs ZSET key for a channel.

        Args:
            channel: The channel name

        Returns:
            Redis ZSET key for scheduled/delayed jobs
        """
        return f"{self.prefix}:sched:{channel}"

    def sched_broadcast(self, channel: str) -> str:
        """
        Scheduled jobs ZSET for broadcast (optional).
        """
        return f"{self.prefix}:sched_broadcast:{channel}"

    def reaper_lock(self, channel: str) -> str:
        """
        Per-channel lock key for the reaper (to avoid races).
        """
        return f"{self.prefix}:lock:reaper:{channel}"

    def channel_pause(self, channel: str) -> str:
        """
        Get the pause flag key for a channel.
        """
        return f"{self.prefix}:channel:{channel}:paused"

    # ----------------------------
    # Job metadata / control
    # ----------------------------
    def job(self, job_id: str) -> str:
        """
        Get the hash key for a specific job's metadata.

        Args:
            job_id: The job's unique identifier

        Returns:
            Redis hash key for job metadata
        """
        return f"{self.prefix}:job:{job_id}"

    def runner_ctl(self, runner_id: str) -> str:
        """
        Get the control channel key for a runner.

        Args:
            runner_id: The runner's unique identifier

        Returns:
            Redis key for runner control messages
        """
        return f"{self.prefix}:runner:{runner_id}:ctl"

    def runner_hb(self, runner_id: str) -> str:
        """
        Get the heartbeat key for a runner.

        Args:
            runner_id: The runner's unique identifier

        Returns:
            Redis key for runner heartbeat (with TTL)
        """
        return f"{self.prefix}:runner:{runner_id}:hb"

    def scheduler_lock(self) -> str:
        """
        Get the scheduler leadership lock key.

        Returns:
            Redis key for scheduler lock
        """
        return f"{self.prefix}:lock:scheduler"

    def stats_counter(self, metric: str) -> str:
        """
        Get a stats counter key.

        Args:
            metric: The metric name (e.g., 'published', 'completed')

        Returns:
            Redis key for the stats counter
        """
        return f"{self.prefix}:stats:{metric}"

    def registry_key(self) -> str:
        """
        Get the job registry hash key.

        Returns:
            Redis key for the job function registry
        """
        return f"{self.prefix}:registry"

    def idempotency(self, key: str) -> str:
        """
        Get the idempotency check key.

        Args:
            key: The idempotency key from the client

        Returns:
            Redis key for idempotency checking
        """
        return f"{self.prefix}:idempotent:{key}"


# Default instance for module-level use
default_keys = JobKeys()
