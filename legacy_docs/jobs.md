# Django-MOJO Jobs System Refactor Plan

## Executive Summary

This document outlines a comprehensive refactor of the Django-MOJO Jobs system to simplify the architecture, improve performance through parallel execution, and fix critical design issues. The refactor maintains the core Redis/Postgres architecture while removing unnecessary abstractions and adding essential monitoring capabilities.

**Key Changes:**
- Remove registry system → Use dynamic imports
- Remove JobContext → Pass Job model directly
- Add parallel execution with thread pools
- Fix payload storage (database only, never Redis)
- Replace internal stats with redis_metrics
- Add comprehensive health monitoring

---

## Critical Issues to Fix

### 1. Payload Storage (HIGH PRIORITY)
**Current Problem:** Full payload stored in Redis (memory), causing potential memory overflow

**Before:**
```python
# WRONG - Payload in Redis
redis.hset(keys.job(job_id), {
    'payload': json.dumps(payload),  # BAD! Could be megabytes!
    ...
})
```

**After:**
```python
# Payload ONLY in database
redis.xadd(stream_key, {
    'job_id': job_id,  # Just the reference
    'func': func_path,  # For logging only
    'created': timestamp
})  # NO PAYLOAD!
```

### 2. Registry Prevents Distributed Execution
**Current Problem:** Functions must be pre-registered on every worker

**Before:**
```python
@async_job(channel="emails")  # Must be imported on every worker!
def send_email(ctx):
    pass
```

**After:**
```python
# No decorator needed - just a plain function
def send_email(job: Job):
    pass

# Publish with module path
publish("mojo.apps.email.jobs.send_email", payload)
```

---

## Phase 1: Core Simplifications

### 1.1 Remove Registry System

**Delete Files:**
- `mojo/apps/jobs/registry.py` (337 lines)

**Replace With:**
```python
# In job_engine.py
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
```

### 1.2 Remove JobContext

**Delete Files:**
- `mojo/apps/jobs/context.py` (147 lines)

**Update Job Functions:**
```python
# Before (with context)
def process_upload(ctx: JobContext):
    file_id = ctx.payload['file_id']
    ctx.set_metadata(processed=True)
    if ctx.should_cancel():
        return "cancelled"

# After (direct model)
def process_upload(job: Job):
    file_id = job.payload['file_id']
    job.metadata['processed'] = True
    if job.cancel_requested:
        return "cancelled"
```

### 1.3 Update publish() Function

```python
def publish(
    func: Union[str, Callable],
    payload: Dict[str, Any],
    channel: str = "default",
    **options
) -> str:
    """
    Publish a job for asynchronous execution.

    Args:
        func: Module path string or callable
              e.g., "mojo.apps.email.jobs.send_email" or send_email
        payload: Job data (stored in database only)
        channel: Queue channel name
    """
    # Convert callable to module path
    if callable(func):
        func_path = f"{func.__module__}.{func.__name__}"
    else:
        func_path = func

    # Validate payload size (before DB storage)
    payload_size = len(json.dumps(payload))
    max_size = settings.JOBS_PAYLOAD_MAX_BYTES
    if payload_size > max_size:
        raise ValueError(f"Payload exceeds {max_size} bytes")

    # Generate job ID
    job_id = uuid.uuid4().hex

    # 1. Store in database (with full payload)
    job = Job.objects.create(
        id=job_id,
        func=func_path,
        payload=payload,  # Full payload in DB
        channel=channel,
        status='pending',
        **options
    )

    # 2. Queue in Redis (minimal data only)
    redis = get_adapter()
    keys = JobKeys()

    if job.run_at and job.run_at > timezone.now():
        # Scheduled job -> ZSET
        score = job.run_at.timestamp() * 1000
        redis.zadd(keys.sched(channel), {job_id: score})
    else:
        # Immediate job -> Stream
        redis.xadd(
            keys.stream(channel),
            {
                'job_id': job_id,
                'func': func_path,  # For logging only
                'created': timezone.now().isoformat()
            },
            maxlen=settings.JOBS_STREAM_MAXLEN
        )

    # 3. Track metrics
    metrics.record("jobs.published", count=1, category="jobs")
    metrics.record(f"jobs.channel.{channel}.published", count=1)

    return job_id
```

---

## Phase 2: Add Parallel Execution

### 2.1 Thread Pool Implementation

