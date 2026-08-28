"""Publish/execute tests moved out of tests/test_job_engine/test_publish_execute.py.

These tests patch shared mojo.apps.jobs internals in-process (`jobs.metrics.record`,
`job_engine.load_job_function`, and — for maestro item #3326 — `jobs.get_adapter`
and `jobs.models.JobLog`), which every parallel test thread sees, so they run only
in the opt-in serial tier (maestro item #1839). Neither `jobs.publish` nor
`JobEngine.execute_job` accepts an injectable metrics/loader parameter, so there
is no seam to convert through.

The #3326 group is fault injection: the only place a Redis mirror can be made to
fail on purpose, which is what proves that an unconfirmed publication leaves the
job executable instead of killing it.
"""
from testit import helpers as th
import threading
from unittest import mock


# Never executed — nothing consumes 'email' during the suite. It exists so setup
# can delete exactly the rows the #3326 tests create.
UNCERTAIN_FUNC = "tests.test_job_engine_extended_serial.test_publish_execute.noop_job"


class WriteThenFailAdapter:
    """Delegates to the real adapter, optionally lands the write, then raises.

    The write-then-raise shape is the fault this item exists for: the RPUSH/ZADD
    reached Redis and only the acknowledgment was lost, so the job may well be
    executable and must not be marked failed. `write=False` is the other half —
    the write never landed at all.
    """

    def __init__(self, real, write=True):
        self._real = real
        self._write = write

    def __getattr__(self, name):
        return getattr(self._real, name)

    def rpush(self, key, *values):
        if self._write:
            self._real.rpush(key, *values)
        raise ConnectionError("simulated redis acknowledgment loss")

    def zadd(self, key, mapping, **kwargs):
        if self._write:
            self._real.zadd(key, mapping, **kwargs)
        raise ConnectionError("simulated redis acknowledgment loss")


def clear_unconfirmed_incident_state():
    """Reset the suppression window and rows for jobs:unconfirmed_publish.

    report_event_suppressed files a (category, key) pair at most once an hour, so
    a second test asserting on the incident would otherwise see the first test's
    suppression instead of its own event.
    """
    from mojo.apps import jobs
    from mojo.apps.incident import notice_key, budget_key
    from mojo.apps.incident.models import Event
    from mojo.helpers.redis import get_connection

    category = jobs.UNCONFIRMED_PUBLISH_CATEGORY
    Event.objects.filter(category=category).delete()
    try:
        redis = get_connection()
    except Exception:
        return
    for key in (notice_key(category, 'email'),
                budget_key(category, jobs.REJECTED_RENOTIFY_SEC)):
        try:
            redis.delete(key)
        except Exception:
            pass


def uncertain_logs(job_id):
    """The delivery-uncertainty JobLog rows recorded for one job."""
    from mojo.apps.jobs.models import JobLog

    return list(JobLog.objects.filter(job_id=job_id, kind='error'))


def assert_uncertainty_recorded(job, log, deferred, operation):
    """The shared shape of a recorded-but-unconfirmed publication."""
    from mojo.apps import jobs

    assert job.status == "pending", \
        f"An unconfirmed mirror must leave the job executable, got {job.status}"
    assert job.attempt == 0, \
        f"An unconfirmed mirror must not consume an attempt, got {job.attempt}"
    assert job.started_at is None, \
        "An unconfirmed mirror must not look like a started execution"
    assert job.finished_at is None, \
        "An unconfirmed mirror must not look like a finished execution"
    assert job.last_error == jobs.UNCONFIRMED_PUBLISH_ERROR, \
        f"The row must carry the uncertainty marker, got {job.last_error!r}"
    assert log.message == jobs.UNCONFIRMED_PUBLISH_ERROR, \
        "The persisted log message must be the fixed safe text (no Redis DSN)"
    assert set(log.meta.keys()) == {"phase", "delivery_state", "deferred", "operation"}, \
        f"The log metadata must stay bounded to four keys, got {sorted(log.meta)}"
    assert log.meta["phase"] == "redis_mirror", \
        f"The log must name the failing phase, got {log.meta['phase']!r}"
    assert log.meta["delivery_state"] == "unknown", \
        "Delivery is unknown, not failed — that is the whole point"
    assert log.meta["deferred"] is deferred, \
        f"The log must record which path failed, got deferred={log.meta['deferred']!r}"
    assert log.meta["operation"] == operation, \
        f"The log must name the Redis operation, got {log.meta['operation']!r}"


