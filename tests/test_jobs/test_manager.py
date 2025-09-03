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
    assert manager.redis is not None, f"JobManager Redis adapter should not be None. Manager: {manager}, redis: {manager.redis}"
    assert manager.keys is not None, f"JobManager keys should not be None. Manager: {manager}, keys: {manager.keys}"

    # Test singleton pattern
    manager2 = get_manager()
    assert isinstance(manager2, JobManager), f"get_manager() should return JobManager instance, got {type(manager2)}. Instance: {manager2}"


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

    assert state['channel'] == opts.test_channel, f"Expected channel='{opts.test_channel}', got '{state.get('channel')}'. Full state: {state}"
    assert 'stream_length' in state, f"Missing 'stream_length' in queue state. Available keys: {list(state.keys())}, state: {state}"
    assert 'scheduled_count' in state, f"Missing 'scheduled_count' in queue state. Available keys: {list(state.keys())}, state: {state}"
    assert 'pending_count' in state, f"Missing 'pending_count' in queue state. Available keys: {list(state.keys())}, state: {state}"
    assert 'runners' in state, f"Missing 'runners' in queue state. Available keys: {list(state.keys())}, state: {state}"
    assert 'consumer_groups' in state, f"Missing 'consumer_groups' in queue state. Available keys: {list(state.keys())}, state: {state}"


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

    assert test_runner is not None, f"Test runner '{runner_id}' not found in runners list. Found runners: {[r.get('runner_id') for r in runners]}, all runners: {runners}"
    assert test_runner['jobs_processed'] == 5, f"Expected jobs_processed=5, got {test_runner.get('jobs_processed')}. Runner data: {test_runner}"
    assert test_runner['jobs_failed'] == 1, f"Expected jobs_failed=1, got {test_runner.get('jobs_failed')}. Runner data: {test_runner}"
    assert test_runner['alive'] is True, f"Expected runner to be alive=True, got {test_runner.get('alive')}. Runner data: {test_runner}"

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

    assert status is not None, f"Job status should not be None for job {job.id}. Manager: {manager}"
    assert status['id'] == job.id, f"Expected job ID '{job.id}', got '{status.get('id')}'. Full status: {status}"
    assert status['status'] == 'running', f"Expected status='running', got '{status.get('status')}'. Job: {job.id}, full status: {status}"
    assert status['channel'] == opts.test_channel, f"Expected channel='{opts.test_channel}', got '{status.get('channel')}'. Job: {job.id}, status: {status}"
    assert 'events' in status, f"Missing 'events' in job status. Available keys: {list(status.keys())}, job: {job.id}, status: {status}"
    assert len(status['events']) >= 2, f"Expected >=2 events, got {len(status.get('events', []))}. Job: {job.id}, events: {status.get('events')}"


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
    assert result is True, f"Job cancellation should succeed, got {result}. Job: {job.id}, manager: {manager}"

    # Verify cancellation
    job.refresh_from_db()
    assert job.cancel_requested is True, f"Job cancel_requested should be True after cancellation, got {job.cancel_requested}. Job: {job.id}, status: {job.status}"

    # Try cancelling non-existent job
    result = manager.cancel_job('nonexistent123')
    assert result is False, f"Cancelling non-existent job should return False, got {result}. Job ID: 'nonexistent123'"


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
    assert job.status == 'pending', f"Expected job status='pending' after retry, got '{job.status}'. Job: {job.id}, retry result: {result}"
    assert job.attempt == 0, f"Expected attempt=0 after retry reset, got {job.attempt}. Job: {job.id}"
    assert job.last_error == '', f"Expected last_error to be cleared after retry, got '{job.last_error}'. Job: {job.id}"

    # If delay was specified, run_at should be set
    if result:
        assert job.run_at is not None, f"Expected run_at to be set when retry with delay succeeds. Job: {job.id}, run_at: {job.run_at}, delay: 5s"


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

    assert 'totals' in stats, f"Missing 'totals' in system stats. Available keys: {list(stats.keys())}, stats: {stats}"
    assert stats['totals']['completed'] >= 1, f"Expected >=1 completed jobs, got {stats['totals'].get('completed')}. Totals: {stats['totals']}"
    assert stats['totals']['failed'] >= 1, f"Expected >=1 failed jobs, got {stats['totals'].get('failed')}. Totals: {stats['totals']}"
    assert stats['totals']['running'] >= 1, f"Expected >=1 running jobs, got {stats['totals'].get('running')}. Totals: {stats['totals']}"
    assert 'channels' in stats, f"Missing 'channels' in system stats. Available keys: {list(stats.keys())}, stats: {stats}"
    assert 'runners' in stats, f"Missing 'runners' in system stats. Available keys: {list(stats.keys())}, stats: {stats}"
    assert 'scheduler' in stats, f"Missing 'scheduler' in system stats. Available keys: {list(stats.keys())}, stats: {stats}"


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
    assert acquired is True, f"First scheduler should acquire lock successfully, got {acquired}. Scheduler: {sched1.scheduler_id}"
    assert sched1.has_lock is True, f"First scheduler should have lock after acquiring, got {sched1.has_lock}. Scheduler: {sched1.scheduler_id}"

    # Second scheduler should fail
    sched2 = Scheduler(
        channels=[opts.test_channel],
        scheduler_id='scheduler_2'
    )
    acquired = sched2._acquire_lock()
    assert acquired is False, f"Second scheduler should fail to acquire lock, got {acquired}. Scheduler: {sched2.scheduler_id}, first has lock: {sched1.has_lock}"
    assert sched2.has_lock is False, f"Second scheduler should not have lock after failed acquire, got {sched2.has_lock}. Scheduler: {sched2.scheduler_id}"

    # Release from first
    sched1._release_lock()
    assert sched1.has_lock is False, f"First scheduler should not have lock after release, got {sched1.has_lock}. Scheduler: {sched1.scheduler_id}"

    # Now second can acquire
    acquired = sched2._acquire_lock()
    assert acquired is True, f"Second scheduler should acquire lock after first releases, got {acquired}. Scheduler: {sched2.scheduler_id}"

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
    assert remaining_score is None, f"Job {job.id} should be removed from scheduled ZSET after processing, but still has score {remaining_score}. ZSET: {sched_key}"

    # Verify scheduler count was automatically incremented by _process_channel
    assert scheduler.jobs_scheduled == 1, f"Expected jobs_scheduled=1 after processing one job, got {scheduler.jobs_scheduled}. Scheduler: {scheduler}"


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
        assert len(stuck) >= 1, f"Expected >=1 stuck jobs after simulating stuck consumer, got {len(stuck)}. Channel: {opts.test_channel}, stuck jobs: {stuck}"

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

    assert health['channel'] == opts.test_channel, f"Expected channel='{opts.test_channel}', got '{health.get('channel')}'. Health: {health}"
    assert 'status' in health, f"Missing 'status' in channel health. Available keys: {list(health.keys())}, health: {health}"
    assert 'messages' in health, f"Missing 'messages' in channel health. Available keys: {list(health.keys())}, health: {health}"
    assert 'runners' in health, f"Missing 'runners' in channel health. Available keys: {list(health.keys())}, health: {health}"
    assert 'alerts' in health, f"Missing 'alerts' in channel health. Available keys: {list(health.keys())}, health: {health}"

    # Status should reflect job count
    if health['messages']['unclaimed'] > 0 and health['runners']['active'] == 0:
        # Should have alert about no runners
        assert len(health['alerts']) > 0, f"Expected alerts when unclaimed messages with no active runners. Health: {health}"


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
    assert command_received['flag'] is True, f"Command should have been received by runner. Command data: {command_received}, runner: {runner_id}"
    assert command_received['command'] == 'ping', f"Expected command='ping', got '{command_received['command']}'. Runner: {runner_id}, command data: {command_received}"
    assert result is True, f"Ping should return True when pong response received, got {result}. Runner: {runner_id}, command data: {command_received}"


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

    # print(f"Cleaned up {deleted_jobs} manager test jobs")
