"""Tests for the jobs domain assistant tools: run_job, run_scheduled_task_now."""
from testit import helpers as th


TEST_ADMIN = "jobtools_admin@test.com"
TEST_NOPRIV = "jobtools_nopriv@test.com"


@th.django_unit_setup()
@th.requires_app("mojo.apps.assistant")
def setup_job_tools(opts):
    from mojo.apps.account.models import User
    from mojo.apps.jobs.models import Job, ScheduledTask

    # Clean up from previous runs
    User.objects.filter(email__in=[TEST_ADMIN, TEST_NOPRIV]).delete()
    ScheduledTask.objects.filter(name__startswith="test_jobtool_").delete()
    Job.objects.filter(func__startswith="mojo.apps.jobs.asyncjobs.prune_jobs").delete()

    # Admin with manage_jobs
    opts.admin = User.objects.create_user(
        username=TEST_ADMIN, email=TEST_ADMIN, password="pass123",
    )
    opts.admin.is_email_verified = True
    opts.admin.save()
    opts.admin.add_permission("view_admin")
    opts.admin.add_permission("view_jobs")
    opts.admin.add_permission("manage_jobs")

    # User without manage_jobs
    opts.nopriv = User.objects.create_user(
        username=TEST_NOPRIV, email=TEST_NOPRIV, password="pass123",
    )
    opts.nopriv.is_email_verified = True
    opts.nopriv.save()
    opts.nopriv.add_permission("view_admin")

    # Create an enabled scheduled task for the admin
    opts.enabled_task = ScheduledTask(
        user=opts.admin,
        name="test_jobtool_enabled",
        task_type="job",
        run_times=["09:00"],
        job_config={"func": "mojo.apps.jobs.asyncjobs.prune_jobs", "payload": {}},
    )
    opts.enabled_task.save()

    # Create a disabled scheduled task for the admin
    opts.disabled_task = ScheduledTask(
        user=opts.admin,
        name="test_jobtool_disabled",
        enabled=False,
        task_type="job",
        run_times=["10:00"],
        job_config={"func": "mojo.apps.jobs.asyncjobs.prune_jobs", "payload": {}},
    )
    opts.disabled_task.save()

    # Create a template job for rerun tests
    from mojo.apps import jobs
    opts.template_job_id = jobs.publish(
        func="mojo.apps.jobs.asyncjobs.prune_jobs",
        payload={"test_key": "template_value"},
        channel="default",
    )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _run_job(params, user):
    from mojo.apps.assistant.services.tools.jobs import _tool_run_job
    return _tool_run_job(params, user)


def _run_task_now(params, user):
    from mojo.apps.assistant.services.tools.jobs import _tool_run_scheduled_task_now
    return _tool_run_scheduled_task_now(params, user)


# ---------------------------------------------------------------------------
# run_job — fresh run
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_run_job_fresh_with_func(opts):
    result = _run_job({
        "func": "mojo.apps.jobs.asyncjobs.prune_jobs",
        "payload": {"foo": "bar"},
    }, opts.admin)
    assert result["ok"] is True, f"Should succeed: {result}"
    assert "job_id" in result, f"Should return job_id: {result}"
    assert result["job_id"], "job_id should not be empty"


@th.django_unit_test()
def test_run_job_fresh_with_channel(opts):
    result = _run_job({
        "func": "mojo.apps.jobs.asyncjobs.prune_jobs",
        "channel": "default",
    }, opts.admin)
    assert result["ok"] is True, f"Should succeed: {result}"
    assert "job_id" in result, f"Should return job_id: {result}"


@th.django_unit_test()
def test_run_job_fresh_with_delay(opts):
    result = _run_job({
        "func": "mojo.apps.jobs.asyncjobs.prune_jobs",
        "delay": 60,
    }, opts.admin)
    assert result["ok"] is True, f"Should succeed: {result}"

    # Verify the job was created with a run_at
    from mojo.apps.jobs.models import Job
    job = Job.objects.get(id=result["job_id"])
    assert job.run_at is not None, "Delayed job should have run_at set"


@th.django_unit_test()
def test_run_job_invalid_func(opts):
    result = _run_job({
        "func": "totally.bogus.nonexistent_function",
    }, opts.admin)
    assert result["ok"] is False, f"Should fail for invalid func: {result}"
    assert "Invalid job function" in result["error"], f"Error should mention invalid function: {result['error']}"


# ---------------------------------------------------------------------------
# run_job — rerun from template
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_run_job_rerun_from_template(opts):
    result = _run_job({
        "job_id": opts.template_job_id,
    }, opts.admin)
    assert result.get("status") is True or result.get("ok") is True, f"Should succeed: {result}"
    new_id = result.get("job_id")
    assert new_id, f"Should return new job_id: {result}"
    assert new_id != opts.template_job_id, "Should create a new job, not reuse the template"