@th.django_unit_setup()
def setup_publish_execute_serial_tests(opts):
    """Setup for publish/execute tests."""
    from mojo.apps.jobs.models import Job, JobEvent
    from mojo.apps.jobs.adapters import get_adapter
    from mojo.apps.jobs.keys import JobKeys

    # Clear test data - using test-specific job names for cleanup
    Job.objects.filter(func__contains='test_publish_execute').delete()
    Job.objects.filter(func=UNCERTAIN_FUNC).delete()
    JobEvent.objects.filter(channel='email').delete()

    opts.redis = get_adapter()
    opts.keys = JobKeys()
    opts.test_channel = 'email'

    # Clear Redis test data
    test_keys = [
        opts.keys.queue(opts.test_channel),
        opts.keys.processing(opts.test_channel),
        opts.keys.sched(opts.test_channel),
        opts.keys.sched_broadcast(opts.test_channel)
    ]

    for key in test_keys:
        opts.redis.delete(key)


@th.django_unit_test()
def test_publish_bookkeeping_failure_does_not_fail_queued_job(opts):
    """A post-queue metric failure must not turn visible work into failed work."""
    from mojo.apps import jobs
    from mojo.apps.jobs.models import Job

    with mock.patch.object(jobs.metrics, "record", side_effect=RuntimeError("simulated")):
        job_id = jobs.publish(
            func="mojo.apps.jobs.examples.sample_jobs.send_email",
            payload={"recipients": ["metrics@example.com"]},
            channel=opts.test_channel,
        )

    job = Job.objects.get(pk=job_id)
    queued = opts.redis.get_client().lrange(
        opts.keys.queue(opts.test_channel), 0, -1,
    )
    assert job.status == "pending", "Bookkeeping failure must not fail a queued Job"
    assert job_id.encode("utf-8") in queued or job_id in queued, \
        "The durable pending Job must remain visible in Redis"


@th.django_unit_test()
def test_duplicate_redis_delivery_claims_job_once(opts):
    """Duplicate Redis copies of one Job ID must execute the handler once."""
    from mojo.apps.jobs import publish
    from mojo.apps.jobs.job_engine import JobEngine
    from mojo.apps.jobs.models import Job

    job_id = publish(
        func="mojo.apps.jobs.examples.sample_jobs.send_email",
        payload={"recipients": ["once@example.com"]},
        channel=opts.test_channel,
    )
    calls = []
    calls_lock = threading.Lock()

    def handler(job):
        with calls_lock:
            calls.append(job.pk)

    first = JobEngine(channels=[opts.test_channel], runner_id="claim-one-engine")
    second = JobEngine(channels=[opts.test_channel], runner_id="claim-two-engine")
    try:
        with mock.patch(
            "mojo.apps.jobs.job_engine.load_job_function",
            return_value=handler,
        ):
            threads = [
                threading.Thread(target=first.execute_job, args=(opts.test_channel, job_id)),
                threading.Thread(target=second.execute_job, args=(opts.test_channel, job_id)),
            ]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=10)
    finally:
        first.executor.shutdown(wait=True)
        second.executor.shutdown(wait=True)

    job = Job.objects.get(pk=job_id)
    assert calls == [job_id], "The database claim fence must admit one handler execution"
    assert job.status == "completed", "The single claimed execution must complete"
    assert job.attempt == 1, "Duplicate delivery must increment the attempt once"


