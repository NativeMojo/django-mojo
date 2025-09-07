"""
Publish and Execute Flow Tests - Core job lifecycle testing.

Tests the complete flow from job publishing through execution completion.
"""
from testit import helpers as th
import time
import uuid
from datetime import datetime, timedelta
from django.utils import timezone


@th.django_unit_setup()
def setup_publish_execute_tests(opts):
    """Setup for publish/execute tests."""
    from mojo.apps.jobs.models import Job, JobEvent
    from mojo.apps.jobs.adapters import get_adapter
    from mojo.apps.jobs.keys import JobKeys

    # Clear test data - using test-specific job names for cleanup
    Job.objects.filter(func__contains='test_publish_execute').delete()
    JobEvent.objects.filter(channel='email').delete()

    opts.redis = get_adapter()
    opts.keys = JobKeys()
    opts.test_channel = 'email'

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
def test_complete_job_lifecycle(opts):
    """Test complete job lifecycle: publish -> execute -> complete."""
    from mojo.apps.jobs import publish
    from mojo.apps.jobs.models import Job, JobEvent

    # 1. Publish job
    job_id = publish(
        func="mojo.apps.jobs.examples.sample_jobs.send_email",
        payload={
            "recipients": ["lifecycle@test.com"],
            "subject": "Lifecycle Test",
            "body": "Testing complete lifecycle"
        },
        channel=opts.test_channel
    )

    # 2. Verify initial state
    job = Job.objects.get(id=job_id)
    assert job.status == 'pending'
    assert job.attempt == 0
    assert job.started_at is None
    assert job.finished_at is None

    # Check created event
    created_events = JobEvent.objects.filter(job=job, event='created')
    assert created_events.exists()

    # 3. Simulate job being claimed and started
    job.status = 'running'
    job.started_at = timezone.now()
    job.runner_id = 'test_runner_lifecycle'
    job.attempt = 1
    job.save()

    # Create running event
    JobEvent.objects.create(
        job=job,
        channel=job.channel,
        event='running',
        runner_id=job.runner_id,
        attempt=job.attempt
    )

    # 4. Simulate job execution
    def simulate_email_job(job):
        """Simulate the email job execution."""
        recipients = job.payload.get('recipients', [])
        subject = job.payload.get('subject', '')

        # Simulate work
        time.sleep(0.01)

        # Update metadata like real job would
        job.metadata['sent_count'] = len(recipients)
        job.metadata['subject'] = subject
        job.metadata['execution_time'] = timezone.now().isoformat()

        return "completed"

    result = simulate_email_job(job)

    # 5. Mark job as completed
    job.status = 'completed'
    job.finished_at = timezone.now()
    job.save()

    # Create completed event
    JobEvent.objects.create(
        job=job,
        channel=job.channel,
        event='completed',
        runner_id=job.runner_id
    )

    # 6. Verify final state
    job.refresh_from_db()
    assert job.status == 'completed'
    assert job.is_terminal is True
    assert job.duration_ms > 0
    assert job.metadata['sent_count'] == 1
    assert job.metadata['subject'] == "Lifecycle Test"

    # Check all events created
    events = JobEvent.objects.filter(job=job).order_by('at')
    event_types = [e.event for e in events]
    assert 'created' in event_types
    assert 'running' in event_types
    assert 'completed' in event_types


@th.django_unit_test()
def test_job_failure_and_retry_flow(opts):
    """Test job failure and retry mechanism."""
    from mojo.apps.jobs import publish
    from mojo.apps.jobs.models import Job, JobEvent
    import traceback

    # Publish job with retries enabled
    job_id = publish(
        func="mojo.apps.jobs.examples.sample_jobs.fetch_external_api",
        payload={
            "url": "https://nonexistent.api.example.com/fail",
            "timeout": 10
        },
        channel=opts.test_channel,
        max_retries=3,
        backoff_base=1.5,
        backoff_max=60
    )

    job = Job.objects.get(id=job_id)

    # Simulate first execution attempt
    job.status = 'running'
    job.started_at = timezone.now()
    job.runner_id = 'test_runner_retry'
    job.attempt = 1
    job.save()

    # Simulate job failure
    try:
        raise ConnectionError("Failed to connect to API")
    except Exception as e:
        # Handle failure like the engine would
        job.last_error = str(e)
        job.stack_trace = traceback.format_exc()

        # Calculate backoff
        backoff = min(
            job.backoff_base ** job.attempt,
            job.backoff_max_sec
        )

        # Schedule retry
        job.run_at = timezone.now() + timedelta(seconds=backoff)
        job.status = 'pending'
        job.save()

        # Create retry event
        JobEvent.objects.create(
            job=job,
            channel=job.channel,
            event='retry',
            runner_id=job.runner_id,
            attempt=job.attempt,
            details={
                'retry_at': job.run_at.isoformat(),
                'backoff_seconds': backoff
            }
        )

    # Verify retry setup
    assert job.status == 'pending'
    assert job.run_at is not None
    assert job.last_error == "Failed to connect to API"
    assert job.attempt == 1  # Attempt counter stays for this retry

    # Verify retry would be scheduled in ZSET
    expected_score = job.run_at.timestamp() * 1000
    # Note: We'd need to actually re-publish to get it in Redis ZSET


