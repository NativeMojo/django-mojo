"""Per-channel concurrency caps (maestro #7407).

A burst of uploads used to let file renditions hold every worker on a box:
every engine consumed `renditions` from the same pool as everything else and
nothing bounded its share. The engine now applies `JOBS_CHANNEL_LIMITS` —
default `{"renditions": 1}` — by leaving a channel at its cap out of the
claim BRPOP until one of its jobs finishes. Jobs over the cap wait on their
queue in Redis while every other channel keeps flowing.

The live test drives the real main loop in a thread against this checkout's
Redis on channels of its own, like `test_reserved_capacity.py`; the module
has its own gates and counters because both modules run in one serial
process. Limits are injected through the `channel_limits=` constructor
argument — the package is strict-scanned, so the setting itself is never
patched, and the warning paths are proven by their return values only.
"""
import threading
import time

from testit import helpers as th


# Module-level so the engine can load the handlers by dotted path.
STARTED = []
FINISHED = []
RAN = []
GATES = {}
MAX_IN_FLIGHT = {"capped": 0}
_COUNT_LOCK = threading.Lock()


def gated_job(job):
    marker = job.payload.get("marker")
    with _COUNT_LOCK:
        STARTED.append(marker)
        in_flight = len(STARTED) - len(FINISHED)
        MAX_IN_FLIGHT["capped"] = max(MAX_IN_FLIGHT["capped"], in_flight)
    GATES[marker].wait(timeout=15)
    with _COUNT_LOCK:
        FINISHED.append(marker)
    return "released"


def quick_job(job):
    RAN.append(job.payload.get("marker"))
    return "ok"


GATED = f"{__name__}.gated_job"
QUICK = f"{__name__}.quick_job"
# Publishable without a declaration because of the suffix; ordinary to the
# engine under test (only `priority` and RUNNER_ID are reserved).
CAPPED = "t7407-capped-engine"
FREE = "t7407-free-engine"
RUNNER_ID = "t7407-limits-engine"


@th.django_unit_setup()
def setup_channel_limits(opts):
    from mojo.apps.jobs.adapters import get_adapter
    from mojo.apps.jobs.keys import JobKeys
    from mojo.apps.jobs.models import Job

    Job.objects.filter(func__in=[GATED, QUICK]).delete()
    keys = JobKeys()
    redis = get_adapter()
    for channel in (CAPPED, FREE, RUNNER_ID):
        redis.delete(keys.queue(channel))
        redis.delete(keys.processing(channel))
    GATES.clear()
    del STARTED[:]
    del FINISHED[:]
    del RAN[:]
    MAX_IN_FLIGHT["capped"] = 0


@th.django_unit_test("channel_limits_for: default, replace-not-merge, 0 uncaps, bad values fall back")
def test_channel_limits_for(opts):
    from mojo.apps.jobs import DEFAULT_CHANNEL_LIMITS
    from mojo.apps.jobs.job_engine import channel_limits_for

    consumed = ["default", "priority", "renditions", "webhooks"]
    th.assert_eq(DEFAULT_CHANNEL_LIMITS, {"renditions": 1},
                 f"the framework default caps renditions at one, got {DEFAULT_CHANNEL_LIMITS}")
    cases = [
        (None, {"renditions": 1}),                       # unset -> default
        ({}, {}),                                        # explicit empty lifts every cap
        ({"renditions": 0}, {}),                         # 0 = uncapped
        ({"webhooks": 3}, {"webhooks": 3}),              # replace, not merge
        ({"renditions": 2, "webhooks": "3"}, {"renditions": 2, "webhooks": 3}),
        ({"emails": 2}, {}),                             # not consumed -> dropped
        ({"renditions": "many"}, {"renditions": 1}),     # bad value -> default's value
        ({"renditions": -1}, {"renditions": 1}),         # negative -> default's value
        ({"webhooks": -1}, {}),                          # bad value, no default -> uncapped
        ("renditions=1", {"renditions": 1}),             # not a dict -> default
        ([("renditions", 2)], {"renditions": 1}),        # not a dict -> default
    ]
    for configured, expected in cases:
        got = channel_limits_for(consumed, configured)
        th.assert_eq(got, expected,
                     f"channel_limits_for(consumed, {configured!r}) must be "
                     f"{expected}, got {got}")
    th.assert_eq(channel_limits_for(["default"], None), {},
                 "an engine that does not consume renditions carries no cap for it")


