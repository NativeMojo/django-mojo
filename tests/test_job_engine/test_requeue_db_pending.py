"""Operator requeue of stranded pending rows (maestro item #3367).

JobManager.requeue_db_pending must re-publish a channel's DB-pending rows
through the live publish mirror — the Plan B queue List and sched ZSETs the
engine actually consumes — and clear the #3326 uncertainty marker
(Job.last_error = UNCONFIRMED_PUBLISH_ERROR) on confirmed delivery. The defect:
it XADDed to the legacy ``mojo:jobs:stream:*`` keys nothing reads any more, so
"recovered" jobs were invisible to every worker and their markers never
cleared.

Real Postgres ORM and the real Redis adapter only — this package is
``default_core`` with no cold budget, so nothing here patches
``mojo.apps.jobs`` internals. Channels are private to this module; requeue is
deliberately not gated by JOBS_ALLOWED_CHANNELS, so they need no declaration.
"""
from testit import helpers as th
import uuid
from datetime import timedelta
from django.utils import timezone


# Never executed — no engine consumes these channels. It exists so setup can
# find and delete exactly the rows this module creates.
TEST_FUNC = "tests.test_job_engine.test_requeue_db_pending.noop_job"

CHANNEL_IMMEDIATE = "t3367_immediate"
CHANNEL_SCHED = "t3367_sched"
CHANNEL_BROADCAST = "t3367_broadcast"
CHANNEL_ORPHANED = "t3390_orphaned"
CHANNEL_CANCELED = "t3390_canceled"


def noop_job(job):
    """Handler stub for TEST_FUNC — present so the dotted path resolves."""
    return True


@th.django_unit_setup()
def setup_requeue_db_pending_tests(opts):
    """Clean this module's rows and Redis keys before creating anything."""
    from mojo.apps.jobs.models import Job
    from mojo.apps.jobs.adapters import get_adapter
    from mojo.apps.jobs.keys import JobKeys

    # Long-lived database: deleting the Job rows cascades their JobEvents.
    Job.objects.filter(func=TEST_FUNC).delete()

    opts.redis = get_adapter()
    opts.keys = JobKeys()

    client = opts.redis.get_client()
    for channel in (CHANNEL_IMMEDIATE, CHANNEL_SCHED, CHANNEL_BROADCAST,
                    CHANNEL_ORPHANED, CHANNEL_CANCELED):
        for key in (
            opts.keys.queue(channel),
            opts.keys.sched(channel),
            opts.keys.sched_broadcast(channel),
            opts.keys.processing(channel),
            opts.keys.stream(channel),
            opts.keys.stream_broadcast(channel),
        ):
            opts.redis.delete(key)
        client.srem(opts.keys.sched_registry(), channel)


def queued_ids(opts, channel):
    """Job ids currently on the channel's immediate queue, as strings."""
    raw = opts.redis.get_client().lrange(opts.keys.queue(channel), 0, -1)
    return [
        item.decode('utf-8') if isinstance(item, (bytes, bytearray)) else item
        for item in (raw or [])
    ]


def create_stranded_job(channel, run_at=None, broadcast=False):
    """One Job row in the exact state an unconfirmed publish leaves behind."""
    from mojo.apps.jobs import UNCONFIRMED_PUBLISH_ERROR
    from mojo.apps.jobs.models import Job

    # Job.id is a CharField primary key with no default — supply it.
    return Job.objects.create(
        id=uuid.uuid4().hex,
        channel=channel,
        func=TEST_FUNC,
        payload={"probe": "3367"},
        status='pending',
        attempt=0,
        last_error=UNCONFIRMED_PUBLISH_ERROR,
        run_at=run_at,
        broadcast=broadcast,
    )


