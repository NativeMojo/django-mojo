"""
Tests for user-scheduled tasks: model CRUD, dispatch logic, job execution,
owner scoping, timezone conversion, run_once, and notifications.
"""
from testit import helpers as th
import uuid
from datetime import datetime, timedelta
from django.utils import timezone


TEST_USER_A = "test_sched_user_a"
TEST_USER_B = "test_sched_user_b"
TEST_PWORD = "testpass123"


@th.django_unit_setup()
def setup_scheduled_tasks(opts):
    """Setup test users and clean up old test data."""
    from mojo.apps.account.models import User, Group
    from mojo.apps.jobs.models import ScheduledTask, TaskResult
    from mojo.decorators.limits import clear_rate_limits
    clear_rate_limits(ip="127.0.0.1")

    # Clean up from previous runs
    User.objects.filter(username__in=[TEST_USER_A, TEST_USER_B]).delete()
    ScheduledTask.objects.filter(name__startswith="test_sched_").delete()

    # Create test group with timezone
    group, _ = Group.objects.get_or_create(
        name="test_sched_group",
        defaults={"metadata": {"timezone": "America/New_York"}}
    )
    group.metadata["timezone"] = "America/New_York"
    group.save()

    # Create test users
    user_a, _ = User.objects.get_or_create(username=TEST_USER_A, defaults={"email": "sched_a@test.com", "display_name": TEST_USER_A})
    user_a.save_password(TEST_PWORD)
    user_a.org = group
    user_a.add_perm("jobs")
    user_a.save()
    opts.user_a = user_a

    user_b, _ = User.objects.get_or_create(username=TEST_USER_B, defaults={"email": "sched_b@test.com", "display_name": TEST_USER_B})
    user_b.save_password(TEST_PWORD)
    user_b.org = group
    user_b.save()
    opts.user_b = user_b



@th.django_unit_test()
def test_create_scheduled_task(opts):
    """Test creating a scheduled task with valid data."""
    from mojo.apps.jobs.models import ScheduledTask

    task = ScheduledTask(
        user=opts.user_a,
        name="test_sched_daily_report",
        description="Daily report at 9am",
        task_type="llm",
        run_times=["09:00"],
        run_days=[],
        job_config={"system_prompt": "You are a reporter.", "user_prompt": "Give me a summary."},
        notify=["email"],
    )
    task.save()

    assert task.id, "Task should have an auto-generated ID"
    assert len(task.id) == 32, f"Task ID should be 32-char UUID hex, got {len(task.id)}"
    assert task.enabled is True, "Task should be enabled by default"
    assert task.run_once is False, "Task should not be run_once by default"
    assert task.run_count == 0, "Task should start with 0 runs"

    # Clean up
    task.delete()


@th.django_unit_test()
def test_validate_run_times_format(opts):
    """Test that invalid run_times are rejected."""
    from mojo.apps.jobs.models import ScheduledTask

    task = ScheduledTask(
        user=opts.user_a,
        name="test_sched_bad_times",
        task_type="job",
        run_times=["25:00"],  # invalid hour
        job_config={"func": "some.func"},
    )

    try:
        task.save()
        assert False, "Should have raised ValueError for invalid time"
    except ValueError as e:
        assert "Invalid time" in str(e), f"Expected time validation error, got: {e}"


@th.django_unit_test()
def test_validate_max_run_times(opts):
    """Test that more than 2 run_times are rejected."""
    from mojo.apps.jobs.models import ScheduledTask

    task = ScheduledTask(
        user=opts.user_a,
        name="test_sched_too_many",
        task_type="job",
        run_times=["09:00", "12:00", "17:00"],  # 3 entries
        job_config={"func": "some.func"},
    )

    try:
        task.save()
        assert False, "Should have raised ValueError for >2 run_times"
    except ValueError as e:
        assert "more than 2" in str(e), f"Expected max entries error, got: {e}"


