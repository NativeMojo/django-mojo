# Django-MOJO Tasks v2: Analysis and Design Document

## Executive Summary

This document provides a comprehensive analysis of the current Django-MOJO tasks system and proposes a v2 architecture that introduces scheduling capabilities, improved reliability, and better performance. The analysis identifies critical issues in the current design and provides a phased implementation roadmap.

---

## Part 1: Current System Analysis

### Architecture Overview

The current tasks system consists of:
- **TaskManager**: Redis-based task state management
- **TaskEngine**: Multi-threaded task executor using ThreadPoolExecutor
- **Local Queue**: In-memory queue for lightweight tasks
- **TaskLog**: Database-backed audit trail

### Current Flow

1. Task published with `publish()` → Redis pending queue
2. TaskEngine monitors channels via Redis pub/sub
3. Task moved from pending → running
4. Executor thread processes task
5. Task moved to completed/error queue
6. TaskLog records state changes

### Identified Issues and Limitations

#### Critical Issues

1. **No Scheduling Support**
   - Tasks execute immediately when published
   - No support for delayed execution
   - No cron-like scheduling capabilities
   - No timezone awareness

2. **Race Conditions**
   - Multiple runners can potentially grab the same task
   - State transitions aren't atomic
   - No distributed locking mechanism
   - Potential data loss during concurrent updates

3. **Database Connection Management**
   - `close_old_connections()` called after each task
   - Potential connection pool exhaustion
   - Thread-local storage issues with Django ORM
   - No connection pooling optimization

4. **Graceful Shutdown Problems**
   - 5-second timeout is arbitrary
   - Force-killing threads with ctypes is dangerous
   - No proper task checkpoint/resume
   - Potential data corruption on forced shutdown

#### Performance Issues

1. **Inefficient Redis Usage**
   - Individual commands instead of pipelines
   - No Lua scripts for atomic operations
   - Excessive Redis round-trips
   - No connection pooling configuration

2. **Memory Leaks**
   - Completed tasks stored for 24 hours
   - Error tasks accumulate indefinitely
   - No automatic cleanup mechanism
   - Task data not compressed

3. **Monitoring Gaps**
   - Runner ping system is rudimentary
   - No metrics on queue depths over time
   - No performance profiling
   - Limited visibility into task execution

#### Design Limitations

1. **No Task Dependencies**
   - Cannot chain tasks
   - No workflow support
   - No conditional execution

2. **No Priority Queues**
   - All tasks treated equally
   - No way to expedite critical tasks
   - No fair scheduling across channels

3. **Limited Retry Mechanism**
   - No exponential backoff
   - No configurable retry policies
   - No dead letter queue

4. **No Rate Limiting**
   - No per-function throttling
   - No per-user rate limits
   - No circuit breaker pattern

---

## Part 2: Proposed Tasks v2 Architecture

### Core Improvements

#### 1. Scheduling System

**Design Approach: Redis Sorted Sets**

```python
# Scheduled tasks stored in sorted set with timestamp as score
scheduled_key = "mojo:tasks:scheduled:{channel}"
ZADD scheduled_key timestamp task_id

# Scheduler thread checks for due tasks
due_tasks = ZRANGEBYSCORE scheduled_key 0 current_timestamp
```

**API Design:**

```python
# Immediate execution (backward compatible)
publish(channel="bg_tasks", function="module.func", data={})

# Delayed execution
publish(channel="bg_tasks", function="module.func", data={}, delay=300)  # 5 minutes

# Scheduled execution
publish(channel="bg_tasks", function="module.func", data={},
        run_at=datetime(2024, 12, 25, 10, 0, 0))

# Recurring tasks (cron-like)
publish_recurring(channel="bg_tasks", function="module.func",
                  schedule="0 */4 * * *", data={})
```

**Implementation Details:**

1. **Scheduler Thread**: Each runner spawns a scheduler thread that:
   - Polls scheduled queue every second
   - Moves due tasks to pending queue atomically
   - Handles timezone conversions
   - Manages recurring task instances

2. **Efficient Polling**: Use Redis BLPOP with timeout for efficient waiting

3. **Clock Skew Handling**: Account for time differences between servers

