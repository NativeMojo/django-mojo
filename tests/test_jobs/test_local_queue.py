"""Tests for local queue functionality and publish_local function."""

from testit import helpers as th
import time
import threading
from datetime import timedelta
from unittest.mock import patch, MagicMock

from django.utils import timezone
from mojo.apps.jobs import publish_local
from mojo.apps.jobs.local_queue import reset_local_queue, get_local_queue


@th.django_unit_setup()
def setup_local_queue_tests(opts):
    """Setup for local queue tests."""
    opts.result_containers = []

    def create_result_container():
        """Factory function to create new result containers for each test."""
        container = []
        opts.result_containers.append(container)
        return container

    opts.create_result_container = create_result_container


@th.django_unit_test()
def test_publish_local_with_callable(opts):
    """Test publish_local with a callable function."""
    # Reset the local queue before test
    reset_local_queue()

    result_container = opts.create_result_container()

    def capture_result(x, y=10):
        result = x + y
        result_container.append(result)
        return result

    # Publish a job
    job_id = publish_local(capture_result, 5, y=15)

    # Wait a moment for execution (single thread processes sequentially)
    time.sleep(0.2)

    # Check that the job was executed
    assert len(result_container) == 1, f"Expected 1 result, got {len(result_container)}"
    assert result_container[0] == 20, f"Expected result 20, got {result_container[0]}"

    # Clean up
    reset_local_queue()


@th.django_unit_test()
def test_publish_local_with_delay(opts):
    """Test publish_local with delayed execution using delay parameter."""
    reset_local_queue()

    result_container = opts.create_result_container()
    start_time = timezone.now()

    def capture_with_timestamp():
        result_container.append(timezone.now())

    # Schedule job to run 0.3 seconds in the future using delay
    job_id = publish_local(capture_with_timestamp, delay=0.3)

    # Check it hasn't run immediately
    time.sleep(0.1)
    assert len(result_container) == 0, "Job should not have run immediately"

    # Wait for delayed execution
    time.sleep(0.4)

    # Check it ran after the delay
    assert len(result_container) == 1, f"Expected 1 result after delay, got {len(result_container)}"

    # Check timing (should be at least 0.3s after start)
    execution_time = result_container[0]
    delay = (execution_time - start_time).total_seconds()
    assert delay >= 0.25, f"Job should have been delayed by ~0.3s, but only took {delay}s"

    reset_local_queue()


@th.django_unit_test()
def test_publish_local_with_run_at(opts):
    """Test publish_local with run_at parameter."""
    reset_local_queue()

    result_container = opts.create_result_container()
    start_time = timezone.now()

    def capture_with_timestamp():
        result_container.append(timezone.now())

    # Schedule job to run 0.3 seconds in the future using run_at
    run_at = timezone.now() + timedelta(seconds=0.3)
    job_id = publish_local(capture_with_timestamp, run_at=run_at)

    # Check it hasn't run immediately
    time.sleep(0.1)
    assert len(result_container) == 0, "Job should not have run immediately"

    # Wait for delayed execution
    time.sleep(0.4)

    # Check it ran after the delay
    assert len(result_container) == 1, f"Expected 1 result after delay, got {len(result_container)}"

    # Check timing
    execution_time = result_container[0]
    delay = (execution_time - start_time).total_seconds()
    assert delay >= 0.25, f"Job should have been delayed by ~0.3s, but only took {delay}s"

    reset_local_queue()


@th.django_unit_test()
def test_local_queue_immediate_execution(opts):
    """Test that jobs without delay execute immediately."""
    reset_local_queue()

    result_container = opts.create_result_container()

    def immediate_job():
        result_container.append('executed')

    # Add immediate job
    queue = get_local_queue()
    queue.put(immediate_job, (), {}, "immediate")

    # Should execute quickly in single worker thread
    time.sleep(0.1)
    assert result_container == ['executed'], f"Job should execute immediately, got {result_container}"

    reset_local_queue()


@th.django_unit_test()
def test_multiple_sequential_jobs(opts):
    """Test that multiple jobs run sequentially in order."""
    reset_local_queue()

    results = []

    def sequential_job(job_name):
        time.sleep(0.05)  # Small delay to make timing visible
        results.append(job_name)

    # Start multiple jobs - they should run in order
    start_time = time.time()
    publish_local(sequential_job, 'job1')
    publish_local(sequential_job, 'job2')
    publish_local(sequential_job, 'job3')

    # Wait for all to complete (should take ~0.15s sequential)
    time.sleep(0.3)

    # All should have completed in order
    assert len(results) == 3, f"Expected 3 results, got {len(results)}"
    assert results == ['job1', 'job2', 'job3'], f"Expected ordered execution, got {results}"

    # Should have taken ~0.15s (sequential) due to single worker thread
    total_time = time.time() - start_time
    assert total_time >= 0.1, f"Jobs should run sequentially, took {total_time}s"

    reset_local_queue()


