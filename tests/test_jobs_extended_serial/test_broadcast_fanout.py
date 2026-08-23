"""Moved from the default-tier sibling (maestro item #1839): these tests mutate shared testit/production module state process-wide (seam rebinding, module-attribute save/restore), which races every parallel module.
"""
"""`broadcast=True` reaches EVERY live runner, not whichever one pops first.

The bug this module pins down (django-mojo <= 1.7.1): under Plan B every runner
BRPOPs the one per-channel List, so a single job id on that List is taken by
exactly ONE runner — competing consumers, the precise opposite of broadcast.
`publish()` recorded `broadcast` on the row and printed it in the log line
while delivering a unicast, so it looked like it worked.

Measured on a two-node fleet before the fix: six broadcasts landed 3 on one
runner and 3 on the other, never 6 on both. Fleet-wide work — edge convergence
after a promote, dnsman's certificate-updated sweep — reached one box per
publish, and the others drifted until a later sweep happened to pick them. A
renewed certificate reaching one node of two is the sharp end of it.

The fix fans out at publish time into one ordinary job per live runner,
addressed to that runner's box-direct channel (every runner_id ends in
'-engine' precisely so that channel is publishable). One job ROW per runner,
because the row owns status/runner_id/attempt and N runners writing one row
would race to last-writer-wins.

`get_runners` is patched here rather than starting real engines: the unit under
test is the routing decision publish() makes from the roster, and a
two-runner fleet cannot otherwise exist inside one test process.
"""

from testit import helpers as th


CH = "t1771_broadcast"                 # declared in JOBS_ALLOWED_CHANNELS
RUNNER_A = "t1771a-engine"             # box-direct: implicitly allowed
RUNNER_B = "t1771b-engine"
CHANNELS = [CH, RUNNER_A, RUNNER_B]

CALLS = []


def record_call(job):
    """Resolvable by dotted path — the real publish path re-imports handlers."""
    CALLS.append(job.channel)
    return "ok"


FUNC = "test_jobs_extended_serial.test_broadcast_fanout.record_call"


def _clear(opts):
    """Tests share a long-lived DB and Redis: delete before creating."""
    from mojo.apps.jobs.models import Job

    CALLS.clear()
    for ch in CHANNELS:
        opts.redis.delete(opts.keys.queue(ch))
        opts.redis.delete(opts.keys.sched(ch))
        opts.redis.delete(opts.keys.sched_broadcast(ch))
        opts.redis.delete(opts.keys.processing(ch))
    opts.redis.get_client().srem(opts.keys.sched_registry(), *CHANNELS)
    Job.objects.filter(channel__in=CHANNELS).delete()


class _Roster:
    """Swap the EXACT roster reader for a fixed roster, restore on exit.

    publish() resolves its fan-out targets through broadcast_roster(), which
    reads get_runners_bounded — item #2729. Stubbing get_runners here instead
    would leave every test in this file silently exercising the degraded
    fallback branch while still passing.
    """

    def __init__(self, runners):
        self.runners = runners

    def __enter__(self):
        from mojo.apps import jobs
        self.original = jobs.get_runners_bounded
        jobs.get_runners_bounded = lambda channel, **kwargs: list(self.runners)
        return self

    def __exit__(self, *exc):
        from mojo.apps import jobs
        jobs.get_runners_bounded = self.original
        return False


class _BrokenRoster:
    """Make the exact reader raise, and record whether the cheap one was used.

    Both readers are swapped so a test can prove which branch ran: the
    fault-selective fallback is the whole design decision under test.
    """

    def __init__(self, fault, fallback=None):
        self.fault = fault
        self.fallback = fallback or []
        self.fallback_calls = 0

    def __enter__(self):
        from mojo.apps import jobs

        def _raise(channel, **kwargs):
            raise RuntimeError(self.fault)

        def _cheap(channel=None):
            self.fallback_calls += 1
            return list(self.fallback)

        self.original_bounded = jobs.get_runners_bounded
        self.original_plain = jobs.get_runners
        jobs.get_runners_bounded = _raise
        jobs.get_runners = _cheap
        return self

    def __exit__(self, *exc):
        from mojo.apps import jobs
        jobs.get_runners_bounded = self.original_bounded
        jobs.get_runners = self.original_plain
        return False


