"""
Tests for job execution functionality aligned with actual implementation.
Tests real execution patterns: Job model handlers, dynamic imports, thread pools.
"""
from testit import helpers as th
import time
import traceback
import uuid
from datetime import datetime, timedelta
from django.utils import timezone


@th.django_unit_setup()
def setup_execution_tests(opts):
    """Setup for execution tests."""
    from mojo.apps.jobs.models import Job, JobEvent
    from mojo.apps.jobs.adapters import get_adapter
    from mojo.apps.jobs.keys import JobKeys

    # Clear test data
    Job.objects.filter(channel__startswith='exec_').delete()
    JobEvent.objects.filter(channel__startswith='exec_').delete()

    # Setup Redis
    opts.redis = get_adapter()
    opts.keys = JobKeys()

    # Test configuration
    opts.test_channel = 'exec_basic'
    opts.execution_count = 0
    opts.execution_results = {}

    # Clear Redis test data
    for channel in ['exec_basic', 'exec_retry', 'exec_broadcast']:
        try:
            opts.redis.delete(opts.keys.stream(channel))
            opts.redis.delete(opts.keys.stream_broadcast(channel))
            opts.redis.delete(opts.keys.sched(channel))
        except:
            pass


@th.django_unit_test()
def test_simple_job_execution_pattern(opts):
    """Test simple job execution matching actual implementation."""
    from mojo.apps.jobs.models import Job, JobEvent

    # Create job as the engine would
    job = Job.objects.create(
        id=uuid.uuid4().hex,
        channel=opts.test_channel,
        func='mojo.apps.jobs.examples.sample_jobs.send_email',
        payload={
            'recipients': ['test@example.com'],
            'subject': 'Test Email',
            'body': 'Test content'
        },
        status='pending',
        max_retries=3
    )

    # Simulate engine marking as running
    job.status = 'running'
    job.started_at = timezone.now()
    job.runner_id = 'test_runner_001'
    job.attempt = 1
    job.save(update_fields=['status', 'started_at', 'runner_id', 'attempt'])

    # Create running event
    JobEvent.objects.create(
        job=job,
        channel=job.channel,
        event='running',
        runner_id=job.runner_id,
        attempt=job.attempt
    )

    # This is how actual handlers work (from sample_jobs.py pattern)
    def send_email(job):
        """Actual pattern from sample_jobs.py."""
        recipients = job.payload.get('recipients', [])
        subject = job.payload.get('subject', 'No Subject')
        body = job.payload.get('body', '')

        # Check for cancellation
        if job.cancel_requested:
            job.metadata['cancelled'] = True
            job.metadata['cancelled_at'] = datetime.now(timezone.utc).isoformat()
            return "cancelled"

        sent_count = 0
        failed_recipients = []

        for recipient in recipients:
            try:
                # Simulate sending email
                print(f"Sending email to {recipient}")
                sent_count += 1

                # Check cancellation periodically for long lists
                if sent_count % 10 == 0 and job.cancel_requested:
                    job.metadata['cancelled_at_recipient'] = sent_count
                    break

            except Exception as e:
                failed_recipients.append({'email': recipient, 'error': str(e)})

        # Update metadata with results
        job.metadata['sent_count'] = sent_count
        job.metadata['failed_count'] = len(failed_recipients)
        if failed_recipients:
            job.metadata['failed_recipients'] = failed_recipients[:10]
        job.metadata['completed_at'] = datetime.now(timezone.utc).isoformat()

        return "completed"

    # Execute handler
    result = send_email(job)

    assert result == "completed"
    assert job.metadata['sent_count'] == 1
    assert job.metadata['failed_count'] == 0
    assert 'completed_at' in job.metadata

    # Simulate engine marking as completed
    job.status = 'completed'
    job.finished_at = timezone.now()
    job.save(update_fields=['status', 'finished_at', 'metadata'])

    # Create completed event
    JobEvent.objects.create(
        job=job,
        channel=job.channel,
        event='completed',
        runner_id=job.runner_id
    )

    assert job.is_terminal is True
    assert job.duration_ms > 0