@th.django_unit_test()
def test_publish_local_error_handling(opts):
    """Test that publish_local handles errors gracefully."""
    reset_local_queue()

    def failing_job():
        raise ValueError("Test error")

    # This should not raise an exception
    job_id = publish_local(failing_job)

    # Wait for execution
    time.sleep(0.2)

    # Queue should still be working
    queue = get_local_queue()
    stats = queue.stats()
    assert stats['errors'] >= 1, "Should have recorded at least one error"

    reset_local_queue()


@th.django_unit_test()
def test_local_queue_stats(opts):
    """Test queue statistics."""
    reset_local_queue()

    results = []

    def test_job():
        results.append(True)

    queue = get_local_queue()
    initial_stats = queue.stats()

    # Should show running after first job is added
    assert initial_stats['size'] == 0, "Queue should start empty"

    # Add some jobs
    queue.put(test_job, (), {}, "job1")
    queue.put(test_job, (), {}, "job2")

    # Wait for execution
    time.sleep(0.2)

    final_stats = queue.stats()
    assert final_stats['running'], "Queue should be running after jobs added"
    assert final_stats['processed'] >= 2, f"Should have processed at least 2 jobs, got {final_stats['processed']}"

    reset_local_queue()


@th.django_unit_test()
def test_publish_local_with_string_function(opts):
    """Test publish_local with string function path."""
    reset_local_queue()

    result_container = opts.create_result_container()

    # Mock the import to use our own function
    def mock_job():
        result_container.append('string_func_executed')

    with patch('importlib.import_module') as mock_import:
        mock_module = MagicMock()
        mock_module.test_func = mock_job
        mock_import.return_value = mock_module

        job_id = publish_local('test.module.test_func')

        # Wait for execution
        time.sleep(0.2)

        assert len(result_container) == 1, "String function should have been executed"
        assert result_container[0] == 'string_func_executed'

    reset_local_queue()


@th.django_unit_test()
def test_run_at_overrides_delay(opts):
    """Test that run_at parameter overrides delay parameter."""
    reset_local_queue()

    result_container = opts.create_result_container()
    start_time = timezone.now()

    def test_job():
        result_container.append(timezone.now())

    # Set both delay (1 second) and run_at (0.2 seconds), run_at should win
    run_at = timezone.now() + timedelta(seconds=0.2)
    job_id = publish_local(test_job, delay=1, run_at=run_at)

    # Wait for execution
    time.sleep(0.4)

    # Should have executed at run_at time (~0.2s), not delay time (1s)
    assert len(result_container) == 1, "Job should have executed"
    execution_time = result_container[0]
    actual_delay = (execution_time - start_time).total_seconds()
    assert 0.15 <= actual_delay <= 0.35, f"Expected ~0.2s delay, got {actual_delay}s"

    reset_local_queue()


@th.django_unit_test()
def test_local_queue_submission_thread_safety(opts):
    """Test that submitting jobs from multiple threads is safe."""
    reset_local_queue()

    results = []

    def simple_job(job_id):
        # No sleep needed - single worker processes sequentially
        results.append(job_id)

    # Submit jobs from multiple threads simultaneously
    threads = []
    for i in range(5):
        def submit_job(job_num=i):
            publish_local(simple_job, f"job_{job_num}")

        thread = threading.Thread(target=submit_job)
        threads.append(thread)
        thread.start()

    # Wait for all submission threads to complete
    for thread in threads:
        thread.join()

    # Wait for all jobs to execute (sequential processing)
    time.sleep(0.3)

    # Should have all 5 jobs completed (order may vary due to threading)
    assert len(results) == 5, f"Expected 5 jobs, got {len(results)}"
    expected_jobs = {f"job_{i}" for i in range(5)}
    actual_jobs = set(results)
    assert actual_jobs == expected_jobs, f"Expected {expected_jobs}, got {actual_jobs}"

    reset_local_queue()


@th.django_unit_test()
def test_local_queue_job_id_uniqueness(opts):
    """Test that job IDs are unique."""
    reset_local_queue()

    job_ids = []

    def capture_job_id_job():
        pass

    # Generate multiple job IDs quickly
    for _ in range(10):
        job_id = publish_local(capture_job_id_job)
        job_ids.append(job_id)

    # All job IDs should be unique
    assert len(set(job_ids)) == len(job_ids), f"Job IDs are not unique: {job_ids}"

    # Job IDs should start with 'local-'
    for job_id in job_ids:
        assert job_id.startswith('local-'), f"Job ID should start with 'local-': {job_id}"

    reset_local_queue()


@th.django_unit_test()
def test_local_queue_graceful_shutdown(opts):
    """Test that the local queue shuts down gracefully."""
    reset_local_queue()

    results = []

    def slow_job(job_name):
        time.sleep(0.1)  # Simulate some work
        results.append(job_name)

    # Get queue instance and start it
    queue = get_local_queue()

    # Add some jobs
    queue.put(slow_job, ('job1',), {}, "job1")
    queue.put(slow_job, ('job2',), {}, "job2")

    # Let first job start
    time.sleep(0.05)

    # Stop the queue
    queue.stop(timeout=1.0)

    # Should have processed at least one job
    assert len(results) >= 1, f"Expected at least 1 job processed, got {len(results)}"

    # Queue should be stopped
    stats = queue.stats()
    assert not stats['running'], "Queue should be stopped"

    reset_local_queue()
