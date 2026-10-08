"""Reserved worker slots (maestro #4857).

On 2026-09-18 every worker on every API node was busy with file renditions
when a release was pushed: the deploy orchestrator waited nine minutes for a
worker and the canary's node job never got one at all. The engine now keeps
`JOBS_ENGINE_RESERVED_WORKERS` slots that ordinary channels may not fill;
only the `priority` channel and the engine's own box-direct channel — the
two channels the deploy plane publishes on — may claim into them.

The live test drives the real main loop in a thread against this checkout's
Redis, on the same `default` channel `test_core_engine` uses and clears.
"""
import threading
import time

from testit import helpers as th


# Module-level so the engine can load the handler by dotted path.
GATE = threading.Event()
STARTED = []
RAN = []


def blocking_job(job):
    STARTED.append(job.payload.get("marker"))
    GATE.wait(timeout=15)
    return "released"


def quick_job(job):
    RAN.append(job.payload.get("marker"))
    return "ok"


GATES = {}


def gated_job(job):
    marker = job.payload.get("marker")
    STARTED.append(marker)
    GATES[marker].wait(timeout=15)
    return "released"


BLOCKING = f"{__name__}.blocking_job"
QUICK = f"{__name__}.quick_job"
GATED = f"{__name__}.gated_job"
ORDINARY = "default"
RUNNER_ID = "t4857-reserved-engine"


@th.django_unit_setup()
def setup_reserved(opts):
    from mojo.apps.jobs.adapters import get_adapter
    from mojo.apps.jobs.keys import JobKeys
    from mojo.apps.jobs.models import Job

    Job.objects.filter(func__in=[BLOCKING, QUICK, GATED]).delete()
    GATES.clear()
    keys = JobKeys()
    redis = get_adapter()
    for channel in (ORDINARY, RUNNER_ID):
        redis.delete(keys.queue(channel))
        redis.delete(keys.processing(channel))
    GATE.clear()
    del STARTED[:]
    del RAN[:]


@th.django_unit_test("reserved slots default to a quarter of the pool, capped at two; explicit values are clamped")
def test_reserved_worker_count(opts):
    from mojo.apps.jobs.job_engine import reserved_worker_count

    cases = [
        ((10, None), 2), ((8, None), 2), ((7, None), 1), ((4, None), 1),
        ((3, None), 0), ((2, None), 0), ((1, None), 0),
        ((10, 3), 3), ((10, 0), 0), ((2, 5), 1), ((1, 5), 0), ((10, -1), 0),
    ]
    for (max_workers, configured), expected in cases:
        got = reserved_worker_count(max_workers, configured)
        th.assert_eq(got, expected,
                     f"reserved_worker_count({max_workers}, {configured}) "
                     f"must be {expected}, got {got}")


@th.django_unit_test("claimable_queues offers ordinary channels only below the reserved line")
def test_claimable_queues(opts):
    from mojo.apps.jobs.job_engine import JobEngine

    engine = JobEngine(channels=[ORDINARY, "renditions", "priority"],
                       runner_id=RUNNER_ID, max_workers=8)
    th.assert_eq(engine.reserved_workers, 2,
                 f"an 8-worker pool must reserve two slots by default, got {engine.reserved_workers}")
    th.assert_eq(engine.reserved_channels, {"priority", RUNNER_ID},
                 f"reserved channels are priority + the box-direct channel, got {engine.reserved_channels}")

    def channels_at(active):
        return [key.rsplit(":", 1)[-1] for key in engine.claimable_queues(active)]

    th.assert_eq(channels_at(0), ["priority", RUNNER_ID, ORDINARY, "renditions"],
                 f"an idle engine claims from every channel, priority then "
                 f"box-direct first, got {channels_at(0)}")
    th.assert_eq(channels_at(5), ["priority", RUNNER_ID, ORDINARY, "renditions"],
                 f"below the reserved line every channel is offered, got {channels_at(5)}")
    th.assert_eq(channels_at(6), ["priority", RUNNER_ID],
                 f"with the ordinary slots full only reserved channels are offered, got {channels_at(6)}")
    th.assert_eq(channels_at(7), ["priority", RUNNER_ID],
                 f"the last reserved slot is still only for reserved channels, got {channels_at(7)}")
    th.assert_eq(channels_at(8), [],
                 f"a full engine claims nothing, got {channels_at(8)}")

    small = JobEngine(channels=[ORDINARY], runner_id=RUNNER_ID, max_workers=2)
    th.assert_eq(small.reserved_workers, 0,
                 f"a two-worker pool reserves nothing by default, got {small.reserved_workers}")
    th.assert_eq([k.rsplit(":", 1)[-1] for k in small.claimable_queues(1)],
                 [RUNNER_ID, ORDINARY],
                 "with no reservation the last slot is open to ordinary work")


