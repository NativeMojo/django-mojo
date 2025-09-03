# Django-MOJO Jobs System Tests

Comprehensive test suite for the Django-MOJO Jobs background task system.

## Test Files

### 1. `basic.py`
Tests fundamental job operations:
- Job publishing with various options
- Delayed and scheduled jobs
- Job expiration settings
- Broadcast jobs
- Idempotency keys
- Job cancellation
- Local job publishing
- Job status API
- Payload validation
- Registry functions

### 2. `redis_ops.py`
Tests Redis adapter and operations:
- Connection management
- Key generation with prefixes
- Stream operations (XADD, XREADGROUP, XACK)
- Consumer group operations
- ZSET operations for scheduling
- Hash operations for job metadata
- Pipeline operations
- Pub/Sub functionality
- Expiration handling
- Connection recovery

### 3. `execution.py`
Tests job execution and worker functionality:
- JobContext creation and operations
- Metadata management
- Cancellation detection
- Job direct execution
- Error handling
- Retry configuration
- Expiration detection
- Broadcast job setup
- Job event recording
- Metadata persistence

### 4. `scheduler_manager.py`
Tests scheduler and manager components:
- Scheduler initialization and locking
- Leadership lock acquisition/renewal
- Job movement from ZSET to streams
- Expired job handling
- JobManager operations
- Runner discovery and health checks
- Queue state inspection
- Runner control (ping/shutdown)
- Job retry functionality
- System statistics

### 5. `verify.py`
Quick verification tests to ensure system is working:
- Component availability
- Redis connectivity
- Basic publish/status operations
- Registry functionality
- Cancellation
- Local queue
- Delayed jobs
- Manager basics

## Running Tests

### Run All Job Tests
```bash
/bin/testit -m test_jobs
```

### Run Specific Test File
```bash
# Run basic tests only
/bin/testit -m test_jobs.basic

# Run Redis operations tests
/bin/testit -m test_jobs.redis_ops

# Run execution tests
/bin/testit -m test_jobs.execution

# Run scheduler/manager tests
/bin/testit -m test_jobs.scheduler_manager

# Run verification tests
/bin/testit -m test_jobs.verify
```

### Run Specific Test
```bash
/bin/testit -m test_jobs.basic -t test_basic_job_publish
```

### Run with Verbose Output
```bash
/bin/testit -m test_jobs -v
```

### Stop on First Failure
```bash
/bin/testit -m test_jobs -s
```

## Test Requirements

1. **Redis Server**: Must be running and accessible
   - Default: `redis://localhost:6379/0`
   - Configure via `JOBS_REDIS_URL` setting

2. **Database**: Django database must be configured
   - Tests will create/delete test data
   - Uses transactions for isolation

3. **Django Settings**: Must be properly configured
   - `DJANGO_SETTINGS_MODULE` should be set
   - Jobs-specific settings optional

## Test Coverage

### Core Functionality
- ✅ Job publishing and queueing
- ✅ Delayed and scheduled jobs
- ✅ Job cancellation
- ✅ Job expiration
- ✅ Retry with exponential backoff
- ✅ Broadcast jobs
- ✅ Local in-process jobs
- ✅ Idempotency keys

### Infrastructure
- ✅ Redis adapter with retry logic
- ✅ Key generation with prefixes
- ✅ Stream operations
- ✅ Consumer groups
- ✅ ZSET scheduling
- ✅ Connection recovery

### Execution
- ✅ JobContext operations
- ✅ Metadata management
- ✅ Cooperative cancellation
- ✅ Error handling
- ✅ Event recording

### Management
- ✅ Scheduler leadership
- ✅ Lock acquisition/renewal
- ✅ Runner health monitoring
- ✅ Queue state inspection
- ✅ System statistics
- ✅ Remote control

## Test Data Cleanup

Tests automatically clean up after themselves:
- Database records are deleted
- Redis keys are removed
- Job registries are cleared

If tests fail, manual cleanup may be needed:
```python
from mojo.apps.jobs.models import Job, JobEvent
Job.objects.filter(channel__startswith='test').delete()
JobEvent.objects.filter(channel__startswith='test').delete()
```

## Common Test Patterns

### Publishing a Test Job
```python
from mojo.apps.jobs import publish, async_job

@async_job(channel="test")
def test_job(ctx):
    return "success"

job_id = publish(
    func=test_job,
    payload={'test': True},
    channel="test"
)
```

### Checking Job Status
```python
from mojo.apps.jobs import status

job_status = status(job_id)
assert job_status['status'] == 'pending'
```

### Direct Job Execution
```python
from mojo.apps.jobs.context import JobContext

ctx = JobContext(
    job_id="test123",
    channel="test",
    payload={'data': 'value'}
)

result = test_job(ctx)
```

## Debugging Failed Tests

1. **Check Redis Connection**:
```bash
redis-cli ping
```

2. **Check Database**:
```python
from mojo.apps.jobs.models import Job
Job.objects.count()
```

3. **Enable Verbose Output**:
```bash
/bin/testit -m test_jobs -v -s
```

4. **Check Redis Keys**:
```bash
redis-cli --scan --pattern "mojo:jobs:*"
```

## Performance Notes

- Tests use small delays and timeouts for speed
- Some tests simulate runners/schedulers with threads
- Redis operations are tested with retries
- Database operations use transactions

## Contributing

When adding new tests:
1. Follow the existing naming convention
2. Use appropriate setup/teardown
3. Clean up all test data
4. Document what the test verifies
5. Use descriptive assertions

## Test Isolation

Each test file has its own setup that:
- Clears relevant database tables
- Removes Redis test keys
- Resets job registries
- Uses unique channel names

This ensures tests don't interfere with each other.