#### 2. Atomic State Management

**Lua Scripts for Atomic Operations:**

```lua
-- Atomic task state transition
local task_id = ARGV[1]
local from_state = ARGV[2]
local to_state = ARGV[3]
local channel = ARGV[4]

-- Check current state
local current_state = redis.call('HGET', 'task:' .. task_id, 'state')
if current_state ~= from_state then
    return 0
end

-- Perform atomic transition
redis.call('SREM', 'queue:' .. from_state .. ':' .. channel, task_id)
redis.call('SADD', 'queue:' .. to_state .. ':' .. channel, task_id)
redis.call('HSET', 'task:' .. task_id, 'state', to_state)
return 1
```

#### 3. Distributed Locking

```python
def acquire_task_lock(task_id, runner_id, timeout=30):
    """
    Acquire exclusive lock on task using Redis SET NX EX.
    """
    lock_key = f"mojo:tasks:lock:{task_id}"
    return redis.set(lock_key, runner_id, nx=True, ex=timeout)
```

#### 4. Connection Pool Management

```python
class TaskConnectionManager:
    """
    Manages database connections per thread with proper pooling.
    """
    def __init__(self):
        self.thread_connections = {}

    def get_connection(self):
        thread_id = threading.current_thread().ident
        if thread_id not in self.thread_connections:
            self.thread_connections[thread_id] = self._create_connection()
        return self.thread_connections[thread_id]

    def cleanup(self, thread_id=None):
        if thread_id:
            self.thread_connections[thread_id].close()
            del self.thread_connections[thread_id]
```

#### 5. Enhanced Retry System

```python
class RetryPolicy:
    def __init__(self, max_retries=3, backoff_base=2, max_delay=3600):
        self.max_retries = max_retries
        self.backoff_base = backoff_base
        self.max_delay = max_delay

    def next_retry_delay(self, attempt):
        """Calculate exponential backoff with jitter."""
        delay = min(
            self.backoff_base ** attempt + random.uniform(0, 1),
            self.max_delay
        )
        return delay
```

#### 6. Priority Queue System

```python
# Multiple priority levels
PRIORITY_CRITICAL = 0
PRIORITY_HIGH = 1
PRIORITY_NORMAL = 2
PRIORITY_LOW = 3

# Separate sorted sets for each priority
def publish_with_priority(function, data, priority=PRIORITY_NORMAL, **kwargs):
    score = priority * 1e10 + time.time()  # Priority as high-order bits
    redis.zadd(f"mojo:tasks:priority:{channel}", {task_id: score})
```

#### 7. Rate Limiting

```python
class RateLimiter:
    def check_rate_limit(self, key, limit, window):
        """
        Token bucket algorithm using Redis.
        """
        pipe = redis.pipeline()
        now = time.time()

        # Remove old entries
        pipe.zremrangebyscore(key, 0, now - window)

        # Count current entries
        pipe.zcard(key)

        # Add new entry if under limit
        pipe.zadd(key, {str(uuid.uuid4()): now})

        # Set expiry
        pipe.expire(key, window)

        results = pipe.execute()
        return results[1] < limit
```

### Local Queue Improvements

The local queue should also support scheduling for short delays:

```python
class ScheduledLocalQueue:
    def __init__(self):
        self.immediate_queue = queue.Queue()
        self.scheduled_queue = []  # Heap queue
        self.lock = threading.Lock()

    def publish_local(self, function, *args, delay=0, **kwargs):
        if delay == 0:
            self.immediate_queue.put((function, args, kwargs))
        elif delay <= 60:  # Max 60 seconds for local scheduling
            run_at = time.time() + delay
            with self.lock:
                heapq.heappush(self.scheduled_queue,
                              (run_at, function, args, kwargs))
        else:
            raise ValueError("Local queue only supports delays up to 60 seconds")

    def get_next_task(self, timeout=0.1):
        # Check scheduled tasks first
        with self.lock:
            while self.scheduled_queue:
                run_at, func, args, kwargs = self.scheduled_queue[0]
                if run_at <= time.time():
                    heapq.heappop(self.scheduled_queue)
                    return (func, args, kwargs)
                else:
                    break

        # Then check immediate queue
        try:
            return self.immediate_queue.get(timeout=timeout)
        except queue.Empty:
            return None
```