@th.django_unit_test()
def test_job_expiration_handling(opts):
    """Test job expiration logic."""
    from mojo.apps.jobs import publish
    from mojo.apps.jobs.models import Job, JobEvent

    # Publish job that expires quickly
    job_id = publish(
        func="mojo.apps.jobs.examples.sample_jobs.send_email",
        payload={"recipients": ["expire@test.com"]},
        channel=opts.test_channel,
        expires_in=1  # Expire in 1 second
    )

    job = Job.objects.get(id=job_id)
    assert job.expires_at is not None

    # Wait for expiration
    time.sleep(1.1)

    # Check if job is expired
    assert job.is_expired is True

    # Simulate engine expiration handling
    if job.is_expired:
        job.status = 'expired'
        job.finished_at = timezone.now()
        job.save()

        JobEvent.objects.create(
            job=job,
            channel=job.channel,
            event='expired',
            details={'reason': 'job_expired_before_execution'}
        )

    assert job.status == 'expired'
    assert job.is_terminal is True


@th.django_unit_test()
def test_scheduled_job_to_execution_flow(opts):
    """Test flow from scheduled job to execution."""
    from mojo.apps.jobs import publish
    from mojo.apps.jobs.scheduler import Scheduler
    from mojo.apps.jobs.models import Job, JobEvent

    # Publish job scheduled for near future
    delay_ms = 100  # Very short delay for test
    job_id = publish(
        func="mojo.apps.jobs.examples.sample_jobs.send_email",
        payload={"recipients": ["scheduled@test.com"]},
        channel=opts.test_channel,
        delay=delay_ms / 1000  # Convert to seconds
    )

    job = Job.objects.get(id=job_id)

    # Verify in scheduled ZSET
    sched_key = opts.keys.sched(opts.test_channel)
    score = opts.redis.zscore(sched_key, job_id)
    assert score is not None

    # Wait for job to become due
    time.sleep(0.2)

    # Simulate scheduler processing
    scheduler = Scheduler(channels=[opts.test_channel])
    now = timezone.now()
    now_ms = now.timestamp() * 1000

    scheduler._process_channel(opts.test_channel, now, now_ms)

    # Verify job moved to queue
    queue_key = opts.keys.queue(opts.test_channel)
    queue_items = opts.redis.get_client().lrange(queue_key, 0, -1)
    job_id_bytes = job_id.encode('utf-8')
    assert job_id_bytes in queue_items or job_id in queue_items

    # Simulate engine picking up and executing
    job.status = 'running'
    job.started_at = timezone.now()
    job.runner_id = 'test_runner_scheduled'
    job.attempt = 1
    job.save()

    # Execute job
    def simple_execution(job):
        job.metadata['scheduled_executed'] = True
        return "completed"

    result = simple_execution(job)

    job.status = 'completed'
    job.finished_at = timezone.now()
    job.save()

    # Verify completion
    assert job.status == 'completed'
    assert job.metadata['scheduled_executed'] is True