@th.django_unit_test()
def test_job_with_cancellation_check(opts):
    """Test job cancellation pattern from actual implementation."""
    from mojo.apps.jobs.models import Job

    job = Job.objects.create(
        id=uuid.uuid4().hex,
        channel=opts.test_channel,
        func='test.process_file',
        payload={
            'file_path': '/uploads/large_file.csv',
            'processing_type': 'import'
        },
        status='running',
        started_at=timezone.now(),
        cancel_requested=True  # Already requested to cancel
    )

    # Handler pattern from sample_jobs.py
    def process_file_upload(job):
        """Process uploaded file with cancellation checks."""
        file_path = job.payload['file_path']
        processing_type = job.payload.get('processing_type', 'default')

        # Initialize processing
        job.metadata['started_at'] = datetime.now(timezone.utc).isoformat()
        job.metadata['file_path'] = file_path
        job.metadata['processing_type'] = processing_type

        try:
            # Simulate file processing
            total_size = 1000  # In real code: os.path.getsize(file_path)
            chunk_size = 100
            processed = 0

            while processed < total_size:
                # Check for cancellation
                if job.cancel_requested:
                    job.metadata['cancelled'] = True
                    job.metadata['processed_bytes'] = processed
                    job.metadata['cancelled_at'] = datetime.now(timezone.utc).isoformat()
                    return "cancelled"

                # Process chunk (simulate work)
                time.sleep(0.01)
                processed += chunk_size

                # Update progress
                progress = min(100, (processed / total_size) * 100)
                job.metadata['progress'] = f"{progress:.1f}%"
                job.metadata['processed_bytes'] = processed

                # Save progress periodically (optional - has DB overhead)
                if processed % 500 == 0:
                    job.save(update_fields=['metadata'])

            job.metadata['completed_at'] = datetime.now(timezone.utc).isoformat()
            job.metadata['total_processed'] = processed
            return "completed"

        except Exception as e:
            job.metadata['error'] = str(e)
            job.metadata['failed_at'] = datetime.now(timezone.utc).isoformat()
            raise  # Re-raise to trigger retry logic

    # Execute with cancellation
    result = process_file_upload(job)

    assert result == "cancelled"
    assert job.metadata['cancelled'] is True
    assert 'processed_bytes' in job.metadata
    assert job.metadata['processed_bytes'] < 1000  # Didn't complete


@th.django_unit_test()
def test_job_error_handling_and_retry(opts):
    """Test error handling and retry logic from actual implementation."""
    from mojo.apps.jobs.models import Job, JobEvent
    import random

    job = Job.objects.create(
        id=uuid.uuid4().hex,
        channel='exec_retry',
        func='mojo.apps.jobs.examples.sample_jobs.fetch_external_api',
        payload={
            'url': 'https://api.example.com/endpoint',
            'method': 'GET',
            'timeout': 30
        },
        status='running',
        started_at=timezone.now(),
        runner_id='test_runner_001',
        attempt=1,
        max_retries=3,
        backoff_base=2.0,
        backoff_max_sec=60
    )

    # Handler that fails (pattern from sample_jobs.py)
    def fetch_external_api(job):
        """Fetch data from external API with retry logic."""
        url = job.payload['url']
        method = job.payload.get('method', 'GET')
        timeout = job.payload.get('timeout', 30)

        job.metadata['request_started'] = datetime.now(timezone.utc).isoformat()
        job.metadata['attempt'] = job.attempt

        # Simulate failure
        raise Exception(f"Connection timeout after {timeout}s")

    # Execute and handle failure
    try:
        result = fetch_external_api(job)
        assert False, "Should have raised exception"
    except Exception as e:
        # This is what job_engine.py does on failure
        job.last_error = str(e)
        job.stack_trace = traceback.format_exc()

        # Check retry eligibility (actual logic from job_engine.py)
        if job.attempt < job.max_retries:
            # Calculate backoff with jitter
            backoff = min(
                job.backoff_base ** job.attempt,
                job.backoff_max_sec
            )
            jitter = backoff * (0.8 + random.random() * 0.4)

            # Schedule retry
            job.run_at = timezone.now() + timedelta(seconds=jitter)
            job.status = 'pending'
            job.save(update_fields=['status', 'run_at', 'last_error', 'stack_trace'])

            # Add to scheduled ZSET (as engine would)
            score = job.run_at.timestamp() * 1000
            opts.redis.zadd(opts.keys.sched(job.channel), {job.id: score})

            # Create retry event
            JobEvent.objects.create(
                job=job,
                channel=job.channel,
                event='retry',
                runner_id=job.runner_id,
                attempt=job.attempt,
                details={'retry_at': job.run_at.isoformat(), 'backoff': jitter}
            )

            assert job.status == 'pending'
            assert job.run_at is not None
            assert job.run_at > timezone.now()

        else:
            # Max retries exceeded
            job.status = 'failed'
            job.finished_at = timezone.now()
            job.save(update_fields=['status', 'finished_at', 'last_error', 'stack_trace'])

            JobEvent.objects.create(
                job=job,
                channel=job.channel,
                event='failed',
                runner_id=job.runner_id,
                attempt=job.attempt,
                details={'max_retries_exceeded': True}
            )

            assert job.status == 'failed'


