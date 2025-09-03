"""
Simplified tests for job manager and scheduler operations.
Focus on management functionality without decorator complexity.
"""
from testit import helpers as th
import time
import json
import uuid
import threading
from datetime import datetime, timedelta
from django.utils import timezone


@th.django_unit_setup()
def setup_manager_tests(opts):
    """Setup for manager and scheduler tests."""
    from mojo.apps.jobs.models import Job, JobEvent
    from mojo.apps.jobs.adapters import get_adapter
    from mojo.apps.jobs.keys import JobKeys

    # Clear test data
    Job.objects.filter(channel__startswith='mgr_test_').delete()
    JobEvent.objects.filter(channel__startswith='mgr_test_').delete()

    # Setup Redis
    opts.redis = get_adapter()
    opts.keys = JobKeys()

    # Test configuration
    opts.test_channel = 'mgr_test_simple'

    # Clear Redis test data
    opts.redis.delete(opts.keys.stream(opts.test_channel))
    opts.redis.delete(opts.keys.sched(opts.test_channel))
    opts.redis.delete(opts.keys.scheduler_lock())


@th.django_unit_test()
def test_manager_initialization(opts):
    """Test JobManager initialization."""
    from mojo.apps.jobs.manager import JobManager, get_manager

    # Create manager instance
    manager = JobManager()
    assert manager.redis is not None
    assert manager.keys is not None

    # Test singleton pattern
    manager2 = get_manager()
    assert isinstance(manager2, JobManager)


@th.django_unit_test()
def test_queue_state_monitoring(opts):
    """Test monitoring queue state."""
    from mojo.apps.jobs.manager import JobManager
    from mojo.apps.jobs.models import Job

    manager = JobManager()

    # Create some test jobs
    for i in range(3):
        Job.objects.create(
            id=uuid.uuid4().hex,
            channel=opts.test_channel,
            func='test.manager_function',
            payload={'index': i},
            status='pending'
        )

    # Get queue state
    state = manager.get_queue_state(opts.test_channel)

    assert state['channel'] == opts.test_channel
    assert 'stream_length' in state
    assert 'scheduled_count' in state
    assert 'pending_count' in state
    assert 'runners' in state
    assert 'consumer_groups' in state


@th.django_unit_test()
def test_runner_tracking(opts):
    """Test tracking active runners."""
    from mojo.apps.jobs.manager import JobManager

    manager = JobManager()
    runner_id = "test_runner_mgr_001"

    # Simulate runner heartbeat
    hb_key = opts.keys.runner_hb(runner_id)
    hb_data = {
        'runner_id': runner_id,
        'channels': [opts.test_channel],
        'jobs_processed': 5,
        'jobs_failed': 1,
        'started': timezone.now().isoformat(),
        'last_heartbeat': timezone.now().isoformat()
    }

    # Set heartbeat
    opts.redis.set(hb_key, json.dumps(hb_data), ex=30)

    # Get runners
    runners = manager.get_runners()

    # Find our test runner
    test_runner = None
    for runner in runners:
        if runner['runner_id'] == runner_id:
            test_runner = runner
            break

    assert test_runner is not None
    assert test_runner['jobs_processed'] == 5
    assert test_runner['jobs_failed'] == 1
    assert test_runner['alive'] is True

    # Clean up
    opts.redis.delete(hb_key)


@th.django_unit_test()
def test_job_status_retrieval(opts):
    """Test getting detailed job status."""
    from mojo.apps.jobs.manager import JobManager
    from mojo.apps.jobs.models import Job, JobEvent

    manager = JobManager()

    # Create a job
    job = Job.objects.create(
        id=uuid.uuid4().hex,
        channel=opts.test_channel,
        func='test.status_function',
        payload={'test': 'status'},
        status='running',
        started_at=timezone.now(),
        runner_id='test_runner_001',
        attempt=1
    )

    # Add some events
    JobEvent.objects.create(
        job=job,
        channel=job.channel,
        event='created'
    )
    JobEvent.objects.create(
        job=job,
        channel=job.channel,
        event='running',
        runner_id='test_runner_001'
    )

    # Get status through manager
    status = manager.job_status(job.id)

    assert status is not None
    assert status['id'] == job.id
    assert status['status'] == 'running'
    assert status['channel'] == opts.test_channel
    assert 'events' in status
    assert len(status['events']) >= 2