@th.django_unit_test()
def test_stranded_pending_rows_become_claimable_after_requeue(opts):
    """The regression: requeued rows must land on the queue workers consume."""
    from mojo.apps.jobs.manager import get_manager
    from mojo.apps.jobs.models import Job

    first = create_stranded_job(CHANNEL_IMMEDIATE)
    second = create_stranded_job(CHANNEL_IMMEDIATE)

    assert queued_ids(opts, CHANNEL_IMMEDIATE) == [], \
        "The stranded state means the channel queue is empty before the requeue"

    result = get_manager().requeue_db_pending(CHANNEL_IMMEDIATE)

    assert result['status'] is True, \
        f"requeue_db_pending must report success, got {result!r}"
    assert result['requeued'] == 2, \
        f"Both stranded pending rows must be requeued, got {result!r}"

    queued = queued_ids(opts, CHANNEL_IMMEDIATE)
    assert first.id in queued, \
        (f"Requeued job {first.id} must be on the live queue list the engine "
         f"BRPOPs, got {queued!r}")
    assert second.id in queued, \
        (f"Requeued job {second.id} must be on the live queue list the engine "
         f"BRPOPs, got {queued!r}")

    # The claim primitive the engine uses must actually surface one of them
    # while its row is still pending — i.e. the recovered job is executable.
    popped = opts.redis.brpop([opts.keys.queue(CHANNEL_IMMEDIATE)], timeout=1)
    assert popped is not None, \
        "BRPOP on the channel queue must claim a requeued job id"
    popped_id = popped[1]
    if isinstance(popped_id, (bytes, bytearray)):
        popped_id = popped_id.decode('utf-8')
    assert popped_id in (first.id, second.id), \
        f"BRPOP must return one of the requeued ids, got {popped_id!r}"
    assert Job.objects.get(pk=popped_id).status == 'pending', \
        "The claimed row must still be pending — claimable by a real worker"

    for job in (first, second):
        job.refresh_from_db()
        assert job.last_error == "", \
            (f"A confirmed requeue must clear the uncertainty marker on "
             f"{job.id}, got {job.last_error!r}")

    assert not opts.redis.exists(opts.keys.stream(CHANNEL_IMMEDIATE)), \
        ("Requeue must publish to the live queue, not the legacy stream key "
         "nothing consumes")


@th.django_unit_test()
def test_scheduled_pending_row_routes_to_sched_zset(opts):
    """A future-dated pending row must be re-mirrored into the sched ZSET."""
    from mojo.apps.jobs.manager import get_manager

    run_at = timezone.now() + timedelta(hours=1)
    job = create_stranded_job(CHANNEL_SCHED, run_at=run_at)

    result = get_manager().requeue_db_pending(CHANNEL_SCHED)

    assert result['status'] is True, \
        f"requeue_db_pending must report success, got {result!r}"
    assert result['requeued'] == 1, \
        f"The scheduled pending row must be requeued, got {result!r}"

    job.refresh_from_db()
    score = opts.redis.zscore(opts.keys.sched(CHANNEL_SCHED), job.id)
    assert score is not None, \
        "A future-dated requeued job must be in the channel's sched ZSET"
    assert abs(score - job.run_at.timestamp() * 1000) < 1.0, \
        (f"The ZSET score must be the row's own run_at in ms, got {score!r} "
         f"vs {job.run_at.timestamp() * 1000!r}")
    assert job.id not in queued_ids(opts, CHANNEL_SCHED), \
        "A future-dated job must not also land on the immediate queue"


@th.django_unit_test()
def test_broadcast_pending_row_lands_on_channel_queue(opts):
    """An immediate broadcast pending row must land on the channel queue."""
    from mojo.apps.jobs.manager import get_manager

    job = create_stranded_job(CHANNEL_BROADCAST, broadcast=True)

    result = get_manager().requeue_db_pending(CHANNEL_BROADCAST)

    assert result['status'] is True, \
        f"requeue_db_pending must report success, got {result!r}"
    assert result['requeued'] == 1, \
        f"The broadcast pending row must be requeued, got {result!r}"

    assert job.id in queued_ids(opts, CHANNEL_BROADCAST), \
        ("An immediate broadcast row must be re-mirrored onto the channel "
         "queue, like the live publish mirror does")
    assert not opts.redis.exists(opts.keys.stream_broadcast(CHANNEL_BROADCAST)), \
        ("Requeue must not write the legacy broadcast stream key nothing "
         "consumes")


def create_orphaned_running_job(channel, cancel_requested=False):
    """A row a dead runner left at status='running' with no ZSET member.

    Same shape as ``create_stranded_job`` but in the state documented under
    "Orphaned running rows" in ``docs/django_developer/jobs/admin.md``: the
    runner claimed the row, wrote status='running', and died before writing a
    terminal status — so nothing remains in the processing ZSET to reap.
    """
    from mojo.apps.jobs.models import Job

    return Job.objects.create(
        id=uuid.uuid4().hex,
        channel=channel,
        func=TEST_FUNC,
        payload={"probe": "3390"},
        status='running',
        attempt=2,
        runner_id=f"dead-runner-{uuid.uuid4().hex[:8]}",
        started_at=timezone.now() - timedelta(hours=1),
        last_error="runner vanished",
        stack_trace="stale trace",
        cancel_requested=cancel_requested,
    )


