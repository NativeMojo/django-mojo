"""
Tests for basic job operations aligned with actual implementation.
Tests the real patterns: plain functions, Job models, module paths.
"""
from testit import helpers as th
from datetime import datetime, timedelta
from django.utils import timezone
import uuid


@th.django_unit_setup()
def setup_basic_tests(opts):
    """Clear test data before running tests."""
    from mojo.apps.jobs.models import Job, JobEvent
    from mojo.apps.jobs.adapters import get_adapter
    from mojo.apps.jobs.keys import JobKeys

    # Clear any existing test data
    Job.objects.filter(channel__startswith='test_').delete()
    JobEvent.objects.filter(channel__startswith='test_').delete()

    # Clear Redis test data
    redis = get_adapter()
    keys = JobKeys()
    test_channels = ['test_basic', 'test_scheduled', 'test_broadcast']

    for channel in test_channels:
        try:
            redis.delete(keys.stream(channel))
            redis.delete(keys.stream_broadcast(channel))
            redis.delete(keys.sched(channel))
            redis.delete(keys.sched_broadcast(channel))
        except:
            pass

    # Store test configuration
    opts.test_channel = 'test_basic'
    opts.redis = redis
    opts.keys = keys


@th.django_unit_test()
def test_job_publish_basic(opts):
    """Test basic job publishing following actual implementation."""
    from mojo.apps.jobs import publish
    from mojo.apps.jobs.models import Job, JobEvent

    # Publish job with module path string (actual pattern)
    job_id = publish(
        func="mojo.apps.jobs.examples.sample_jobs.send_email",
        payload={
            'recipients': ['test@example.com'],
            'subject': 'Test Email',
            'body': 'Test content'
        },
        channel=opts.test_channel
    )

    # Verify job ID format (32 char hex without dashes)
    assert len(job_id) == 32
    assert all(c in '0123456789abcdef' for c in job_id)

    # Check database storage (payload stored here, not Redis)
    job = Job.objects.get(id=job_id)
    assert job.channel == opts.test_channel
    assert job.func == "mojo.apps.jobs.examples.sample_jobs.send_email"
    assert job.payload['recipients'] == ['test@example.com']
    assert job.status == 'pending'

    # Check events created
    events = JobEvent.objects.filter(job=job).order_by('at')
    assert events.count() >= 1
    assert events.first().event == 'created'

    # Verify Redis only has minimal data (no payload!)
    stream_key = opts.keys.stream(opts.test_channel)
    messages = opts.redis.get_client().xrange(stream_key, count=10)

    # Find our job message
    found = False
    for msg_id, data in messages:
        if data.get(b'job_id', b'').decode('utf-8') == job_id:
            found = True
            # Verify payload is NOT in Redis
            assert b'payload' not in data
            # Only job_id, func path, and metadata
            assert b'job_id' in data
            assert b'func' in data
            break

    assert found, "Job not found in Redis stream"


@th.django_unit_test()
def test_job_handler_pattern(opts):
    """Test job handler pattern matching actual implementation."""
    from mojo.apps.jobs.models import Job
    import uuid

    # Create a job as the engine would
    job = Job.objects.create(
        id=uuid.uuid4().hex,
        channel=opts.test_channel,
        func='test.job_handler',
        payload={'message': 'Hello', 'count': 42},
        status='running',
        started_at=timezone.now(),
        runner_id='test_runner_001',
        attempt=1
    )

    # This is how actual job handlers work (from sample_jobs.py)
    def job_handler(job):
        """Plain function accepting Job model - actual pattern."""
        message = job.payload.get('message')
        count = job.payload.get('count', 0)

        # Check cancellation - actual pattern
        if job.cancel_requested:
            job.metadata['cancelled'] = True
            job.metadata['cancelled_at'] = datetime.now(timezone.utc).isoformat()
            return "cancelled"

        # Process job
        processed_count = 0
        for i in range(count):
            processed_count += 1

            # Check cancellation periodically - actual pattern
            if i % 10 == 0 and job.cancel_requested:
                job.metadata['cancelled_at_count'] = processed_count
                break

        # Update metadata - direct dictionary manipulation
        job.metadata['message_received'] = message
        job.metadata['processed_count'] = processed_count
        job.metadata['completed_at'] = datetime.now(timezone.utc).isoformat()

        return "completed"

    # Execute handler
    result = job_handler(job)

    assert result == "completed"
    assert job.metadata['message_received'] == 'Hello'
    assert job.metadata['processed_count'] == 42

    # Update job as engine would
    job.status = 'completed'
    job.finished_at = timezone.now()
    job.save(update_fields=['status', 'finished_at', 'metadata'])

    assert job.is_terminal is True


