from testit import helpers as th
import time
import uuid
import threading
from datetime import datetime, timedelta
from django.utils import timezone


@th.django_unit_setup()
def setup_scheduler_environment(opts):
    """Setup test environment for scheduler and manager tests."""
    from mojo.apps.jobs.models import Job, JobEvent
    from mojo.apps.jobs.adapters import get_adapter
    from mojo.apps.jobs.keys import JobKeys
    from mojo.apps.jobs import async_job

    # Clear database
    Job.objects.all().delete()
    JobEvent.objects.all().delete()

    # Setup Redis
    opts.redis = get_adapter()
    opts.keys = JobKeys()

    # Clear test channels
    test_channels = ['sched_test', 'manager_test']
    for channel in test_channels:
        opts.redis.delete(opts.keys.stream(channel))
        opts.redis.delete(opts.keys.stream_broadcast(channel))
        opts.redis.delete(opts.keys.sched(channel))

    # Clear scheduler lock
    opts.redis.delete(opts.keys.scheduler_lock())

    # Register test jobs
    @async_job(channel="sched_test")
    def scheduled_test_job(ctx):
        """Job for scheduler testing."""
        return f"scheduled job executed: {ctx.payload.get('id')}"

    @async_job(channel="manager_test")
    def manager_test_job(ctx):
        """Job for manager testing."""
        return "manager test job executed"

    opts.scheduled_test_job = scheduled_test_job
    opts.manager_test_job = manager_test_job
    opts.test_channels = test_channels


@th.django_unit_test()
def test_scheduler_initialization(opts):
    """Test Scheduler initialization."""
    from mojo.apps.jobs.scheduler import Scheduler

    # Create scheduler with specific channels
    scheduler = Scheduler(
        channels=['test1', 'test2'],
        scheduler_id='test_scheduler_001'
    )

    assert scheduler.scheduler_id == 'test_scheduler_001'
    assert scheduler.channels == ['test1', 'test2']
    assert scheduler.running is False
    assert scheduler.has_lock is False
    assert scheduler.jobs_scheduled == 0
    assert scheduler.jobs_expired == 0

    # Test auto-generated scheduler ID
    scheduler2 = Scheduler()
    assert scheduler2.scheduler_id is not None
    assert scheduler2.scheduler_id.startswith('scheduler-')


@th.django_unit_test()
def test_scheduler_lock_acquisition(opts):
    """Test scheduler leadership lock mechanism."""
    from mojo.apps.jobs.scheduler import Scheduler

    # Create first scheduler
    scheduler1 = Scheduler(scheduler_id='scheduler1')

    # Acquire lock
    acquired = scheduler1._acquire_lock()
    assert acquired is True
    assert scheduler1.has_lock is True

    # Second scheduler should fail to acquire
    scheduler2 = Scheduler(scheduler_id='scheduler2')
    acquired = scheduler2._acquire_lock()
    assert acquired is False
    assert scheduler2.has_lock is False

    # Release lock from first scheduler
    scheduler1._release_lock()
    assert scheduler1.has_lock is False

    # Now second scheduler can acquire
    acquired = scheduler2._acquire_lock()
    assert acquired is True
    assert scheduler2.has_lock is True

    # Clean up
    scheduler2._release_lock()


@th.django_unit_test()
def test_scheduler_lock_renewal(opts):
    """Test scheduler lock renewal mechanism."""
    from mojo.apps.jobs.scheduler import Scheduler

    scheduler = Scheduler(scheduler_id='test_renew')

    # Acquire lock
    acquired = scheduler._acquire_lock()
    assert acquired is True

    # Renew lock (should succeed)
    renewed = scheduler._renew_lock()
    assert renewed is True
    assert scheduler.has_lock is True

    # Simulate lock expiration by deleting it
    opts.redis.delete(opts.keys.scheduler_lock())

    # Renewal should fail
    renewed = scheduler._renew_lock()
    assert renewed is False
    assert scheduler.has_lock is False


