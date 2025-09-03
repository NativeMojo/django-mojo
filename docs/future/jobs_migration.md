# Django-MOJO Jobs System Migration Guide

This guide helps you migrate from the old registry-based jobs system to the new simplified architecture.

## Key Changes

### 1. No More Registry or Decorators

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
# No imports or decorators needed for the job function!

def send_newsletter(job):
    subscribers = job.payload['subscribers']
    job.metadata['count'] = len(subscribers)
    if job.cancel_requested:
        return "cancelled"
    # ... send emails ...
```

### 2. JobContext Removed - Use Job Model Directly

**Before:**
```python
def process_data(ctx: JobContext):
    data = ctx.payload['data']
    ctx.set_metadata(processed=True)
    ctx.log("Processing data")
    
    if ctx.should_cancel():
        return "cancelled"
    
    model = ctx.get_model()  # Get Job model
```

**After:**
```python
def process_data(job: Job):
    data = job.payload['data']
    job.metadata['processed'] = True
    # Use standard logging
    print(f"Processing job {job.id}: data")
    
    if job.cancel_requested:
        return "cancelled"
    
    # You already have the Job model!
```

### 3. Publishing Jobs - Use Module Paths

**Before:**
```python
from myapp.jobs import send_email  # Must import!
from mojo.apps.jobs import publish

job_id = publish(
    send_email,  # Required registration
    payload={'to': 'user@example.com'}
)
```

**After:**
```python
from mojo.apps.jobs import publish

# Method 1: Module path string (no import needed!)
job_id = publish(
    "myapp.jobs.send_email",
    payload={'to': 'user@example.com'}
)

# Method 2: If you have it imported (extracts path)
from myapp.jobs import send_email
job_id = publish(
    send_email,  # Will convert to module path
    payload={'to': 'user@example.com'}
)
```

### 4. Payload Storage Fixed

The payload is now stored ONLY in the database, never in Redis. This prevents memory overflow with large payloads.

**What this means:**
- No more size limits from Redis memory
- Payloads can be megabytes (limited by `JOBS_PAYLOAD_MAX_BYTES`)
- Redis only stores job IDs and minimal metadata

## Migration Steps

### Step 1: Remove Decorators

Find all `@async_job` decorators and remove them:

```python
# Old
from mojo.apps.jobs import async_job

@async_job(channel="reports", max_retries=5)
def generate_report(ctx):
    pass

# New
def generate_report(job):
    pass
```

### Step 2: Update Function Signatures

Change from `JobContext` to `Job`:

```python
# Old
def my_job(ctx: JobContext):
    pass

# New
from mojo.apps.jobs.models import Job

def my_job(job: Job):
    pass
```

### Step 3: Update Job Function Bodies

| Old (JobContext) | New (Job Model) |
|------------------|-----------------|
| `ctx.payload` | `job.payload` |
| `ctx.job_id` | `job.id` |
| `ctx.channel` | `job.channel` |
| `ctx.set_metadata(**kw)` | `job.metadata.update(kw)` or `job.metadata['key'] = value` |
| `ctx.get_metadata()` | `job.metadata` |
| `ctx.should_cancel()` | `job.cancel_requested` |
| `ctx.get_model()` | Just use `job` - it IS the model! |
| `ctx.log(msg)` | Use standard logging |

### Step 4: Update Publish Calls

```python
# Old - Required registration
from myapp.jobs import process_upload
publish(process_upload, {...})

# New - Use module path
publish("myapp.jobs.process_upload", {...})
```

### Step 5: Update Imports

Remove these imports:
```python
# Remove these
from mojo.apps.jobs import async_job, local_async_job, JobContext
from mojo.apps.jobs.registry import get_job_function
```

Keep/Add these:
```python
# Keep these
from mojo.apps.jobs import publish, cancel, status
from mojo.apps.jobs.models import Job  # If type hinting
```

## Common Patterns

### Pattern 1: Simple Job

```python
def send_notification(job: Job):
    """Send a notification."""
    user_id = job.payload['user_id']
    message = job.payload['message']
    
    # Do the work
    send_to_user(user_id, message)
    
    # Update metadata
    job.metadata['sent_at'] = datetime.now().isoformat()
    
    return "success"
```

### Pattern 2: Job with Cancellation

```python
def process_large_dataset(job: Job):
    """Process dataset with cancellation checks."""
    dataset = load_dataset(job.payload['dataset_id'])
    
    for i, record in enumerate(dataset):
        # Check cancellation periodically
        if i % 100 == 0 and job.cancel_requested:
            job.metadata['cancelled_at_record'] = i
            return "cancelled"
        
        process_record(record)
    
    job.metadata['total_processed'] = len(dataset)
    return "completed"
```

### Pattern 3: Job with Progress Updates

```python
def generate_report(job: Job):
    """Generate report with progress tracking."""
    steps = ['fetch_data', 'process', 'format', 'save']
    
    for i, step in enumerate(steps):
        if job.cancel_requested:
            return "cancelled"
        
        # Update progress
        job.metadata['current_step'] = step
        job.metadata['progress'] = f"{(i+1)/len(steps)*100:.0f}%"
        
        # Optionally save to DB (has overhead)
        job.save(update_fields=['metadata'])
        
        # Do the work
        execute_step(step)
    
    return "completed"