@th.django_unit_test()
def test_run_job_rerun_with_payload_override(opts):
    result = _run_job({
        "job_id": opts.template_job_id,
        "payload": {"overridden": True},
    }, opts.admin)
    assert result.get("status") is True or result.get("ok") is True, f"Should succeed: {result}"
    new_id = result.get("job_id")
    assert new_id, f"Should return new job_id: {result}"

    from mojo.apps.jobs.models import Job
    new_job = Job.objects.get(id=new_id)
    assert new_job.payload.get("overridden") is True, \
        f"Payload should be overridden, got: {new_job.payload}"


@th.django_unit_test()
def test_run_job_nonexistent_job_id(opts):
    result = _run_job({
        "job_id": "00000000000000000000000000000000",
    }, opts.admin)
    assert result["ok"] is False, f"Should fail for nonexistent job: {result}"
    assert "not found" in result["error"].lower(), f"Error should say not found: {result['error']}"


# ---------------------------------------------------------------------------
# run_job — validation
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_run_job_both_func_and_job_id(opts):
    result = _run_job({
        "func": "mojo.apps.jobs.asyncjobs.prune_jobs",
        "job_id": opts.template_job_id,
    }, opts.admin)
    assert result["ok"] is False, f"Should reject both func and job_id: {result}"
    assert "not both" in result["error"].lower(), f"Error should mention 'not both': {result['error']}"


@th.django_unit_test()
def test_run_job_neither_func_nor_job_id(opts):
    result = _run_job({}, opts.admin)
    assert result["ok"] is False, f"Should reject empty params: {result}"
    assert "func" in result["error"].lower() or "job_id" in result["error"].lower(), \
        f"Error should mention required fields: {result['error']}"


# ---------------------------------------------------------------------------
# run_scheduled_task_now
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_run_task_now_enabled(opts):
    result = _run_task_now({"task_id": opts.enabled_task.id}, opts.admin)
    assert result["ok"] is True, f"Should succeed for enabled task: {result}"
    assert "job_id" in result, f"Should return job_id: {result}"
    assert result["job_id"], "job_id should not be empty"


@th.django_unit_test()
def test_run_task_now_disabled(opts):
    result = _run_task_now({"task_id": opts.disabled_task.id}, opts.admin)
    assert result["ok"] is True, f"Should succeed even for disabled task: {result}"
    assert "job_id" in result, f"Should return job_id: {result}"


@th.django_unit_test()
def test_run_task_now_nonexistent(opts):
    result = _run_task_now({"task_id": "00000000000000000000000000000000"}, opts.admin)
    assert result["ok"] is False, f"Should fail for nonexistent task: {result}"
    assert "not found" in result["error"].lower(), f"Error should say not found: {result['error']}"


@th.django_unit_test()
def test_run_task_now_other_users_task(opts):
    """User should not be able to run another user's task."""
    result = _run_task_now({"task_id": opts.enabled_task.id}, opts.nopriv)
    assert result["ok"] is False, f"Should fail for other user's task: {result}"
    assert "not found" in result["error"].lower(), f"Should appear as not found: {result['error']}"


@th.django_unit_test()
def test_run_task_now_returns_trackable_job(opts):
    result = _run_task_now({"task_id": opts.enabled_task.id}, opts.admin)
    assert result["ok"] is True, f"Should succeed: {result}"

    from mojo.apps.jobs.models import Job
    job = Job.objects.get(id=result["job_id"])
    assert job.func == "mojo.apps.jobs.asyncjobs.run_scheduled_task", \
        f"Job func should be run_scheduled_task, got: {job.func}"
    assert job.payload["task_id"] == opts.enabled_task.id, \
        f"Job payload should contain task_id, got: {job.payload}"
    assert job.payload["force"] is True, \
        f"Job payload should have force=True, got: {job.payload}"


# ---------------------------------------------------------------------------
# Tool registration
# ---------------------------------------------------------------------------

@th.django_unit_test()
def test_run_job_registered(opts):
    from mojo.apps.assistant import get_registry
    registry = get_registry()
    assert "run_job" in registry, "run_job should be registered"
    entry = registry["run_job"]
    assert entry["permission"] == "manage_jobs", \
        f"Permission should be manage_jobs, got: {entry['permission']}"
    assert entry["mutates"] is True, "run_job should be mutating"
    assert entry["domain"] == "jobs", f"Domain should be 'jobs', got: {entry['domain']}"


@th.django_unit_test()
def test_run_scheduled_task_now_registered(opts):
    from mojo.apps.assistant import get_registry
    registry = get_registry()
    assert "run_scheduled_task_now" in registry, "run_scheduled_task_now should be registered"
    entry = registry["run_scheduled_task_now"]
    assert entry["permission"] == "manage_jobs", \
        f"Permission should be manage_jobs, got: {entry['permission']}"
    assert entry["mutates"] is True, "run_scheduled_task_now should be mutating"
    assert entry["domain"] == "jobs", f"Domain should be 'jobs', got: {entry['domain']}"
