"""
Core Jobs Engine Tests - Basic functionality verification.

Tests the essential job engine operations without complex scenarios.
"""
from testit import helpers as th
import time
import uuid
from datetime import datetime, timedelta
from django.utils import timezone


@th.django_unit_setup()
def setup_core_engine_tests(opts):
    """Setup for core engine tests."""
    from mojo.apps.jobs.models import Job, JobEvent
    from mojo.apps.jobs.adapters import get_adapter
    from mojo.apps.jobs.keys import JobKeys

    # Clear test data - using test-specific job names for cleanup
    Job.objects.filter(func__contains='test_core_engine').delete()
    JobEvent.objects.filter(channel='default').delete()

    # Setup Redis and keys
    opts.redis = get_adapter()
    opts.keys = JobKeys()
    opts.test_channel = 'default'

    # Clear Redis test data
    test_keys = [
        opts.keys.queue(opts.test_channel),
        opts.keys.processing(opts.test_channel),
        opts.keys.sched(opts.test_channel),
        opts.keys.sched_broadcast(opts.test_channel)
    ]

    for key in test_keys:
        opts.redis.delete(key)


@th.django_unit_test()
def test_basic_job_publish_and_queue(opts):
    """Test basic job publishing creates correct Redis structures."""
    from mojo.apps.jobs import publish
    from mojo.apps.jobs.models import Job

    # Publish immediate job
    job_id = publish(
        func="mojo.apps.jobs.examples.sample_jobs.send_email",
        payload={"recipients": ["test@example.com"], "subject": "Test"},
        channel=opts.test_channel
    )

    # Verify job in database
    job = Job.objects.get(id=job_id)
    assert job.status == 'pending', f"Expected status 'pending', got '{job.status}'"
    assert job.channel == opts.test_channel, f"Expected channel '{opts.test_channel}', got '{job.channel}'"
    assert job.func == "mojo.apps.jobs.examples.sample_jobs.send_email", f"Expected func 'mojo.apps.jobs.examples.sample_jobs.send_email', got '{job.func}'"

    # Verify job in Redis queue (Plan B)
    queue_key = opts.keys.queue(opts.test_channel)
    qlen = opts.redis.llen(queue_key)
    assert qlen >= 1, f"Job not found in Redis queue: {queue_key}"

    # Check the job ID is in the queue
    queue_items = opts.redis.get_client().lrange(queue_key, 0, -1)
    assert job_id.encode('utf-8') in queue_items or job_id in queue_items, f"Job ID {job_id} not found in queue items: {queue_items}"


@th.django_unit_test()
def test_scheduled_job_publish_and_zset(opts):
    """Test scheduled job publishing goes to correct ZSET."""
    from mojo.apps.jobs import publish
    from mojo.apps.jobs.models import Job

    # Publish delayed job
    delay_seconds = 30
    job_id = publish(
        func="mojo.apps.jobs.examples.sample_jobs.send_email",
        payload={"recipients": ["delayed@example.com"]},
        channel=opts.test_channel,
        delay=delay_seconds
    )

    # Verify job in database with run_at
    job = Job.objects.get(id=job_id)
    assert job.run_at is not None, f"Expected run_at to be set, got {job.run_at}"
    assert job.status == 'pending', f"Expected status 'pending', got '{job.status}'"

    # Verify job in scheduled ZSET
    sched_key = opts.keys.sched(opts.test_channel)
    score = opts.redis.zscore(sched_key, job_id)
    assert score is not None, f"Scheduled job not found in ZSET: {sched_key}"

    # Score should be roughly run_at timestamp in ms
    expected_score = job.run_at.timestamp() * 1000
    assert abs(score - expected_score) < 1000, f"Score mismatch: expected ~{expected_score}, got {score}"


@th.django_unit_test()
def test_engine_initialization(opts):
    """Test JobEngine initializes correctly."""
    from mojo.apps.jobs.job_engine import JobEngine

    # Create engine
    engine = JobEngine(channels=[opts.test_channel])

    assert engine.channels == [opts.test_channel], f"Expected channels {[opts.test_channel]}, got {engine.channels}"
    assert engine.runner_id is not None, f"Expected runner_id to be set, got {engine.runner_id}"
    assert engine.max_workers > 0, f"Expected max_workers > 0, got {engine.max_workers}"
    assert not engine.running, f"Expected engine.running to be False, got {engine.running}"
    assert not engine.is_initialized, f"Expected engine.is_initialized to be False, got {engine.is_initialized}"

    # Initialize
    engine.initialize()

    assert engine.is_initialized, f"Expected engine.is_initialized to be True after initialize(), got {engine.is_initialized}"
    assert engine.running, f"Expected engine.running to be True after initialize(), got {engine.running}"
    assert engine.start_time is not None, f"Expected engine.start_time to be set after initialize(), got {engine.start_time}"

    # Clean up
    engine.stop()
    assert not engine.running, f"Expected engine.running to be False after stop(), got {engine.running}"


