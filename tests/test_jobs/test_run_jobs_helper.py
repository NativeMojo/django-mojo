"""testit's synchronous job drain — `th.run_jobs()`.

The property that matters and that a daemon could never give us: the handler
runs in THIS interpreter, so `mock.patch` reaches it. Every test here leans on
that.

CHANNEL ISOLATION — every call below is scoped to CHANNEL, a private name
deliberately absent from JOBS_CHANNELS. `th.run_jobs()` and `th.clear_jobs()`
with no channel mean "every channel this box consumes", and
tests/test_dnsman/6_certs_service.py drives its cert flow through both of them
that way. Modules run as parallel threads (testit/runner.py, ThreadPoolExecutor)
against one Redis and one database, so on a shared channel that module could
drain this one's jobs mid-test, or delete their rows between publish and drain —
and this module's cleanup could do the same to it. Nothing reaches a private
channel, so the two stay out of each other's way.

The cost, recorded so it is not silently lost: the no-argument form of
`th.run_jobs()` — "drain every channel in JOBS_CHANNELS" — is no longer
exercised here. tests/test_dnsman/6_certs_service.py covers that path
end-to-end, publishing through the real service and draining with no channel.
"""

from testit import helpers as th


# Legal per jobs.validate_channel_name (letters, digits, underscore) and not in
# DEFAULT_CHANNELS, so no consumer and no other module's global drain sees it.
CHANNEL = "testit_run_jobs_helper"


# Module-level sink the job handler writes to. It has to be module-level (not a
# closure) because the real publish path stores a dotted function PATH and
# re-imports it at execution time — a locally-defined function could not be
# resolved.
CALLS = []


def record_call(job):
    """A real job handler, resolvable by dotted path."""
    CALLS.append(job.payload.get("marker"))
    return "ok"


def boom(job):
    raise RuntimeError("handler blew up on purpose")


# Derive the dotted path from __name__ rather than hardcoding it. testit
# imports this file as "test_jobs.test_run_jobs_helper" (tests/ is on
# sys.path), so a hardcoded "tests.test_jobs..." would import a SECOND copy of
# this module under a different name — with its own CALLS list, and immune to
# any patch applied here. Handlers must be addressed by the same name the
# module is actually loaded under.
HANDLER = f"{__name__}.record_call"
FAILING_HANDLER = f"{__name__}.boom"


@th.django_unit_setup()
def setup_run_jobs(opts):
    th.clear_jobs(channel=CHANNEL)


def _reset():
    """django_unit_setup runs once per MODULE, so per-test state resets here."""
    CALLS.clear()
    th.clear_jobs(channel=CHANNEL)


@th.django_unit_test("run_jobs executes a published job in-process")
def test_run_jobs_executes(opts):
    _reset()
    from mojo.apps import jobs

    job_id = jobs.publish(func=HANDLER, payload={"marker": "alpha"}, channel=CHANNEL)
    assert CALLS == [], "the job must not run until run_jobs() is called"
    assert th.pending_job_count(channel=CHANNEL) >= 1, \
        "publish should leave a job on the queue"

    result = th.run_jobs(channel=CHANNEL)

    # Scoped to OUR job rather than the total. The private channel makes that
    # exact now, but the id is also what "publish → drain works" actually
    # means, so the assertion says what it means either way.
    drained = [str(j) for j in result.job_ids]
    assert str(job_id) in drained, \
        f"the published job {job_id} should have been drained, drained {drained}"
    assert CALLS == ["alpha"], f"the real handler should have run, got {CALLS}"


@th.django_unit_test("run_jobs marks the Job row completed")
def test_run_jobs_marks_completed(opts):
    _reset()
    from mojo.apps import jobs
    from mojo.apps.jobs.models import Job

    job_id = jobs.publish(func=HANDLER, payload={"marker": "beta"}, channel=CHANNEL)
    th.run_jobs(channel=CHANNEL)

    job = Job.objects.get(id=job_id)
    assert job.status == "completed", \
        f"the drain must drive real state transitions, job is {job.status}"
    assert job.finished_at is not None, "a completed job should have finished_at set"


@th.django_unit_test("the payload round-trips through the real publish path")
def test_run_jobs_payload_roundtrip(opts):
    _reset()
    from mojo.apps import jobs

    # A pure in-process call would never catch a payload that cannot survive
    # JSON. Going through publish() means it does.
    jobs.publish(func=HANDLER, payload={"marker": {"nested": [1, 2, 3]}}, channel=CHANNEL)
    th.run_jobs(channel=CHANNEL)

    assert CALLS == [{"nested": [1, 2, 3]}], \
        f"payload should survive the JSON round-trip intact, got {CALLS}"