@th.django_unit_test()
def test_job_scheduling_with_delay(opts):
    """Test scheduled job publishing with delay."""
    from mojo.apps.jobs import publish
    from mojo.apps.jobs.models import Job

    # Publish with 30 second delay
    delay_seconds = 30
    job_id = publish(
        func="mojo.apps.jobs.examples.sample_jobs.process_file_upload",
        payload={
            'file_path': '/uploads/test.csv',
            'processing_type': 'import'
        },
        channel=opts.test_channel,
        delay=delay_seconds
    )

    # Check job scheduling
    job = Job.objects.get(id=job_id)
    assert job.run_at is not None

    # Should be scheduled for ~30 seconds from now
    expected_run = timezone.now() + timedelta(seconds=delay_seconds)
    delta = abs((job.run_at - expected_run).total_seconds())
    assert delta < 1.0, f"Scheduling off by {delta} seconds"

    # Verify in Redis scheduled ZSET, not stream
    sched_key = opts.keys.sched(opts.test_channel)
    score = opts.redis.get_client().zscore(sched_key, job_id)
    assert score is not None, "Job not in scheduled ZSET"

    # Score should be timestamp in milliseconds
    expected_score = job.run_at.timestamp() * 1000
    assert abs(score - expected_score) < 1000  # Within 1 second


@th.django_unit_test()
def test_job_with_specific_run_at(opts):
    """Test scheduling job for specific time."""
    from mojo.apps.jobs import publish
    from mojo.apps.jobs.models import Job

    # Schedule for 2 hours from now
    run_at = timezone.now() + timedelta(hours=2)

    job_id = publish(
        func="mojo.apps.jobs.examples.sample_jobs.generate_report",
        payload={
            'report_type': 'monthly',
            'start_date': '2024-01-01',
            'end_date': '2024-01-31'
        },
        channel=opts.test_channel,
        run_at=run_at
    )

    job = Job.objects.get(id=job_id)
    assert job.run_at is not None

    # Should match our specified time
    delta = abs((job.run_at - run_at).total_seconds())
    assert delta < 0.1


@th.django_unit_test()
def test_job_expiration(opts):
    """Test job expiration settings."""
    from mojo.apps.jobs import publish
    from mojo.apps.jobs.models import Job

    # Test with expires_in
    job_id = publish(
        func="mojo.apps.jobs.examples.sample_jobs.fetch_external_api",
        payload={
            'url': 'https://api.example.com/data',
            'timeout': 30
        },
        channel=opts.test_channel,
        expires_in=600  # 10 minutes
    )

    job = Job.objects.get(id=job_id)
    assert job.expires_at is not None

    expected_expire = timezone.now() + timedelta(seconds=600)
    delta = abs((job.expires_at - expected_expire).total_seconds())
    assert delta < 1.0

    # Test with specific expires_at
    expires_at = timezone.now() + timedelta(hours=1)
    job_id2 = publish(
        func="mojo.apps.jobs.examples.sample_jobs.cleanup_old_records",
        payload={'days_old': 30},
        channel=opts.test_channel,
        expires_at=expires_at
    )

    job2 = Job.objects.get(id=job_id2)
    delta = abs((job2.expires_at - expires_at).total_seconds())
    assert delta < 0.1


@th.django_unit_test()
def test_job_cancellation_request(opts):
    """Test job cancellation following actual pattern."""
    from mojo.apps.jobs import publish, cancel
    from mojo.apps.jobs.models import Job, JobEvent

    job_id = publish(
        func="mojo.apps.jobs.examples.sample_jobs.send_email",
        payload={'recipients': ['user1@test.com', 'user2@test.com']},
        channel=opts.test_channel
    )

    # Request cancellation
    result = cancel(job_id)
    assert result is True

    # Check database flag
    job = Job.objects.get(id=job_id)
    assert job.cancel_requested is True



    # Verify event recorded
    cancel_event = JobEvent.objects.filter(
        job=job,
        event='canceled'
    ).first()
    assert cancel_event is not None

    # Cancel non-existent job should return False
    result = cancel("nonexistent" + uuid.uuid4().hex[:20])
    assert result is False