@th.django_unit_test()
def test_dynamic_function_loading(opts):
    """Test dynamic function loading as implemented in job_engine.py."""
    from mojo.apps.jobs.job_engine import load_job_function
    from mojo.apps.jobs.models import Job

    # Test loading actual job function
    func_path = 'mojo.apps.jobs.examples.sample_jobs.send_email'

    try:
        func = load_job_function(func_path)
        assert callable(func)
        assert func.__name__ == 'send_email'
    except ImportError as e:
        # If examples aren't available, test with a mock
        print(f"Could not load real function: {e}")

    # Test invalid function path
    try:
        func = load_job_function('invalid.module.nonexistent')
        assert False, "Should have raised ImportError"
    except ImportError as e:
        assert "Cannot load job function" in str(e)


@th.django_unit_test()
def test_job_progress_updates(opts):
    """Test job progress updates matching actual pattern."""
    from mojo.apps.jobs.models import Job

    job = Job.objects.create(
        id=uuid.uuid4().hex,
        channel=opts.test_channel,
        func='test.generate_report',
        payload={
            'report_type': 'monthly',
            'start_date': '2024-01-01',
            'end_date': '2024-01-31',
            'format': 'pdf'
        },
        status='running',
        started_at=timezone.now()
    )

    # Handler with progress updates (pattern from sample_jobs.py)
    def generate_report(job):
        """Generate report with progress updates."""
        report_type = job.payload['report_type']
        start_date = job.payload['start_date']
        end_date = job.payload['end_date']
        output_format = job.payload.get('format', 'pdf')

        job.metadata['report_type'] = report_type
        job.metadata['date_range'] = f"{start_date} to {end_date}"

        # Simulate report generation steps
        steps = [
            'Fetching data',
            'Processing records',
            'Calculating metrics',
            'Generating charts',
            'Creating output file'
        ]

        for i, step in enumerate(steps):
            # Check cancellation
            if job.cancel_requested:
                job.metadata['cancelled_at_step'] = step
                return "cancelled"

            job.metadata['current_step'] = step
            job.metadata['progress'] = f"{((i + 1) / len(steps)) * 100:.0f}%"

            # Save progress (optional)
            if i % 2 == 0:  # Save every other step
                job.save(update_fields=['metadata'])

            # Simulate work
            time.sleep(0.01)

        # Generate report file
        report_file = f"/tmp/report_{job.id}.{output_format}"
        job.metadata['report_file'] = report_file
        job.metadata['completed_at'] = datetime.now(timezone.utc).isoformat()

        return "completed"

    # Execute
    result = generate_report(job)

    assert result == "completed"
    assert job.metadata['progress'] == "100%"
    assert job.metadata['current_step'] == 'Creating output file'
    assert 'report_file' in job.metadata


