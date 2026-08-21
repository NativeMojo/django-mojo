"""Publish/execute tests moved out of tests/test_job_engine/test_publish_execute.py.

Both tests patch shared mojo.apps.jobs internals in-process (`jobs.metrics.record`
and `job_engine.load_job_function`), which every parallel test thread sees, so
they run only in the opt-in serial tier (maestro item #1839). Neither
`jobs.publish` nor `JobEngine.execute_job` accepts an injectable
metrics/loader parameter, so there is no seam to convert through.
"""
from testit import helpers as th
import threading
from unittest import mock


@th.django_unit_setup()
def setup_publish_execute_serial_tests(opts):
    """Setup for publish/execute tests."""
    from mojo.apps.jobs.models import Job, JobEvent
    from mojo.apps.jobs.adapters import get_adapter
    from mojo.apps.jobs.keys import JobKeys

    # Clear test data - using test-specific job names for cleanup
    Job.objects.filter(func__contains='test_publish_execute').delete()
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
