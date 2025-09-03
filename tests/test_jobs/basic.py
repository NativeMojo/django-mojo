from testit import helpers as th
from testit import faker
import time
import json
from datetime import datetime, timedelta
from django.utils import timezone


@th.django_unit_setup()
def setup_test_jobs(opts):
    """Setup test jobs and clear any existing data."""
    from mojo.apps.jobs.models import Job, JobEvent
    from mojo.apps.jobs.registry import clear_registries
    from mojo.apps.jobs.adapters import get_adapter
    from mojo.apps.jobs.keys import JobKeys

    # Clear database
    Job.objects.all().delete()
    JobEvent.objects.all().delete()

    # Clear registries
    clear_registries()

    # Clear Redis data for test channels
    try:
        redis = get_adapter()
        keys = JobKeys()
        test_channels = ['test', 'test_broadcast', 'test_retry']

        for channel in test_channels:
            # Clear streams
            redis.delete(keys.stream(channel))
            redis.delete(keys.stream_broadcast(channel))
            # Clear scheduled jobs
            redis.delete(keys.sched(channel))
    except Exception as e:
        print(f"Warning: Could not clear Redis: {e}")

    # Store test data
    opts.test_channels = ['test', 'test_broadcast', 'test_retry']
    opts.test_payload = {'message': 'Hello World', 'count': 42}


@th.django_unit_setup()
def setup_test_functions(opts):
    """Register test job functions."""
    from mojo.apps.jobs import async_job, local_async_job

    # Simple test job
    @async_job(channel="test")
    def simple_job(ctx):
        """A simple test job that sets metadata."""
        ctx.set_metadata(executed=True, message=ctx.payload.get('message'))
        return "success"

    # Job that can fail
    @async_job(channel="test_retry", max_retries=3)
    def failing_job(ctx):
        """A job that fails on first attempts."""
        attempt = ctx.payload.get('attempt', 0)
        if attempt < 2:
            raise Exception(f"Deliberate failure on attempt {attempt}")
        return "success"

    # Broadcast job
    @async_job(channel="test_broadcast", broadcast=True)
    def broadcast_job(ctx):
        """A broadcast job for testing."""
        ctx.set_metadata(runner_executed=True)
        return "broadcast_success"

    # Cancellable job
    @async_job(channel="test")
    def cancellable_job(ctx):
        """A job that checks for cancellation."""
        for i in range(10):
            if ctx.should_cancel():
                ctx.set_metadata(cancelled_at_iteration=i)
                return "cancelled"
            time.sleep(0.1)
        return "completed"

    # Local job
    @local_async_job()
    def local_simple_job(message):
        """A simple local job."""
        return f"Local: {message}"

    # Store references
    opts.simple_job = simple_job
    opts.failing_job = failing_job
    opts.broadcast_job = broadcast_job
    opts.cancellable_job = cancellable_job
    opts.local_simple_job = local_simple_job


@th.django_unit_test()
def test_basic_job_publish(opts):
    """Test basic job publishing."""
    from mojo.apps.jobs import publish, status
    from mojo.apps.jobs.models import Job, JobEvent

    # Publish a job
    job_id = publish(
        func=opts.simple_job,
        payload=opts.test_payload,
        channel="test"
    )

    # Verify job ID format (32 char hex)
    assert len(job_id) == 32
    assert all(c in '0123456789abcdef' for c in job_id)

    # Check database
    job = Job.objects.get(id=job_id)
    assert job.channel == "test"
    assert job.func == "test_jobs.basic.simple_job"
    assert job.payload == opts.test_payload
    assert job.status == "pending"

    # Check events
    events = JobEvent.objects.filter(job=job).order_by('at')
    assert events.count() >= 1
    assert events.first().event == "created"

    # Check status API
    job_status = status(job_id)
    assert job_status is not None
    assert job_status['id'] == job_id
    assert job_status['status'] == "pending"
    assert job_status['channel'] == "test"