```

### Pattern 4: Job with Retries

```python
def fetch_external_data(job: Job):
    """Fetch data with automatic retries."""
    url = job.payload['url']
    
    try:
        response = requests.get(url, timeout=30)
        response.raise_for_status()
        
        job.metadata['response_size'] = len(response.content)
        return "success"
        
    except requests.exceptions.Timeout:
        # Will retry based on job.max_retries
        raise  # Let job engine handle retry
        
    except requests.exceptions.HTTPError as e:
        if e.response.status_code in [429, 502, 503, 504]:
            raise  # Retry on these status codes
        else:
            # Don't retry on client errors
            job.metadata['http_error'] = e.response.status_code
            return "failed"
```

## Configuration Updates

Add these to your Django settings:

```python
# Required
JOBS_REDIS_URL = "redis://localhost:6379/0"

# Performance tuning
JOBS_ENGINE_MAX_WORKERS = 20  # Parallel execution threads
JOBS_ENGINE_CLAIM_BUFFER = 2  # Can claim 2x workers
JOBS_ENGINE_CLAIM_BATCH = 5   # Claim 5 jobs at once

# Defaults
JOBS_DEFAULT_EXPIRES_SEC = 900  # 15 minutes
JOBS_DEFAULT_MAX_RETRIES = 3
JOBS_PAYLOAD_MAX_BYTES = 1048576  # 1MB
```

## Testing Your Jobs

### Unit Testing

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
    mock_job.attempt = 1
    
    # Test the function directly
    result = process_data(mock_job)
    
    assert result == "completed"
    assert mock_job.metadata['count'] == 3
```

### Integration Testing

```python
from mojo.apps.jobs import publish, status
from mojo.apps.jobs.models import Job

def test_job_execution():
    # Publish job
    job_id = publish(
        "myapp.jobs.process_data",
        payload={'data': [1, 2, 3]},
        channel="test"
    )
    
    # Check it was created
    job = Job.objects.get(id=job_id)
    assert job.status == 'pending'
    
    # Check status
    job_status = status(job_id)
    assert job_status['channel'] == 'test'
```

## Troubleshooting

### Import Errors

**Error:** `ImportError: Cannot load job function 'myapp.jobs.my_function'`

**Solution:** Ensure the module path is correct and the function exists. The path must be importable from where the job engine runs.

### Metadata Not Saving

**Issue:** Metadata updates not persisting

**Solution:** Metadata is saved when the job completes. For progress updates during execution, explicitly call `job.save(update_fields=['metadata'])`.

### Large Payloads

**Error:** `ValueError: Payload exceeds 1048576 bytes`

**Solution:** 
1. Increase `JOBS_PAYLOAD_MAX_BYTES` in settings
2. Or store large data elsewhere and pass a reference:
```python
# Store large data in cache/S3/database
cache.set(f"data_{job_id}", large_data)

# Pass reference in payload
publish("myapp.jobs.process", payload={'data_key': f"data_{job_id}"})
```

### Jobs Not Running

**Check:**
1. Job engine is running: `python manage.py jobs_engine`
2. Correct channel: Engine must be listening to the job's channel
3. Redis connection: Check `JOBS_REDIS_URL` is correct
4. Check health: Use JobManager to inspect queue state

## Benefits of the New System

1. **True Distributed Execution**: No registration needed on workers
2. **Simpler Code**: ~500 lines removed
3. **Better Performance**: Parallel execution with thread pools
4. **Safer**: Payloads in database, not Redis memory
5. **Easier Testing**: Just test plain functions
6. **Hot Reload**: Code changes picked up immediately

## Getting Help

1. Check job status:
```python
from mojo.apps.jobs import status
print(status(job_id))
```

2. Monitor health:
```python
from mojo.apps.jobs.manager import get_manager
manager = get_manager()
print(manager.get_channel_health('default'))
```

3. View logs:
- Job engine logs: Check your engine output
- Job errors: Stored in `job.last_error` and `job.stack_trace`

## Quick Reference Card

### Publishing
```python
from mojo.apps.jobs import publish

job_id = publish(
    "module.path.function",  # Or callable
    payload={'key': 'value'},
    channel='default',
    delay=60,  # Seconds
    max_retries=3,
    expires_in=900
)
```

### Job Function
```python
def my_job(job: Job) -> str:
    # Access data
    data = job.payload['data']
    
    # Check cancellation
    if job.cancel_requested:
        return "cancelled"
    
    # Update metadata
    job.metadata['result'] = 'value'
    
    # Return status
    return "completed"
```

### Management
```python
from mojo.apps.jobs import cancel, status
from mojo.apps.jobs.manager import get_manager

# Cancel job
cancel(job_id)

# Check status
info = status(job_id)

# Monitor
manager = get_manager()
health = manager.get_channel_health('default')
```