```python
# job_engine.py
import concurrent.futures
from threading import Lock, Semaphore

class JobEngine:
    def __init__(self, channels: List[str], runner_id: str = None,
                 max_workers: int = None):
        self.channels = channels
        self.runner_id = runner_id or self._generate_runner_id()

        # Thread pool configuration
        self.max_workers = max_workers or settings.JOBS_ENGINE_MAX_WORKERS
        self.executor = concurrent.futures.ThreadPoolExecutor(
            max_workers=self.max_workers,
            thread_name_prefix=f"JobWorker-{self.runner_id}"
        )

        # Track active jobs
        self.active_jobs = {}
        self.active_lock = Lock()

        # Limit claimed jobs (prevent overwhelming)
        claim_buffer = settings.JOBS_ENGINE_CLAIM_BUFFER
        self.max_claimed = self.max_workers * claim_buffer
        self.claim_semaphore = Semaphore(self.max_claimed)

        self.redis = get_adapter()
        self.keys = JobKeys()
        self.running = False

    def _main_loop(self):
        """Main loop - claims jobs based on capacity."""
        while self.running:
            # Check available capacity
            with self.active_lock:
                active_count = len(self.active_jobs)

            if active_count >= self.max_claimed:
                time.sleep(0.1)
                continue

            # Claim jobs up to available capacity
            available = self.max_claimed - active_count
            messages = self._claim_jobs(min(available, 5))  # Max 5 at once

            for stream_key, msg_id, job_id in messages:
                # Submit to thread pool
                future = self.executor.submit(
                    self._execute_job_wrapper,
                    stream_key, msg_id, job_id
                )

                # Track active job
                with self.active_lock:
                    self.active_jobs[job_id] = {
                        'future': future,
                        'started': timezone.now(),
                        'stream': stream_key,
                        'msg_id': msg_id
                    }

                # Cleanup callback
                future.add_done_callback(
                    lambda f, jid=job_id: self._job_completed(jid)
                )

    def _claim_jobs(self, count: int) -> List[Tuple[str, str, str]]:
        """Claim up to 'count' jobs from Redis streams."""
        claimed = []

        for channel in self.channels:
            if len(claimed) >= count:
                break

            stream_key = self.keys.stream(channel)
            group = self.keys.group_workers(channel)

            try:
                # Non-blocking read
                messages = self.redis.xreadgroup(
                    group=group,
                    consumer=self.runner_id,
                    streams={stream_key: '>'},
                    count=count - len(claimed),
                    block=100  # 100ms timeout
                )

                if messages:
                    for msg_id, data in messages[0][1]:
                        job_id = data.get(b'job_id', b'').decode('utf-8')
                        if job_id:
                            claimed.append((stream_key, msg_id, job_id))

            except Exception as e:
                logit.error(f"Failed to claim jobs from {channel}: {e}")

        return claimed

    def _execute_job_wrapper(self, stream_key: str, msg_id: str, job_id: str):
        """Execute job and handle all state updates."""
        try:
            # Load job from database
            job = Job.objects.select_for_update().get(id=job_id)

            # Check if already processed or cancelled
            if job.status in ('completed', 'cancelled'):
                self._ack_message(stream_key, msg_id)
                return

            # Check expiration
            if job.expires_at and timezone.now() > job.expires_at:
                job.status = 'expired'
                job.finished_at = timezone.now()
                job.save(update_fields=['status', 'finished_at'])
                self._ack_message(stream_key, msg_id)
                metrics.record("jobs.expired", count=1)
                return

            # Mark as running
            job.status = 'running'
            job.started_at = timezone.now()
            job.runner_id = self.runner_id
            job.attempt += 1
            job.save(update_fields=['status', 'started_at', 'runner_id', 'attempt'])

            # Load and execute function
            func = load_job_function(job.func)
            result = func(job)

            # Mark complete
            job.status = 'completed'
            job.finished_at = timezone.now()
            job.save(update_fields=['status', 'finished_at', 'metadata'])

            # ACK message
            self._ack_message(stream_key, msg_id)

            # Metrics
            duration_ms = int((job.finished_at - job.started_at).total_seconds() * 1000)
            metrics.record("jobs.completed", count=1)
            metrics.record(f"jobs.channel.{job.channel}.completed", count=1)
            metrics.record("jobs.duration_ms", count=duration_ms)

        except Exception as e:
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
                job.run_at = timezone.now() + timedelta(seconds=jitter)
                job.status = 'pending'
                job.save(update_fields=[
                    'status', 'run_at', 'last_error', 'stack_trace'
                ])

                # Add to scheduled ZSET
                score = job.run_at.timestamp() * 1000
                self.redis.zadd(self.keys.sched(job.channel), {job_id: score})

                metrics.record("jobs.retried", count=1)
            else:
                # Max retries exceeded
                job.status = 'failed'
                job.finished_at = timezone.now()
                job.save(update_fields=[
                    'status', 'finished_at', 'last_error', 'stack_trace'
                ])

                metrics.record("jobs.failed", count=1)
                metrics.record(f"jobs.channel.{job.channel}.failed", count=1)

            # Always ACK to prevent redelivery
            self._ack_message(stream_key, msg_id)

        except Exception as e:
            logit.error(f"Failed to handle job failure: {e}")

    def _job_completed(self, job_id: str):
        """Callback when job future completes."""
        with self.active_lock:
            self.active_jobs.pop(job_id, None)
        self.jobs_processed += 1

    def stop(self, timeout: float = 30.0):
        """Graceful shutdown."""
        self.running = False

        # Stop claiming new jobs
        logit.info(f"Stopping JobEngine {self.runner_id}")

        # Wait for active jobs
        with self.active_lock:
            active = list(self.active_jobs.values())

        if active:
            logit.info(f"Waiting for {len(active)} active jobs...")
            futures = [j['future'] for j in active]
            concurrent.futures.wait(futures, timeout=timeout)

        # Shutdown executor
        self.executor.shutdown(wait=True, timeout=10.0)

        logit.info(f"JobEngine stopped. Processed: {self.jobs_processed}")
```