def _clear_degraded_notices(channels=CHANNELS):
    """The suppression key has an hour TTL on a long-lived Redis, so a suite
    re-run inside the hour would see no second Event and mis-read it."""
    from mojo.apps import incident
    from mojo.apps.incident.models import Event
    from mojo.apps.jobs import DEGRADED_BROADCAST_CATEGORY
    from mojo.helpers.redis import get_connection

    client = get_connection()
    for channel in channels:
        client.delete(incident.notice_key(DEGRADED_BROADCAST_CATEGORY, channel))
    Event.objects.filter(category=DEGRADED_BROADCAST_CATEGORY).delete()


def _degraded_events():
    from mojo.apps.incident.models import Event
    from mojo.apps.jobs import DEGRADED_BROADCAST_CATEGORY
    return Event.objects.filter(category=DEGRADED_BROADCAST_CATEGORY)


def _alive(runner_id):
    return {"runner_id": runner_id, "hostname": runner_id, "alive": True}


@th.django_unit_setup()
def setup_broadcast_fanout(opts):
    from mojo.apps.jobs.adapters import get_adapter
    from mojo.apps.jobs.keys import JobKeys

    opts.redis = get_adapter()
    opts.keys = JobKeys()
    _clear(opts)
    _clear_degraded_notices()


@th.django_unit_test("an immediate broadcast reaches EVERY live runner")
def test_broadcast_fans_out_to_every_runner(opts):
    from mojo.apps import jobs
    from mojo.apps.jobs.models import Job

    _clear(opts)
    try:
        with _Roster([_alive(RUNNER_A), _alive(RUNNER_B)]):
            result = jobs.publish(FUNC, {"n": 1}, channel=CH, broadcast=True)

        assert isinstance(result, list), (
            f"an immediate broadcast must return one job id per runner, got "
            f"{type(result).__name__}: {result!r}")
        assert len(result) == 2, (
            f"expected 2 job ids for a 2-runner fleet, got {len(result)}: "
            f"{result!r}")

        rows = list(Job.objects.filter(id__in=result))
        assert len(rows) == 2, (
            f"expected 2 Job rows, found {len(rows)} — one row shared by two "
            f"runners would race on status/runner_id")

        channels = sorted(r.channel for r in rows)
        assert channels == sorted([RUNNER_A, RUNNER_B]), (
            f"each job must be addressed to a runner's box-direct channel; "
            f"got {channels}. Landing on {CH!r} is the bug: every runner "
            f"BRPOPs that one queue, so exactly one would execute it.")

        assert Job.objects.filter(channel=CH).count() == 0, (
            f"nothing may be left on the shared channel queue {CH!r} — a job "
            f"there is consumed by a single runner")

        for row in rows:
            assert row.broadcast is False, (
                f"the fanned-out job on {row.channel} must be an ordinary job; "
                f"leaving broadcast=True risks a second fan-out")
            assert row.func == FUNC, (
                f"fan-out changed the handler: {row.func!r} != {FUNC!r}")
            assert row.payload == {"n": 1}, (
                f"fan-out changed the payload on {row.channel}: {row.payload!r}")

        depth = opts.redis.get_client().llen(opts.keys.queue(CH))
        assert depth == 0, (
            f"shared queue {CH!r} has {depth} entries; the fan-out must enqueue "
            f"onto the per-runner queues instead")
        for runner_id in (RUNNER_A, RUNNER_B):
            d = opts.redis.get_client().llen(opts.keys.queue(runner_id))
            assert d == 1, (
                f"runner queue {runner_id!r} has {d} entries, expected exactly 1")
    finally:
        _clear(opts)