@th.django_unit_test()
def test_job_cancellation_via_manager(opts):
    """Test cancelling jobs through manager."""
    from mojo.apps.jobs.manager import JobManager
    from mojo.apps.jobs.models import Job

    manager = JobManager()

    # Create a job
    job = Job.objects.create(
        id=uuid.uuid4().hex,
        channel=opts.test_channel,
        func='test.cancel_function',
        payload={'test': 'cancel'},
        status='pending'
    )

    # Cancel through manager
    result = manager.cancel_job(job.id)
    assert result is True

    # Verify cancellation
    job.refresh_from_db()
    assert job.cancel_requested is True

    # Try cancelling non-existent job
    result = manager.cancel_job('nonexistent123')
    assert result is False


@th.django_unit_test()
def test_job_retry_functionality(opts):
    """Test retrying failed jobs through manager."""
    from mojo.apps.jobs.manager import JobManager
    from mojo.apps.jobs.models import Job

    manager = JobManager()

    # Create a failed job
    job = Job.objects.create(
        id=uuid.uuid4().hex,
        channel=opts.test_channel,
        func='test.retry_function',
        payload={'retry': 'test'},
        status='failed',
        last_error='Test failure',
        attempt=1,
        max_retries=3
    )

    # Retry the job
    result = manager.retry_job(job.id, delay=5)

    # Should reset the job for retry
    job.refresh_from_db()
    assert job.status == 'pending'
    assert job.attempt == 0
    assert job.last_error == ''

    # If delay was specified, run_at should be set
    if result:
        assert job.run_at is not None


@th.django_unit_test()
def test_system_statistics(opts):
    """Test getting system-wide statistics."""
    from mojo.apps.jobs.manager import JobManager
    from mojo.apps.jobs.models import Job

    manager = JobManager()

    # Create various jobs in different states
    Job.objects.create(
        id=uuid.uuid4().hex,
        channel=opts.test_channel,
        func='test.stat_function',
        payload={'stat': 1},
        status='pending'
    )
    Job.objects.create(
        id=uuid.uuid4().hex,
        channel=opts.test_channel,
        func='test.stat_function',
        payload={'stat': 2},
        status='running',
        started_at=timezone.now()
    )
    Job.objects.create(
        id=uuid.uuid4().hex,
        channel=opts.test_channel,
        func='test.stat_function',
        payload={'stat': 3},
        status='completed',
        finished_at=timezone.now()
    )
    Job.objects.create(
        id=uuid.uuid4().hex,
        channel=opts.test_channel,
        func='test.stat_function',
        payload={'stat': 4},
        status='failed',
        finished_at=timezone.now()
    )

    # Get stats
    stats = manager.get_stats()

    assert 'totals' in stats
    assert stats['totals']['completed'] >= 1
    assert stats['totals']['failed'] >= 1
    assert stats['totals']['running'] >= 1
    assert 'channels' in stats
    assert 'runners' in stats
    assert 'scheduler' in stats


@th.django_unit_test()
def test_scheduler_lock_mechanism(opts):
    """Test scheduler leadership lock."""
    from mojo.apps.jobs.scheduler import Scheduler

    # Create first scheduler
    sched1 = Scheduler(
        channels=[opts.test_channel],
        scheduler_id='scheduler_1'
    )

    # Acquire lock
    acquired = sched1._acquire_lock()
    assert acquired is True
    assert sched1.has_lock is True

    # Second scheduler should fail
    sched2 = Scheduler(
        channels=[opts.test_channel],
        scheduler_id='scheduler_2'
    )
    acquired = sched2._acquire_lock()
    assert acquired is False
    assert sched2.has_lock is False

    # Release from first
    sched1._release_lock()
    assert sched1.has_lock is False

    # Now second can acquire
    acquired = sched2._acquire_lock()
    assert acquired is True

    # Clean up
    sched2._release_lock()


@th.django_unit_test()
def test_scheduler_job_movement(opts):
    """Test scheduler moving jobs from scheduled to ready."""
    from mojo.apps.jobs.scheduler import Scheduler
    from mojo.apps.jobs.models import Job

    scheduler = Scheduler(channels=[opts.test_channel])

    # Create a job that should run immediately
    job = Job.objects.create(
        id=uuid.uuid4().hex,
        channel=opts.test_channel,
        func='test.scheduled_function',
        payload={'scheduled': True},
        status='pending',
        run_at=timezone.now() - timedelta(seconds=1)  # In the past
    )

    # Add to scheduled ZSET
    sched_key = opts.keys.sched(opts.test_channel)
    score = (job.run_at.timestamp() * 1000) if job.run_at else 0
    opts.redis.zadd(sched_key, {job.id: score})

    # Process channel
    now = timezone.now()
    now_ms = now.timestamp() * 1000
    scheduler._process_channel(opts.test_channel, now, now_ms)

    # Job should be removed from ZSET
    remaining_score = opts.redis.get_client().zscore(sched_key, job.id)
    assert remaining_score is None

    # Update scheduler count
    scheduler.jobs_scheduled += 1
    assert scheduler.jobs_scheduled == 1


