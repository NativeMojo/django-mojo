from testit import helpers as th
import time
import threading
from datetime import datetime, timedelta
from django.utils import timezone


@th.django_unit_setup()
def setup_execution_environment(opts):
    """Setup test environment for job execution."""
    from mojo.apps.jobs.models import Job, JobEvent
    from mojo.apps.jobs.registry import clear_registries
    from mojo.apps.jobs.adapters import get_adapter
    from mojo.apps.jobs.keys import JobKeys
    from mojo.apps.jobs import async_job

    # Clear existing data
    Job.objects.all().delete()
    JobEvent.objects.all().delete()
    clear_registries()

    # Clear Redis for test channels
    redis = get_adapter()
    keys = JobKeys()
    for channel in ['exec_test', 'retry_test', 'broadcast_test']:
        redis.delete(keys.stream(channel))
        redis.delete(keys.stream_broadcast(channel))
        redis.delete(keys.sched(channel))

    # Store shared test data
    opts.execution_results = {}
    opts.execution_count = 0
    opts.retry_attempts = []

    # Register test jobs
    @async_job(channel="exec_test")
    def execution_test_job(ctx):
        """Job that records execution details."""
        opts.execution_count += 1
        opts.execution_results[ctx.job_id] = {
            'executed': True,
            'payload': ctx.payload,
            'job_id': ctx.job_id,
            'channel': ctx.channel
        }
        ctx.set_metadata(execution_time=datetime.now().isoformat())
        return "success"

    @async_job(channel="exec_test")
    def error_job(ctx):
        """Job that raises an error."""
        error_msg = ctx.payload.get('error_message', 'Test error')
        raise Exception(error_msg)

    @async_job(channel="retry_test", max_retries=3, backoff_base=1.0, backoff_max=5)
    def retry_test_job(ctx):
        """Job that fails initially then succeeds."""
        opts.retry_attempts.append(ctx.payload.get('attempt', 0))

        # Fail on first two attempts
        if len(opts.retry_attempts) < 3:
            raise Exception(f"Retry attempt {len(opts.retry_attempts)}")

        return "success after retries"

    @async_job(channel="exec_test")
    def long_running_job(ctx):
        """Job that runs for a while and checks cancellation."""
        start_time = time.time()
        iterations = 0

        while time.time() - start_time < 5:  # Run for up to 5 seconds
            if ctx.should_cancel():
                ctx.set_metadata(
                    cancelled=True,
                    iterations=iterations,
                    duration=time.time() - start_time
                )
                return "cancelled"

            time.sleep(0.1)
            iterations += 1

        ctx.set_metadata(
            completed=True,
            iterations=iterations,
            duration=time.time() - start_time
        )
        return "completed"

    @async_job(channel="broadcast_test", broadcast=True)
    def broadcast_test_job(ctx):
        """Broadcast job that records runner execution."""
        runner_id = ctx.payload.get('runner_id', 'unknown')
        if 'broadcast_executions' not in opts.execution_results:
            opts.execution_results['broadcast_executions'] = []

        opts.execution_results['broadcast_executions'].append({
            'runner_id': runner_id,
            'job_id': ctx.job_id,
            'executed_at': datetime.now().isoformat()
        })

        return f"broadcast executed by {runner_id}"

    @async_job(channel="exec_test")
    def metadata_test_job(ctx):
        """Job that tests metadata operations."""
        # Set various metadata
        ctx.set_metadata(
            start_time=datetime.now().isoformat(),
            input_count=len(ctx.payload),
            test_flag=True
        )

        # Get job model
        job_model = ctx.get_model()
        if job_model:
            ctx.set_metadata(
                job_status=job_model.status,
                job_channel=job_model.channel
            )

        # Log with context
        ctx.log("Processing metadata test job", level='info')
        ctx.log(f"Payload size: {len(ctx.payload)}", level='debug')

        return "metadata_processed"

    # Store job references
    opts.execution_test_job = execution_test_job
    opts.error_job = error_job
    opts.retry_test_job = retry_test_job
    opts.long_running_job = long_running_job
    opts.broadcast_test_job = broadcast_test_job
    opts.metadata_test_job = metadata_test_job