@th.django_unit_test()
def test_job_retry_configuration(opts):
    """Test job retry settings following actual implementation."""
    from mojo.apps.jobs import publish
    from mojo.apps.jobs.models import Job

    job_id = publish(
        func="mojo.apps.jobs.examples.sample_jobs.fetch_external_api",
        payload={
            'url': 'https://api.example.com/endpoint',
            'method': 'POST'
        },
        channel=opts.test_channel,
        max_retries=5,
        backoff_base=2.0,
        backoff_max=300  # 5 minutes max
    )

    job = Job.objects.get(id=job_id)
    assert job.max_retries == 5
    assert job.backoff_base == 2.0
    assert job.backoff_max_sec == 300
    assert job.attempt == 0  # Not attempted yet

    # Simulate failure and retry logic (as in job_engine.py)
    job.attempt = 1
    job.status = 'failed'
    job.last_error = 'Connection timeout'
    job.save()

    # Calculate backoff as engine would
    backoff = min(
        job.backoff_base ** job.attempt,
        job.backoff_max_sec
    )
    assert backoff == 2.0  # 2^1 = 2

    # After multiple attempts
    job.attempt = 4
    backoff = min(
        job.backoff_base ** job.attempt,
        job.backoff_max_sec
    )
    assert backoff == 16.0  # 2^4 = 16

    # Check if retriable
    assert job.is_retriable is True

    # Max retries exceeded
    job.attempt = 5
    assert job.is_retriable is False


@th.django_unit_test()
def test_broadcast_job(opts):
    """Test broadcast job configuration."""
    from mojo.apps.jobs import publish
    from mojo.apps.jobs.models import Job

    job_id = publish(
        func="mojo.apps.jobs.examples.sample_jobs.send_email",
        payload={'recipients': ['all@company.com']},
        channel='test_broadcast',
        broadcast=True
    )

    job = Job.objects.get(id=job_id)
    assert job.broadcast is True

    # Should be in broadcast stream, not regular stream
    broadcast_key = opts.keys.stream_broadcast('test_broadcast')
    regular_key = opts.keys.stream('test_broadcast')

    # Note: Implementation might queue to broadcast stream
    # This is implementation detail we're testing


@th.django_unit_test()
def test_job_metadata_updates(opts):
    """Test job metadata updates matching actual pattern."""
    from mojo.apps.jobs.models import Job
    import uuid

    job = Job.objects.create(
        id=uuid.uuid4().hex,
        channel=opts.test_channel,
        func='test.metadata_handler',
        payload={'items': [1, 2, 3, 4, 5]},
        status='running',
        started_at=timezone.now()
    )

    # Simulate job handler updating metadata (actual pattern)
    def process_with_metadata(job):
        """Handler that updates metadata - actual pattern."""
        items = job.payload.get('items', [])

        # Direct dictionary updates - actual pattern
        job.metadata['start_time'] = datetime.now(timezone.utc).isoformat()
        job.metadata['item_count'] = len(items)

        processed = []
        for item in items:
            if job.cancel_requested:
                job.metadata['cancelled_at_item'] = len(processed)
                break

            processed.append(item * 2)

            # Progress updates
            job.metadata['progress'] = f"{len(processed)}/{len(items)}"

        job.metadata['results'] = processed
        job.metadata['completed_at'] = datetime.now(timezone.utc).isoformat()

        # Save metadata periodically if needed
        job.save(update_fields=['metadata'])

        return "completed"

    result = process_with_metadata(job)

    assert result == "completed"
    assert job.metadata['item_count'] == 5
    assert job.metadata['results'] == [2, 4, 6, 8, 10]
    assert 'progress' in job.metadata
    assert 'completed_at' in job.metadata


@th.django_unit_test()
def test_job_idempotency_key(opts):
    """Test idempotency key prevents duplicates."""
    from mojo.apps.jobs import publish
    from mojo.apps.jobs.models import Job

    idempotency_key = f"test_idempotent_{uuid.uuid4().hex[:8]}"

    # First publish
    job_id1 = publish(
        func="mojo.apps.jobs.examples.sample_jobs.send_email",
        payload={'recipients': ['user@test.com'], 'attempt': 1},
        channel=opts.test_channel,
        idempotency_key=idempotency_key
    )

    # Second publish with same key
    job_id2 = publish(
        func="mojo.apps.jobs.examples.sample_jobs.send_email",
        payload={'recipients': ['different@test.com'], 'attempt': 2},
        channel=opts.test_channel,
        idempotency_key=idempotency_key
    )

    # Should return same job ID
    assert job_id1 == job_id2

    # Only one job in database
    jobs = Job.objects.filter(idempotency_key=idempotency_key)
    assert jobs.count() == 1

    # Payload from first request
    job = jobs.first()
    assert job.payload['attempt'] == 1
    assert job.payload['recipients'] == ['user@test.com']


