"""
Django-MOJO Jobs System Configuration Settings

Add these settings to your Django settings.py file to configure the jobs system.
"""

# Redis Configuration
JOBS_REDIS_URL = "redis://localhost:6379/0"
JOBS_REDIS_PREFIX = "mojo:jobs"

# Engine Configuration
JOBS_ENGINE_MAX_WORKERS = 10          # Thread pool size per engine
JOBS_ENGINE_CLAIM_BUFFER = 2          # Claim up to buffer * max_workers jobs
JOBS_ENGINE_CLAIM_BATCH = 5           # Max jobs to claim in one request
JOBS_ENGINE_READ_TIMEOUT = 100        # Redis XREADGROUP timeout in ms

# Job Defaults
JOBS_DEFAULT_CHANNEL = "default"
JOBS_DEFAULT_EXPIRES_SEC = 900        # 15 minutes default expiration
JOBS_DEFAULT_MAX_RETRIES = 3
JOBS_DEFAULT_BACKOFF_BASE = 2.0       # Exponential backoff base
JOBS_DEFAULT_BACKOFF_MAX = 3600       # Max backoff 1 hour

# Limits
JOBS_PAYLOAD_MAX_BYTES = 1048576      # 1MB max payload size
JOBS_STREAM_MAXLEN = 100000           # Max messages per stream
JOBS_LOCAL_QUEUE_MAXSIZE = 1000       # Max local queue size

# Timeouts
JOBS_IDLE_TIMEOUT_MS = 60000          # Consider job stuck after 1 minute idle
JOBS_XPENDING_IDLE_MS = 60000         # Reclaim jobs idle for 1 minute
JOBS_RUNNER_HEARTBEAT_SEC = 5         # Heartbeat interval
JOBS_SCHEDULER_LOCK_TTL_MS = 5000     # Scheduler leadership lock TTL

# Webhook-specific Configuration
JOBS_WEBHOOK_MAX_RETRIES = 5          # More retries for webhooks (network issues)
JOBS_WEBHOOK_DEFAULT_TIMEOUT = 30     # Default webhook timeout in seconds
JOBS_WEBHOOK_MAX_TIMEOUT = 300        # Maximum allowed webhook timeout
JOBS_WEBHOOK_USER_AGENT = "Django-MOJO-Webhook/1.0"  # Default User-Agent header

# Channels Configuration
JOBS_CHANNELS = [
    'default',
    'emails',
    'uploads',
    'webhooks',
    'maintenance',
    'reports'
]

# Example Full Configuration
"""
# In your Django settings.py:

# Basic Configuration
JOBS_REDIS_URL = os.environ.get('REDIS_URL', 'redis://localhost:6379/0')
JOBS_ENGINE_MAX_WORKERS = 20  # Process 20 jobs in parallel

# High-throughput Configuration
JOBS_ENGINE_MAX_WORKERS = 50
JOBS_ENGINE_CLAIM_BUFFER = 3  # Can claim up to 150 jobs
JOBS_ENGINE_CLAIM_BATCH = 10  # Claim 10 at a time

# Conservative Configuration
JOBS_ENGINE_MAX_WORKERS = 5
JOBS_DEFAULT_MAX_RETRIES = 5
JOBS_DEFAULT_EXPIRES_SEC = 1800  # 30 minutes

# Channel-specific Workers
# Run different workers for different channels:
# python manage.py jobs_engine --channels emails,notifications --max-workers 20
# python manage.py jobs_engine --channels uploads --max-workers 5
# python manage.py jobs_engine --channels maintenance --max-workers 2
"""