@th.django_unit_test()
def test_job_context_creation(opts):
    """Test JobContext initialization and basic operations."""
    from mojo.apps.jobs.context import JobContext
    from mojo.apps.jobs.adapters import get_adapter
    from mojo.apps.jobs.keys import JobKeys

    # Create context
    job_id = "test_context_job_123"
    channel = "test_channel"
    payload = {"test": "data", "count": 5}

    ctx = JobContext(
        job_id=job_id,
        channel=channel,
        payload=payload,
        redis_adapter=get_adapter(),
        redis_keys=JobKeys()
    )

    # Test basic attributes
    assert ctx.job_id == job_id
    assert ctx.channel == channel
    assert ctx.payload == payload
    assert ctx.payload['test'] == 'data'
    assert ctx.payload['count'] == 5

    # Test string representations
    assert str(ctx) == f"JobContext({job_id}@{channel})"
    assert job_id in repr(ctx)
    assert channel in repr(ctx)


@th.django_unit_test()
def test_job_context_metadata(opts):
    """Test JobContext metadata operations."""
    from mojo.apps.jobs.context import JobContext

    ctx = JobContext(
        job_id="metadata_test_123",
        channel="test",
        payload={}
    )

    # Set metadata
    ctx.set_metadata(key1="value1", key2=42, key3=True)

    # Get metadata
    metadata = ctx.get_metadata()
    assert metadata['key1'] == "value1"
    assert metadata['key2'] == 42
    assert metadata['key3'] is True

    # Update metadata
    ctx.set_metadata(key2=100, key4="new")
    metadata = ctx.get_metadata()
    assert metadata['key2'] == 100
    assert metadata['key4'] == "new"
    assert metadata['key1'] == "value1"  # Original still there


@th.django_unit_test()
def test_job_context_cancel_check(opts):
    """Test JobContext cancellation checking."""
    from mojo.apps.jobs import publish, cancel
    from mojo.apps.jobs.context import JobContext
    from mojo.apps.jobs.adapters import get_adapter
    from mojo.apps.jobs.keys import JobKeys

    # Create and cancel a job
    job_id = publish(
        func=opts.long_running_job,
        payload={'test': 'cancel'},
        channel="exec_test"
    )

    cancel(job_id)

    # Create context for cancelled job
    ctx = JobContext(
        job_id=job_id,
        channel="exec_test",
        payload={'test': 'cancel'},
        redis_adapter=get_adapter(),
        redis_keys=JobKeys()
    )

    # Should detect cancellation
    assert ctx.should_cancel() is True

    # Test with non-cancelled job
    job_id2 = publish(
        func=opts.execution_test_job,
        payload={'test': 'normal'},
        channel="exec_test"
    )

    ctx2 = JobContext(
        job_id=job_id2,
        channel="exec_test",
        payload={'test': 'normal'},
        redis_adapter=get_adapter(),
        redis_keys=JobKeys()
    )

    assert ctx2.should_cancel() is False


@th.django_unit_test()
def test_job_context_model_access(opts):
    """Test JobContext access to job model."""
    from mojo.apps.jobs import publish
    from mojo.apps.jobs.models import Job
    from mojo.apps.jobs.context import JobContext

    # Create a job in database
    job_id = publish(
        func=opts.execution_test_job,
        payload={'test': 'model_access'},
        channel="exec_test"
    )

    # Create context
    ctx = JobContext(
        job_id=job_id,
        channel="exec_test",
        payload={'test': 'model_access'}
    )

    # Get model
    job_model = ctx.get_model()
    assert job_model is not None
    assert isinstance(job_model, Job)
    assert job_model.id == job_id
    assert job_model.channel == "exec_test"

    # Should cache the model
    job_model2 = ctx.get_model()
    assert job_model2 is job_model  # Same instance

    # Test with non-existent job
    ctx_missing = JobContext(
        job_id="nonexistent123",
        channel="test",
        payload={}
    )
    missing_model = ctx_missing.get_model()
    assert missing_model is None


