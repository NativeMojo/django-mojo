"""Commit-ordered publication regressions (maestro item #3326).

jobs.publish() must never make a job ID visible to a worker before the
transaction that wrote the Job row commits: a worker that wins the race loads
the row, gets DoesNotExist, drops the queue entry and leaves the job stranded
at attempt 0 forever.

Real Postgres transactions and the real Redis adapter only — this package is
``default_core`` with a zero cold budget, so nothing here patches
``mojo.apps.jobs`` internals. The channel is the suite's declared "email"
channel; an invented name is refused by JOBS_ALLOWED_CHANNELS enforcement.
"""
from testit import helpers as th
import threading
import time
from datetime import timedelta
from django.db import transaction, connections
from django.utils import timezone


# Never executed — no engine consumes during these tests. It exists so setup
# can find and delete exactly the rows this module creates.
TEST_FUNC = "tests.test_job_engine.test_publish_on_commit.noop_job"


@th.django_unit_setup()
def setup_publish_on_commit_tests(opts):
    """Setup for commit-ordered publication tests."""
    from mojo.apps.jobs.models import Job
    from mojo.apps.jobs.adapters import get_adapter
    from mojo.apps.jobs.keys import JobKeys

    # Long-lived database: clear our own rows before creating any.
    Job.objects.filter(func=TEST_FUNC).delete()

    opts.redis = get_adapter()
    opts.keys = JobKeys()
    opts.test_channel = 'email'

    for key in (
        opts.keys.queue(opts.test_channel),
        opts.keys.processing(opts.test_channel),
        opts.keys.sched(opts.test_channel),
        opts.keys.sched_broadcast(opts.test_channel),
    ):
        opts.redis.delete(key)


def queued_ids(opts):
    """Job ids currently on the channel's immediate queue, as strings."""
    raw = opts.redis.get_client().lrange(opts.keys.queue(opts.test_channel), 0, -1)
    return [
        item.decode('utf-8') if isinstance(item, (bytes, bytearray)) else item
        for item in (raw or [])
    ]


def publish_test_job(opts, **kwargs):
    """Publish one job from this module onto the declared test channel."""
    from mojo.apps import jobs

    return jobs.publish(
        TEST_FUNC,
        {"probe": "3326"},
        channel=opts.test_channel,
        **kwargs)


def row_exists_on_new_connection(job_id):
    """Whether the Job row is visible to a connection this thread does not own."""
    from mojo.apps.jobs.models import Job

    seen = []

    def probe():
        try:
            seen.append(Job.objects.filter(pk=job_id).exists())
        finally:
            connections.close_all()

    worker = threading.Thread(target=probe)
    worker.start()
    worker.join(timeout=10)
    return seen[0] if seen else None


@th.django_unit_test()
def test_autocommit_publish_is_queued_immediately(opts):
    """With no enclosing transaction, publish must still queue synchronously."""
    from mojo.apps.jobs.models import Job

    job_id = publish_test_job(opts)

    assert job_id in queued_ids(opts), \
        "An autocommit publish must place the job id on the queue before returning"
    job = Job.objects.get(pk=job_id)
    assert job.status == "pending", \
        f"A freshly published job must be pending, got {job.status}"
    assert job.last_error == "", \
        "The immediate path confirms delivery inline, so it must leave no uncertainty marker"


@th.django_unit_test()
def test_publish_in_transaction_is_invisible_until_commit(opts):
    """The regression: a queue entry must not appear before the writer commits."""
    from mojo.apps.jobs.models import Job

    with transaction.atomic():
        job_id = publish_test_job(opts)
        assert job_id not in queued_ids(opts), \
            ("A job id must NOT be visible to workers while the transaction that "
             "wrote its row is still open — a worker claiming it now cannot load "
             "the row and would strand the job at attempt 0")

    assert job_id in queued_ids(opts), \
        "After the writer transaction commits, the job id must reach the queue"
    job = Job.objects.get(pk=job_id)
    assert job.status == "pending", \
        f"A committed, mirrored job must be pending, got {job.status}"
    assert job.last_error == "", \
        "A confirmed mirror must clear the pre-commit uncertainty marker"


@th.django_unit_test()
def test_publish_in_transaction_row_invisible_to_other_connection(opts):
    """The row a worker would need is genuinely unreadable before commit."""
    from mojo.apps.jobs.models import Job

    with transaction.atomic():
        job_id = publish_test_job(opts)
        visible = row_exists_on_new_connection(job_id)
        assert visible is False, \
            ("Another connection (what a worker uses) must not see the uncommitted "
             f"Job row, got {visible!r}")

    assert row_exists_on_new_connection(job_id) is True, \
        "After commit the Job row must be readable from any connection"
    assert Job.objects.filter(pk=job_id).count() == 1, \
        "Exactly one Job row must survive the commit"