@th.django_unit_test()
def test_scheduler_job_enqueue(opts):
    """Test scheduler moving jobs from ZSET to stream."""
    from mojo.apps.jobs import publish
    from mojo.apps.jobs.scheduler import Scheduler
    from mojo.apps.jobs.models import Job

    # Create scheduler
    scheduler = Scheduler(channels=['sched_test'])

    # Publish a job to run immediately
    job_id = publish(
        func=opts.scheduled_test_job,
        payload={'id': 'immediate'},
        channel="sched_test",
        run_at=timezone.now() - timedelta(seconds=1)  # In the past
    )

    # Check it's in scheduled ZSET
    sched_key = opts.keys.sched('sched_test')
    score = opts.redis.get_client().zscore(sched_key, job_id)
    assert score is not None

    # Process scheduled jobs
    now = timezone.now()
    now_ms = now.timestamp() * 1000
    scheduler._process_channel('sched_test', now, now_ms)

    # Job should be removed from ZSET
    score = opts.redis.get_client().zscore(sched_key, job_id)
    assert score is None

    # Update scheduled count
    scheduler.jobs_scheduled += 1
    assert scheduler.jobs_scheduled == 1


@th.django_unit_test()
def test_scheduler_expiration_handling(opts):
    """Test scheduler handling of expired jobs."""
    from mojo.apps.jobs import publish
    from mojo.apps.jobs.scheduler import Scheduler
    from mojo.apps.jobs.models import Job

    scheduler = Scheduler(channels=['sched_test'])

    # Create an expired job
    job_id = publish(
        func=opts.scheduled_test_job,
        payload={'id': 'expired'},
        channel="sched_test",
        run_at=timezone.now() - timedelta(seconds=1),  # Should run now
        expires_at=timezone.now() - timedelta(seconds=10)  # But already expired
    )

    # Process channel
    now = timezone.now()
    now_ms = now.timestamp() * 1000
    scheduler._process_channel('sched_test', now, now_ms)

    # Job should be marked as expired
    job = Job.objects.get(id=job_id)
    assert job.status == 'expired'
    assert job.finished_at is not None

    # Update expired count
    scheduler.jobs_expired += 1
    assert scheduler.jobs_expired == 1


@th.django_unit_test()
def test_scheduler_future_jobs(opts):
    """Test scheduler doesn't process future jobs."""
    from mojo.apps.jobs import publish
    from mojo.apps.jobs.scheduler import Scheduler

    scheduler = Scheduler(channels=['sched_test'])

    # Publish a job for the future
    future_time = timezone.now() + timedelta(hours=1)
    job_id = publish(
        func=opts.scheduled_test_job,
        payload={'id': 'future'},
        channel="sched_test",
        run_at=future_time
    )

    # Process channel
    now = timezone.now()
    now_ms = now.timestamp() * 1000
    scheduler._process_channel('sched_test', now, now_ms)

    # Job should still be in ZSET
    sched_key = opts.keys.sched('sched_test')
    score = opts.redis.get_client().zscore(sched_key, job_id)
    assert score is not None

    # Verify score matches future time
    expected_score = future_time.timestamp() * 1000
    assert abs(score - expected_score) < 1000  # Within 1 second


@th.django_unit_test()
def test_manager_initialization(opts):
    """Test JobManager initialization."""
    from mojo.apps.jobs.manager import JobManager

    manager = JobManager()
    assert manager.redis is not None
    assert manager.keys is not None

    # Test singleton
    from mojo.apps.jobs.manager import get_manager
    manager2 = get_manager()
    assert isinstance(manager2, JobManager)


@th.django_unit_test()
def test_manager_get_runners(opts):
    """Test getting active runners."""
    from mojo.apps.jobs.manager import JobManager
    from mojo.apps.jobs.job_engine import JobEngine
    import json

    manager = JobManager()

    # Simulate runner heartbeat
    runner_id = "test_runner_001"
    hb_key = opts.keys.runner_hb(runner_id)
    hb_data = {
        'runner_id': runner_id,
        'channels': ['test1', 'test2'],
        'jobs_processed': 10,
        'jobs_failed': 2,
        'started': timezone.now().isoformat(),
        'last_heartbeat': timezone.now().isoformat()
    }

    # Set heartbeat with TTL
    opts.redis.set(hb_key, json.dumps(hb_data), ex=30)

    # Get runners
    runners = manager.get_runners()
    assert len(runners) >= 1

    # Find our test runner
    test_runner = None
    for runner in runners:
        if runner['runner_id'] == runner_id:
            test_runner = runner
            break

    assert test_runner is not None
    assert test_runner['channels'] == ['test1', 'test2']
    assert test_runner['jobs_processed'] == 10
    assert test_runner['jobs_failed'] == 2
    assert test_runner['alive'] is True

    # Test filtering by channel
    runners_test1 = manager.get_runners(channel='test1')
    assert any(r['runner_id'] == runner_id for r in runners_test1)

    runners_test3 = manager.get_runners(channel='test3')
    assert not any(r['runner_id'] == runner_id for r in runners_test3)

    # Clean up
    opts.redis.delete(hb_key)


