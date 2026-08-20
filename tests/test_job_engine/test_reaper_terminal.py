"""The reaper and jobs that are already over.

A row can be terminal while its in-flight ZSET entry is still there — the
engine writes the durable row and then dies before the ZREM, which is exactly
what happens on a node that deploys itself (update.sh stops the engine running
the deploy job). The reaper's job in that case is to clean up Redis and say
nothing else about the row.

It used to say something else about the row: `completed`/`canceled` were
recognised as over, but `failed` and `expired` were not, so a finished job got
rewritten to `failed` with "Exceeded max retries after reaper timeout" — a
retry story about a job that was never going to be retried. `_reaper_loop`
cannot requeue a non-`running` row anyway (the branch below the message
checks), so this only ever falsified the record.

The engine is driven for real here — one pass of `_reaper_loop` against the
checkout's Redis — because the property under test is a branch inside that
loop, and a channel nobody else consumes keeps it out of the live engine's way.
"""
import threading
import time
import uuid

from testit import helpers as th

FUNC = "tests.test_job_engine.test_reaper_terminal.noop"
# Far older than any JOBS_VISIBILITY_TIMEOUT_MS the settings could carry.
STALE_AGE_MS = 3600 * 1000


@th.django_unit_setup()
def setup_reaper_terminal(opts):
    from mojo.apps.jobs.models import Job, JobEvent

    JobEvent.objects.filter(job__func=FUNC).delete()
    Job.objects.filter(func=FUNC).delete()


def _channel():
    return "reaper-test-%s" % uuid.uuid4().hex[:8]


def _stale_job(channel, status, **extra):
    """A durable row in `status` whose in-flight entry the engine never removed."""
    from mojo.apps.jobs.adapters import get_adapter
    from mojo.apps.jobs.keys import JobKeys
    from mojo.apps.jobs.models import Job

    job = Job.objects.create(
        id=uuid.uuid4().hex, channel=channel, func=FUNC, status=status, **extra)
    redis = get_adapter()
    score = int(time.time() * 1000) - STALE_AGE_MS
    redis.zadd(JobKeys().processing(channel), {job.id: score})
    return job


def _reap_once(channel):
    """Run `_reaper_loop` until it has drained this channel's in-flight set,
    then stop it. Returns nothing — the assertions read the durable row."""
    from mojo.apps.jobs.adapters import get_adapter
    from mojo.apps.jobs.job_engine import JobEngine
    from mojo.apps.jobs.keys import JobKeys

    key = JobKeys().processing(channel)
    redis = get_adapter()
    engine = JobEngine(channels=[channel],
                       runner_id="reaper-test-%s" % uuid.uuid4().hex[:6])
    engine.running = True
    engine.stop_event.clear()
    thread = threading.Thread(target=engine._reaper_loop, daemon=True)
    thread.start()
    try:
        deadline = time.time() + 20
        while time.time() < deadline:
            if not redis.zrangebyscore(key, float("-inf"), float("inf")):
                break
            time.sleep(0.1)
    finally:
        engine.running = False
        engine.stop_event.set()
        thread.join(timeout=20)
        engine.executor.shutdown(wait=False)
    th.assert_true(
        not redis.zrangebyscore(key, float("-inf"), float("inf")),
        "the reaper must always clear a stale in-flight entry — the rest of "
        "this module is about what it does to the durable row while doing so")


@th.django_unit_test("a completed job with a stale in-flight entry is only cleaned up")
def test_reaper_leaves_a_completed_job_alone(opts):
    from mojo.apps.jobs.models import Job, JobEvent

    channel = _channel()
    job = _stale_job(channel, "completed")

    _reap_once(channel)

    job.refresh_from_db()
    th.assert_eq(job.status, "completed",
                 f"a completed job must stay completed, got {job.status!r}")
    events = list(JobEvent.objects.filter(job=job).values_list("event", flat=True))
    th.assert_eq(events, [],
                 f"cleaning up Redis is not an event about the job, got {events!r}")


@th.django_unit_test("a failed job is not re-failed with a story about retries")
def test_reaper_does_not_rewrite_a_failed_job(opts):
    """The defect. `failed` was missing from the already-terminal check, so the
    reaper fell through to the max-retries branch and overwrote the real
    failure with a retry claim — for a job it could not have requeued."""
    from mojo.apps.jobs.models import Job, JobEvent

    channel = _channel()
    job = _stale_job(channel, "failed",
                     last_error="handler raised ValueError: the real reason")

    _reap_once(channel)

    job.refresh_from_db()
    th.assert_eq(job.status, "failed",
                 f"an already-failed job must stay failed, got {job.status!r}")
    th.assert_eq(job.last_error, "handler raised ValueError: the real reason",
                 f"the reaper must not overwrite the real failure reason with "
                 f"its own, got {job.last_error!r}")
    events = list(JobEvent.objects.filter(job=job).values_list("event", flat=True))
    th.assert_eq(events, [],
                 f"a job that was already over must not collect a second "
                 f"terminal event from the reaper, got {events!r}")


@th.django_unit_test("an expired job is not turned into a failed one")
def test_reaper_does_not_rewrite_an_expired_job(opts):
    from mojo.apps.jobs.models import Job, JobEvent

    channel = _channel()
    job = _stale_job(channel, "expired")

    _reap_once(channel)

    job.refresh_from_db()
    th.assert_eq(job.status, "expired",
                 f"an already-expired job must stay expired — 'failed' is a "
                 f"different outcome, got {job.status!r}")
    events = list(JobEvent.objects.filter(job=job).values_list("event", flat=True))
    th.assert_eq(events, [],
                 f"an already-expired job must not collect a reaper event, "
                 f"got {events!r}")


@th.django_unit_test("the running-lease message says what actually happened")
def test_reaper_message_does_not_claim_retries(opts):
    """A genuinely running job whose lease expired and which cannot be retried
    still gets failed by the reaper — that part is real. What it says about
    itself now describes the lease, not an attempt count nobody exceeded."""
    from mojo.apps.jobs.models import Job, JobEvent

    channel = _channel()
    job = _stale_job(channel, "running", attempt=0, max_retries=0)

    _reap_once(channel)

    job.refresh_from_db()
    th.assert_eq(job.status, "failed",
                 f"a running job with an expired lease and no retries left "
                 f"must be failed, got {job.status!r}")
    th.assert_eq(job.last_error,
                 "in-flight lease expired and the job is not retryable",
                 f"the recorded reason must be the lease, not a max-retries "
                 f"story for a job published with none, got {job.last_error!r}")
    th.assert_true(job.finished_at is not None,
                   "a reaper-failed job must be stamped finished")
    events = list(JobEvent.objects.filter(job=job).values_list("event", flat=True))
    th.assert_eq(events, ["failed"],
                 f"the real terminal transition still gets exactly one event, "
                 f"got {events!r}")