### Database Schema Enhancements

```python
class TaskSchedule(models.Model, MojoModel):
    """
    Stores recurring task schedules.
    """
    name = models.CharField(max_length=255, unique=True)
    function = models.CharField(max_length=255)
    channel = models.CharField(max_length=100, default="scheduled")

    # Scheduling
    cron_expression = models.CharField(max_length=100, null=True, blank=True)
    interval_seconds = models.IntegerField(null=True, blank=True)

    # Configuration
    data = models.JSONField(default=dict)
    priority = models.IntegerField(default=2)
    max_retries = models.IntegerField(default=3)
    timeout_seconds = models.IntegerField(default=1800)

    # State
    enabled = models.BooleanField(default=True)
    last_run = models.DateTimeField(null=True, blank=True)
    next_run = models.DateTimeField(db_index=True)

    # Audit
    created = models.DateTimeField(auto_now_add=True, db_index=True)
    modified = models.DateTimeField(auto_now=True, db_index=True)

    class Meta:
        indexes = [
            models.Index(fields=['enabled', 'next_run']),
        ]
```

### Monitoring & Metrics

```python
class TaskMetrics:
    """
    Enhanced metrics collection for tasks.
    """

    @classmethod
    def record_task_published(cls, channel, function, scheduled=False):
        metrics.record("tasks.published", tags={
            "channel": channel,
            "function": function,
            "scheduled": scheduled
        })

    @classmethod
    def record_task_completed(cls, channel, function, duration):
        metrics.record("tasks.completed", tags={
            "channel": channel,
            "function": function
        })
        metrics.timing("tasks.duration", duration, tags={
            "channel": channel,
            "function": function
        })

    @classmethod
    def record_queue_depth(cls):
        """Record queue depths periodically."""
        for channel in TaskManager.get_all_channels():
            for state in ['pending', 'scheduled', 'running']:
                depth = TaskManager.get_queue_depth(channel, state)
                metrics.gauge(f"tasks.queue.{state}", depth, tags={
                    "channel": channel
                })
```

### Error Recovery & Dead Letter Queue

```python
class DeadLetterQueue:
    """
    Manages failed tasks that exceed retry limits.
    """

    def add_failed_task(self, task_data, error_info):
        dlq_key = f"mojo:tasks:dlq:{task_data['channel']}"
        dlq_data = {
            **task_data,
            'failed_at': time.time(),
            'error': error_info,
            'retry_count': task_data.get('retry_count', 0)
        }
        redis.rpush(dlq_key, json.dumps(dlq_data))

        # Keep only last N failed tasks
        redis.ltrim(dlq_key, -1000, -1)

    def retry_failed_task(self, task_id):
        """Manually retry a task from DLQ."""
        # Implementation here
        pass
```

---

## Part 3: Implementation Roadmap

### Phase 1: Foundation (Week 1-2)
**Goal**: Establish core scheduling infrastructure without breaking existing functionality.

1. **Atomic State Management**
   - Implement Lua scripts for state transitions
   - Add distributed locking
   - Test concurrent runner scenarios

2. **Basic Scheduling**
   - Add sorted set for scheduled tasks
   - Implement scheduler thread in TaskEngine
   - Support `delay` and `run_at` parameters
   - Update TaskManager.publish() method

3. **Local Queue Scheduling**
   - Upgrade local queue to support delays up to 60 seconds
   - Use heap queue for efficient scheduling
   - Maintain backward compatibility

4. **Testing**
   - Unit tests for scheduling logic
   - Integration tests for state transitions
   - Load tests for concurrent operations

### Phase 2: Reliability (Week 3-4)
**Goal**: Improve system reliability and error handling.

1. **Connection Management**
   - Implement proper connection pooling
   - Fix thread-local storage issues
   - Add connection health checks

2. **Enhanced Retry System**
   - Exponential backoff with jitter
   - Configurable retry policies
   - Dead letter queue implementation