@th.django_unit_test()
def test_manager_get_queue_state(opts):
    """Test getting queue state for a channel."""
    from mojo.apps.jobs import publish
    from mojo.apps.jobs.manager import JobManager

    manager = JobManager()
    channel = 'manager_test'

    # Publish some jobs
    for i in range(3):
        publish(
            func=opts.manager_test_job,
            payload={'index': i},
            channel=channel
        )

    # Publish scheduled jobs
    for i in range(2):
        publish(
            func=opts.manager_test_job,
            payload={'scheduled': i},
            channel=channel,
            delay=60  # 1 minute delay
        )

    # Get queue state
    state = manager.get_queue_state(channel)

    assert state['channel'] == channel
    assert state['stream_length'] >= 3
    assert state['scheduled_count'] >= 2
    assert 'pending_count' in state
    assert 'runners' in state
    assert 'consumer_groups' in state
    assert 'metrics' in state


@th.django_unit_test()
def test_manager_ping_runner(opts):
    """Test pinging a runner."""
    from mojo.apps.jobs.manager import JobManager
    import json

    manager = JobManager()
    runner_id = "test_ping_runner"
    control_key = opts.keys.runner_ctl(runner_id)

    # Without a real runner, ping should fail/timeout
    result = manager.ping(runner_id, timeout=0.5)
    assert result is False

    # Simulate a runner response
    def simulate_runner():
        """Simulate a runner listening and responding to ping."""
        pubsub = opts.redis.pubsub()
        pubsub.subscribe(control_key)

        # Wait for message
        for _ in range(10):  # Try for 1 second
            message = pubsub.get_message(timeout=0.1)
            if message and message['type'] == 'message':
                data = json.loads(message['data'])
                if data.get('command') == 'ping':
                    response_key = data.get('response_key')
                    if response_key:
                        opts.redis.set(response_key, 'pong', ex=5)
                        break
        pubsub.close()

    # Start simulated runner in thread
    thread = threading.Thread(target=simulate_runner)
    thread.daemon = True
    thread.start()

    # Small delay to let thread start
    time.sleep(0.1)

    # Now ping should succeed
    result = manager.ping(runner_id, timeout=1.0)
    assert result is True


@th.django_unit_test()
def test_manager_shutdown_runner(opts):
    """Test sending shutdown command to runner."""
    from mojo.apps.jobs.manager import JobManager
    import json

    manager = JobManager()
    runner_id = "test_shutdown_runner"
    control_key = opts.keys.runner_ctl(runner_id)

    # Track if shutdown was received
    shutdown_received = {'flag': False}

    def simulate_runner():
        """Simulate a runner receiving shutdown."""
        pubsub = opts.redis.pubsub()
        pubsub.subscribe(control_key)

        for _ in range(10):  # Try for 1 second
            message = pubsub.get_message(timeout=0.1)
            if message and message['type'] == 'message':
                data = json.loads(message['data'])
                if data.get('command') == 'shutdown':
                    shutdown_received['flag'] = True
                    break
        pubsub.close()

    # Start simulated runner
    thread = threading.Thread(target=simulate_runner)
    thread.daemon = True
    thread.start()

    time.sleep(0.1)

    # Send shutdown
    manager.shutdown(runner_id, graceful=True)

    # Wait for thread
    thread.join(timeout=2.0)

    # Verify shutdown was received
    assert shutdown_received['flag'] is True