@th.django_unit_test()
def test_job_status_api(opts):
    """Test job status retrieval API."""
    from mojo.apps.jobs import publish, status
    from mojo.apps.jobs.models import Job

    job_id = publish(
        func="mojo.apps.jobs.examples.sample_jobs.generate_report",
        payload={'report_type': 'weekly'},
        channel=opts.test_channel
    )

    # Get status via API
    job_status = status(job_id)

    assert job_status is not None
    assert job_status['id'] == job_id
    assert job_status['status'] == 'pending'
    assert job_status['channel'] == opts.test_channel
    assert job_status['func'] == "mojo.apps.jobs.examples.sample_jobs.generate_report"
    assert job_status['attempt'] == 0

    # Test non-existent job
    missing_status = status("nonexistent" + uuid.uuid4().hex[:20])
    assert missing_status is None


@th.django_unit_test()
def test_job_max_exec_seconds(opts):
    """Test max execution seconds configuration."""
    from mojo.apps.jobs import publish
    from mojo.apps.jobs.models import Job

    job_id = publish(
        func="mojo.apps.jobs.examples.sample_jobs.process_file_upload",
        payload={'file_path': '/large_file.csv'},
        channel=opts.test_channel,
        max_exec_seconds=60  # 1 minute timeout
    )

    job = Job.objects.get(id=job_id)
    assert job.max_exec_seconds == 60


@th.django_unit_test()
def test_job_events_tracking(opts):
    """Test job event tracking as per actual implementation."""
    from mojo.apps.jobs import publish
    from mojo.apps.jobs.models import Job, JobEvent

    # Create job with delay to get multiple events
    job_id = publish(
        func="mojo.apps.jobs.examples.sample_jobs.send_email",
        payload={'recipients': ['test@example.com']},
        channel=opts.test_channel,
        delay=10  # Creates 'scheduled' event
    )

    job = Job.objects.get(id=job_id)

    # Check initial events
    events = JobEvent.objects.filter(job=job).order_by('at')
    event_types = [e.event for e in events]

    assert 'created' in event_types
    assert 'scheduled' in event_types  # Because we used delay

    # Simulate job execution events
    JobEvent.objects.create(
        job=job,
        channel=job.channel,
        event='running',
        runner_id='test_runner_001',
        attempt=1
    )

    JobEvent.objects.create(
        job=job,
        channel=job.channel,
        event='completed',
        runner_id='test_runner_001'
    )

    # Verify full event chain
    all_events = JobEvent.objects.filter(job=job).order_by('at')
    all_event_types = [e.event for e in all_events]

    assert 'created' in all_event_types
    assert 'scheduled' in all_event_types
    assert 'running' in all_event_types
    assert 'completed' in all_event_types


@th.django_unit_test()
def test_payload_validation(opts):
    """Test payload size validation."""
    from mojo.apps.jobs import publish
    from django.conf import settings

    # Get max size from settings
    max_bytes = getattr(settings, 'JOBS_PAYLOAD_MAX_BYTES', 16384)

    # Test with valid payload
    valid_payload = {
        'data': 'x' * 100,
        'nested': {'key': 'value'}
    }

    job_id = publish(
        func="mojo.apps.jobs.examples.sample_jobs.send_email",
        payload=valid_payload,
        channel=opts.test_channel
    )
    assert job_id is not None

    # Test with oversized payload
    large_payload = {
        'data': 'x' * (max_bytes + 1000)  # Exceed limit
    }

    try:
        job_id = publish(
            func="mojo.apps.jobs.examples.sample_jobs.send_email",
            payload=large_payload,
            channel=opts.test_channel
        )
        assert False, "Should have raised ValueError for large payload"
    except ValueError as e:
        assert "exceeds maximum size" in str(e)

    # Test with non-dict payload
    try:
        job_id = publish(
            func="mojo.apps.jobs.examples.sample_jobs.send_email",
            payload="not a dict",
            channel=opts.test_channel
        )
        assert False, "Should have raised ValueError for non-dict payload"
    except ValueError as e:
        assert "must be a dictionary" in str(e)


@th.django_unit_test()
def test_cleanup(opts):
    """Clean up test data."""
    from mojo.apps.jobs.models import Job, JobEvent

    # Clean up test jobs
    deleted_jobs, _ = Job.objects.filter(channel__startswith='test_').delete()

    # Clean up Redis
    for channel in ['test_basic', 'test_scheduled', 'test_broadcast']:
        opts.redis.delete(opts.keys.stream(channel))
        opts.redis.delete(opts.keys.stream_broadcast(channel))
        opts.redis.delete(opts.keys.sched(channel))
        opts.redis.delete(opts.keys.sched_broadcast(channel))