@th.django_unit_test("mock.patch reaches the handler — the whole point")
def test_run_jobs_is_patchable(opts):
    _reset()
    from unittest import mock
    from mojo.apps import jobs
    import sys
    mod = sys.modules[__name__]

    # This is what a separate daemon process could never do, and why job-backed
    # code was previously untestable without hitting real providers.
    with mock.patch.object(mod, "record_call", lambda job: CALLS.append("patched")):
        jobs.publish(func=HANDLER, payload={"marker": "ignored"}, channel=CHANNEL)
        th.run_jobs(channel=CHANNEL)

    assert CALLS == ["patched"], \
        f"the patch must apply to the executing handler, got {CALLS}"


@th.django_unit_test("a failing handler does not abort the drain")
def test_run_jobs_handles_failure(opts):
    _reset()
    from mojo.apps import jobs
    from mojo.apps.jobs.models import Job

    bad_id = jobs.publish(func=FAILING_HANDLER, payload={}, channel=CHANNEL)
    good_id = jobs.publish(func=HANDLER, payload={"marker": "after-failure"},
                           channel=CHANNEL)

    result = th.run_jobs(channel=CHANNEL)

    drained = [str(j) for j in result.job_ids]
    assert str(bad_id) in drained, \
        f"the failing job {bad_id} should have been drained, drained {drained}"
    assert str(good_id) in drained, \
        f"the job queued after the failure should still be drained, drained {drained}"
    assert CALLS == ["after-failure"], \
        f"a job queued after a failing one must still run, got {CALLS}"
    bad = Job.objects.get(id=bad_id)
    assert bad.status in ("failed", "retrying"), \
        f"a raising handler should not be recorded as completed, got {bad.status}"


@th.django_unit_test("run_jobs on an empty queue is a cheap no-op")
def test_run_jobs_empty(opts):
    _reset()
    result = th.run_jobs(channel=CHANNEL)
    assert result.count == 0, f"nothing was published, drained {result.count}"
    assert result.hit_limit is False, "an empty drain should not report hitting the limit"


@th.django_unit_test("max_jobs stops a self-republishing handler")
def test_run_jobs_max_jobs(opts):
    _reset()
    from mojo.apps import jobs

    for i in range(4):
        jobs.publish(func=HANDLER, payload={"marker": i}, channel=CHANNEL)

    result = th.run_jobs(max_jobs=2, channel=CHANNEL)
    assert result.count == 2, f"max_jobs=2 should stop after 2, got {result.count}"
    assert result.hit_limit is True, "hitting the cap should be reported, not silent"
    assert len(CALLS) == 2, \
        f"the cap should have let exactly 2 handlers run, got {CALLS}"

    th.run_jobs(channel=CHANNEL)  # drain the rest so the next test starts clean
    # Sorted, not ordered: the cap must PAUSE the queue rather than drop from
    # it. Which end the drain works from is queue discipline (rpush + brpop is
    # LIFO today) and not what max_jobs is for.
    assert sorted(CALLS) == [0, 1, 2, 3], \
        f"the capped drain must pause the queue, not drop from it, got {CALLS}"


@th.django_unit_test("delayed jobs are promoted and run")
def test_run_jobs_promotes_scheduled(opts):
    _reset()
    from mojo.apps import jobs

    # A delay= job lands in a ZSET, invisible to the queue until the Scheduler
    # promotes it. run_jobs() does that promotion itself.
    jobs.publish(func=HANDLER, payload={"marker": "delayed"}, delay=-5, channel=CHANNEL)

    result = th.run_jobs(channel=CHANNEL)
    assert result.count == 1, f"a due scheduled job should be drained, got {result.count}"
    assert CALLS == ["delayed"], f"the delayed handler should have run, got {CALLS}"


@th.django_unit_test("a not-yet-due job is left alone")
def test_run_jobs_leaves_future_jobs(opts):
    _reset()
    from mojo.apps import jobs

    jobs.publish(func=HANDLER, payload={"marker": "future"}, delay=3600, channel=CHANNEL)

    result = th.run_jobs(channel=CHANNEL)
    assert result.count == 0, f"a future job must not be run early, drained {result.count}"
    assert CALLS == [], f"nothing should have executed, got {CALLS}"

    th.clear_jobs(channel=CHANNEL)
