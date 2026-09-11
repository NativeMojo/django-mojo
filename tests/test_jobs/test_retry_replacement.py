"""Manual retry publishes a runnable, linked replacement (#4246).

Retrying an expired job used to copy its past `expires_at` onto the
replacement, so the engine expired the replacement before ever running it —
found repairing a WMWX production provisioning job. The same code also reset
the ORIGINAL row to `pending`, erasing its diagnostics and leaving a zombie
that `requeue_db_pending` would have executed a second time.

Every test here drives the real engine through `th.run_jobs()`: a retried job
must be CLAIMED AND COMPLETED, not merely published. CHANNEL is private to this
module (declared in bin/create_testproject's JOBS_ALLOWED_CHANNELS) so parallel
modules never drain or delete these rows mid-test.
"""

TESTIT_TIER = "bug"

from datetime import timedelta

from testit import helpers as th


CHANNEL = "t4246_retry"

# Module-level sink, and handlers addressed by the name this module is actually
# imported under — see tests/test_jobs/test_run_jobs_helper.py for why.
CALLS = []


def record_call(job):
    CALLS.append(job.payload.get("marker"))
    return "ok"


def boom(job):
    raise RuntimeError("handler blew up on purpose")


HANDLER = f"{__name__}.record_call"
FAILING_HANDLER = f"{__name__}.boom"

TERMINAL_FAILURES = ("failed", "canceled", "expired")


@th.django_unit_setup()
def setup_retry_replacement(opts):
    _reset()


def _reset():
    """Setup runs once per module; every test starts from a clean channel."""
    from mojo.apps.jobs.models import Job

    CALLS.clear()
    th.clear_jobs(channel=CHANNEL)
    # Terminal rows survive clear_jobs (it only drops pending/running).
    Job.objects.filter(channel=CHANNEL).delete()


def _expired_job(handler, marker="alpha"):
    """Publish a job that is already past its deadline and let the ENGINE
    expire it — the fixture is proven through execute_job's own is_expired
    branch, never by hand-setting status."""
    from django.utils import timezone
    from mojo.apps import jobs
    from mojo.apps.jobs.models import Job

    job_id = jobs.publish(
        func=handler, payload={"marker": marker}, channel=CHANNEL,
        max_retries=0, expires_at=timezone.now() - timedelta(seconds=60))
    th.run_jobs(channel=CHANNEL)

    job = Job.objects.get(id=job_id)
    assert job.status == "expired", \
        f"fixture: the engine should have expired the job, status is {job.status}"
    assert CALLS == [], f"fixture: an expired job must not run, got {CALLS}"
    return job


@th.django_unit_test("retrying an expired job produces a replacement that runs to completion")
def test_retry_of_expired_job_runs_to_completion(opts):
    _reset()
    from django.utils import timezone
    from mojo.apps.jobs.models import Job, JobEvent
    from mojo.apps.jobs.services import JobActionsService

    job = _expired_job(HANDLER, marker="alpha")
    original_finished_at = job.finished_at

    result = JobActionsService.retry_job(job)
    assert result["status"] is True, f"retry should succeed, got {result}"
    new_id = result["new_job_id"]
    assert new_id != job.id, "the retry must publish a NEW job id"

    replacement = Job.objects.get(id=new_id)
    assert replacement.status == "pending", \
        f"replacement should be pending before the drain, is {replacement.status}"
    assert replacement.expires_at > timezone.now(), \
        f"replacement inherited an expired deadline: {replacement.expires_at}"

    th.run_jobs(channel=CHANNEL)

    assert CALLS == ["alpha"], \
        f"the replacement must be claimed and run by the engine, got {CALLS}"
    replacement.refresh_from_db()
    assert replacement.status == "completed", \
        f"replacement should be completed, is {replacement.status}"
    assert replacement.metadata.get("retried_from") == job.id, \
        f"replacement should name its source, metadata={replacement.metadata}"

    job.refresh_from_db()
    assert job.status == "expired", \
        f"the original must stay a terminal record, is {job.status}"
    assert job.finished_at == original_finished_at, \
        "the original's finished_at must not be rewritten by a retry"
    assert job.metadata.get("retried_as") == new_id, \
        f"the original should name its replacement, metadata={job.metadata}"

    events = list(JobEvent.objects.filter(job=job, event="retry"))
    assert len(events) == 1, f"expected exactly one retry event, got {len(events)}"
    assert events[0].details.get("new_job_id") == new_id, \
        f"retry event should link the replacement, details={events[0].details}"
    assert events[0].details.get("previous_status") == "expired", \
        f"retry event should record the prior state, details={events[0].details}"

    assert Job.objects.filter(channel=CHANNEL, status="pending").count() == 0, \
        "no row may be left pending — the original must not become a zombie"


@th.django_unit_test("retrying a failed job leaves the original failed with its diagnostics")
def test_retry_of_failed_job_keeps_original_terminal(opts):
    _reset()
    from mojo.apps import jobs
    from mojo.apps.jobs.models import Job
    from mojo.apps.jobs.services import JobActionsService

    job_id = jobs.publish(func=FAILING_HANDLER, payload={"marker": "x"},
                          channel=CHANNEL, max_retries=0)
    th.run_jobs(channel=CHANNEL)
    job = Job.objects.get(id=job_id)
    assert job.status == "failed", f"fixture: expected failed, got {job.status}"
    assert job.last_error, "fixture: a failed job should carry last_error"
    attempt = job.attempt

    result = JobActionsService.retry_job(job)
    assert result["status"] is True, f"retry should succeed, got {result}"

    job.refresh_from_db()
    assert job.status == "failed", \
        f"the original must stay failed, is {job.status}"
    assert job.last_error, "the original's last_error must not be wiped"
    assert job.attempt == attempt, \
        f"the original's attempt counter must not be reset, is {job.attempt}"

    pending = list(Job.objects.filter(channel=CHANNEL, status="pending")
                   .values_list("id", flat=True))
    assert pending == [result["new_job_id"]], \
        f"only the replacement may be pending, got {pending}"