@th.django_unit_test("a broadcast with no live runners still queues, once")
def test_broadcast_without_runners_degrades_to_one_queued_job(opts):
    from mojo.apps import jobs
    from mojo.apps.jobs.models import Job

    _clear(opts)
    try:
        with _Roster([]):
            result = jobs.publish(FUNC, {"n": 2}, channel=CH, broadcast=True)

        assert isinstance(result, str), (
            f"with no runners there is nothing to fan out to, so a single id "
            f"is expected, got {type(result).__name__}: {result!r}")
        rows = list(Job.objects.filter(channel=CH))
        assert len(rows) == 1, (
            f"expected the job to wait on the shared queue, found {len(rows)} "
            f"rows on {CH!r} — dropping it would be worse than queueing it")
        assert rows[0].id == result, (
            f"returned id {result!r} does not match the queued row "
            f"{rows[0].id!r}")
    finally:
        _clear(opts)


@th.django_unit_test("a dead runner is not a fan-out target")
def test_broadcast_skips_dead_runners(opts):
    from mojo.apps import jobs
    from mojo.apps.jobs.models import Job

    _clear(opts)
    try:
        dead = {"runner_id": RUNNER_B, "hostname": RUNNER_B, "alive": False}
        with _Roster([_alive(RUNNER_A), dead]):
            result = jobs.publish(FUNC, {"n": 3}, channel=CH, broadcast=True)

        assert len(result) == 1, (
            f"only the live runner should receive the job, got {len(result)} "
            f"ids: {result!r} — get_runners keeps reporting a dead runner for "
            f"one heartbeat TTL, and publishing to it strands the work")
        row = Job.objects.get(id=result[0])
        assert row.channel == RUNNER_A, (
            f"job went to {row.channel!r}, expected the live runner "
            f"{RUNNER_A!r}")
        assert Job.objects.filter(channel=RUNNER_B).count() == 0, (
            f"a job was addressed to the dead runner {RUNNER_B!r}")
    finally:
        _clear(opts)


@th.django_unit_test("a DELAYED broadcast is not silently fanned out")
def test_delayed_broadcast_is_not_fanned_out(opts):
    """The roster at publish time is not the roster at fire time, so a delayed
    broadcast still promotes to the shared queue. Pinned so the limitation is a
    recorded decision rather than a surprise — and so that implementing it
    later fails this test loudly instead of passing unnoticed."""
    from mojo.apps import jobs
    from mojo.apps.jobs.models import Job

    _clear(opts)
    try:
        with _Roster([_alive(RUNNER_A), _alive(RUNNER_B)]):
            result = jobs.publish(FUNC, {"n": 4}, channel=CH,
                                  broadcast=True, delay=3600)

        assert isinstance(result, str), (
            f"a delayed broadcast is not fanned out, so one id is expected; "
            f"got {type(result).__name__}: {result!r}")
        row = Job.objects.get(id=result)
        assert row.channel == CH, (
            f"delayed broadcast should stay on {CH!r}, got {row.channel!r}")
        assert row.broadcast is True, (
            "the delayed row must keep broadcast=True so the scheduler still "
            "routes it through sched_broadcast")
        scheduled = opts.redis.get_client().zcard(opts.keys.sched_broadcast(CH))
        assert scheduled == 1, (
            f"expected the job in the sched_broadcast ZSET, found {scheduled}")
    finally:
        _clear(opts)


@th.django_unit_test("a timeout does NOT fall back to the unbounded scan")
def test_unreadable_roster_timeout_does_not_fall_back(opts):
    """get_runners scans the keyspace on the process pool, whose socket
    timeout defaults to 60s. Answering 'Redis is slow' with that, inside
    publish(), inside a cron minute, is worse than not answering."""
    from mojo.apps import jobs
    from mojo.apps.jobs.models import Job

    _clear(opts)
    _clear_degraded_notices()
    try:
        with _BrokenRoster("runner_roster_timeout",
                           fallback=[_alive(RUNNER_A)]) as roster:
            result = jobs.publish(FUNC, {"n": 5}, channel=CH, broadcast=True)

        assert roster.fallback_calls == 0, (
            "a roster TIMEOUT fell back to the unbounded keyspace scan — the "
            "one fault where the cheap reader is the wrong answer")
        assert isinstance(result, str), (
            f"with no usable roster the job waits on the shared queue, so one "
            f"id is expected, got {type(result).__name__}: {result!r}")
        assert Job.objects.filter(channel=CH).count() == 1, (
            "the job should be queued once on the shared channel queue")
        assert _degraded_events().count() == 1, (
            f"an unprovable roster must be reported, not swallowed; found "
            f"{_degraded_events().count()} incidents")
    finally:
        _clear(opts)
        _clear_degraded_notices()