@th.django_unit_test()
def test_job_engine_initialization(opts):
    """Test JobEngine initialization."""
    from mojo.apps.jobs.job_engine import JobEngine

    # Create engine
    engine = JobEngine(
        channels=['test1', 'test2'],
        runner_id='test_runner_001'
    )

    assert engine.runner_id == 'test_runner_001'
    assert engine.channels == ['test1', 'test2']
    assert engine.running is False
    assert engine.jobs_processed == 0
    assert engine.jobs_failed == 0

    # Test auto-generated runner ID
    engine2 = JobEngine(channels=['test'])
    assert engine2.runner_id is not None
    assert '-' in engine2.runner_id  # Format: hostname-pid-random


@th.django_unit_test()
def test_job_direct_execution(opts):
    """Test direct execution of job functions."""
    from mojo.apps.jobs.context import JobContext

    # Create a context
    ctx = JobContext(
        job_id="direct_exec_123",
        channel="exec_test",
        payload={'message': 'Hello', 'value': 42}
    )

    # Reset execution count
    opts.execution_count = 0

    # Execute job directly
    result = opts.execution_test_job(ctx)

    assert result == "success"
    assert opts.execution_count == 1
    assert "direct_exec_123" in opts.execution_results

    exec_result = opts.execution_results["direct_exec_123"]
    assert exec_result['executed'] is True
    assert exec_result['payload']['message'] == 'Hello'
    assert exec_result['job_id'] == "direct_exec_123"


@th.django_unit_test()
def test_job_error_handling(opts):
    """Test job error handling and recording."""
    from mojo.apps.jobs import publish
    from mojo.apps.jobs.models import Job
    from mojo.apps.jobs.context import JobContext
    from mojo.apps.jobs.job_engine import JobEngine

    # Publish a job that will error
    job_id = publish(
        func=opts.error_job,
        payload={'error_message': 'Deliberate test error'},
        channel="exec_test",
        max_retries=0  # Don't retry
    )

    # Create context
    ctx = JobContext(
        job_id=job_id,
        channel="exec_test",
        payload={'error_message': 'Deliberate test error'}
    )

    # Try to execute (should raise)
    try:
        opts.error_job(ctx)
        assert False, "Should have raised exception"
    except Exception as e:
        assert str(e) == 'Deliberate test error'


@th.django_unit_test()
def test_retry_configuration(opts):
    """Test retry configuration and backoff settings."""
    from mojo.apps.jobs import publish
    from mojo.apps.jobs.models import Job

    # Publish with custom retry settings
    job_id = publish(
        func=opts.retry_test_job,
        payload={'test': 'retry_config'},
        channel="retry_test",
        max_retries=5,
        backoff_base=2.5,
        backoff_max=120
    )

    # Check job configuration
    job = Job.objects.get(id=job_id)
    assert job.max_retries == 5
    assert job.backoff_base == 2.5
    assert job.backoff_max_sec == 120
    assert job.attempt == 0


@th.django_unit_test()
def test_job_expiration_check(opts):
    """Test job expiration detection."""
    from mojo.apps.jobs import publish
    from mojo.apps.jobs.models import Job
    from mojo.apps.jobs.job_engine import JobEngine

    # Create an already-expired job
    past_time = timezone.now() - timedelta(minutes=1)

    job_id = publish(
        func=opts.execution_test_job,
        payload={'expired': True},
        channel="exec_test",
        expires_at=past_time  # Already expired
    )

    job = Job.objects.get(id=job_id)
    assert job.expires_at < timezone.now()

    # Create engine to test expiration check
    engine = JobEngine(channels=['exec_test'])

    # Load job and check expiration
    job_data = {
        'expires_at': job.expires_at.isoformat(),
        'status': 'pending'
    }

    is_expired = engine._is_expired(job_data)
    assert is_expired is True


