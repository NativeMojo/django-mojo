# Test vs Reality: Jobs System Comparison

## Critical Discovery: Original Tests Were Testing Non-Existent Features

After analyzing both the original tests and the actual jobs implementation, I've discovered that the original tests were testing features that **don't actually exist** in the current implementation.

## What Original Tests Assumed (BUT DOESN'T EXIST)

### 1. Decorators (`@async_job`, `@local_async_job`)
```python
# ORIGINAL TESTS ASSUMED THIS EXISTS:
from mojo.apps.jobs import async_job

@async_job(channel="default")
def my_job(ctx):
    ctx.set_metadata(...)
    return "success"
```

**REALITY:** These decorators don't exist in the actual implementation.

### 2. JobContext Object
```python
# ORIGINAL TESTS ASSUMED THIS EXISTS:
from mojo.apps.jobs import JobContext

def my_job(ctx: JobContext):
    ctx.payload.get('data')
    ctx.set_metadata(key='value')
    ctx.should_cancel()
    ctx.log("message")
```

**REALITY:** JobContext class doesn't exist. Jobs receive the `Job` model directly.

### 3. Registry Module
```python
# ORIGINAL TESTS ASSUMED THIS EXISTS:
from mojo.apps.jobs.registry import (
    clear_registries,
    list_jobs,
    list_local_jobs,
    get_job_function
)
```

**REALITY:** No registry module exists in the implementation.

## What Actually Exists

### 1. Plain Functions Accepting Job Model
```python
# ACTUAL IMPLEMENTATION PATTERN:
from mojo.apps.jobs.models import Job

def send_email(job: Job) -> str:
    """Job handler that receives Job model directly."""
    recipients = job.payload.get('recipients', [])
    
    # Check cancellation via model field
    if job.cancel_requested:
        job.metadata['cancelled'] = True
        return "cancelled"
    
    # Update metadata directly on model
    job.metadata['sent_count'] = len(recipients)
    return "completed"
```

### 2. Publishing by Module Path
```python
# ACTUAL IMPLEMENTATION:
from mojo.apps.jobs import publish

# Publish by module path string
job_id = publish(
    func="mojo.apps.jobs.examples.sample_jobs.send_email",
    payload={'recipients': ['user@example.com']},
    channel="emails"
)

# Or by callable reference (extracts module path)
from mojo.apps.jobs.examples.sample_jobs import send_email
job_id = publish(
    func=send_email,  # Will extract module path
    payload={'recipients': ['user@example.com']},
    channel="emails"
)
```

### 3. Dynamic Function Loading
```python
# ACTUAL IMPLEMENTATION IN JOB ENGINE:
def load_job_function(func_path: str) -> Callable:
    """Dynamically import a job function."""
    module_path, func_name = func_path.rsplit('.', 1)
    module = importlib.import_module(module_path)
    return getattr(module, func_name)

# Then execute:
func = load_job_function(job.func)  # e.g., "myapp.jobs.send_email"
result = func(job)  # Pass Job model directly
```

## Comparison Table

| Feature | Original Tests Assumed | Actual Implementation | Refactored Tests |
|---------|------------------------|----------------------|------------------|
| **Job Definition** | `@async_job` decorator | Plain function accepting `Job` | Plain function accepting `Job` |
| **Job Context** | `JobContext` object with methods | `Job` model instance | Direct `Job` model usage |
| **Metadata** | `ctx.set_metadata()` | `job.metadata['key'] = value` | Direct metadata dict |
| **Cancellation** | `ctx.should_cancel()` | `job.cancel_requested` field | Check model field |
| **Logging** | `ctx.log()` | Standard Python logging | Standard logging |
| **Registry** | Decorator-based registry | No registry (dynamic import) | No registry needed |
| **Publishing** | Via registered function | Via module path string | Direct model creation or publish() |

## Why Original Tests Were Wrong

1. **Testing Non-Existent Decorators**: The tests spent significant effort testing `@async_job` decorator behavior that doesn't exist.

2. **Testing Non-Existent Registry**: Tests for `list_jobs()`, `clear_registries()`, etc. were testing phantom features.

3. **Wrong Job Interface**: Tests assumed jobs receive `JobContext` but they actually receive `Job` model.

4. **Overcomplicated Setup**: Tests had complex setup for decorator registration that wasn't needed.

## How Refactored Tests Fix This

### 1. Test Real Patterns
```python
# REFACTORED TEST - Matches Reality:
def simple_handler(job: Job) -> str:
    """Plain function matching actual implementation."""
    data = job.payload.get('data')
    job.metadata['processed'] = True
    
    if job.cancel_requested:
        return "cancelled"
    
    return "completed"
```

### 2. Direct Model Testing
```python
# REFACTORED TEST - Direct model usage:
job = Job.objects.create(
    id=uuid.uuid4().hex,
    channel='test',
    func='path.to.function',  # Module path string
    payload={'data': 'value'},
    status='pending'
)

# Execute handler
result = simple_handler(job)
```

### 3. No Registry Testing
The refactored tests don't test registry functionality because it doesn't exist. Instead, they test:
- Direct job creation via models
- Job execution with plain functions
- Dynamic function loading patterns

## Key Takeaways

1. **Original tests were testing imaginary features** - They assumed a decorator-based system that doesn't exist.

2. **Actual system is simpler** - Plain functions, direct model usage, dynamic imports.

3. **Refactored tests match reality** - They test what actually exists, not what was imagined.

4. **Documentation mismatch** - The README examples show decorator usage that doesn't work.

## Recommendation

The jobs system should either:

1. **Implement the missing features** (decorators, JobContext, registry) to match expectations, OR
2. **Update all documentation** to show the real patterns (plain functions, Job model)

The refactored tests correctly test what actually exists, making them more valuable than the original tests which tested phantom features.