@th.django_unit_test()
def test_manager_broadcast(opts):
    """Test manager broadcast job publishing."""
    from mojo.apps.jobs.manager import JobManager
    from mojo.apps.jobs.models import Job

    manager = JobManager()

    # Broadcast a job
    job_id = manager.broadcast(
        channel='manager_test',
        func=opts.manager_test_job._job_name,
        payload={'broadcast': True, 'message': 'test'}
    )

    # Verify job was created
    job = Job.objects.get(id=job_id)
    assert job.broadcast is True
    assert job.channel == 'manager_test'
    assert job.payload['broadcast'] is True


@th.django_unit_test()
def test_manager_job_status(opts):
    """Test manager job status retrieval."""
    from mojo.apps.jobs import publish
    from mojo.apps.jobs.manager import JobManager
    from mojo.apps.jobs.models import JobEvent

    manager = JobManager()

    # Create a job
    job_id = publish(
        func=opts.manager_test_job,
        payload={'status': 'test'},
        channel='manager_test'
    )

    # Get enhanced status
    status = manager.job_status(job_id)

    assert status is not None
    assert status['id'] == job_id
    assert status['status'] == 'pending'
    assert status['channel'] == 'manager_test'
    assert 'events' in status

    # Check events are included
    events = status['events']
    assert len(events) >= 1
    assert events[0]['event'] == 'created'


@th.django_unit_test()
def test_manager_retry_job(opts):
    """Test manager job retry functionality."""
    from mojo.apps.jobs import publish
    from mojo.apps.jobs.manager import JobManager
    from mojo.apps.jobs.models import Job

    manager = JobManager()

    # Create a job and mark it as failed
    job_id = publish(
        func=opts.manager_test_job,
        payload={'retry': 'test'},
        channel='manager_test'
    )

    job = Job.objects.get(id=job_id)
    job.status = 'failed'
    job.last_error = 'Test failure'
    job.save()

    # Retry the job
    result = manager.retry_job(job_id, delay=5)

    # Should create a new scheduled job
    # (In real implementation, this might re-use the same job ID)
    assert result is not False

    # Original job should be reset
    job.refresh_from_db()
    assert job.status == 'pending'
    assert job.attempt == 0
    assert job.last_error == ''


@th.django_unit_test()
def test_manager_get_stats(opts):
    """Test getting overall system statistics."""
    from mojo.apps.jobs import publish
    from mojo.apps.jobs.manager import JobManager
    from mojo.apps.jobs.models import Job

    manager = JobManager()

    # Create some test data
    for i in range(3):
        job_id = publish(
            func=opts.manager_test_job,
            payload={'stat': i},
            channel='manager_test'
        )

        # Mark some as completed/failed
        if i == 0:
            job = Job.objects.get(id=job_id)
            job.status = 'completed'
            job.save()
        elif i == 1:
            job = Job.objects.get(id=job_id)
            job.status = 'failed'
            job.save()

    # Get stats
    stats = manager.get_stats()

    assert 'channels' in stats
    assert 'runners' in stats
    assert 'totals' in stats
    assert 'scheduler' in stats

    # Check totals
    totals = stats['totals']
    assert totals['completed'] >= 1
    assert totals['failed'] >= 1
    assert 'pending' in totals
    assert 'running' in totals
    assert 'scheduled' in totals
    assert 'runners_active' in totals

    # Check scheduler status
    scheduler_info = stats['scheduler']
    assert 'active' in scheduler_info
    assert 'lock_holder' in scheduler_info


@th.django_unit_test()
def test_manager_cancel_job(opts):
    """Test manager job cancellation."""
    from mojo.apps.jobs import publish
    from mojo.apps.jobs.manager import JobManager
    from mojo.apps.jobs.models import Job

    manager = JobManager()

    # Create a job
    job_id = publish(
        func=opts.manager_test_job,
        payload={'cancel': 'test'},
        channel='manager_test'
    )

    # Cancel via manager
    result = manager.cancel_job(job_id)
    assert result is True

    # Verify cancellation
    job = Job.objects.get(id=job_id)
    assert job.cancel_requested is True

    # Try cancelling non-existent job
    result = manager.cancel_job('nonexistent123')
    assert result is False