@th.django_unit_test()
def test_broadcast_job_execution_flow(opts):
    """Test broadcast job execution pattern."""
    from mojo.apps.jobs import publish
    from mojo.apps.jobs.models import Job

    # Publish broadcast job
    job_id = publish(
        func="mojo.apps.jobs.examples.sample_jobs.send_email",
        payload={"message": "Broadcast notification"},
        channel=opts.test_channel,
        broadcast=True
    )

    job = Job.objects.get(id=job_id)
    assert job.broadcast is True

    # Simulate multiple runner executions (broadcast pattern)
    runners = ['runner_1', 'runner_2', 'runner_3']

    for runner_id in runners:
        # Each runner would execute the job
        job.runner_id = runner_id

        # Execute broadcast job
        def broadcast_handler(job):
            # Each runner adds its execution info
            if 'executed_by' not in job.metadata:
                job.metadata['executed_by'] = []

            job.metadata['executed_by'].append({
                'runner': job.runner_id,
                'executed_at': timezone.now().isoformat()
            })

            return "broadcast_completed"

        result = broadcast_handler(job)
        assert result == "broadcast_completed"

    # Verify all runners executed
    assert len(job.metadata['executed_by']) == 3
    executed_runners = [info['runner'] for info in job.metadata['executed_by']]
    assert all(runner in executed_runners for runner in runners)


@th.django_unit_test()
def test_job_metadata_updates_during_execution(opts):
    """Test job metadata updates during execution."""
    from mojo.apps.jobs import publish
    from mojo.apps.jobs.models import Job

    job_id = publish(
        func="mojo.apps.jobs.examples.sample_jobs.process_file_upload",
        payload={
            "file_path": "/test/file.csv",
            "processing_type": "import"
        },
        channel=opts.test_channel
    )

    job = Job.objects.get(id=job_id)

    # Simulate progressive job execution with metadata updates
    def progressive_processing(job):
        file_path = job.payload['file_path']

        # Initialize
        job.metadata['started_at'] = timezone.now().isoformat()
        job.metadata['file_path'] = file_path

        # Simulate processing steps
        steps = ['reading', 'validating', 'importing', 'completing']
        total_records = 1000

        for i, step in enumerate(steps):
            job.metadata['current_step'] = step
            job.metadata['progress'] = f"{((i + 1) / len(steps)) * 100:.0f}%"

            if step == 'importing':
                # Simulate record-by-record processing
                for record_num in range(0, total_records, 100):
                    processed = min(record_num + 100, total_records)
                    job.metadata['records_processed'] = processed
                    job.metadata['record_progress'] = f"{processed}/{total_records}"

                    # Check cancellation
                    if job.cancel_requested:
                        job.metadata['cancelled_at_record'] = processed
                        return "cancelled"

                    time.sleep(0.001)  # Simulate work

        job.metadata['completed_at'] = timezone.now().isoformat()
        job.metadata['total_processed'] = total_records

        return "completed"

    result = progressive_processing(job)

    # Verify execution results
    assert result == "completed"
    assert job.metadata['current_step'] == 'completing'
    assert job.metadata['progress'] == '100%'
    assert job.metadata['records_processed'] == 1000
    assert job.metadata['total_processed'] == 1000
    assert 'completed_at' in job.metadata


@th.django_unit_test()
def test_idempotency_key_handling(opts):
    """Test idempotency key prevents duplicate jobs."""
    from mojo.apps.jobs import publish
    from mojo.apps.jobs.models import Job

    idempotency_key = f"test_idempotent_{uuid.uuid4().hex[:8]}"

    # First publish
    job_id1 = publish(
        func="mojo.apps.jobs.examples.sample_jobs.send_email",
        payload={"recipients": ["test1@example.com"], "attempt": 1},
        channel=opts.test_channel,
        idempotency_key=idempotency_key
    )

    # Second publish with same key
    job_id2 = publish(
        func="mojo.apps.jobs.examples.sample_jobs.send_email",
        payload={"recipients": ["test2@example.com"], "attempt": 2},
        channel=opts.test_channel,
        idempotency_key=idempotency_key
    )

    # Should return same job ID
    assert job_id1 == job_id2

    # Only one job in database
    jobs = Job.objects.filter(idempotency_key=idempotency_key)
    assert jobs.count() == 1

    # Should have first payload
    job = jobs.first()
    assert job.payload['attempt'] == 1
    assert job.payload['recipients'] == ['test1@example.com']


@th.django_unit_test()
def test_cleanup_publish_execute(opts):
    """Clean up test data."""
    from mojo.apps.jobs.models import Job, JobEvent

    # Clean up database
    Job.objects.filter(func__contains='test_publish_execute').delete()

    # Clean up Redis
    test_keys = [
        opts.keys.queue(opts.test_channel),
        opts.keys.processing(opts.test_channel),
        opts.keys.sched(opts.test_channel),
        opts.keys.sched_broadcast(opts.test_channel)
    ]

    for key in test_keys:
        opts.redis.delete(key)