@th.django_unit_test()
def test_broadcast_job_setup(opts):
    """Test broadcast job configuration."""
    from mojo.apps.jobs import publish
    from mojo.apps.jobs.models import Job
    from mojo.apps.jobs.adapters import get_adapter
    from mojo.apps.jobs.keys import JobKeys

    # Publish broadcast job
    job_id = publish(
        func=opts.broadcast_test_job,
        payload={'runner_id': 'test_runner_1'},
        channel="broadcast_test",
        broadcast=True
    )

    # Check database
    job = Job.objects.get(id=job_id)
    assert job.broadcast is True
    assert job.channel == "broadcast_test"

    # Check Redis (job metadata)
    redis = get_adapter()
    keys = JobKeys()
    job_data = redis.hgetall(keys.job(job_id))
    assert job_data.get('broadcast') == '1'


@th.django_unit_test()
def test_job_max_exec_seconds(opts):
    """Test max execution seconds configuration."""
    from mojo.apps.jobs import publish
    from mojo.apps.jobs.models import Job

    # Publish with execution time limit
    job_id = publish(
        func=opts.long_running_job,
        payload={'test': 'timeout'},
        channel="exec_test",
        max_exec_seconds=30
    )

    job = Job.objects.get(id=job_id)
    assert job.max_exec_seconds == 30


@th.django_unit_test()
def test_job_event_recording(opts):
    """Test that job events are properly recorded."""
    from mojo.apps.jobs import publish, cancel
    from mojo.apps.jobs.models import Job, JobEvent

    # Create a job
    job_id = publish(
        func=opts.execution_test_job,
        payload={'events': 'test'},
        channel="exec_test",
        delay=2  # Schedule for later
    )

    # Get job
    job = Job.objects.get(id=job_id)

    # Check events
    events = JobEvent.objects.filter(job=job).order_by('at')
    assert events.count() >= 2  # Should have 'created' and 'scheduled'

    # Check event types
    event_types = [e.event for e in events]
    assert 'created' in event_types
    assert 'scheduled' in event_types

    # Cancel the job
    cancel(job_id)

    # Should have cancel event
    events = JobEvent.objects.filter(job=job).order_by('at')
    event_types = [e.event for e in events]
    assert 'canceled' in event_types


@th.django_unit_test()
def test_job_metadata_persistence(opts):
    """Test that job metadata is persisted correctly."""
    from mojo.apps.jobs import publish
    from mojo.apps.jobs.models import Job
    from mojo.apps.jobs.context import JobContext
    from mojo.apps.jobs.adapters import get_adapter
    from mojo.apps.jobs.keys import JobKeys

    # Publish job
    job_id = publish(
        func=opts.metadata_test_job,
        payload={'test': 'metadata', 'items': [1, 2, 3]},
        channel="exec_test"
    )

    # Create context and set metadata
    redis = get_adapter()
    keys = JobKeys()

    ctx = JobContext(
        job_id=job_id,
        channel="exec_test",
        payload={'test': 'metadata', 'items': [1, 2, 3]},
        redis_adapter=redis,
        redis_keys=keys
    )

    # Execute job to set metadata
    result = opts.metadata_test_job(ctx)
    assert result == "metadata_processed"

    # Check metadata was set in Redis
    job_data = redis.hgetall(keys.job(job_id))
    if 'metadata' in job_data:
        import json
        metadata = json.loads(job_data['metadata'])
        assert 'start_time' in metadata
        assert metadata['input_count'] == 2  # Two keys in payload
        assert metadata['test_flag'] is True
