from testit import helpers as th
import time
from datetime import datetime, timedelta
from django.utils import timezone


@th.django_unit_setup()
def setup_verify_environment(opts):
    """Quick setup for verification tests."""
    from mojo.apps.jobs.models import Job, JobEvent
    from mojo.apps.jobs.registry import clear_registries
    from mojo.apps.jobs import async_job

    # Clear any existing test data
    Job.objects.filter(channel='verify_test').delete()
    clear_registries()

    # Register a simple test job
    @async_job(channel="verify_test")
    def verify_job(ctx):
        """Simple verification job."""
        ctx.set_metadata(verified=True, timestamp=datetime.now().isoformat())
        return f"Verified: {ctx.payload.get('message', 'OK')}"

    opts.verify_job = verify_job
    opts.verify_count = 0


@th.django_unit_test()
def test_basic_components_available(opts):
    """Verify all basic job components are importable."""
    try:
        from mojo.apps.jobs import publish, cancel, status, async_job, JobContext
        from mojo.apps.jobs.models import Job, JobEvent
        from mojo.apps.jobs.adapters import get_adapter
        from mojo.apps.jobs.keys import JobKeys
        from mojo.apps.jobs.registry import get_job_function, list_jobs
        from mojo.apps.jobs.manager import JobManager
        from mojo.apps.jobs.scheduler import Scheduler
        from mojo.apps.jobs.job_engine import JobEngine
        from mojo.apps.jobs.local_queue import get_local_queue

        assert publish is not None, "publish function not available"
        assert cancel is not None, "cancel function not available"
        assert status is not None, "status function not available"
        assert async_job is not None, "async_job decorator not available"
        assert JobContext is not None, "JobContext not available"

        # Verify models
        assert Job._meta.db_table == 'jobs_job', "Job model table incorrect"
        assert JobEvent._meta.db_table == 'jobs_jobevent', "JobEvent model table incorrect"

    except ImportError as e:
        assert False, f"Failed to import job components: {e}"


@th.django_unit_test()
def test_redis_connectivity(opts):
    """Verify Redis connection is working."""
    from mojo.apps.jobs.adapters import get_adapter

    adapter = get_adapter()
    assert adapter.ping() is True, "Redis connection failed"

    # Test basic operations
    test_key = "verify:test:key"
    adapter.set(test_key, "test_value", ex=10)
    value = adapter.get(test_key)
    assert value == "test_value", f"Redis get/set failed: got {value}"

    adapter.delete(test_key)
    assert adapter.get(test_key) is None, "Redis delete failed"


@th.django_unit_test()
def test_job_publish_and_status(opts):
    """Verify basic job publishing and status checking."""
    from mojo.apps.jobs import publish, status
    from mojo.apps.jobs.models import Job

    # Publish a simple job
    job_id = publish(
        func=opts.verify_job,
        payload={'message': 'Verification Test'},
        channel='verify_test'
    )

    # Verify job ID format
    assert len(job_id) == 32, f"Invalid job ID length: {len(job_id)}"
    assert all(c in '0123456789abcdef' for c in job_id), "Invalid job ID format"

    # Check status
    job_status = status(job_id)
    assert job_status is not None, "Job status not found"
    assert job_status['id'] == job_id, "Job ID mismatch"
    assert job_status['status'] == 'pending', f"Unexpected status: {job_status['status']}"
    assert job_status['channel'] == 'verify_test', "Channel mismatch"

    # Verify in database
    job = Job.objects.get(id=job_id)
    assert job.channel == 'verify_test', "Database channel mismatch"
    assert job.payload['message'] == 'Verification Test', "Payload not saved correctly"

    opts.verify_count += 1


@th.django_unit_test()
def test_job_registry(opts):
    """Verify job registry is working."""
    from mojo.apps.jobs.registry import list_jobs, get_job_function

    # List jobs
    jobs = list_jobs()

    # Our verify_job should be registered
    verify_job_name = 'test_jobs.verify.verify_job'
    assert verify_job_name in jobs, f"Verify job not in registry: {list(jobs.keys())}"

    # Get the function
    func = get_job_function(verify_job_name)
    assert func is not None, "Could not retrieve job function from registry"
    assert func == opts.verify_job, "Retrieved wrong function from registry"

    # Check metadata
    job_meta = jobs[verify_job_name]
    assert job_meta['channel'] == 'verify_test', "Channel metadata incorrect"
    assert job_meta['broadcast'] is False, "Broadcast metadata incorrect"