@th.django_unit_test()
def test_validate_run_days(opts):
    """Test that invalid run_days are rejected."""
    from mojo.apps.jobs.models import ScheduledTask

    task = ScheduledTask(
        user=opts.user_a,
        name="test_sched_bad_days",
        task_type="job",
        run_times=["09:00"],
        run_days=[7],  # invalid, max is 6
        job_config={"func": "some.func"},
    )

    try:
        task.save()
        assert False, "Should have raised ValueError for invalid weekday"
    except ValueError as e:
        assert "Invalid weekday" in str(e), f"Expected weekday validation error, got: {e}"


@th.django_unit_test()
def test_validate_notify_channels(opts):
    """Test that invalid notify channels are rejected."""
    from mojo.apps.jobs.models import ScheduledTask

    task = ScheduledTask(
        user=opts.user_a,
        name="test_sched_bad_notify",
        task_type="job",
        run_times=["09:00"],
        notify=["telegram"],  # not a valid channel
        job_config={"func": "some.func"},
    )

    try:
        task.save()
        assert False, "Should have raised ValueError for invalid notify channel"
    except ValueError as e:
        assert "Invalid notify channel" in str(e), f"Expected notify validation error, got: {e}"


@th.django_unit_test()
def test_matches_day(opts):
    """Test weekday matching logic."""
    from mojo.apps.jobs.models import ScheduledTask

    task = ScheduledTask(
        user=opts.user_a,
        name="test_sched_days",
        task_type="job",
        run_times=["09:00"],
        run_days=[0, 2, 4],  # Mon, Wed, Fri
        job_config={"func": "some.func"},
    )

    assert task.matches_day(0) is True, "Should match Monday"
    assert task.matches_day(2) is True, "Should match Wednesday"
    assert task.matches_day(1) is False, "Should not match Tuesday"
    assert task.matches_day(6) is False, "Should not match Sunday"


@th.django_unit_test()
def test_matches_day_empty_means_every_day(opts):
    """Test that empty run_days means every day."""
    from mojo.apps.jobs.models import ScheduledTask

    task = ScheduledTask(
        user=opts.user_a,
        name="test_sched_alldays",
        task_type="job",
        run_times=["09:00"],
        run_days=[],
        job_config={"func": "some.func"},
    )

    for day in range(7):
        assert task.matches_day(day) is True, f"Empty run_days should match day {day}"


@th.django_unit_test()
def test_get_run_times_for_hour(opts):
    """Test extracting matching run times for a given hour."""
    from mojo.apps.jobs.models import ScheduledTask

    task = ScheduledTask(
        user=opts.user_a,
        name="test_sched_hours",
        task_type="job",
        run_times=["09:30", "17:00"],
        job_config={"func": "some.func"},
    )

    matches_9 = task.get_run_times_for_hour(9)
    assert len(matches_9) == 1, f"Expected 1 match for hour 9, got {len(matches_9)}"
    assert matches_9[0] == (9, 30), f"Expected (9, 30), got {matches_9[0]}"

    matches_17 = task.get_run_times_for_hour(17)
    assert len(matches_17) == 1, f"Expected 1 match for hour 17, got {len(matches_17)}"
    assert matches_17[0] == (17, 0), f"Expected (17, 0), got {matches_17[0]}"

    matches_12 = task.get_run_times_for_hour(12)
    assert len(matches_12) == 0, f"Expected 0 matches for hour 12, got {len(matches_12)}"


@th.django_unit_test()
def test_run_scheduled_task_job_type(opts):
    """Test executing a job-type scheduled task via run_pending_jobs."""
    from mojo.apps.jobs.models import ScheduledTask, TaskResult, Job
    from mojo.apps.jobs.asyncjobs import run_scheduled_task

    # Clean up
    Job.objects.filter(func="mojo.apps.jobs.asyncjobs.run_scheduled_task").delete()

    task = ScheduledTask(
        user=opts.user_a,
        name="test_sched_job_exec",
        task_type="job",
        run_times=["09:00"],
        job_config={
            "func": "mojo.apps.jobs.asyncjobs.prune_jobs",
            "payload": {},
        },
    )
    task.save()

    # Create a mock job record directly and call the function
    mock_job = Job.objects.create(
        id=uuid.uuid4().hex,
        channel="default",
        func="mojo.apps.jobs.asyncjobs.run_scheduled_task",
        payload={"task_id": str(task.id)},
        status="pending",
    )
    run_scheduled_task(mock_job)

    # Check task was updated
    task.refresh_from_db()
    assert task.run_count >= 1, f"Expected run_count >= 1, got {task.run_count}"
    assert task.last_run is not None, "Expected last_run to be set"

    # Check TaskResult was created
    results = TaskResult.objects.filter(task=task)
    assert results.exists(), "Expected a TaskResult to be created"
    result = results.first()
    assert result.status == "success", f"Expected success status, got {result.status}"
    assert result.user == opts.user_a, "TaskResult owner should match task owner"

    # Clean up
    TaskResult.objects.filter(task=task).delete()
    mock_job.delete()
    task.delete()


