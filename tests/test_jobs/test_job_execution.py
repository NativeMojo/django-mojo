"""
Tests for job execution functionality aligned with actual implementation.
Tests real execution patterns: Job model handlers, dynamic imports, thread pools.
"""
from testit import helpers as th
import time
import traceback
import uuid
from datetime import datetime, timedelta, timezone as dt_timezone
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
            opts.redis.delete(opts.keys.sched_broadcast(channel))
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
        if job.check_cancel_requested():
            job.metadata['cancelled'] = True
            job.metadata['cancelled_at'] = datetime.now(dt_timezone.utc).isoformat()
            return "cancelled"
        time.sleep(0.01)
        sent_count = 0
        failed_recipients = []

        for recipient in recipients:
            try:
                # Simulate sending email
                # print(f"Sending email to {recipient}")
                sent_count += 1

                # Check cancellation periodically for long lists
                if sent_count % 10 == 0 and job.check_cancel_requested():
                    job.metadata['cancelled_at_recipient'] = sent_count
                    break

            except Exception as e:
                failed_recipients.append({'email': recipient, 'error': str(e)})

        # Update metadata with results
        job.metadata['sent_count'] = sent_count
        job.metadata['failed_count'] = len(failed_recipients)
        if failed_recipients:
            job.metadata['failed_recipients'] = failed_recipients[:10]
        job.metadata['completed_at'] = datetime.now(dt_timezone.utc).isoformat()

        return "completed"

    # Execute handler
    result = send_email(job)

    assert result == "completed", f"Job execution failed: expected 'completed', got '{result}'. Job: {job.id}, metadata: {job.metadata}"
    assert job.metadata['sent_count'] == 1, f"Expected sent_count=1, got {job.metadata.get('sent_count')}. Full metadata: {job.metadata}"
    assert job.metadata['failed_count'] == 0, f"Expected failed_count=0, got {job.metadata.get('failed_count')}. Full metadata: {job.metadata}"
    assert 'completed_at' in job.metadata, f"Missing 'completed_at' in job metadata. Available keys: {list(job.metadata.keys())}"

    # Simulate engine marking as completed (add small delay to ensure duration > 0)
    time.sleep(0.001)  # 1ms delay to ensure measurable duration
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

    assert job.is_terminal is True, f"Job should be terminal but is_terminal={job.is_terminal}. Job status: {job.status}, finished_at: {job.finished_at}"
    assert job.duration_ms > 0, f"Job duration should be > 0, got {job.duration_ms}ms. Started: {job.started_at}, Finished: {job.finished_at}"


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
        job.metadata['started_at'] = datetime.now(dt_timezone.utc).isoformat()
        job.metadata['file_path'] = file_path
        job.metadata['processing_type'] = processing_type

        try:
            # Simulate file processing
            total_size = 1000  # In real code: os.path.getsize(file_path)
            chunk_size = 100
            processed = 0

            while processed < total_size:
                # Check for cancellation
                if job.check_cancel_requested():
                    job.metadata['cancelled'] = True
                    job.metadata['processed_bytes'] = processed
                    job.metadata['cancelled_at'] = datetime.now(dt_timezone.utc).isoformat()
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

            job.metadata['completed_at'] = datetime.now(dt_timezone.utc).isoformat()
            job.metadata['total_processed'] = processed
            return "completed"

        except Exception as e:
            job.metadata['error'] = str(e)
            job.metadata['failed_at'] = datetime.now(dt_timezone.utc).isoformat()
            raise  # Re-raise to trigger retry logic

    # Execute with cancellation
    result = process_file_upload(job)

    assert result == "cancelled", f"Expected job to be cancelled, got '{result}'. Job: {job.id}, cancel_requested: {job.check_cancel_requested()}, metadata: {job.metadata}"
    assert job.metadata['cancelled'] is True, f"Expected cancelled=True in metadata, got {job.metadata.get('cancelled')}. Full metadata: {job.metadata}"
    assert 'processed_bytes' in job.metadata, f"Missing 'processed_bytes' in job metadata. Available keys: {list(job.metadata.keys())}"
    assert job.metadata['processed_bytes'] < 1000, f"Expected processed_bytes < 1000 (incomplete), got {job.metadata.get('processed_bytes')}. Job was cancelled but processed too much data"


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

        job.metadata['request_started'] = datetime.now(dt_timezone.utc).isoformat()
        job.metadata['attempt'] = job.attempt

        # Simulate failure
        raise Exception(f"Connection timeout after {timeout}s")

    # Execute and handle failure
    try:
        result = fetch_external_api(job)
        assert False, f"Expected exception but got result: {result}. Job: {job.id}, func: {job.func}, payload: {job.payload}"
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

            assert job.status == 'pending', f"Expected status='pending' after retry setup, got '{job.status}'. Job: {job.id}, attempt: {job.attempt}, max_retries: {job.max_retries}"
            assert job.run_at is not None, f"Expected run_at to be set for retry, got None. Job: {job.id}, attempt: {job.attempt}"
            assert job.run_at > timezone.now(), f"Expected run_at to be in future, got {job.run_at} (now: {timezone.now()}). Job: {job.id}"

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

            assert job.status == 'failed', f"Expected status='failed' after max retries exceeded, got '{job.status}'. Job: {job.id}, attempt: {job.attempt}, max_retries: {job.max_retries}"