@th.django_unit_test()
def test_scheduler_initialization(opts):
    """Test Scheduler initializes correctly."""
    from mojo.apps.jobs.scheduler import Scheduler

    # Create scheduler
    scheduler = Scheduler(channels=[opts.test_channel])

    assert scheduler.channels == [opts.test_channel], f"Expected channels {[opts.test_channel]}, got {scheduler.channels}"
    assert scheduler.scheduler_id is not None, f"Expected scheduler_id to be set, got {scheduler.scheduler_id}"
    assert not scheduler.running, f"Expected scheduler.running to be False, got {scheduler.running}"
    assert not scheduler.has_lock, f"Expected scheduler.has_lock to be False, got {scheduler.has_lock}"

    # Don't actually start (would need lock), just verify setup
    assert scheduler.lock_key is not None, f"Expected scheduler.lock_key to be set, got {scheduler.lock_key}"
    assert scheduler.lock_ttl_ms > 0, f"Expected scheduler.lock_ttl_ms > 0, got {scheduler.lock_ttl_ms}"


@th.django_unit_test()
def test_job_manager_basic_operations(opts):
    """Test JobManager basic operations."""
    from mojo.apps.jobs.manager import get_manager
    from mojo.apps.jobs import publish

    manager = get_manager()

    # Test basic stats
    stats = manager.get_stats()
    assert 'channels' in stats, f"Expected 'channels' in stats, got keys: {list(stats.keys())}"
    assert 'runners' in stats, f"Expected 'runners' in stats, got keys: {list(stats.keys())}"
    assert 'totals' in stats, f"Expected 'totals' in stats, got keys: {list(stats.keys())}"

    # Test queue state
    state = manager.get_queue_state(opts.test_channel)
    assert state['channel'] == opts.test_channel, f"Expected channel '{opts.test_channel}', got '{state['channel']}'"
    assert 'queued_count' in state, f"Expected 'queued_count' in state, got keys: {list(state.keys())}"
    assert 'inflight_count' in state, f"Expected 'inflight_count' in state, got keys: {list(state.keys())}"
    assert 'scheduled_count' in state, f"Expected 'scheduled_count' in state, got keys: {list(state.keys())}"

    # Publish a job and check state changes
    job_id = publish(
        func="mojo.apps.jobs.examples.sample_jobs.send_email",
        payload={"test": "manager"},
        channel=opts.test_channel
    )

    updated_state = manager.get_queue_state(opts.test_channel)
    assert updated_state['queued_count'] >= 1, f"Expected queued_count >= 1 after publishing job, got {updated_state['queued_count']}"


@th.django_unit_test()
def test_simple_job_execution_flow(opts):
    """Test the basic job execution flow."""
    from mojo.apps.jobs.job_engine import JobEngine
    from mojo.apps.jobs import publish
    from mojo.apps.jobs.models import Job

    # Create simple test job function result tracker
    execution_results = []

    def simple_test_job(job):
        """Simple job for testing execution."""
        message = job.payload.get('message', 'default')
        execution_results.append(f"executed: {message}")
        job.metadata['test_executed'] = True
        job.metadata['message_received'] = message
        return "completed"

    # Publish job
    job_id = publish(
        func="tests.test_jobs.test_core_engine.simple_test_job",  # Won't actually load, but tests structure
        payload={"message": "test execution"},
        channel=opts.test_channel
    )

    # Create engine
    engine = JobEngine(channels=[opts.test_channel])

    # Simulate job execution directly (without starting full engine)
    job = Job.objects.get(id=job_id)

    # Mark as running
    job.status = 'running'
    job.started_at = timezone.now()
    job.runner_id = 'test_runner'
    job.attempt = 1
    job.save()

    # Execute the job function
    result = simple_test_job(job)

    # Mark as completed (add small delay to ensure duration > 0)
    time.sleep(0.001)  # 1ms delay to ensure measurable duration
    job.status = 'completed'
    job.finished_at = timezone.now()
    job.save()

    # Verify execution with better debug info
    assert result == "completed", f"Expected 'completed', got '{result}'"
    assert job.metadata.get('test_executed') is True, f"test_executed not set in metadata: {job.metadata}"
    assert job.metadata.get('message_received') == "test execution", f"Expected 'test execution', got '{job.metadata.get('message_received')}'"
    assert job.status == 'completed', f"Expected status 'completed', got '{job.status}'"
    assert job.duration_ms > 0, f"Job duration should be > 0, got {job.duration_ms}ms. Started: {job.started_at}, Finished: {job.finished_at}"