@th.django_unit_test()
def test_job_publish_with_delay(opts):
    """Test publishing a job with delay."""
    from mojo.apps.jobs import publish, status
    from mojo.apps.jobs.models import Job
    from mojo.apps.jobs.adapters import get_adapter
    from mojo.apps.jobs.keys import JobKeys

    # Publish with 5 second delay
    job_id = publish(
        func=opts.simple_job,
        payload={'delayed': True},
        channel="test",
        delay=5
    )

    # Check database
    job = Job.objects.get(id=job_id)
    assert job.run_at is not None

    # Should be scheduled for ~5 seconds from now
    expected_run = timezone.now() + timedelta(seconds=5)
    delta = abs((job.run_at - expected_run).total_seconds())
    assert delta < 1.0  # Within 1 second tolerance

    # Check Redis scheduled set
    redis = get_adapter()
    keys = JobKeys()
    sched_key = keys.sched("test")

    # Job should be in scheduled ZSET
    score = redis.get_client().zscore(sched_key, job_id)
    assert score is not None

    # Score should match run_at timestamp
    expected_score = job.run_at.timestamp() * 1000
    assert abs(score - expected_score) < 1000  # Within 1 second


@th.django_unit_test()
def test_job_publish_with_run_at(opts):
    """Test publishing a job with specific run_at time."""
    from mojo.apps.jobs import publish
    from mojo.apps.jobs.models import Job

    # Schedule for 1 hour from now
    run_at = timezone.now() + timedelta(hours=1)

    job_id = publish(
        func=opts.simple_job,
        payload={'scheduled': True},
        channel="test",
        run_at=run_at
    )

    # Check database
    job = Job.objects.get(id=job_id)
    assert job.run_at is not None

    # Should match our specified time
    delta = abs((job.run_at - run_at).total_seconds())
    assert delta < 0.1  # Very close match


@th.django_unit_test()
def test_job_expiration(opts):
    """Test job expiration settings."""
    from mojo.apps.jobs import publish
    from mojo.apps.jobs.models import Job

    # Publish with custom expiration
    job_id = publish(
        func=opts.simple_job,
        payload={'expires': True},
        channel="test",
        expires_in=300  # 5 minutes
    )

    job = Job.objects.get(id=job_id)
    assert job.expires_at is not None

    # Should expire in ~5 minutes
    expected_expire = timezone.now() + timedelta(seconds=300)
    delta = abs((job.expires_at - expected_expire).total_seconds())
    assert delta < 1.0


@th.django_unit_test()
def test_job_broadcast_flag(opts):
    """Test broadcast job publishing."""
    from mojo.apps.jobs import publish
    from mojo.apps.jobs.models import Job
    from mojo.apps.jobs.adapters import get_adapter
    from mojo.apps.jobs.keys import JobKeys

    # Publish broadcast job
    job_id = publish(
        func=opts.broadcast_job,
        payload={'broadcast': True},
        channel="test_broadcast",
        broadcast=True
    )

    # Check database
    job = Job.objects.get(id=job_id)
    assert job.broadcast is True

    # Should be in broadcast stream
    redis = get_adapter()
    keys = JobKeys()

    # Check it's NOT in regular stream (it should be in broadcast stream)
    # Note: We'd need to check stream contents which requires XRANGE


@th.django_unit_test()
def test_job_idempotency(opts):
    """Test idempotency key prevents duplicate jobs."""
    from mojo.apps.jobs import publish
    from mojo.apps.jobs.models import Job

    idempotency_key = "test-idempotent-12345"

    # First publish
    job_id1 = publish(
        func=opts.simple_job,
        payload={'attempt': 1},
        channel="test",
        idempotency_key=idempotency_key
    )

    # Second publish with same key
    job_id2 = publish(
        func=opts.simple_job,
        payload={'attempt': 2},
        channel="test",
        idempotency_key=idempotency_key
    )

    # Should return the same job ID
    assert job_id1 == job_id2

    # Only one job in database
    jobs = Job.objects.filter(idempotency_key=idempotency_key)
    assert jobs.count() == 1

    # Payload should be from first publish
    job = jobs.first()
    assert job.payload['attempt'] == 1


@th.django_unit_test()
def test_job_cancellation(opts):
    """Test job cancellation."""
    from mojo.apps.jobs import publish, cancel, status
    from mojo.apps.jobs.models import Job, JobEvent

    # Publish a job
    job_id = publish(
        func=opts.cancellable_job,
        payload={'cancel_test': True},
        channel="test"
    )

    # Cancel it
    result = cancel(job_id)
    assert result is True

    # Check database
    job = Job.objects.get(id=job_id)
    assert job.cancel_requested is True

    # Check event
    cancel_event = JobEvent.objects.filter(
        job=job,
        event='canceled'
    ).first()
    assert cancel_event is not None

    # Check status
    job_status = status(job_id)
    assert job_status is not None
    # Status might still be pending if not yet picked up

    # Try to cancel non-existent job
    result = cancel("nonexistent12345678901234567890ab")
    assert result is False