@th.django_unit_test("a bare engine caps renditions at one; default and priority stay uncapped")
def test_bare_engine_default_cap(opts):
    from mojo.apps.jobs.job_engine import JobEngine

    engine = JobEngine(channels=["default", "priority", "renditions"],
                       runner_id=RUNNER_ID, max_workers=8)
    th.assert_eq(engine.channel_limits, {"renditions": 1},
                 f"with no explicit limits the engine must cap only renditions, "
                 f"got {engine.channel_limits}")

    def channels_at(active, by_channel=None):
        return [k.rsplit(":", 1)[-1] for k in engine.claimable_queues(active, by_channel)]

    everything = ["priority", RUNNER_ID, "default", "renditions"]
    th.assert_eq(channels_at(0, {}), everything,
                 f"an idle engine offers every channel in claim order, got {channels_at(0, {})}")
    th.assert_eq(channels_at(1, {"renditions": 1}), ["priority", RUNNER_ID, "default"],
                 f"with one rendition in flight its queue is withheld and the "
                 f"others keep their order, got {channels_at(1, {'renditions': 1})}")
    th.assert_eq(channels_at(3, {"default": 3}), everything,
                 f"an uncapped channel is never withheld, got {channels_at(3, {'default': 3})}")
    th.assert_eq(channels_at(3, {"priority": 2, "default": 1}), everything,
                 "priority is never capped by the default")
    th.assert_eq(channels_at(1), everything,
                 "the one-argument form counts nothing and so caps nothing "
                 "(the #4857 call shape)")
    th.assert_eq(engine.capped_channels(None), set(),
                 "no per-channel counts means no capped channel")


@th.django_unit_test("an explicit dict replaces the default: {} and {renditions: 0} uncap, {other: n} drops renditions")
def test_explicit_limits_replace_default(opts):
    from mojo.apps.jobs.job_engine import JobEngine

    channels = ["default", "renditions", CAPPED]
    for explicit in ({}, {"renditions": 0}):
        engine = JobEngine(channels=channels, runner_id=RUNNER_ID,
                           max_workers=8, channel_limits=explicit)
        th.assert_eq(engine.channel_limits, {},
                     f"channel_limits={explicit} must lift every cap, got {engine.channel_limits}")
        got = [k.rsplit(":", 1)[-1] for k in engine.claimable_queues(1, {"renditions": 1})]
        th.assert_true("renditions" in got,
                       f"an uncapped renditions channel stays claimable with one "
                       f"in flight, got {got}")

    engine = JobEngine(channels=channels, runner_id=RUNNER_ID,
                       max_workers=8, channel_limits={CAPPED: 2})
    th.assert_eq(engine.channel_limits, {CAPPED: 2},
                 f"an explicit dict replaces the default rather than merging "
                 f"with it, got {engine.channel_limits}")
    got = [k.rsplit(":", 1)[-1] for k in engine.claimable_queues(3, {"renditions": 3, CAPPED: 2})]
    th.assert_eq(got, [RUNNER_ID, "default", "renditions"],
                 f"the capped channel is withheld at its cap while the now-"
                 f"uncapped renditions is offered, got {got}")
    got = [k.rsplit(":", 1)[-1] for k in engine.claimable_queues(1, {CAPPED: 1})]
    th.assert_true(CAPPED in got,
                   f"a channel below its cap of 2 is still offered, got {got}")


@th.django_unit_test("caps compose with the #4857 reserve; every withheld channel means a short wait, not a spin")
def test_caps_compose_with_reserve(opts):
    from mojo.apps.jobs.job_engine import JobEngine, RESERVED_ONLY_POP_TIMEOUT

    engine = JobEngine(channels=[CAPPED, "renditions", "priority"],
                       runner_id=RUNNER_ID, max_workers=8)
    th.assert_eq(engine.reserved_workers, 2,
                 f"an 8-worker pool reserves two slots, got {engine.reserved_workers}")

    def channels_at(active, by_channel):
        return [k.rsplit(":", 1)[-1] for k in engine.claimable_queues(active, by_channel)]

    th.assert_eq(channels_at(6, {"renditions": 1, CAPPED: 5}), ["priority", RUNNER_ID],
                 "with the ordinary slots full only the reserved channels are "
                 "offered, exactly as before the caps existed")
    th.assert_eq(channels_at(8, {"renditions": 1, CAPPED: 7}), [],
                 "a full engine claims nothing")
    th.assert_eq(channels_at(1, {"renditions": 1}), ["priority", RUNNER_ID, CAPPED],
                 "below the reserved line a capped channel is the only thing withheld")

    th.assert_eq(engine.claim_pop_timeout(0, {}), 1,
                 "with nothing withheld the pop keeps its one-second wait")
    th.assert_eq(engine.claim_pop_timeout(1, {"renditions": 1}), RESERVED_ONLY_POP_TIMEOUT,
                 "while a capped channel is at its cap the pop is short so a "
                 "freed capped slot is refilled promptly")
    th.assert_eq(engine.claim_pop_timeout(1, {CAPPED: 1}), 1,
                 "an uncapped channel in flight does not shorten the pop")
    th.assert_eq(engine.claim_pop_timeout(6, {CAPPED: 6}), RESERVED_ONLY_POP_TIMEOUT,
                 "the reserved-only short pop is unchanged")