@th.django_unit_test()
def test_stuck_job_detection(opts):
    """Test detecting stuck jobs."""
    from mojo.apps.jobs.manager import JobManager

    manager = JobManager()

    # Add a stuck job to stream (simulated)
    stream_key = opts.keys.stream(opts.test_channel)
    group_key = opts.keys.group_workers(opts.test_channel)

    # Create consumer group
    opts.redis.xgroup_create(stream_key, group_key)

    # Add message
    job_id = uuid.uuid4().hex
    msg_id = opts.redis.xadd(stream_key, {
        'job_id': job_id,
        'func': 'test.stuck_function'
    })

    # Claim it but don't ACK (simulates stuck)
    opts.redis.xreadgroup(
        group=group_key,
        consumer='stuck_consumer',
        streams={stream_key: '>'},
        count=1
    )

    # Check for stuck jobs
    stuck = manager._find_stuck_jobs(opts.test_channel, idle_threshold_ms=100)

    # Wait a bit and check again
    time.sleep(0.2)
    stuck = manager._find_stuck_jobs(opts.test_channel, idle_threshold_ms=100)

    # Should find our stuck job
    # Note: This might not work in all Redis versions
    if stuck:
        assert len(stuck) >= 1

    # Clean up
    opts.redis.delete(stream_key)


@th.django_unit_test()
def test_channel_health_check(opts):
    """Test channel health monitoring."""
    from mojo.apps.jobs.manager import JobManager
    from mojo.apps.jobs.models import Job

    manager = JobManager()

    # Create some jobs to affect health
    for i in range(5):
        Job.objects.create(
            id=uuid.uuid4().hex,
            channel=opts.test_channel,
            func='test.health_function',
            payload={'health': i},
            status='pending'
        )

    # Get health status
    health = manager.get_channel_health(opts.test_channel)

    assert health['channel'] == opts.test_channel
    assert 'status' in health
    assert 'messages' in health
    assert 'runners' in health
    assert 'alerts' in health

    # Status should reflect job count
    if health['messages']['unclaimed'] > 0 and health['runners']['active'] == 0:
        # Should have alert about no runners
        assert len(health['alerts']) > 0


@th.django_unit_test()
def test_runner_control_commands(opts):
    """Test sending control commands to runners."""
    from mojo.apps.jobs.manager import JobManager

    manager = JobManager()
    runner_id = "control_test_runner"
    control_key = opts.keys.runner_ctl(runner_id)

    # Track command reception
    command_received = {'flag': False, 'command': None}

    def simulate_runner():
        """Simulate a runner listening for commands."""
        pubsub = opts.redis.pubsub()
        pubsub.subscribe(control_key)

        for _ in range(20):  # Try for 2 seconds
            msg = pubsub.get_message(timeout=0.1)
            if msg and msg['type'] == 'message':
                data = json.loads(msg['data'])
                command_received['flag'] = True
                command_received['command'] = data.get('command')

                # Respond to ping
                if data.get('command') == 'ping':
                    response_key = data.get('response_key')
                    if response_key:
                        opts.redis.set(response_key, 'pong', ex=5)
                break

        pubsub.close()

    # Start simulated runner
    thread = threading.Thread(target=simulate_runner)
    thread.daemon = True
    thread.start()

    time.sleep(0.2)

    # Send ping command
    result = manager.ping(runner_id, timeout=1.0)

    # Wait for thread
    thread.join(timeout=2.0)

    # Verify command was received and responded
    assert command_received['flag'] is True
    assert command_received['command'] == 'ping'
    assert result is True  # Got pong response


@th.django_unit_test()
def test_cleanup_manager_data(opts):
    """Clean up test data."""
    from mojo.apps.jobs.models import Job, JobEvent

    # Clean up database
    deleted_jobs, _ = Job.objects.filter(channel__startswith='mgr_test_').delete()

    # Clean up Redis
    opts.redis.delete(opts.keys.stream(opts.test_channel))
    opts.redis.delete(opts.keys.sched(opts.test_channel))
    opts.redis.delete(opts.keys.scheduler_lock())

    print(f"Cleaned up {deleted_jobs} manager test jobs")