@th.django_unit_test()
def test_job_cancellation(opts):
    """Verify job cancellation works."""
    from mojo.apps.jobs import publish, cancel, status
    from mojo.apps.jobs.models import Job

    # Publish a job
    job_id = publish(
        func=opts.verify_job,
        payload={'cancel_test': True},
        channel='verify_test'
    )

    # Cancel it
    result = cancel(job_id)
    assert result is True, "Cancel returned False"

    # Check cancellation flag
    job = Job.objects.get(id=job_id)
    assert job.cancel_requested is True, "Cancel flag not set in database"

    # Status should still work
    job_status = status(job_id)
    assert job_status is not None, "Status failed after cancel"

    opts.verify_count += 1


@th.django_unit_test()
def test_local_queue(opts):
    """Verify local queue is working."""
    from mojo.apps.jobs import local_async_job, publish_local
    from mojo.apps.jobs.local_queue import get_local_queue

    # Register a local job
    @local_async_job()
    def local_verify_job(message):
        """Local verification job."""
        opts.verify_count += 1
        return f"Local: {message}"

    # Publish to local queue
    job_id = publish_local(local_verify_job, "Verify Local")
    assert job_id.startswith("local-"), f"Invalid local job ID: {job_id}"

    # Give it time to process
    time.sleep(0.5)

    # Check queue stats
    queue = get_local_queue()
    stats = queue.stats()
    assert stats['processed'] >= 1, "Local job not processed"

    opts.verify_count += 1


@th.django_unit_test()
def test_delayed_job(opts):
    """Verify delayed job scheduling."""
    from mojo.apps.jobs import publish
    from mojo.apps.jobs.models import Job
    from mojo.apps.jobs.adapters import get_adapter
    from mojo.apps.jobs.keys import JobKeys

    # Publish with delay
    delay_seconds = 5
    job_id = publish(
        func=opts.verify_job,
        payload={'delayed': True},
        channel='verify_test',
        delay=delay_seconds
    )

    # Check run_at is set
    job = Job.objects.get(id=job_id)
    assert job.run_at is not None, "run_at not set for delayed job"

    # Should be scheduled for ~5 seconds from now
    expected_run = timezone.now() + timedelta(seconds=delay_seconds)
    delta = abs((job.run_at - expected_run).total_seconds())
    assert delta < 1.0, f"Incorrect delay: expected ~{delay_seconds}s, got {delta}s difference"

    # Check Redis ZSET
    redis = get_adapter()
    keys = JobKeys()
    sched_key = keys.sched('verify_test')

    score = redis.get_client().zscore(sched_key, job_id)
    assert score is not None, "Job not in scheduled ZSET"

    # Clean up
    redis.get_client().zrem(sched_key, job_id)

    opts.verify_count += 1


@th.django_unit_test()
def test_manager_basic(opts):
    """Verify JobManager basic functionality."""
    from mojo.apps.jobs.manager import JobManager, get_manager

    # Get manager instance
    manager = get_manager()
    assert isinstance(manager, JobManager), "Invalid manager instance"

    # Test queue state (even if empty)
    state = manager.get_queue_state('verify_test')
    assert state is not None, "Queue state returned None"
    assert state['channel'] == 'verify_test', "Channel mismatch in queue state"
    assert 'stream_length' in state, "Missing stream_length in state"
    assert 'scheduled_count' in state, "Missing scheduled_count in state"

    # Test getting stats
    stats = manager.get_stats()
    assert stats is not None, "Stats returned None"
    assert 'totals' in stats, "Missing totals in stats"
    assert 'channels' in stats, "Missing channels in stats"

    opts.verify_count += 1


@th.django_unit_test()
def test_verification_summary(opts):
    """Final verification summary."""
    from mojo.apps.jobs.models import Job

    # Count test jobs created
    test_jobs = Job.objects.filter(channel='verify_test').count()

    print(f"\n{'='*60}")
    print(f"VERIFICATION COMPLETE")
    print(f"{'='*60}")
    print(f"✓ Components imported successfully")
    print(f"✓ Redis connection verified")
    print(f"✓ Job publishing works")
    print(f"✓ Job registry functional")
    print(f"✓ Job cancellation works")
    print(f"✓ Local queue operational")
    print(f"✓ Delayed jobs work")
    print(f"✓ JobManager functional")
    print(f"")
    print(f"Verification checks passed: {opts.verify_count}")
    print(f"Test jobs created: {test_jobs}")
    print(f"{'='*60}\n")

    assert opts.verify_count >= 7, f"Not all verifications ran: {opts.verify_count}/7"

    # Clean up test jobs
    Job.objects.filter(channel='verify_test').delete()