@th.django_unit_test("the effective caps ride in the heartbeat")
def test_heartbeat_carries_channel_limits(opts):
    import json
    from mojo.apps.jobs.job_engine import JobEngine

    engine = JobEngine(channels=["default", "renditions"], runner_id=RUNNER_ID,
                       max_workers=2, channel_limits={"renditions": 2})
    engine.initialize()
    try:
        raw = engine.redis.get(engine.keys.runner_hb(RUNNER_ID))
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8")
        row = json.loads(raw)
    finally:
        engine.stop()
    th.assert_eq(row.get("channel_limits"), {"renditions": 2},
                 f"the heartbeat must advertise this engine's effective caps, got {row}")
    th.assert_eq(row.get("channels"), engine.channels,
                 "the rest of the heartbeat is unchanged")


@th.django_unit_test("the CLI's --channel-limits parses a JSON object and refuses anything else")
def test_parse_channel_limits_arg(opts):
    from mojo.apps.jobs.cli import parse_channel_limits_arg

    th.assert_eq(parse_channel_limits_arg(None), None, "absent means the engine decides")
    th.assert_eq(parse_channel_limits_arg(""), None, "empty means the engine decides")
    th.assert_eq(parse_channel_limits_arg('{"renditions": 0}'), {"renditions": 0},
                 "a JSON object becomes the limits dict")
    for bad in ('[1, 2]', '"renditions"', '{renditions: 1}', 'renditions=1'):
        try:
            parse_channel_limits_arg(bad)
        except ValueError as e:
            th.assert_true("--channel-limits" in str(e),
                           f"the error must name the flag, got {e}")
        else:
            raise AssertionError(f"--channel-limits {bad!r} must be refused")


@th.django_unit_test("live engine: capped jobs start one at a time while another channel runs immediately")
def test_capped_channel_runs_one_at_a_time(opts):
    from mojo.apps import jobs
    from mojo.apps.jobs.job_engine import JobEngine
    from mojo.apps.jobs.models import Job

    # 4 workers -> 1 reserved -> 3 ordinary slots: room for all three capped
    # jobs if the cap did not hold them back.
    for index in range(3):
        GATES[f"capped-{index}"] = threading.Event()
        jobs.publish(GATED, {"marker": f"capped-{index}"}, channel=CAPPED)

    engine = JobEngine(channels=[CAPPED, FREE], runner_id=RUNNER_ID,
                       max_workers=4, channel_limits={CAPPED: 1})
    th.assert_eq(engine.channel_limits, {CAPPED: 1},
                 f"the test engine caps its capped channel at one, got {engine.channel_limits}")
    engine.initialize()
    loop = threading.Thread(target=engine._main_loop, name="t7407-loop", daemon=True)
    loop.start()
    try:
        deadline = time.time() + 10
        while not STARTED and time.time() < deadline:
            time.sleep(0.05)
        time.sleep(1.2)  # long enough for the loop to have claimed more if allowed
        th.assert_eq(len(STARTED), 1,
                     f"only one capped job may run at a time, got {STARTED}")

        jobs.publish(QUICK, {"marker": "free"}, channel=FREE)
        deadline = time.time() + 10
        while not RAN and time.time() < deadline:
            time.sleep(0.05)
        th.assert_eq(RAN, ["free"],
                     "a job on another channel runs immediately while the "
                     "capped channel's queue waits — that is the whole point")
        th.assert_eq(len(STARTED), 1,
                     f"claiming the free job must not admit a second capped job, got {STARTED}")

        freed = time.time()
        GATES[STARTED[0]].set()
        deadline = freed + 5
        while len(STARTED) < 2 and time.time() < deadline:
            time.sleep(0.01)
        th.assert_eq(len(STARTED), 2,
                     f"freeing the capped slot admits the next capped job, got {STARTED}")
        th.assert_true(time.time() - freed < 1.0,
                       f"a freed capped slot must be refilled promptly, took "
                       f"{time.time() - freed:.2f}s")
    finally:
        for gate in GATES.values():
            gate.set()
        deadline = time.time() + 15
        while len(FINISHED) < 3 and time.time() < deadline:
            time.sleep(0.05)
        time.sleep(0.3)
        engine.stop()
        loop.join(timeout=10)

    th.assert_eq(sorted(FINISHED), [f"capped-{i}" for i in range(3)],
                 f"once released every capped job must run, got {FINISHED}")
    th.assert_eq(MAX_IN_FLIGHT["capped"], 1,
                 f"at no point may more than one capped job be in flight, "
                 f"peak was {MAX_IN_FLIGHT['capped']}")
    th.assert_true(not loop.is_alive(), "the main loop must exit on stop()")
    Job.objects.filter(func__in=[GATED, QUICK]).delete()