3. **Graceful Shutdown**
   - Implement task checkpointing
   - Safe thread termination
   - State persistence during shutdown

4. **Monitoring**
   - Queue depth metrics
   - Task duration tracking
   - Error rate monitoring
   - Runner health dashboard

### Phase 3: Advanced Features (Week 5-6)
**Goal**: Add enterprise features and optimizations.

1. **Priority Queues**
   - Multiple priority levels
   - Fair scheduling algorithm
   - Priority inheritance for dependencies

2. **Rate Limiting**
   - Per-function rate limits
   - Per-user/group rate limits
   - Circuit breaker implementation

3. **Recurring Tasks**
   - Cron expression support
   - Interval-based scheduling
   - TaskSchedule model and management

4. **Task Dependencies**
   - Task chaining
   - Conditional execution
   - Workflow support

### Phase 4: Optimization (Week 7-8)
**Goal**: Performance optimization and production hardening.

1. **Redis Optimization**
   - Pipeline commands
   - Connection pooling
   - Memory optimization
   - Data compression

2. **Database Optimization**
   - Batch inserts for TaskLog
   - Async logging
   - Index optimization
   - Partition old logs

3. **Performance Testing**
   - Load testing at scale
   - Memory leak detection
   - CPU profiling
   - Network optimization

4. **Documentation & Migration**
   - API documentation
   - Migration guide
   - Performance tuning guide
   - Operational runbook

---

## Part 4: Migration Strategy

### Backward Compatibility

1. **API Compatibility**
   - Existing publish() calls continue to work
   - New parameters are optional
   - Gradual deprecation of old patterns

2. **Data Migration**
   - Existing tasks remain in current format
   - New fields added as optional
   - Background migration for historical data

3. **Rolling Deployment**
   - Version detection in task data
   - Handlers for both old and new formats
   - Staged rollout with feature flags

### Migration Steps

```python
# Step 1: Deploy new code with compatibility layer
if task_data.get('version', 1) == 1:
    handle_v1_task(task_data)
else:
    handle_v2_task(task_data)

# Step 2: Update publishers to use v2 format
publish(function="...", data={}, version=2, delay=300)

# Step 3: Migrate existing scheduled tasks
migrate_scheduled_tasks_to_v2()

# Step 4: Remove v1 compatibility layer (after verification)
```

---

## Part 5: Configuration & Settings

### New Settings

```python
# Task scheduling
TASK_SCHEDULER_ENABLED = True
TASK_SCHEDULER_POLL_INTERVAL = 1.0  # seconds
TASK_MAX_SCHEDULE_DELAY = 86400 * 30  # 30 days
TASK_TIMEZONE = "UTC"

# Retry configuration
TASK_DEFAULT_MAX_RETRIES = 3
TASK_RETRY_BACKOFF_BASE = 2
TASK_RETRY_MAX_DELAY = 3600

# Rate limiting
TASK_RATE_LIMIT_ENABLED = True
TASK_RATE_LIMIT_WINDOW = 60  # seconds
TASK_RATE_LIMIT_DEFAULT = 100  # tasks per window

# Priority levels
TASK_PRIORITY_LEVELS = 4
TASK_PRIORITY_DEFAULT = 2

# Connection management
TASK_DB_CONNECTION_POOL_SIZE = 10
TASK_REDIS_CONNECTION_POOL_SIZE = 20

# Monitoring
TASK_METRICS_ENABLED = True
TASK_METRICS_INTERVAL = 10  # seconds

# Cleanup
TASK_CLEANUP_COMPLETED_AFTER = 86400  # 24 hours
TASK_CLEANUP_ERROR_AFTER = 86400 * 7  # 7 days
TASK_CLEANUP_SCHEDULED_AFTER = 86400 * 30  # 30 days
```

---

## Part 6: Security Considerations

### Input Validation

1. **Task Data Validation**
   - Schema validation for task data
   - Size limits on payloads
   - Sanitization of user inputs

2. **Function Whitelist**
   - Restrict callable functions
   - Validate module paths
   - Prevent code injection

### Access Control

1. **Channel-based Permissions**
   - User/group access to channels
   - Channel ownership model
   - Audit trail for all operations