# Settings Documentation
"""
Configuration Options:

JOBS_REDIS_URL
    Redis connection URL. Supports standard Redis URL format.
    Default: "redis://localhost:6379/0"

JOBS_REDIS_PREFIX
    Prefix for all Redis keys used by the jobs system.
    Default: "mojo:jobs"

JOBS_ENGINE_MAX_WORKERS
    Maximum number of threads in the job engine's thread pool.
    Controls how many jobs can run in parallel per engine.
    Default: 10

JOBS_ENGINE_CLAIM_BUFFER
    Buffer multiplier for job claiming. Engine can claim up to
    max_workers * claim_buffer jobs to keep the thread pool busy.
    Default: 2

JOBS_ENGINE_CLAIM_BATCH
    Maximum number of jobs to claim in a single XREADGROUP call.
    Prevents claiming too many jobs at once.
    Default: 5

JOBS_ENGINE_READ_TIMEOUT
    Timeout in milliseconds for XREADGROUP blocking reads.
    Lower values = more responsive to shutdown, higher = less CPU.
    Default: 100

JOBS_DEFAULT_CHANNEL
    Default channel for jobs if not specified.
    Default: "default"

JOBS_DEFAULT_EXPIRES_SEC
    Default expiration time in seconds for jobs.
    Jobs not executed within this time are marked as expired.
    Default: 900 (15 minutes)

JOBS_DEFAULT_MAX_RETRIES
    Default maximum retry attempts for failed jobs.
    Default: 3

JOBS_DEFAULT_BACKOFF_BASE
    Base for exponential backoff calculation.
    Retry delay = backoff_base ^ attempt (capped at backoff_max).
    Default: 2.0

JOBS_DEFAULT_BACKOFF_MAX
    Maximum backoff time in seconds between retries.
    Default: 3600 (1 hour)

JOBS_PAYLOAD_MAX_BYTES
    Maximum size in bytes for job payloads.
    Larger payloads will be rejected at publish time.
    Default: 1048576 (1MB)

JOBS_STREAM_MAXLEN
    Maximum length of Redis streams. Older messages are trimmed.
    Uses approximate trimming for performance.
    Default: 100000

JOBS_LOCAL_QUEUE_MAXSIZE
    Maximum size of the local in-process job queue.
    Default: 1000

JOBS_IDLE_TIMEOUT_MS
    Time in milliseconds before a claimed job is considered stuck.
    Used for health monitoring and potential job reclamation.
    Default: 60000 (1 minute)

JOBS_XPENDING_IDLE_MS
    Time in milliseconds before attempting to reclaim idle jobs
    from dead/stuck workers using XCLAIM.
    Default: 60000 (1 minute)

JOBS_RUNNER_HEARTBEAT_SEC
    Interval in seconds between runner heartbeat updates.
    Used to detect dead runners.
    Default: 5

JOBS_SCHEDULER_LOCK_TTL_MS
    TTL in milliseconds for the scheduler leadership lock.
    Only one scheduler should be active cluster-wide.
    Default: 5000 (5 seconds)

JOBS_CHANNELS
    List of configured channels. Used by scheduler and manager
    to know which channels to monitor.
    Default: ['default']

Redis Keys (KISS approach)
    With the KISS design, Redis is used for transport and timing only (Postgres is the source of truth).
    - Scheduling uses two ZSETs per channel (prefixed by JOBS_REDIS_PREFIX):
        • sched:{channel} for non-broadcast delayed jobs
        • sched_broadcast:{channel} for broadcast delayed jobs
      The ZSET score is the scheduled time in epoch milliseconds (run_at_ms).
    - Immediate jobs are written directly to streams:
        • stream:{channel}
        • stream:{channel}:broadcast
    - To pause a channel during maintenance, a pause flag key is set:
        • channel:{channel}:paused (value "1" when paused)
"""

# Performance Tuning Guide
"""
Performance Tuning:

For High Throughput (10,000+ jobs/minute):
    JOBS_ENGINE_MAX_WORKERS = 50-100
    JOBS_ENGINE_CLAIM_BUFFER = 3
    JOBS_ENGINE_CLAIM_BATCH = 20
    JOBS_STREAM_MAXLEN = 500000
    # Run multiple engine instances

For Low Latency (< 100ms pickup time):
    JOBS_ENGINE_READ_TIMEOUT = 10-50
    JOBS_ENGINE_CLAIM_BATCH = 1-2
    JOBS_RUNNER_HEARTBEAT_SEC = 2

For Resource Constrained:
    JOBS_ENGINE_MAX_WORKERS = 5
    JOBS_ENGINE_CLAIM_BUFFER = 1
    JOBS_PAYLOAD_MAX_BYTES = 102400  # 100KB
    JOBS_STREAM_MAXLEN = 10000

For Reliability:
    JOBS_DEFAULT_MAX_RETRIES = 5-10
    JOBS_DEFAULT_EXPIRES_SEC = 3600  # 1 hour
    JOBS_IDLE_TIMEOUT_MS = 300000    # 5 minutes
    JOBS_DEFAULT_BACKOFF_MAX = 7200  # 2 hours
"""