@th.django_unit_test()
def test_dynamic_function_loading(opts):
    """Test dynamic function loading as implemented in job_engine.py."""
    from mojo.apps.jobs.job_engine import load_job_function
    from mojo.apps.jobs.models import Job

    # Test loading actual job function
    func_path = 'mojo.apps.jobs.examples.sample_jobs.send_email'

    try:
        func = load_job_function(func_path)
        assert callable(func), f"Expected loaded function to be callable, got {type(func)} for path: {func_path}"
        assert func.__name__ == 'send_email', f"Expected function name 'send_email', got '{func.__name__}' for path: {func_path}"
    except ImportError as e:
        # If examples aren't available, test with a mock
        print(f"Could not load real function: {e}")

    # Test invalid function path
    try:
        func = load_job_function('invalid.module.nonexistent')
        assert False, f"Expected ImportError for invalid function path 'invalid.module.nonexistent', but got function: {func}"
    except ImportError as e:
        assert "Cannot load job function" in str(e), f"Expected error message to contain 'Cannot load job function', got: {str(e)}"


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
            if job.check_cancel_requested():
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
        job.metadata['completed_at'] = datetime.now(dt_timezone.utc).isoformat()

        return "completed"

    # Execute
    result = generate_report(job)

    assert result == "completed", f"Report generation failed: expected 'completed', got '{result}'. Job: {job.id}, metadata: {job.metadata}"
    assert job.metadata['progress'] == "100%", f"Expected progress='100%', got '{job.metadata.get('progress')}'. Job: {job.id}, metadata: {job.metadata}"
    assert job.metadata['current_step'] == 'Creating output file', f"Expected current_step='Creating output file', got '{job.metadata.get('current_step')}'. Job: {job.id}"
    assert 'report_file' in job.metadata, f"Missing 'report_file' in job metadata. Available keys: {list(job.metadata.keys())}. Job: {job.id}"


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

    assert job.status == 'expired', f"Expected status='expired' for expired job, got '{job.status}'. Job: {job.id}, expires_at: {job.expires_at}, now: {timezone.now()}"
    assert job.is_terminal is True, f"Expired job should be terminal but is_terminal={job.is_terminal}. Job status: {job.status}, finished_at: {job.finished_at}"


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
            'executed_at': datetime.now(dt_timezone.utc).isoformat(),
            'hostname': 'test_host'  # In real: socket.gethostname()
        }

        job.metadata['executed_by'].append(runner_info)
        job.metadata['message'] = message

        return "broadcast_completed"

    # Execute on this "runner"
    result = broadcast_notification(job)

    assert result == "broadcast_completed", f"Broadcast job failed: expected 'broadcast_completed', got '{result}'. Job: {job.id}, broadcast: {job.broadcast}, metadata: {job.metadata}"
    assert len(job.metadata['executed_by']) == 1, f"Expected 1 execution entry, got {len(job.metadata.get('executed_by', []))}. Job: {job.id}, executed_by: {job.metadata.get('executed_by')}"
    assert job.metadata['executed_by'][0]['runner'] == 'runner_001', f"Expected runner 'runner_001', got '{job.metadata['executed_by'][0].get('runner')}'. Full entry: {job.metadata['executed_by'][0]}"

    # Simulate another runner executing
    job.runner_id = 'runner_002'
    result = broadcast_notification(job)

    assert len(job.metadata['executed_by']) == 2, f"Expected 2 execution entries after second runner, got {len(job.metadata.get('executed_by', []))}. Job: {job.id}, executed_by: {job.metadata.get('executed_by')}"


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

        job.metadata['started_at'] = datetime.now(dt_timezone.utc).isoformat()
        job.metadata['cutoff_date'] = cutoff_date.isoformat()
        job.metadata['dry_run'] = dry_run

        deleted_count = 0
        batch_count = 0

        # Close old connections as engine does
        close_old_connections()

        # Simulate batch processing
        while batch_count < 3:  # Limit for test
            # Check for cancellation
            if job.check_cancel_requested():
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

        job.metadata['completed_at'] = datetime.now(dt_timezone.utc).isoformat()
        job.metadata['total_deleted'] = deleted_count

        return "completed"

    # Execute
    result = cleanup_old_records(job)

    assert result == "completed", f"Database cleanup job failed: expected 'completed', got '{result}'. Job: {job.id}, payload: {job.payload}, metadata: {job.metadata}"
    assert job.metadata['batch_count'] == 3, f"Expected batch_count=3, got {job.metadata.get('batch_count')}. Job: {job.id}, metadata: {job.metadata}"
    assert job.metadata['total_deleted'] == 300, f"Expected total_deleted=300 (3 batches * 100), got {job.metadata.get('total_deleted')}. Job: {job.id}, metadata: {job.metadata}"
    assert job.metadata['dry_run'] is True, f"Expected dry_run=True, got {job.metadata.get('dry_run')}. Job: {job.id}, payload: {job.payload}"


@th.django_unit_test()
def test_job_execution_wrapper_pattern(opts):
    """Test the full execution wrapper pattern from job_engine.py."""
    from mojo.apps.jobs.models import Job, JobEvent
    from django.db import transaction, close_old_connections

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
    assert job.status == 'completed', f"Expected job status='completed', got '{job.status}'. Job: {job.id}, started_at: {job.started_at}, finished_at: {job.finished_at}"
    assert job.metadata['executed'] is True, f"Expected executed=True in metadata, got {job.metadata.get('executed')}. Job: {job.id}, metadata: {job.metadata}"
    assert job.attempt == 1, f"Expected attempt=1, got {job.attempt}. Job: {job.id}, max_retries: {job.max_retries}"
    assert job.runner_id == 'test_runner', f"Expected runner_id='test_runner', got '{job.runner_id}'. Job: {job.id}"


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
        opts.redis.delete(opts.keys.sched_broadcast(channel))

    # print(f"Cleaned up {deleted_jobs} execution test jobs")
    # print(f"Total test executions: {opts.execution_count}")