@th.django_unit_test()
def test_outer_rollback_publishes_nothing(opts):
    """A rolled-back writer transaction must leave no row and no queue entry."""
    from mojo.apps.jobs.models import Job

    job_id = None
    try:
        with transaction.atomic():
            job_id = publish_test_job(opts)
            raise RuntimeError("rollback the publishing transaction")
    except RuntimeError:
        pass

    assert job_id, "publish() must still return an id inside a transaction"
    assert not Job.objects.filter(pk=job_id).exists(), \
        "A rolled-back publish must leave no Job row"
    assert job_id not in queued_ids(opts), \
        "A rolled-back publish must place nothing on the queue"
    assert opts.redis.zscore(opts.keys.sched(opts.test_channel), job_id) is None, \
        "A rolled-back publish must place nothing in the scheduled ZSET"


@th.django_unit_test()
def test_savepoint_rollback_publishes_nothing(opts):
    """An inner savepoint rollback discards only its own publication."""
    from mojo.apps.jobs.models import Job

    with transaction.atomic():
        control_id = publish_test_job(opts)
        inner_id = None
        try:
            with transaction.atomic():
                inner_id = publish_test_job(opts)
                raise RuntimeError("rollback the inner savepoint")
        except RuntimeError:
            pass

    queued = queued_ids(opts)
    assert not Job.objects.filter(pk=inner_id).exists(), \
        "The savepoint-rolled-back job must leave no Job row"
    assert inner_id not in queued, \
        "The savepoint-rolled-back job must never reach the queue"
    assert Job.objects.filter(pk=control_id).exists(), \
        "The surviving outer publish must keep its Job row"
    assert control_id in queued, \
        "The surviving outer publish must reach the queue on commit"


@th.django_unit_test()
def test_scheduled_publish_defers_to_sched_zset(opts):
    """A delayed publish lands in the scheduled ZSET at commit, not before."""
    from mojo.apps import jobs
    from mojo.apps.jobs.models import Job, JobEvent

    sched_key = opts.keys.sched(opts.test_channel)
    with transaction.atomic():
        job_id = publish_test_job(opts, delay=3600)
        expected_run_at = Job.objects.get(pk=job_id).run_at
        assert opts.redis.zscore(sched_key, job_id) is None, \
            "A scheduled job must not enter the ZSET before its transaction commits"

    score = opts.redis.zscore(sched_key, job_id)
    assert score is not None, \
        "After commit the scheduled job must be in the channel's sched ZSET"
    job = Job.objects.get(pk=job_id)
    assert abs(score - job.run_at.timestamp() * 1000) < 1.0, \
        "The ZSET score must be the original run_at, not a recomputed one"
    assert job.run_at == expected_run_at, \
        "Deferring publication must not move run_at"
    assert job_id not in queued_ids(opts), \
        "A future-dated job must not also land on the immediate queue"
    assert opts.test_channel in jobs.get_sched_channels(), \
        "The scheduler registry must learn about the channel on commit"
    assert JobEvent.objects.filter(job_id=job_id, event='scheduled').exists(), \
        "A confirmed scheduled mirror must record a 'scheduled' event"


@th.django_unit_test()
def test_due_scheduled_publish_queues_on_commit(opts):
    """A transaction that outlives run_at queues the job instead of scheduling it."""
    from mojo.apps.jobs.models import Job, JobEvent

    with transaction.atomic():
        job_id = publish_test_job(opts, run_at=timezone.now() + timedelta(seconds=1))
        published_expires_at = Job.objects.get(pk=job_id).expires_at
        time.sleep(1.5)

    job = Job.objects.get(pk=job_id)
    assert job_id in queued_ids(opts), \
        ("A job whose run_at passed while the transaction was open must go straight "
         "onto the queue, not wait for a scheduler poll")
    assert opts.redis.zscore(opts.keys.sched(opts.test_channel), job_id) is None, \
        "A now-due job must not be parked in the scheduled ZSET"
    assert JobEvent.objects.filter(job_id=job_id, event='queued').exists(), \
        "A due job mirrored at commit must record a 'queued' event"
    assert job.expires_at == published_expires_at, \
        "Deferring publication must never extend the original expiry"


@th.django_unit_test()
def test_idempotent_republish_in_transaction_keeps_one_row(opts):
    """Republishing the same key in one transaction keeps one row, mirrored on commit."""
    from mojo.apps.jobs.models import Job

    key = f"t3326-idem-{timezone.now().timestamp()}"
    with transaction.atomic():
        first_id = publish_test_job(opts, idempotency_key=key)
        second_id = publish_test_job(opts, idempotency_key=key)
        assert first_id not in queued_ids(opts), \
            "Neither publish may expose the id before the transaction commits"

    assert second_id == first_id, \
        "An idempotent republish must return the original job id"
    assert Job.objects.filter(idempotency_key=key).count() == 1, \
        "An idempotent republish must not create a second Job row"
    assert first_id in queued_ids(opts), \
        "The idempotent job must reach the queue once its transaction commits"
    job = Job.objects.get(pk=first_id)
    assert job.status == "pending", \
        f"The idempotent job must remain pending, got {job.status}"
    assert job.last_error == "", \
        "A confirmed mirror must clear the pre-commit uncertainty marker"