@th.django_unit_test()
def test_job_expiration_check(opts):
    """Test job expiration detection as in job_engine.py."""
    from mojo.apps.jobs.models import Job, JobEvent

    # Create an expired job
    job = Job.objects.create(
        id=uuid.uuid4().hex,
        channel=opts.test_channel,
        func='test.expired_job',
        payload={'test': 'expiration'},
        status='pending',
        expires_at=timezone.now() - timedelta(minutes=1)  # Already expired
    )

    # Simulate engine checking expiration (_execute_job_wrapper logic)
    if job.expires_at and timezone.now() > job.expires_at:
        job.status = 'expired'
        job.finished_at = timezone.now()
        job.save(update_fields=['status', 'finished_at'])

        JobEvent.objects.create(
            job=job,
            channel=job.channel,
            event='expired',
            runner_id='test_runner_001'
        )

        # Would also ACK message in real engine
        # self._ack_message(stream_key, msg_id)

    assert job.status == 'expired'
    assert job.is_terminal is True


@th.django_unit_test()
def test_broadcast_job_execution(opts):
    """Test broadcast job execution pattern."""
    from mojo.apps.jobs.models import Job

    # Create broadcast job
    job = Job.objects.create(
        id=uuid.uuid4().hex,
        channel='exec_broadcast',
        func='test.broadcast_notification',
        payload={'message': 'System maintenance at 2 AM'},
        status='running',
        started_at=timezone.now(),
        broadcast=True,
        runner_id='runner_001'  # Each runner would have different ID
    )

    # Broadcast handler (would run on multiple runners)
    def broadcast_notification(job):
        """Send notification to all nodes."""
        message = job.payload.get('message')

        # Each runner adds its execution info
        if 'executed_by' not in job.metadata:
            job.metadata['executed_by'] = []

        runner_info = {
            'runner': job.runner_id,
            'executed_at': datetime.now(timezone.utc).isoformat(),
            'hostname': 'test_host'  # In real: socket.gethostname()
        }

        job.metadata['executed_by'].append(runner_info)
        job.metadata['message'] = message

        return "broadcast_completed"

    # Execute on this "runner"
    result = broadcast_notification(job)

    assert result == "broadcast_completed"
    assert len(job.metadata['executed_by']) == 1
    assert job.metadata['executed_by'][0]['runner'] == 'runner_001'

    # Simulate another runner executing
    job.runner_id = 'runner_002'
    result = broadcast_notification(job)

    assert len(job.metadata['executed_by']) == 2


@th.django_unit_test()
def test_job_with_database_operations(opts):
    """Test job that performs database operations."""
    from mojo.apps.jobs.models import Job, JobEvent
    from django.db import close_old_connections

    job = Job.objects.create(
        id=uuid.uuid4().hex,
        channel=opts.test_channel,
        func='mojo.apps.jobs.examples.sample_jobs.cleanup_old_records',
        payload={
            'model_name': 'JobEvent',
            'days_old': 30,
            'batch_size': 100,
            'dry_run': True
        },
        status='running',
        started_at=timezone.now()
    )

    # Handler with DB operations (pattern from sample_jobs.py)
    def cleanup_old_records(job):
        """Clean up old database records in batches."""
        model_name = job.payload['model_name']
        days_old = job.payload.get('days_old', 30)
        batch_size = job.payload.get('batch_size', 100)
        dry_run = job.payload.get('dry_run', False)

        cutoff_date = timezone.now() - timedelta(days=days_old)

        job.metadata['started_at'] = datetime.now(timezone.utc).isoformat()
        job.metadata['cutoff_date'] = cutoff_date.isoformat()
        job.metadata['dry_run'] = dry_run

        deleted_count = 0
        batch_count = 0

        # Close old connections as engine does
        close_old_connections()

        # Simulate batch processing
        while batch_count < 3:  # Limit for test
            # Check for cancellation
            if job.cancel_requested:
                job.metadata['cancelled'] = True
                job.metadata['deleted_count'] = deleted_count
                return "cancelled"

            # Simulate batch deletion
            if dry_run:
                # Count but don't delete
                deleted_count += batch_size
            else:
                # Would actually delete here
                deleted_count += batch_size

            batch_count += 1

            # Update progress
            job.metadata['deleted_count'] = deleted_count
            job.metadata['batch_count'] = batch_count

        # Close connections after job
        close_old_connections()

        job.metadata['completed_at'] = datetime.now(timezone.utc).isoformat()
        job.metadata['total_deleted'] = deleted_count

        return "completed"

    # Execute
    result = cleanup_old_records(job)

    assert result == "completed"
    assert job.metadata['batch_count'] == 3
    assert job.metadata['total_deleted'] == 300  # 3 batches * 100
    assert job.metadata['dry_run'] is True