@th.django_unit_test()
def test_job_cancellation_flow(opts):
    """Test job cancellation functionality."""
    from mojo.apps.jobs import publish, cancel
    from mojo.apps.jobs.models import Job, JobEvent

    # Publish job
    job_id = publish(
        func="mojo.apps.jobs.examples.sample_jobs.send_email",
        payload={"recipients": ["cancel@test.com"]},
        channel=opts.test_channel
    )

    # Cancel job
    result = cancel(job_id)
    assert result is True, f"Expected cancel to return True, got {result}"

    # Verify cancellation in DB
    job = Job.objects.get(id=job_id)
    assert job.cancel_requested is True, f"Expected job.cancel_requested to be True, got {job.cancel_requested}"

    # Verify cancel event created
    cancel_events = JobEvent.objects.filter(job=job, event='canceled')
    assert cancel_events.exists(), f"Expected cancel event to exist, found {cancel_events.count()} events"


@th.django_unit_test()
def test_job_retry_configuration(opts):
    """Test job retry settings are properly stored."""
    from mojo.apps.jobs import publish
    from mojo.apps.jobs.models import Job

    job_id = publish(
        func="mojo.apps.jobs.examples.sample_jobs.fetch_external_api",
        payload={"url": "https://api.example.com/test"},
        channel=opts.test_channel,
        max_retries=5,
        backoff_base=1.5,
        backoff_max=300
    )

    job = Job.objects.get(id=job_id)
    assert job.max_retries == 5, f"Expected max_retries=5, got {job.max_retries}"
    assert job.backoff_base == 1.5, f"Expected backoff_base=1.5, got {job.backoff_base}"
    assert job.backoff_max_sec == 300, f"Expected backoff_max_sec=300, got {job.backoff_max_sec}"

    # Test is_retriable logic: first mark job as failed to test retry capability
    job.status = 'failed'
    job.attempt = 2  # Has made some attempts but still under max_retries
    job.save()
    assert job.is_retriable, f"Expected job.is_retriable to be True for failed job (attempt={job.attempt}, max_retries={job.max_retries}), got {job.is_retriable}"

    # Test when max retries exceeded
    job.attempt = 5  # Equal to max_retries
    job.save()
    assert not job.is_retriable, f"Expected job.is_retriable to be False when attempt >= max_retries (attempt={job.attempt}, max_retries={job.max_retries}), got {job.is_retriable}"


@th.django_unit_test()
def test_scheduler_job_movement(opts):
    """Test scheduler moving due jobs from ZSET to queue."""
    from mojo.apps.jobs import publish
    from mojo.apps.jobs.scheduler import Scheduler
    from mojo.apps.jobs.models import Job

    # Create a job scheduled for future execution
    future_time = timezone.now() + timedelta(seconds=10)

    # Publish with specific run_at time in the future
    job_id = publish(
        func="mojo.apps.jobs.examples.sample_jobs.send_email",
        payload={"test": "scheduler"},
        channel=opts.test_channel,
        run_at=future_time
    )

    # Verify it's in the ZSET
    sched_key = opts.keys.sched(opts.test_channel)
    score = opts.redis.zscore(sched_key, job_id)
    assert score is not None, "Job should be in scheduled ZSET"

    # Create scheduler and process this channel
    scheduler = Scheduler(channels=[opts.test_channel])

    # Simulate time passing - process with a future time to make the job due
    future_now = future_time + timedelta(seconds=1)  # 1 second after the job's run_at time
    future_now_ms = future_now.timestamp() * 1000

    # Process the channel (this would normally be called in main loop)
    scheduler._process_channel(opts.test_channel, future_now, future_now_ms)

    # Verify job moved to queue
    queue_key = opts.keys.queue(opts.test_channel)
    queue_items = opts.redis.get_client().lrange(queue_key, 0, -1)
    job_id_bytes = job_id.encode('utf-8')
    assert job_id_bytes in queue_items or job_id in queue_items, f"Job {job_id} should be moved to queue. Queue items: {queue_items}"

    # Verify removed from ZSET
    score_after = opts.redis.zscore(sched_key, job_id)
    assert score_after is None, f"Job {job_id} should be removed from ZSET after scheduling, but still has score {score_after}"