### 2.2 Configuration Settings

```python
# settings.py
JOBS_ENGINE_MAX_WORKERS = 10          # Thread pool size
JOBS_ENGINE_CLAIM_BUFFER = 2          # Claim up to 2x workers
JOBS_ENGINE_CLAIM_BATCH = 5           # Max jobs to claim at once
JOBS_IDLE_TIMEOUT_MS = 60000          # Reclaim stuck jobs after 1 minute
JOBS_PAYLOAD_MAX_BYTES = 1048576      # 1MB max payload
```

---

## Phase 3: Enhanced Monitoring

### 3.1 Health Monitoring

```python
class JobManager:
    def get_channel_health(self, channel: str) -> Dict[str, Any]:
        """Get comprehensive health metrics for a channel."""
        stream_key = self.keys.stream(channel)
        group_key = self.keys.group_workers(channel)
        sched_key = self.keys.sched(channel)

        # Stream info
        try:
            stream_info = self.redis.xinfo_stream(stream_key)
            total_messages = stream_info.get('length', 0)
        except:
            total_messages = 0

        # Pending info
        try:
            pending_info = self.redis.xpending(stream_key, group_key)
            pending_count = pending_info.get('pending', 0)
        except:
            pending_count = 0

        # Calculate unclaimed (waiting to be picked up)
        unclaimed = total_messages - pending_count

        # Scheduled jobs
        scheduled_count = self.redis.zcard(sched_key)

        # Find stuck jobs (claimed but idle too long)
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
                'scheduled': scheduled_count,
                'stuck': len(stuck)
            },
            'runners': {
                'active': len(active_runners),
                'total': len(runners)
            },
            'alerts': []
        }

        # Health checks
        if unclaimed > 100:
            health['alerts'].append(f"High unclaimed count: {unclaimed}")
            health['status'] = 'warning'

        if len(stuck) > 0:
            health['alerts'].append(f"Stuck jobs detected: {len(stuck)}")
            health['status'] = 'warning'

        if len(active_runners) == 0 and total_messages > 0:
            health['alerts'].append("No active runners for channel with pending jobs")
            health['status'] = 'critical'

        return health

    def _find_stuck_jobs(self, channel: str,
                        idle_threshold_ms: int = 60000) -> List[Dict]:
        """Find jobs that have been claimed but not processed."""
        stream_key = self.keys.stream(channel)
        group_key = self.keys.group_workers(channel)

        stuck = []
        try:
            # Get detailed pending info
            pending = self.redis.get_client().xpending_range(
                stream_key, group_key,
                min='-', max='+',
                count=100
            )

            for entry in pending:
                if entry['time_since_delivered'] > idle_threshold_ms:
                    stuck.append({
                        'message_id': entry['message_id'],
                        'consumer': entry['consumer'],
                        'idle_ms': entry['time_since_delivered'],
                        'delivery_count': entry['times_delivered']
                    })
        except Exception as e:
            logit.error(f"Failed to check stuck jobs: {e}")

        return stuck
```