# ---------------------------------------------------------------------------
# Unconfirmed publication (maestro item #3326)
# ---------------------------------------------------------------------------


def queued_ids(opts):
    """Job ids currently on the channel's immediate queue, as strings."""
    raw = opts.redis.get_client().lrange(opts.keys.queue(opts.test_channel), 0, -1)
    return [
        item.decode('utf-8') if isinstance(item, (bytes, bytearray)) else item
        for item in (raw or [])
    ]


def publish_uncertain(opts, key, adapter=None):
    """Publish one #3326 probe job, optionally through a faulted adapter."""
    from mojo.apps import jobs

    def call():
        return jobs.publish(
            UNCERTAIN_FUNC, {"probe": "3326"},
            channel=opts.test_channel, idempotency_key=key)

    if adapter is None:
        return call()
    with mock.patch.object(jobs, "get_adapter", return_value=adapter):
        return call()


@th.django_unit_test()
def test_immediate_mirror_failure_leaves_job_executable(opts):
    """A lost acknowledgment must raise, yet leave the job runnable, not failed."""
    from mojo.apps import jobs
    from mojo.apps.jobs.models import Job
    from mojo.apps.incident.models import Event

    clear_unconfirmed_incident_state()
    key = "t3326-immediate"
    raised = None
    try:
        publish_uncertain(opts, key, WriteThenFailAdapter(jobs.get_adapter()))
    except RuntimeError as e:
        raised = e

    assert raised is not None, \
        "The immediate path must still raise when delivery cannot be confirmed"
    assert "delivery unconfirmed" in str(raised), \
        f"The error must say delivery is uncertain, got {str(raised)!r}"
    assert "idempotency_key" in str(raised), \
        ("The error must name the only safe retry — without it a blind retry is "
         f"at-least-once now that the row stays executable, got {str(raised)!r}")

    job = Job.objects.get(idempotency_key=key)
    logs = uncertain_logs(job.pk)
    assert len(logs) == 1, \
        f"One unconfirmed mirror must record exactly one error log, got {len(logs)}"
    assert_uncertainty_recorded(job, logs[0], False, "rpush")
    assert job.pk in queued_ids(opts), \
        "The write did land — the job must remain claimable, which is why it is not failed"
    assert Event.objects.filter(
        category=jobs.UNCONFIRMED_PUBLISH_CATEGORY).count() == 1, \
        "An unconfirmed publication must file exactly one operator incident"


@th.django_unit_test()
def test_deferred_mirror_failure_does_not_raise_or_block_later_callbacks(opts):
    """A deferred mirror fault must be recorded, never propagated out of commit."""
    from django.db import transaction
    from mojo.apps import jobs
    from mojo.apps.jobs.models import Job
    from mojo.apps.incident.models import Event

    clear_unconfirmed_incident_state()
    key = "t3326-deferred"
    sentinel = []
    with mock.patch.object(jobs, "get_adapter",
                           return_value=WriteThenFailAdapter(jobs.get_adapter())):
        with transaction.atomic():
            jobs.publish(UNCERTAIN_FUNC, {"probe": "3326"},
                         channel=opts.test_channel, idempotency_key=key)
            transaction.on_commit(lambda: sentinel.append("ran"))

    assert sentinel == ["ran"], \
        ("A raising commit callback aborts every later one — the deferred publish "
         "must swallow its own fault")
    job = Job.objects.get(idempotency_key=key)
    logs = uncertain_logs(job.pk)
    assert len(logs) == 1, \
        f"One unconfirmed mirror must record exactly one error log, got {len(logs)}"
    assert_uncertainty_recorded(job, logs[0], True, "rpush")
    assert job.pk in queued_ids(opts), \
        "The deferred write landed, so the id must be on the queue"
    assert Event.objects.filter(
        category=jobs.UNCONFIRMED_PUBLISH_CATEGORY).count() == 1, \
        "An unconfirmed deferred publication must file exactly one operator incident"