def apply_documented_reset(channel):
    """The exact filtered update the admin doc's recovery recipe prescribes."""
    from mojo.apps.jobs.models import Job

    return Job.objects.filter(channel=channel, status='running').exclude(
        cancel_requested=True
    ).update(
        status='pending',
        runner_id=None,
        started_at=None,
        finished_at=None,
        attempt=0,
        last_error='',
        stack_trace='',
    )


@th.django_unit_test()
def test_documented_recovery_recipe_requeues_orphaned_running_row(opts):
    """The documented two-step recipe must make an orphaned row runnable."""
    from mojo.apps.jobs.manager import get_manager

    job = create_orphaned_running_job(CHANNEL_ORPHANED)

    assert opts.redis.zscore(opts.keys.processing(CHANNEL_ORPHANED), job.id) is None, \
        ("The orphaned state means no processing-ZSET member exists — that is "
         "why the engine reaper can never see this row")
    assert job.id not in queued_ids(opts, CHANNEL_ORPHANED), \
        "An orphaned running row must not already be on the channel queue"

    reset = apply_documented_reset(CHANNEL_ORPHANED)
    assert reset == 1, \
        f"The documented update must reset exactly the orphaned row, got {reset!r}"

    result = get_manager().requeue_db_pending(CHANNEL_ORPHANED)
    assert result['status'] is True, \
        f"requeue_db_pending must report success, got {result!r}"
    assert result['requeued'] == 1, \
        f"The reset row must be requeued, got {result!r}"

    assert job.id in queued_ids(opts, CHANNEL_ORPHANED), \
        (f"After the documented recipe, job {job.id} must be on the live queue "
         f"list the engine BRPOPs, got {queued_ids(opts, CHANNEL_ORPHANED)!r}")

    job.refresh_from_db()
    assert job.status == 'pending', \
        (f"The recovered row must be pending — execute_job drops any delivery "
         f"whose row is not pending, got {job.status!r}")
    assert job.attempt == 0, \
        f"The recipe must clear the attempt counter, got {job.attempt!r}"
    assert job.runner_id is None, \
        f"The dead runner must be cleared off the row, got {job.runner_id!r}"


@th.django_unit_test()
def test_documented_recovery_recipe_skips_cancelled_rows(opts):
    """.exclude(cancel_requested=True) must keep cancelled work cancelled.

    cancel() sets cancel_requested=True and deliberately leaves status alone,
    so a job cancelled while running whose runner then died is itself an
    orphaned 'running' row. Resetting it would re-run work an operator
    explicitly cancelled.
    """
    from mojo.apps.jobs.manager import get_manager

    recoverable = create_orphaned_running_job(CHANNEL_CANCELED)
    cancelled = create_orphaned_running_job(CHANNEL_CANCELED, cancel_requested=True)

    reset = apply_documented_reset(CHANNEL_CANCELED)
    assert reset == 1, \
        (f"Only the non-cancelled orphan may be reset — the exclude must skip "
         f"the cancelled row, got {reset!r} rows updated")

    result = get_manager().requeue_db_pending(CHANNEL_CANCELED)
    assert result['status'] is True, \
        f"requeue_db_pending must report success, got {result!r}"

    queued = queued_ids(opts, CHANNEL_CANCELED)

    recoverable.refresh_from_db()
    assert recoverable.status == 'pending', \
        (f"The non-cancelled orphan must be recovered by the recipe, got "
         f"{recoverable.status!r}")
    assert recoverable.id in queued, \
        f"The non-cancelled orphan must be back on the queue, got {queued!r}"

    cancelled.refresh_from_db()
    assert cancelled.status == 'running', \
        (f"A cancelled row must not be reset to pending by the recipe, got "
         f"{cancelled.status!r}")
    assert cancelled.cancel_requested is True, \
        "The cancel flag must survive the recovery recipe untouched"
    assert cancelled.id not in queued, \
        (f"Requeueing must never put an operator-cancelled job back on the "
         f"queue, got {queued!r}")