### 3.2 Broadcast Commands

```python
class JobManager:
    def broadcast_command(self, command: str, data: Dict = None,
                         timeout: float = 2.0) -> List[Dict]:
        """
        Send command to all runners and collect responses.

        Commands:
        - status: Get runner status
        - shutdown: Graceful shutdown
        - pause: Stop claiming new jobs
        - resume: Resume claiming jobs
        """
        reply_channel = f"mojo:jobs:replies:{uuid.uuid4().hex[:8]}"

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
                    response = json.loads(msg['data'])
                    responses.append(response)
                except:
                    pass

        pubsub.close()
        return responses
```

---

## Phase 4: Update Job Functions

### 4.1 Job Function Examples

```python
# mojo/apps/email/jobs.py

def send_email(job: Job):
    """Send email to recipients."""
    recipients = job.payload['recipients']
    template = job.payload['template']

    # Check cancellation
    if job.cancel_requested:
        job.metadata['cancelled'] = True
        return "cancelled"

    sent_count = 0
    for recipient in recipients:
        try:
            send_mail(recipient, template)
            sent_count += 1
        except Exception as e:
            job.metadata[f'error_{recipient}'] = str(e)

    # Update metadata
    job.metadata['sent_count'] = sent_count
    job.metadata['completed_at'] = timezone.now().isoformat()

    return "completed"

def process_upload(job: Job):
    """Process uploaded file."""
    file_path = job.payload['file_path']

    # Long running with periodic cancel check
    total_lines = count_lines(file_path)
    processed = 0

    for chunk in read_chunks(file_path):
        # Check cancellation every chunk
        if job.cancel_requested:
            job.metadata['cancelled_at_line'] = processed
            return "cancelled"

        process_chunk(chunk)
        processed += len(chunk)

        # Update progress
        job.metadata['progress'] = f"{processed}/{total_lines}"
        job.save(update_fields=['metadata'])  # Save progress

    job.metadata['processed_lines'] = processed
    return "completed"
```

---

## Migration Guide

### Converting Existing Jobs

**Before:**
```python
from mojo.apps.jobs import async_job

@async_job(channel="emails", max_retries=5)
def send_newsletter(ctx):
    subscribers = ctx.payload['subscribers']
    ctx.set_metadata(count=len(subscribers))
    if ctx.should_cancel():
        return "cancelled"
    # ... send emails ...
```

**After:**
```python
# No imports or decorators needed!

def send_newsletter(job):
    subscribers = job.payload['subscribers']
    job.metadata['count'] = len(subscribers)
    if job.cancel_requested:
        return "cancelled"
    # ... send emails ...
```

### Publishing Jobs

**Before:**
```python
from myapp.jobs import send_newsletter
job_id = publish(send_newsletter, {...})  # Required import
```

**After:**
```python
# No import needed!
job_id = publish("myapp.jobs.send_newsletter", {...})
```

---

## Testing

### Unit Testing Jobs

```python
from unittest.mock import Mock
from myapp.jobs import process_data

def test_process_data():
    # Create mock job
    mock_job = Mock()
    mock_job.id = 'test123'
    mock_job.payload = {'data': [1, 2, 3]}
    mock_job.metadata = {}
    mock_job.cancel_requested = False

    # Test function directly
    result = process_data(mock_job)

    assert result == "completed"
    assert mock_job.metadata['count'] == 3
```

---

## Files to Delete

1. `registry.py` - 337 lines (replaced by dynamic imports)
2. `context.py` - 147 lines (use Job model directly)
3. Internal stats keys - use redis_metrics instead

**Total Lines Removed: ~500+**

---

## Summary

This refactor:
- **Simplifies** the system by removing unnecessary abstractions
- **Fixes** critical payload storage issue
- **Adds** parallel execution capability (5-10x throughput)
- **Improves** monitoring and health checks
- **Enables** true distributed execution without registration
- **Reduces** code by ~500+ lines

The system becomes simpler, faster, and more reliable while maintaining the solid Redis/Postgres foundation.