@th.django_unit_test()
def test_run_once_auto_disables(opts):
    """Test that run_once tasks auto-disable after execution."""
    from mojo.apps.jobs.models import ScheduledTask, TaskResult, Job
    from mojo.apps.jobs.asyncjobs import run_scheduled_task

    task = ScheduledTask(
        user=opts.user_a,
        name="test_sched_run_once",
        task_type="job",
        run_times=["09:00"],
        run_once=True,
        job_config={
            "func": "mojo.apps.jobs.asyncjobs.prune_jobs",
            "payload": {},
        },
    )
    task.save()
    assert task.enabled is True, "Task should start enabled"

    # Create mock job and execute directly
    mock_job = Job.objects.create(
        id=uuid.uuid4().hex,
        channel="default",
        func="mojo.apps.jobs.asyncjobs.run_scheduled_task",
        payload={"task_id": str(task.id)},
        status="pending",
    )
    run_scheduled_task(mock_job)

    # Check auto-disabled
    task.refresh_from_db()
    assert task.enabled is False, "run_once task should be disabled after execution"
    assert task.run_count >= 1, f"Expected run_count >= 1, got {task.run_count}"

    # Clean up
    TaskResult.objects.filter(task=task).delete()
    mock_job.delete()
    task.delete()


@th.django_unit_test()
def test_disabled_task_skipped(opts):
    """Test that disabled tasks are skipped at execution time."""
    from mojo.apps.jobs.models import ScheduledTask, TaskResult, Job
    from mojo.apps.jobs.asyncjobs import run_scheduled_task

    task = ScheduledTask(
        user=opts.user_a,
        name="test_sched_disabled",
        task_type="job",
        run_times=["09:00"],
        enabled=False,
        job_config={
            "func": "mojo.apps.jobs.asyncjobs.prune_jobs",
            "payload": {},
        },
    )
    task.save()

    # Create mock job and execute directly
    mock_job = Job.objects.create(
        id=uuid.uuid4().hex,
        channel="default",
        func="mojo.apps.jobs.asyncjobs.run_scheduled_task",
        payload={"task_id": str(task.id)},
        status="pending",
    )
    run_scheduled_task(mock_job)

    # No TaskResult should be created
    results = TaskResult.objects.filter(task=task)
    assert not results.exists(), "Disabled task should not produce a TaskResult"

    # Clean up
    mock_job.delete()
    task.delete()