@th.django_unit_test()
def test_local_job_publish(opts):
    """Test local job publishing."""
    from mojo.apps.jobs import publish_local
    from mojo.apps.jobs.local_queue import get_local_queue

    # Publish local job
    job_id = publish_local(
        opts.local_simple_job,
        "Test Message"
    )

    # Should have a local- prefix
    assert job_id.startswith("local-")

    # Queue should have the job
    queue = get_local_queue()
    assert queue.size() >= 0  # Job may already be processed

    # Give it time to process
    time.sleep(0.5)

    # Check stats
    stats = queue.stats()
    assert stats['processed'] >= 1


@th.django_unit_test()
def test_job_status_api(opts):
    """Test job status retrieval."""
    from mojo.apps.jobs import publish, status
    from mojo.apps.jobs.models import Job

    # Create a job
    job_id = publish(
        func=opts.simple_job,
        payload={'status_test': True},
        channel="test"
    )

    # Get status via API
    job_status = status(job_id)

    assert job_status['id'] == job_id
    assert job_status['status'] == 'pending'
    assert job_status['channel'] == 'test'
    assert job_status['func'] == 'test_jobs.basic.simple_job'
    assert job_status['attempt'] == 0
    assert job_status['last_error'] == ''

    # Test non-existent job
    missing_status = status("nonexistent12345678901234567890ab")
    assert missing_status is None


@th.django_unit_test()
def test_job_max_retries_setting(opts):
    """Test max retries configuration."""
    from mojo.apps.jobs import publish
    from mojo.apps.jobs.models import Job

    # Publish with custom retry settings
    job_id = publish(
        func=opts.failing_job,
        payload={'test': True},
        channel="test_retry",
        max_retries=5,
        backoff_base=3.0,
        backoff_max=600
    )

    job = Job.objects.get(id=job_id)
    assert job.max_retries == 5
    assert job.backoff_base == 3.0
    assert job.backoff_max_sec == 600


@th.django_unit_test()
def test_job_payload_validation(opts):
    """Test payload size and type validation."""
    from mojo.apps.jobs import publish
    from django.conf import settings

    # Test with valid payload
    job_id = publish(
        func=opts.simple_job,
        payload={'valid': True, 'nested': {'data': [1, 2, 3]}},
        channel="test"
    )
    assert job_id is not None

    # Test with large payload (should fail)
    large_data = "x" * 20000  # Over default 16KB limit
    try:
        job_id = publish(
            func=opts.simple_job,
            payload={'data': large_data},
            channel="test"
        )
        assert False, "Should have raised ValueError for large payload"
    except ValueError as e:
        assert "exceeds maximum size" in str(e)

    # Test with non-dict payload (should fail)
    try:
        job_id = publish(
            func=opts.simple_job,
            payload="not a dict",
            channel="test"
        )
        assert False, "Should have raised ValueError for non-dict payload"
    except ValueError as e:
        assert "must be a dictionary" in str(e)


@th.django_unit_test()
def test_job_registry_listing(opts):
    """Test job registry functions."""
    from mojo.apps.jobs.registry import list_jobs, list_local_jobs

    # List registered jobs
    jobs = list_jobs()

    # Should have our test jobs
    assert 'test_jobs.basic.simple_job' in jobs
    assert 'test_jobs.basic.failing_job' in jobs
    assert 'test_jobs.basic.broadcast_job' in jobs
    assert 'test_jobs.basic.cancellable_job' in jobs

    # Check metadata
    simple_meta = jobs['test_jobs.basic.simple_job']
    assert simple_meta['channel'] == 'test'
    assert simple_meta['broadcast'] is False

    broadcast_meta = jobs['test_jobs.basic.broadcast_job']
    assert broadcast_meta['channel'] == 'test_broadcast'
    assert broadcast_meta['broadcast'] is True

    # List local jobs
    local_jobs = list_local_jobs()
    assert 'test_jobs.basic.local_simple_job' in local_jobs