@th.django_unit_test()
def test_all_mirror_operations_failing_records_one_uncertainty(opts):
    """A mirror that never reaches Redis is recorded the same way — as unknown."""
    from mojo.apps import jobs
    from mojo.apps.jobs.models import Job

    clear_unconfirmed_incident_state()
    key = "t3326-nowrite"
    raised = None
    try:
        publish_uncertain(
            opts, key, WriteThenFailAdapter(jobs.get_adapter(), write=False))
    except RuntimeError as e:
        raised = e

    assert raised is not None, \
        "A mirror that never landed must still raise on the immediate path"
    job = Job.objects.get(idempotency_key=key)
    logs = uncertain_logs(job.pk)
    assert len(logs) == 1, \
        f"A failed mirror must record exactly one error log, got {len(logs)}"
    assert_uncertainty_recorded(job, logs[0], False, "rpush")
    assert job.pk not in queued_ids(opts), \
        "Nothing reached Redis, so nothing may appear on the queue"


@th.django_unit_test()
def test_uncertainty_diagnostics_failure_is_survivable(opts):
    """Losing the diagnostic write must not cost the commit callbacks after it."""
    from django.db import transaction
    from mojo.apps import jobs
    from mojo.apps.jobs.models import Job

    clear_unconfirmed_incident_state()
    key = "t3326-nodiag"
    broken_log = mock.MagicMock()
    broken_log.objects.using.return_value.create.side_effect = RuntimeError(
        "simulated JobLog failure")
    sentinel = []
    with mock.patch.object(jobs, "get_adapter",
                           return_value=WriteThenFailAdapter(jobs.get_adapter())), \
            mock.patch("mojo.apps.jobs.models.JobLog", broken_log):
        with transaction.atomic():
            jobs.publish(UNCERTAIN_FUNC, {"probe": "3326"},
                         channel=opts.test_channel, idempotency_key=key)
            transaction.on_commit(lambda: sentinel.append("ran"))

    assert sentinel == ["ran"], \
        "A failed diagnostic write must not abort the remaining commit callbacks"
    job = Job.objects.get(idempotency_key=key)
    assert job.status == "pending", \
        f"The job must stay executable through a diagnostics failure, got {job.status}"
    assert job.last_error == jobs.UNCONFIRMED_PUBLISH_ERROR, \
        "Each diagnostic write is guarded separately — the row marker must survive"
    assert uncertain_logs(job.pk) == [], \
        "The JobLog write was faulted, so no log row may exist"


@th.django_unit_test()
def test_mirror_failure_after_claim_preserves_execution_state(opts):
    """A late uncertainty record must never overwrite a claimed job's own state."""
    from django.utils import timezone
    from mojo.apps import jobs
    from mojo.apps.jobs.models import Job

    clear_unconfirmed_incident_state()
    key = "t3326-after-claim"
    job_id = publish_uncertain(opts, key)
    Job.objects.filter(pk=job_id).update(
        status='running', attempt=1, runner_id='t3326-engine',
        started_at=timezone.now(), last_error='handler exploded')

    # Exactly what a deferred callback does when its mirror fails after a
    # duplicate delivery has already let a worker claim the row.
    jobs._record_uncertain_publication(
        job_id, opts.test_channel, 'default',
        ConnectionError("simulated"), True, 'rpush')

    job = Job.objects.get(pk=job_id)
    assert job.status == "running", \
        f"A mirror error must not change the status of a claimed job, got {job.status}"
    assert job.attempt == 1, \
        f"A mirror error must not change the attempt of a claimed job, got {job.attempt}"
    assert job.last_error == "handler exploded", \
        f"An execution error must never be replaced by the marker, got {job.last_error!r}"
    assert len(uncertain_logs(job_id)) == 1, \
        "The uncertainty is still recorded as a log, just not on the row"

    returned = publish_uncertain(
        opts, key, WriteThenFailAdapter(jobs.get_adapter(), write=False))
    job = Job.objects.get(pk=job_id)
    assert returned == job_id, \
        "Republishing a claimed idempotent job must return the same id"
    assert job.status == "running" and job.attempt == 1, \
        f"Republishing must not disturb a running job, got {job.status}/{job.attempt}"