@th.django_unit_test("an invalid roster falls back and still fans out")
def test_unreadable_roster_invalid_falls_back(opts):
    """A stale registry entry or a skewed clock makes the exact reader raise
    while Redis is answering promptly — there the cheap reader is right, and
    is likely to return the better roster."""
    from mojo.apps import jobs
    from mojo.apps.jobs.models import Job

    _clear(opts)
    _clear_degraded_notices()
    try:
        with _BrokenRoster("runner_roster_invalid",
                           fallback=[_alive(RUNNER_A), _alive(RUNNER_B)]) as roster:
            result = jobs.publish(FUNC, {"n": 6}, channel=CH, broadcast=True)

        assert roster.fallback_calls == 1, (
            f"an invalid roster must fall back to the cheap reader, got "
            f"{roster.fallback_calls} calls")
        assert isinstance(result, list) and len(result) == 2, (
            f"the fallback roster must still fan out to both runners, got "
            f"{result!r}")
        channels = sorted(r.channel for r in Job.objects.filter(id__in=result))
        assert channels == sorted([RUNNER_A, RUNNER_B]), (
            f"degraded delivery still addresses box-direct channels: {channels}")
        assert _degraded_events().count() == 1, (
            "a degraded roster must be reported even when delivery succeeded")
    finally:
        _clear(opts)
        _clear_degraded_notices()


@th.django_unit_test("a repeat degradation inside the window files no second incident")
def test_degraded_roster_incident_is_suppressed(opts):
    """dnsman publishes one broadcast per certificate on a bulk renewal; an
    Event per publish is the flood the suppression exists to prevent."""
    from mojo.apps import jobs

    _clear(opts)
    _clear_degraded_notices()
    try:
        with _BrokenRoster("runner_roster_timeout"):
            jobs.publish(FUNC, {"n": 7}, channel=CH, broadcast=True)
            jobs.publish(FUNC, {"n": 8}, channel=CH, broadcast=True)

        assert _degraded_events().count() == 1, (
            f"expected exactly one incident for two degraded publishes on one "
            f"channel, got {_degraded_events().count()}")
    finally:
        _clear(opts)
        _clear_degraded_notices()


@th.django_unit_test("edge publish_pool shares the same roster policy")
def test_convergence_publish_pool_reports_degraded_roster(opts):
    """publish_pool hand-rolls the fan-out, so it inherited the same defect
    and must inherit the same fix."""
    from mojo.apps import jobs
    from mojo.apps.edge.services import convergence

    _clear_degraded_notices([convergence.EDGE_CHANNEL])
    try:
        with _BrokenRoster("runner_roster_invalid",
                           fallback=[_alive(RUNNER_A)]) as roster:
            result = convergence.publish_pool(
                "default", jobs_module=jobs, generation="g-2729")

        assert roster.fallback_calls == 1, (
            f"publish_pool did not use the shared roster policy, got "
            f"{roster.fallback_calls} fallback calls")
        assert result.status == "published", (
            f"a degraded-but-usable roster must still publish: {result}")
        assert result.targets == [RUNNER_A], (
            f"publish_pool should address the fallback roster, got {result.targets}")
        assert _degraded_events().count() == 1, (
            "publish_pool must report an unprovable roster like publish() does")
    finally:
        from mojo.apps.jobs.models import Job
        Job.objects.filter(channel__in=[RUNNER_A, convergence.EDGE_CHANNEL]).delete()
        _clear_degraded_notices([convergence.EDGE_CHANNEL])
