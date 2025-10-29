# Refactored Jobs System Tests

Simplified test suite for the Django-MOJO Jobs system focusing on core functionality without decorator complexity.

## Overview

These refactored tests provide cleaner, more maintainable test coverage by:
- Removing decorator testing complexity
- Focusing on core job functionality
- Using direct model operations where appropriate
- Reducing shared state between tests
- Making tests more readable and easier to debug

## Test Files

### test_basic.py
Tests fundamental job model operations:
- Direct job creation via models
- Status transitions
- Job scheduling and expiration
- Cancellation functionality
- Retry configuration
- Broadcast jobs
- Metadata storage
- Idempotency keys
- Event tracking
- Payload handling

### test_job_execution.py
Tests job execution logic without decorators:
- Simple job handlers (plain functions)
- Cancellation checking
- Error handling
- Retry logic with backoff
- Progress reporting
- External API simulation
- Database operations
- Expiration checks
- Broadcast job execution

### test_redis_operations.py
Tests Redis adapter functionality:
- Connection management
- Key generation patterns
- Stream operations (XADD, XREADGROUP)
- Consumer groups
- Sorted sets for scheduling
- Hash operations for metadata
- Expiration and TTL
- Pipeline operations
- Pub/Sub messaging
- Data type handling

### test_manager.py
Tests management and scheduler components:
- JobManager operations
- Queue state monitoring
- Runner tracking
- Job status retrieval
- Cancellation via manager
- Retry functionality
- System statistics
- Scheduler locking
- Job movement from scheduled to ready
- Stuck job detection
- Channel health checks
- Runner control commands

## Running Tests

### Run All Refactored Tests
```bash
./bin/testit.py -m test_jobs_refactored
```

### Run Specific Test File
```bash
# Basic tests
./bin/testit.py -m test_jobs_refactored.test_basic

# Execution tests
./bin/testit.py -m test_jobs_refactored.test_job_execution

# Redis tests
./bin/testit.py -m test_jobs_refactored.test_redis_operations

# Manager tests
./bin/testit.py -m test_jobs_refactored.test_manager
```

### Run Specific Test
```bash
./bin/testit.py -m test_jobs_refactored.test_basic -t test_create_job_directly
```

### Verbose Output
```bash
./bin/testit.py -m test_jobs_refactored -v
```

## Key Differences from Original Tests

1. **No Decorator Testing**: Tests focus on job functionality, not decorator mechanics
2. **Direct Model Usage**: Many tests create jobs directly via models instead of through decorators
3. **Simpler Job Handlers**: Job handlers are plain functions that accept a Job model instance
4. **Less Shared State**: Each test file has minimal setup with less shared state
5. **Clearer Test Intent**: Each test has a single, clear purpose
6. **Better Isolation**: Tests clean up after themselves more thoroughly

## Test Patterns

### Creating a Job Directly
```python
from mojo.apps.jobs.models import Job
import uuid

job = Job.objects.create(
    id=uuid.uuid4().hex,
    channel='test_channel',
    func='path.to.function',
    payload={'key': 'value'},
    status='pending'
)
```

### Simple Job Handler
```python
def simple_handler(job):
    """A simple job handler without decorators."""
    # Access payload
    data = job.payload.get('data')
    
    # Update metadata
    job.metadata['processed'] = True
    job.metadata['result'] = process_data(data)
    
    # Check cancellation
    if job.cancel_requested:
        return "cancelled"
    
    return "completed"
```

### Testing Job Execution
```python
# Create job
job = Job.objects.create(...)

# Execute handler
result = simple_handler(job)

# Update job status
job.status = 'completed' if result == "completed" else 'failed'
job.save()
```

## Requirements

- Django with database configured
- Redis server running
- TestIt framework installed
- Django-MOJO Jobs app configured

## Benefits of This Approach

1. **Easier to Debug**: Direct function calls are easier to step through than decorated functions
2. **Faster Tests**: Less overhead from decorator machinery
3. **Better Coverage**: Can test edge cases more easily
4. **Simpler Maintenance**: Less magic, more explicit code
5. **Educational**: Shows how the jobs system works at a fundamental level

## Notes

- Tests use `test_` and `mgr_test_` channel prefixes to avoid conflicts
- All tests clean up their data in teardown
- Redis keys are properly namespaced
- Tests can run independently without order dependencies