@th.django_unit_test()
def test_uncertain_then_confirmed_republish_clears_the_marker(opts):
    """A confirmed re-mirror of the same row must retire the uncertainty marker."""
    from mojo.apps import jobs
    from mojo.apps.jobs.models import Job

    clear_unconfirmed_incident_state()
    key = "t3326-recover"
    try:
        publish_uncertain(
            opts, key, WriteThenFailAdapter(jobs.get_adapter(), write=False))
    except RuntimeError:
        pass

    job = Job.objects.get(idempotency_key=key)
    assert job.last_error == jobs.UNCONFIRMED_PUBLISH_ERROR, \
        "The failed publish must have left the marker to recover from"

    returned = publish_uncertain(opts, key)

    assert returned == job.pk, \
        "Recovering with the same idempotency key must reuse the same row"
    assert Job.objects.filter(idempotency_key=key).count() == 1, \
        "The recovery publish must not create a second Job row"
    job.refresh_from_db()
    assert job.status == "pending", \
        f"The recovered job must be pending, got {job.status}"
    assert job.last_error == "", \
        f"A confirmed mirror must clear the marker, got {job.last_error!r}"
    assert job.pk in queued_ids(opts), \
        "The recovered job must now be on the queue"


@th.django_unit_test()
def test_marker_cleared_even_after_worker_claim(opts):
    """The clear filters on the marker text alone, so a mid-window claim cannot strand it."""
    from django.utils import timezone
    from mojo.apps import jobs
    from mojo.apps.jobs.models import Job

    clear_unconfirmed_incident_state()
    key = "t3326-claim-clear"
    try:
        publish_uncertain(
            opts, key, WriteThenFailAdapter(jobs.get_adapter(), write=False))
    except RuntimeError:
        pass

    job = Job.objects.get(idempotency_key=key)
    assert job.last_error == jobs.UNCONFIRMED_PUBLISH_ERROR, \
        "The failed publish must have left a marker for the clear to find"
    # A worker claims the row between the successful RPUSH and the clear.
    Job.objects.filter(pk=job.pk).update(
        status='running', attempt=1, runner_id='t3326-engine',
        started_at=timezone.now())

    jobs._clear_uncertainty_marker(job.pk, 'default')

    job.refresh_from_db()
    assert job.last_error == "", \
        f"A claimed row must not be left carrying a stale marker, got {job.last_error!r}"
    assert job.status == "running", \
        f"Clearing the marker must not touch the status, got {job.status}"
    assert job.attempt == 1, \
        f"Clearing the marker must not touch the attempt, got {job.attempt}"


@th.django_unit_test()
def test_legacy_failed_unstarted_publish_still_resumes(opts):
    """Rows written by an older release still recover through the resume branch."""
    import uuid
    from mojo.apps import jobs
    from mojo.apps.jobs.models import Job

    key = "t3326-legacy"
    payload = {"probe": "3326"}
    legacy_id = uuid.uuid4().hex
    Job.objects.create(
        id=legacy_id,
        channel=opts.test_channel,
        func=UNCERTAIN_FUNC,
        payload=payload,
        status='failed',
        attempt=0,
        last_error='Failed to queue: simulated',
        idempotency_key=key)

    returned = jobs.publish(UNCERTAIN_FUNC, payload,
                            channel=opts.test_channel, idempotency_key=key)

    job = Job.objects.get(pk=legacy_id)
    assert returned == legacy_id, \
        "The legacy resume path must return the original job id"
    assert job.status == "pending", \
        f"A legacy 'Failed to queue:' row must return to pending, got {job.status}"
    assert job.last_error == "", \
        f"Resuming must clear the legacy queue error, got {job.last_error!r}"
    assert legacy_id in queued_ids(opts), \
        "The resumed job must reach the queue"