@th.django_unit_test("live engine: a box-direct job starts while ordinary jobs saturate the pool")
def test_direct_job_runs_through_saturation(opts):
    from mojo.apps import jobs
    from mojo.apps.jobs.job_engine import JobEngine
    from mojo.apps.jobs.models import Job

    # 4 workers -> 1 reserved -> 3 ordinary slots. Five blocking jobs: three
    # run, two queue. The box-direct job must still get the reserved slot.
    for index in range(5):
        jobs.publish(BLOCKING, {"marker": f"ordinary-{index}"}, channel=ORDINARY)

    engine = JobEngine(channels=[ORDINARY], runner_id=RUNNER_ID, max_workers=4)
    th.assert_eq(engine.reserved_workers, 1,
                 f"a four-worker pool reserves one slot, got {engine.reserved_workers}")
    engine.initialize()
    loop = threading.Thread(target=engine._main_loop, name="t4857-loop", daemon=True)
    loop.start()
    try:
        deadline = time.time() + 10
        while len(STARTED) < 3 and time.time() < deadline:
            time.sleep(0.05)
        th.assert_eq(len(STARTED), 3,
                     f"three ordinary jobs fill the ordinary slots, got {STARTED}")
        time.sleep(1.5)  # long enough for BRPOP to have claimed a fourth if allowed
        th.assert_eq(len(STARTED), 3,
                     f"ordinary work must not claim into the reserved slot, got {STARTED}")

        jobs.publish(QUICK, {"marker": "direct"}, channel=RUNNER_ID)
        deadline = time.time() + 10
        while not RAN and time.time() < deadline:
            time.sleep(0.05)
        th.assert_eq(RAN, ["direct"],
                     "the box-direct job must run while the ordinary queue is "
                     "still saturated — that reserved slot is what lets a deploy start")
        th.assert_eq(len(STARTED), 3,
                     f"the reserved slot must go back to waiting for reserved work, got {STARTED}")
    finally:
        GATE.set()
        deadline = time.time() + 15
        while len(STARTED) < 5 and time.time() < deadline:
            time.sleep(0.05)
        engine.stop()
        loop.join(timeout=10)

    th.assert_eq(sorted(STARTED), sorted(f"ordinary-{i}" for i in range(5)),
                 f"once released, the queued ordinary jobs must all run, got {STARTED}")
    th.assert_true(not loop.is_alive(), "the main loop must exit on stop()")
    Job.objects.filter(func__in=[BLOCKING, QUICK]).delete()


@th.django_unit_test("box-direct is claimed before ordinary channels even without a priority channel")
def test_direct_claimed_before_ordinary(opts):
    from mojo.apps.jobs.job_engine import JobEngine

    engine = JobEngine(channels=["renditions", ORDINARY], runner_id=RUNNER_ID, max_workers=3)
    got = [key.rsplit(":", 1)[-1] for key in engine.claimable_queues(0)]
    th.assert_eq(got, [RUNNER_ID, "renditions", ORDINARY],
                 f"a node update must not sit behind every queued ordinary job, got {got}")