@th.django_unit_test("a delayed retry stays alive until it is due")
def test_delayed_retry_lifetime_anchored_at_run_at(opts):
    _reset()
    from django.utils import timezone
    from mojo.apps.jobs import JOBS_DEFAULT_EXPIRES_SEC
    from mojo.apps.jobs.models import Job
    from mojo.apps.jobs.services import JobActionsService

    job = _expired_job(HANDLER, marker="delayed")
    before = timezone.now()

    result = JobActionsService.retry_job(job, delay=600)
    assert result["status"] is True, f"delayed retry should succeed, got {result}"
    assert result["delayed"] is True, f"result should say it was delayed, got {result}"

    replacement = Job.objects.get(id=result["new_job_id"])
    assert replacement.run_at is not None, "a delayed retry must carry run_at"
    assert abs((replacement.run_at - (before + timedelta(seconds=600))).total_seconds()) < 5, \
        f"run_at should be ~600s out, got {replacement.run_at} (before={before})"
    assert replacement.expires_at >= replacement.run_at + timedelta(seconds=JOBS_DEFAULT_EXPIRES_SEC - 5), \
        (f"the lifetime must be anchored at run_at, not now: run_at={replacement.run_at} "
         f"expires_at={replacement.expires_at} default={JOBS_DEFAULT_EXPIRES_SEC}")
    assert th.pending_job_count(channel=CHANNEL) == 0, \
        "a delayed replacement belongs in the scheduled set, not the immediate queue"


@th.django_unit_test("a source published with a longer window keeps that window on retry")
def test_retry_keeps_a_longer_source_window(opts):
    _reset()
    from django.utils import timezone
    from mojo.apps.jobs.models import Job
    from mojo.apps.jobs.services import JobActionsService

    job = _expired_job(HANDLER, marker="window")
    now = timezone.now()
    # A publisher-chosen 2h window (expires_in=7200) that has since lapsed.
    Job.objects.filter(pk=job.id).update(
        created=now - timedelta(hours=3), expires_at=now - timedelta(hours=1))
    job.refresh_from_db()

    result = JobActionsService.retry_job(job)
    assert result["status"] is True, f"retry should succeed, got {result}"

    replacement = Job.objects.get(id=result["new_job_id"])
    expected = now + timedelta(seconds=7200)
    assert abs((replacement.expires_at - expected).total_seconds()) < 5, \
        (f"replacement should inherit the source's 2h window, expires_at="
         f"{replacement.expires_at} expected≈{expected}")


@th.django_unit_test("a second retry is refused while the replacement is live")
def test_second_retry_refused_while_replacement_live(opts):
    _reset()
    from mojo.apps.jobs.models import Job
    from mojo.apps.jobs.services import JobActionsService

    job = _expired_job(FAILING_HANDLER, marker="twice")

    first = JobActionsService.retry_job(job)
    assert first["status"] is True, f"first retry should succeed, got {first}"
    first_id = first["new_job_id"]

    job.refresh_from_db()
    second = JobActionsService.retry_job(job)
    assert second["status"] is False, \
        f"a second retry must be refused while the replacement is pending, got {second}"
    assert "already retried" in second.get("error", ""), \
        f"the refusal must say the job was already retried, got {second}"
    assert first_id in second.get("error", ""), \
        f"the refusal must name the live replacement {first_id}, got {second}"
    assert Job.objects.filter(channel=CHANNEL).count() == 2, \
        "a refused retry must not publish another row"

    # The replacement runs and fails; the original may now be retried again.
    th.run_jobs(channel=CHANNEL)
    assert Job.objects.get(id=first_id).status == "failed", \
        "fixture: the replacement should have failed on the drain"

    job.refresh_from_db()
    third = JobActionsService.retry_job(job)
    assert third["status"] is True, \
        f"once the replacement failed, retrying the original is allowed, got {third}"
    assert third["new_job_id"] != first_id, "the third retry must publish a new row"
    job.refresh_from_db()
    assert job.metadata.get("retried_as") == third["new_job_id"], \
        f"retried_as should point at the newest replacement, metadata={job.metadata}"


@th.django_unit_test("JobManager.retry_job behaves like the service path")
def test_manager_retry_matches_service(opts):
    _reset()
    from mojo.apps.jobs.manager import get_manager
    from mojo.apps.jobs.models import Job, JobEvent

    job = _expired_job(HANDLER, marker="manager")

    new_id = get_manager().retry_job(job.id)
    assert new_id, "the manager should return the replacement id for an expired job"
    assert new_id != job.id, "the manager must publish a NEW job id"

    th.run_jobs(channel=CHANNEL)
    assert CALLS == ["manager"], \
        f"the manager's replacement must be claimed and run, got {CALLS}"
    assert Job.objects.get(id=new_id).status == "completed", \
        "the manager's replacement should complete"

    job.refresh_from_db()
    assert job.status == "expired", \
        f"the manager path must also leave the original terminal, is {job.status}"
    assert JobEvent.objects.filter(job=job, event="retry").exists(), \
        "the manager path must record the retry event on the original"

    assert get_manager().retry_job("does-not-exist") is False, \
        "an unknown job id should return False"