@th.django_unit_test()
def test_broadcast_job_handling(opts):
    """Test broadcast job publishing and routing."""
    from mojo.apps.jobs import publish
    from mojo.apps.jobs.models import Job

    # Publish broadcast job
    job_id = publish(
        func="mojo.apps.jobs.examples.sample_jobs.send_email",
        payload={"recipients": ["broadcast@test.com"]},
        channel=opts.test_channel,
        broadcast=True
    )

    # Verify job marked as broadcast in DB
    job = Job.objects.get(id=job_id)
    assert job.broadcast is True, f"Expected job.broadcast to be True, got {job.broadcast}"

    # For immediate broadcast jobs, should still go to regular queue in Plan B
    queue_key = opts.keys.queue(opts.test_channel)
    qlen = opts.redis.llen(queue_key)
    assert qlen >= 1, f"Expected broadcast job to be in queue (qlen >= 1), got qlen={qlen} for queue {queue_key}"


@th.django_unit_test()
def test_job_status_api(opts):
    """Test job status retrieval."""
    from mojo.apps.jobs import publish, status
    from mojo.apps.jobs.models import Job

    job_id = publish(
        func="mojo.apps.jobs.examples.sample_jobs.send_email",
        payload={"test": "status"},
        channel=opts.test_channel
    )

    # Get status
    job_status = status(job_id)

    assert job_status is not None, f"Expected job status to be returned, got None"
    assert job_status['id'] == job_id, f"Expected job status id '{job_id}', got '{job_status.get('id')}'"
    assert job_status['status'] == 'pending', f"Expected job status 'pending', got '{job_status.get('status')}'"
    assert job_status['channel'] == opts.test_channel, f"Expected job channel '{opts.test_channel}', got '{job_status.get('channel')}'"
    assert job_status['func'] == "mojo.apps.jobs.examples.sample_jobs.send_email", f"Expected func 'mojo.apps.jobs.examples.sample_jobs.send_email', got '{job_status.get('func')}'"


@th.django_unit_test()
def test_redis_key_patterns(opts):
    """Test that Redis keys follow expected patterns."""
    from mojo.apps.jobs.keys import JobKeys

    keys = JobKeys()

    # Test key patterns
    queue_key = keys.queue(opts.test_channel)
    assert f"queue:{opts.test_channel}" in queue_key, f"Expected 'queue:{opts.test_channel}' in '{queue_key}'"

    sched_key = keys.sched(opts.test_channel)
    assert f"sched:{opts.test_channel}" in sched_key, f"Expected 'sched:{opts.test_channel}' in '{sched_key}'"

    processing_key = keys.processing(opts.test_channel)
    assert f"processing:{opts.test_channel}" in processing_key, f"Expected 'processing:{opts.test_channel}' in '{processing_key}'"

    # Test runner keys
    runner_id = "test_runner_123"
    hb_key = keys.runner_hb(runner_id)
    assert f"runner:{runner_id}:hb" in hb_key, f"Expected 'runner:{runner_id}:hb' in '{hb_key}'"

    ctl_key = keys.runner_ctl(runner_id)
    assert f"runner:{runner_id}:ctl" in ctl_key, f"Expected 'runner:{runner_id}:ctl' in '{ctl_key}'"


@th.django_unit_test()
def test_cleanup_core_engine(opts):
    """Clean up test data."""
    from mojo.apps.jobs.models import Job, JobEvent

    # Clean up database
    Job.objects.filter(channel__startswith='test_').delete()

    # Clean up Redis
    test_keys = [
        opts.keys.queue(opts.test_channel),
        opts.keys.processing(opts.test_channel),
        opts.keys.sched(opts.test_channel),
        opts.keys.sched_broadcast(opts.test_channel)
    ]

    for key in test_keys:
        opts.redis.delete(key)