@th.django_unit_test()
def test_dispatch_scheduled_tasks(opts):
    """Test the hourly dispatch function finds and publishes matching tasks."""
    from mojo.apps.jobs.models import ScheduledTask, Job
    from mojo.apps.jobs.cronjobs import dispatch_scheduled_tasks
    import pytz

    # Clean up
    Job.objects.filter(channel="default").delete()
    ScheduledTask.objects.filter(name__startswith="test_sched_dispatch").delete()

    # Get user's timezone
    user_tz_name = opts.user_a.org.timezone if opts.user_a.org else "America/Los_Angeles"
    user_tz = pytz.timezone(user_tz_name)

    # Get current hour in user timezone
    now = timezone.now()
    user_now = now.astimezone(user_tz)
    current_hour = user_now.hour
    current_minute = user_now.minute

    # Create a task that matches current hour but a future minute
    # Use minute 59 to ensure it's in the future (unless we're at :59)
    target_minute = 59 if current_minute < 59 else 58
    run_time = f"{current_hour:02d}:{target_minute:02d}"

    task = ScheduledTask(
        user=opts.user_a,
        name="test_sched_dispatch_match",
        task_type="job",
        run_times=[run_time],
        run_days=[],
        job_config={"func": "mojo.apps.jobs.asyncjobs.prune_jobs", "payload": {}},
    )
    task.save()

    # Run dispatch
    dispatch_scheduled_tasks()

    # Check a job was published
    published_jobs = Job.objects.filter(
        func="mojo.apps.jobs.asyncjobs.run_scheduled_task",
        payload__task_id=str(task.id),
    )
    assert published_jobs.exists(), "Dispatch should have published a job for the matching task"

    published_job = published_jobs.first()
    assert published_job.run_at is not None, "Published job should have a run_at time"
    assert published_job.idempotency_key.startswith(f"schtask:{task.id}:"), \
        f"Idempotency key should start with schtask:{task.id}:, got {published_job.idempotency_key}"

    # Clean up
    published_jobs.delete()
    task.delete()


@th.django_unit_test()
def test_dispatch_idempotency(opts):
    """Test that running dispatch twice doesn't double-publish."""
    from mojo.apps.jobs.models import ScheduledTask, Job
    from mojo.apps.jobs.cronjobs import dispatch_scheduled_tasks
    import pytz

    Job.objects.filter(channel="default").delete()
    ScheduledTask.objects.filter(name__startswith="test_sched_idem").delete()

    user_tz_name = opts.user_a.org.timezone if opts.user_a.org else "America/Los_Angeles"
    user_tz = pytz.timezone(user_tz_name)
    now = timezone.now()
    user_now = now.astimezone(user_tz)
    current_hour = user_now.hour
    current_minute = user_now.minute
    target_minute = 59 if current_minute < 59 else 58
    run_time = f"{current_hour:02d}:{target_minute:02d}"

    task = ScheduledTask(
        user=opts.user_a,
        name="test_sched_idem_task",
        task_type="job",
        run_times=[run_time],
        job_config={"func": "mojo.apps.jobs.asyncjobs.prune_jobs", "payload": {}},
    )
    task.save()

    # Run dispatch twice
    dispatch_scheduled_tasks()
    dispatch_scheduled_tasks()

    # Should still only have 1 job (idempotency key prevents duplicates)
    count = Job.objects.filter(
        func="mojo.apps.jobs.asyncjobs.run_scheduled_task",
        payload__task_id=str(task.id),
    ).count()
    assert count == 1, f"Expected exactly 1 job after double dispatch, got {count}"

    # Clean up
    Job.objects.filter(func="mojo.apps.jobs.asyncjobs.run_scheduled_task",
                       payload__task_id=str(task.id)).delete()
    task.delete()


@th.django_unit_test()
def test_crud_rest_create(opts):
    """Test creating a scheduled task via REST."""
    opts.client.login(TEST_USER_A, TEST_PWORD)
    resp = opts.client.post("/api/jobs/scheduled_task", json={
        "user": opts.user_a.id,
        "name": "test_sched_rest_create",
        "task_type": "llm",
        "run_times": ["09:00"],
        "run_days": [0, 1, 2, 3, 4],
        "job_config": {"system_prompt": "Test", "user_prompt": "Hello"},
        "notify": ["email"],
    })
    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.json}"


@th.django_unit_test()
def test_crud_rest_list(opts):
    """Test listing scheduled tasks via REST."""
    opts.client.login(TEST_USER_A, TEST_PWORD)
    resp = opts.client.get("/api/jobs/scheduled_task")
    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.json}"


@th.django_unit_test()
def test_task_result_read_only(opts):
    """Test that TaskResult cannot be created via REST."""
    resp = opts.client.post("/api/jobs/task_result", json={
        "status": "success",
        "output": "test",
    })
    # Should fail — TaskResult has no SAVE_PERMS
    assert resp.status_code != 200, f"TaskResult should not be writable via REST, got {resp.status_code}"