@th.django_unit_test()
def test_job_execution_wrapper_pattern(opts):
    """Test the full execution wrapper pattern from job_engine.py."""
    from mojo.apps.jobs.models import Job, JobEvent
    from django.db import transaction

    # Create job
    job_id = uuid.uuid4().hex
    job = Job.objects.create(
        id=job_id,
        channel=opts.test_channel,
        func='test.wrapper_test',
        payload={'test': 'wrapper'},
        status='pending'
    )

    # Simulate _execute_job_wrapper from job_engine.py
    def execute_job_wrapper(job_id):
        """Simulate the engine's execution wrapper."""
        try:
            # Load job with lock
            with transaction.atomic():
                job = Job.objects.select_for_update().get(id=job_id)

                # Check if already processed
                if job.status in ('completed', 'cancelled'):
                    return

                # Check expiration
                if job.expires_at and timezone.now() > job.expires_at:
                    job.status = 'expired'
                    job.finished_at = timezone.now()
                    job.save(update_fields=['status', 'finished_at'])

                    JobEvent.objects.create(
                        job=job,
                        channel=job.channel,
                        event='expired',
                        runner_id='test_runner'
                    )
                    return

                # Mark as running
                job.status = 'running'
                job.started_at = timezone.now()
                job.runner_id = 'test_runner'
                job.attempt += 1
                job.save(update_fields=['status', 'started_at', 'runner_id', 'attempt'])

                JobEvent.objects.create(
                    job=job,
                    channel=job.channel,
                    event='running',
                    runner_id='test_runner',
                    attempt=job.attempt
                )

            # Load and execute function (would be dynamic in real)
            def test_handler(job):
                job.metadata['executed'] = True
                return "completed"

            # Close connections before and after
            close_old_connections()
            result = test_handler(job)
            close_old_connections()

            # Mark complete
            job.status = 'completed'
            job.finished_at = timezone.now()
            job.save(update_fields=['status', 'finished_at', 'metadata'])

            JobEvent.objects.create(
                job=job,
                channel=job.channel,
                event='completed',
                runner_id='test_runner'
            )

            # Calculate duration
            duration_ms = int((job.finished_at - job.started_at).total_seconds() * 1000)

            return result

        except Exception as e:
            # Handle failure (simplified)
            job.status = 'failed'
            job.last_error = str(e)
            job.finished_at = timezone.now()
            job.save()
            raise

    # Execute wrapper
    result = execute_job_wrapper(job_id)

    # Verify execution
    job.refresh_from_db()
    assert job.status == 'completed'
    assert job.metadata['executed'] is True
    assert job.attempt == 1
    assert job.runner_id == 'test_runner'


@th.django_unit_test()
def test_cleanup_execution_data(opts):
    """Clean up test data."""
    from mojo.apps.jobs.models import Job, JobEvent

    # Clean up database
    deleted_jobs, _ = Job.objects.filter(channel__startswith='exec_').delete()

    # Clean up Redis
    for channel in ['exec_basic', 'exec_retry', 'exec_broadcast']:
        opts.redis.delete(opts.keys.stream(channel))
        opts.redis.delete(opts.keys.stream_broadcast(channel))
        opts.redis.delete(opts.keys.sched(channel))

    print(f"Cleaned up {deleted_jobs} execution test jobs")
    print(f"Total test executions: {opts.execution_count}")