2. **Rate Limiting per User**
   - Prevent task flooding
   - User-specific quotas
   - IP-based rate limiting

### Data Protection

1. **Encryption**
   - Encrypt sensitive task data
   - Secure credential storage
   - TLS for Redis connections

2. **Audit & Compliance**
   - Complete audit trail
   - GDPR compliance for task data
   - Data retention policies

---

## Part 7: Testing Strategy

### Unit Tests

```python
class TestScheduling:
    def test_delay_parameter(self):
        """Test task scheduling with delay."""
        task_id = publish("test", "func", {}, delay=5)
        assert not is_task_in_pending(task_id)
        assert is_task_in_scheduled(task_id)

        time.sleep(6)
        assert is_task_in_pending(task_id)

    def test_run_at_parameter(self):
        """Test task scheduling with specific time."""
        run_time = datetime.now() + timedelta(minutes=5)
        task_id = publish("test", "func", {}, run_at=run_time)

        scheduled_time = get_task_scheduled_time(task_id)
        assert scheduled_time == run_time
```

### Integration Tests

```python
class TestConcurrency:
    def test_multiple_runners_same_task(self):
        """Ensure only one runner executes a task."""
        # Start multiple runners
        # Publish single task
        # Verify single execution
        pass

    def test_scheduler_failover(self):
        """Test scheduler thread recovery."""
        # Kill scheduler thread
        # Verify another runner takes over
        # Check no duplicate scheduling
        pass
```

### Performance Tests

```python
class TestPerformance:
    def test_high_volume_scheduling(self):
        """Test system under load."""
        # Schedule 10,000 tasks
        # Measure scheduling latency
        # Verify memory usage
        # Check Redis performance
        pass
```

---

## Part 8: Operational Considerations

### Monitoring Dashboard

Create REST endpoints for operational visibility:

```python
@md.GET('tasks/health')
def task_system_health(request):
    return {
        'runners': get_active_runners_count(),
        'queue_depths': {
            'pending': get_total_pending(),
            'scheduled': get_total_scheduled(),
            'running': get_total_running()
        },
        'error_rate': calculate_error_rate(hours=1),
        'avg_duration': calculate_avg_duration(hours=1),
        'scheduler_status': check_scheduler_health()
    }
```

### Alerting Rules

1. **Queue Depth Alerts**
   - Alert if pending > 1000 for > 5 minutes
   - Alert if scheduled > 10000
   - Alert if error rate > 5%

2. **Runner Health**
   - Alert if no active runners
   - Alert if runner memory > 80%
   - Alert if scheduler thread dead

3. **Performance Alerts**
   - Alert if avg duration > baseline * 2
   - Alert if Redis latency > 100ms
   - Alert if DB connection pool exhausted

### Maintenance Operations

```python
# Clear stuck tasks
python manage.py tasks clear-stuck --older-than 1h

# Retry failed tasks
python manage.py tasks retry-failed --channel bg_tasks

# Export metrics
python manage.py tasks export-metrics --format prometheus

# Vacuum old logs
python manage.py tasks vacuum-logs --older-than 30d
```

---

## Summary

The proposed Tasks v2 system addresses all identified issues while maintaining backward compatibility. The phased implementation approach allows for gradual rollout with minimal risk. Key improvements include:

1. **Robust scheduling** with support for delays, specific times, and recurring tasks
2. **Atomic operations** eliminating race conditions
3. **Improved reliability** through proper retry mechanisms and error handling
4. **Better performance** via optimized Redis usage and connection management
5. **Enhanced monitoring** for operational visibility
6. **Security hardening** with validation and access controls

The system is designed to scale horizontally and handle millions of tasks daily while maintaining sub-second scheduling precision and reliable execution guarantees.

---

## Next Steps

1. **Review & Feedback**: Gather team feedback on this design
2. **Prototype**: Build proof-of-concept for scheduling system
3. **Testing**: Develop comprehensive test suite
4. **Implementation**: Follow phased roadmap
5. **Documentation**: Create user and operational guides
6. **Migration**: Plan production rollout

---

*Document Version: 1.0*
*Last Updated: December 2024*
*Status: DRAFT - Pending Review*