@th.django_unit_test("an engine that consumes no reserved channel reserves nothing")
def test_no_reserved_channel_reserves_nothing(opts):
    from mojo.apps.jobs.job_engine import engine_reserve

    cases = [
        # channels, expected reserved channels, expected reserved workers (10-worker pool)
        (["renditions"], set(), 0),
        (["renditions", "priority"], {"priority"}, 2),
        (["renditions", RUNNER_ID], {RUNNER_ID}, 2),
        (["renditions", "priority", RUNNER_ID], {"priority", RUNNER_ID}, 2),
    ]
    for channels, want_channels, want_workers in cases:
        got_channels, got_workers = engine_reserve(channels, RUNNER_ID, 10)
        th.assert_eq(got_channels, want_channels,
                     f"reserved channels for {channels} must be {want_channels}, got {got_channels}")
        th.assert_eq(got_workers, want_workers,
                     f"an engine on {channels} must reserve {want_workers} of 10, got {got_workers}")
    th.assert_eq(engine_reserve(["renditions"], RUNNER_ID, 10, 5)[1], 0,
                 "an explicit reserve is dead capacity too when nothing reserved is consumed")


@th.django_unit_test("the claim pop waits a tenth of a second, not a second, while only reserved channels are offered")
def test_claim_pop_timeout(opts):
    from mojo.apps.jobs.job_engine import JobEngine

    engine = JobEngine(channels=[ORDINARY, "priority"], runner_id=RUNNER_ID, max_workers=8)
    for active, expected in ((0, 1), (5, 1), (6, 0.1), (7, 0.1)):
        got = engine.claim_pop_timeout(active)
        th.assert_eq(got, expected,
                     f"at {active} of 8 in flight the pop may block {expected}s, got {got}")
    small = JobEngine(channels=[ORDINARY], runner_id=RUNNER_ID, max_workers=2)
    th.assert_eq(small.claim_pop_timeout(1), 1,
                 "with nothing reserved the pop keeps its one-second wait")


@th.django_unit_test("live engine: a freed ordinary slot is refilled within a fraction of a second while the reserve is in force")
def test_freed_ordinary_slot_is_refilled_quickly(opts):
    from mojo.apps import jobs
    from mojo.apps.jobs.job_engine import JobEngine
    from mojo.apps.jobs.models import Job

    # 4 workers -> 1 reserved -> 3 ordinary slots, all held. Each round queues
    # one more ordinary job, lets the loop settle into its reserved-only pop,
    # frees one slot and times how long the queued job waits for it.
    del STARTED[:]
    GATES.clear()
    for index in range(6):
        GATES[f"gated-{index}"] = threading.Event()
    for index in range(3):
        jobs.publish(GATED, {"marker": f"gated-{index}"}, channel=ORDINARY)

    engine = JobEngine(channels=[ORDINARY], runner_id=RUNNER_ID, max_workers=4)
    engine.initialize()
    loop = threading.Thread(target=engine._main_loop, name="t4857-refill", daemon=True)
    loop.start()
    waits = []
    try:
        deadline = time.time() + 10
        while len(STARTED) < 3 and time.time() < deadline:
            time.sleep(0.05)
        th.assert_eq(len(STARTED), 3, f"three jobs fill the ordinary slots, got {STARTED}")
        for round_index in range(3):
            waiting = f"gated-{3 + round_index}"
            jobs.publish(GATED, {"marker": waiting}, channel=ORDINARY)
            time.sleep(0.3)
            th.assert_true(waiting not in STARTED,
                           f"{waiting} must wait: the only free slot is reserved, got {STARTED}")
            freed = time.time()
            GATES[f"gated-{round_index}"].set()
            deadline = freed + 5
            while waiting not in STARTED and time.time() < deadline:
                time.sleep(0.01)
            th.assert_true(waiting in STARTED, f"{waiting} never started, got {STARTED}")
            waits.append(time.time() - freed)
    finally:
        for gate in GATES.values():
            gate.set()
        time.sleep(0.3)
        engine.stop()
        loop.join(timeout=10)

    # Before the short pop each wait was up to a full second (the BRPOP on the
    # reserved channels had to time out first); now it is the 0.1s pop plus
    # the job's own completion bookkeeping.
    th.assert_true(max(waits) < 0.5,
                   f"a freed ordinary slot must be refilled within a fraction "
                   f"of a second, waits were {[round(w, 2) for w in waits]}")
    Job.objects.filter(func__in=[GATED]).delete()
