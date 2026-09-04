"""Channel-routing tests moved out of tests/test_jobs/test_channel_routing.py.

Every test here patches shared mojo.apps.jobs module attributes in-process —
`JOBS_ALLOWED_CHANNELS` (via `_enforced`/`_monitor`), `JOBS_HOSTNAME_CHANNEL`,
`scheduler.get_sched_channels`, or the CLI's Scheduler/DaemonRunner — which
every parallel test thread sees, so they run only in the opt-in serial tier
(maestro item #1839). None of the gates read an injectable parameter, so there
is no seam to convert through; the patches wrap direct in-process calls (not
opts.client traffic), so none were deletable no-ops.

See the source module's docstring for the 906/936 background. Channel names are
kept identical: this module is serial, so it never runs concurrently with
tests/test_jobs.
"""

import time
import uuid

from testit import helpers as th


# Module-level sinks/handlers: the real publish path stores a dotted path and
# re-imports it at execution time, so handlers must be resolvable by name.
CALLS = []


def record_call(job):
    CALLS.append(job.payload.get("marker"))
    return "ok"


HANDLER = f"{__name__}.record_call"

CH_SCHED = "t906_sched"
CH_UNDECLARED = "t936_undeclared"    # in NO list — publish must refuse it
CH_DECLARED = "t936_allowed"         # in JOBS_ALLOWED_CHANNELS only
CH_BOX_DIRECT = "t936-box-engine"    # implicitly allowed by the suffix
CH_EXPLICIT_RUNNER = "t2860-sites-deploy"  # live direct id, no suffix

TEST_CHANNELS = [CH_SCHED, CH_UNDECLARED, CH_DECLARED, CH_BOX_DIRECT,
                 CH_EXPLICIT_RUNNER]


def _clear(opts):
    """Reset our channels. Tests share a long-lived DB and Redis, so anything
    this module will create must be deleted before it is created."""
    from mojo.apps.jobs.models import Job
    from mojo.apps.incident.models import Event

    CALLS.clear()
    for ch in TEST_CHANNELS:
        opts.redis.delete(opts.keys.queue(ch))
        opts.redis.delete(opts.keys.sched(ch))
        opts.redis.delete(opts.keys.sched_broadcast(ch))
        opts.redis.delete(opts.keys.processing(ch))
    # Our channels must not linger in the shared sched registry.
    opts.redis.get_client().srem(opts.keys.sched_registry(), *TEST_CHANNELS)
    for ch in TEST_CHANNELS:
        opts.redis.delete(f"{opts.keys.prefix}:alerted:unconsumed:{ch}")
        opts.redis.delete(f"{opts.keys.prefix}:alerted:rejected:{ch}")
        opts.redis.delete(f"{opts.keys.prefix}:alerted:undeclared:{ch}")
    Job.objects.filter(channel__in=TEST_CHANNELS).delete()
    Event.objects.filter(category="jobs:unconsumed_channel",
                         title__in=[_alert_title(ch) for ch in TEST_CHANNELS]).delete()
    Event.objects.filter(category="jobs:rejected_channel",
                         title__in=[_rejected_title(ch) for ch in TEST_CHANNELS]).delete()
    Event.objects.filter(category="jobs:undeclared_channel",
                         title__in=[_undeclared_title(ch) for ch in TEST_CHANNELS]).delete()


def _alert_title(channel):
    """The title check_unconsumed_channels gives its incident."""
    return f"Unconsumed job channel: {channel}"


def _rejected_title(channel):
    """The title a refused publish gives its incident."""
    return f"Rejected job channel: {channel}"


def _undeclared_title(channel):
    """The title a monitor-mode undeclared publish gives its incident."""
    return f"Undeclared job channel: {channel}"


def _enforced(*declared):
    """Pin enforcement ON with exactly `declared` as JOBS_ALLOWED_CHANNELS.

    The gate reads the module global at call time, so patching it makes these
    tests independent of whatever the box's testproject settings declare —
    a stale settings file (no JOBS_ALLOWED_CHANNELS → monitor mode) and a
    freshly generated one must both pass this module.
    """
    from unittest import mock
    from mojo.apps import jobs
    return mock.patch.object(jobs, "JOBS_ALLOWED_CHANNELS", list(declared))


def _monitor():
    """Pin monitor mode (JOBS_ALLOWED_CHANNELS unset)."""
    from unittest import mock
    from mojo.apps import jobs
    return mock.patch.object(jobs, "JOBS_ALLOWED_CHANNELS", None)


@th.django_unit_setup()
def setup_channel_routing(opts):
    from mojo.apps.jobs.adapters import get_adapter
    from mojo.apps.jobs.keys import JobKeys

    opts.redis = get_adapter()
    opts.keys = JobKeys()
    _clear(opts)


def _queued_ids(opts, channel):
    """Job ids sitting on a channel's immediate queue."""
    return opts.redis.get_client().lrange(opts.keys.queue(channel), 0, -1)


def _drain(opts, engine, max_jobs=10):
    """Claim and execute everything on this engine's own channels."""
    queue_keys = [engine.keys.queue(ch) for ch in engine.channels]
    executed = []
    while len(executed) < max_jobs:
        target = next((key for key in queue_keys
                       if opts.redis.llen(key) > 0), None)
        if target is None:
            break
        popped = opts.redis.brpop([target], timeout=1)
        if not popped:
            break
        queue_key, job_id = popped
        engine.execute_job(queue_key.split(":")[-1], job_id)
        executed.append(job_id)
    return executed


@th.django_unit_test("two engines with disjoint channels each claim only their own jobs")
def test_disjoint_engines_claim_only_own_channels(opts):
    """One Redis and two engines must not permit cross-channel claims.

    This integration test owns real Job rows and Redis queues. It runs in the
    serial package because suite-wide Jobs maintenance intentionally scans and
    repairs every Redis jobs prefix.
    """
    _clear(opts)
    from mojo.apps.jobs.job_engine import JobEngine
    from mojo.apps.jobs.keys import JobKeys
    from mojo.apps.jobs.models import Job

    suffix = uuid.uuid4().hex[:10]
    channel_a = f"t906-a-{suffix}"
    channel_b = f"t906-b-{suffix}"
    owned_channels = [channel_a, channel_b]
    owned_keys = JobKeys(prefix=f"testit:t906:{suffix}")
    engines = []

    def queued(channel):
        return opts.redis.get_client().lrange(
            owned_keys.queue(channel), 0, -1)

    try:
        id_a = uuid.uuid4().hex
        id_b = uuid.uuid4().hex
        Job.objects.create(
            id=id_a, channel=channel_a, func=HANDLER,
            payload={"marker": "a"})
        Job.objects.create(
            id=id_b, channel=channel_b, func=HANDLER,
            payload={"marker": "b"})
        opts.redis.rpush(owned_keys.queue(channel_a), id_a)
        opts.redis.rpush(owned_keys.queue(channel_b), id_b)

        engine_a = JobEngine(
            channels=[channel_a], runner_id=f"t906-a-{suffix}")
        engine_a.keys = owned_keys
        engines.append(engine_a)
        drained = _drain(opts, engine_a)

        assert drained == [id_a], (
            f"the {channel_a!r} engine should claim only its own job, "
            f"drained {drained}"
        )
        assert CALLS == ["a"], (
            f"only the {channel_a!r} job should have executed, got {CALLS}"
        )
        assert id_b in queued(channel_b), (
            f"job {id_b} must still be waiting on {channel_b!r} for its own "
            f"engine, queue holds {queued(channel_b)}"
        )

        engine_b = JobEngine(
            channels=[channel_b], runner_id=f"t906-b-{suffix}")
        engine_b.keys = owned_keys
        engines.append(engine_b)
        drained_b = _drain(opts, engine_b)

        assert drained_b == [id_b], (
            f"the {channel_b!r} engine should then claim its own job, "
            f"drained {drained_b}"
        )
        assert CALLS == ["a", "b"], (
            f"both jobs should have run by now, got {CALLS}"
        )
    finally:
        for engine in engines:
            engine.executor.shutdown(wait=True)
        for channel in owned_channels:
            opts.redis.delete(owned_keys.queue(channel))
            opts.redis.delete(owned_keys.processing(channel))
            opts.redis.delete(owned_keys.sched(channel))
            opts.redis.delete(owned_keys.sched_broadcast(channel))
        Job.objects.filter(channel__in=owned_channels).delete()


@th.django_unit_test("the scheduler promotes a channel this box does not consume")
def test_scheduler_registry_covers_foreign_channel(opts):
    """Auto mode promotes delayed work for another box's channel.

    The test owns a real delayed queue and registry entry, so it shares the
    serial boundary with the other Jobs maintenance integration cases.
    """
    _clear(opts)
    from mojo.apps import jobs
    from mojo.apps.jobs.scheduler import Scheduler

    job_id = jobs.publish(func=HANDLER, payload={"marker": "sched"},
                          channel=CH_SCHED, delay=1)

    assert opts.redis.zcard(opts.keys.sched(CH_SCHED)) == 1, (
        f"a delayed job should be parked in the {CH_SCHED!r} sched ZSET"
    )
    assert CH_SCHED in jobs.get_sched_channels(), (
        f"publish should register {CH_SCHED!r} so the cluster scheduler finds it, "
        f"registry holds {jobs.get_sched_channels()}"
    )

    scheduler = Scheduler(channels=None, scheduler_id="t906-sched")
    assert scheduler.auto_channels is True, \
        "Scheduler(channels=None) must be in auto mode"
    scheduler._refresh_channels()
    assert CH_SCHED in scheduler.channels, (
        f"auto mode should pick up {CH_SCHED!r} from the registry, "
        f"serving {scheduler.channels}"
    )

    time.sleep(1.2)
    scheduler._process_scheduled_jobs()

    assert job_id in _queued_ids(opts, CH_SCHED), (
        f"the due job should have been promoted onto the {CH_SCHED!r} queue, "
        f"queue holds {_queued_ids(opts, CH_SCHED)}"
    )


@th.django_unit_test("enforced: an undeclared channel is refused — ValueError, no job, one incident")
def test_publish_undeclared_channel_refused(opts):
    """The 936 contract with enforcement on (JOBS_ALLOWED_CHANNELS set):
    route-as-named holds for DECLARED channels; a typo'd or undeclared one
    fails at the call site instead of stranding a job on a queue nothing
    consumes."""
    _clear(opts)
    from mojo.apps import jobs
    from mojo.apps.jobs.models import Job
    from mojo.apps.incident.models import Event

    with _enforced(CH_DECLARED):
        for _ in range(2):  # second attempt exercises the suppression window
            try:
                jobs.publish(func=HANDLER, payload={}, channel=CH_UNDECLARED)
            except ValueError:
                pass
            else:
                raise AssertionError(
                    f"publish(channel={CH_UNDECLARED!r}) must raise ValueError"
                )

    assert not Job.objects.filter(channel=CH_UNDECLARED).exists(), (
        "a refused publish must not create a Job row"
    )
    assert opts.redis.llen(opts.keys.queue(CH_UNDECLARED)) == 0, (
        "a refused publish must not touch the channel's queue"
    )
    events = Event.objects.filter(category="jobs:rejected_channel",
                                  title=_rejected_title(CH_UNDECLARED)).count()
    assert events == 1, (
        f"two refusals inside the suppression window must file exactly one "
        f"jobs:rejected_channel incident, got {events}"
    )


@th.django_unit_test("monitor mode: an undeclared channel still routes, with an incident")
def test_publish_undeclared_channel_monitor_mode(opts):
    """JOBS_ALLOWED_CHANNELS unset = no flag day: an upgrading deployment's
    undeclared publishes keep working exactly as before and file a
    jobs:undeclared_channel incident naming what to declare."""
    _clear(opts)
    from mojo.apps import jobs
    from mojo.apps.jobs.models import Job
    from mojo.apps.incident.models import Event

    with _monitor():
        first = jobs.publish(func=HANDLER, payload={"marker": "m1"},
                             channel=CH_UNDECLARED)
        second = jobs.publish(func=HANDLER, payload={"marker": "m2"},
                              channel=CH_UNDECLARED)

    queued = _queued_ids(opts, CH_UNDECLARED)
    assert first in queued and second in queued, (
        f"monitor mode must route undeclared publishes as named, queue holds {queued}"
    )
    assert Job.objects.filter(channel=CH_UNDECLARED).count() == 2, (
        "monitor mode must create the Job rows normally"
    )
    events = Event.objects.filter(category="jobs:undeclared_channel",
                                  title=_undeclared_title(CH_UNDECLARED)).count()
    assert events == 1, (
        f"two undeclared publishes inside the suppression window must file "
        f"exactly one jobs:undeclared_channel incident, got {events}"
    )


@th.django_unit_test("a channel declared in JOBS_ALLOWED_CHANNELS routes as named")
def test_publish_declared_channel_routes(opts):
    _clear(opts)
    from mojo.apps import jobs

    with _enforced(CH_DECLARED):
        job_id = jobs.publish(func=HANDLER, payload={"marker": "declared"},
                              channel=CH_DECLARED)
    assert job_id in _queued_ids(opts, CH_DECLARED), (
        f"a JOBS_ALLOWED_CHANNELS channel must queue as named, "
        f"queue holds {_queued_ids(opts, CH_DECLARED)}"
    )


@th.django_unit_test("a '-engine' channel is implicitly allowed even when enforced")
def test_publish_engine_suffix_implicitly_allowed(opts):
    """Hostnames vary per deployment and cannot live in a hand-written list,
    so the runner-id suffix IS the allow rule — even with an empty declared
    list. A typo'd host channel passes the gate and is caught by the
    unconsumed-channel incident instead."""
    _clear(opts)
    from unittest import mock
    from mojo.apps import jobs

    with _enforced(), mock.patch.object(
            jobs, "_is_live_runner_channel",
            side_effect=AssertionError("static channel performed live lookup")):
        job_id = jobs.publish(
            func=HANDLER, payload={"marker": "direct"},
            channel=CH_BOX_DIRECT)
    assert job_id in _queued_ids(opts, CH_BOX_DIRECT), (
        f"a '-engine' channel must be publishable with zero configuration, "
        f"queue holds {_queued_ids(opts, CH_BOX_DIRECT)}"
    )


@th.django_unit_test("a live explicit runner id is an enforced direct publish target")
def test_publish_live_explicit_runner_id(opts):
    """An accepted --runner-id and its advertised direct channel must form one
    usable contract even when the id does not end in ``-engine``."""
    _clear(opts)
    from mojo.apps import jobs
    from mojo.apps.jobs import job_engine as je

    engine = je.JobEngine(channels=[CH_DECLARED], runner_id=CH_EXPLICIT_RUNNER)
    engine.start_time = je.dates.utcnow()
    try:
        engine._publish_heartbeat()
        assert not jobs.is_channel_allowed(CH_EXPLICIT_RUNNER), (
            "a live heartbeat must not weaken static ScheduledTask channel "
            "validation"
        )
        with _enforced():
            job_id = jobs.publish(
                func=HANDLER,
                payload={"marker": "explicit-runner"},
                channel=CH_EXPLICIT_RUNNER,
            )
            try:
                jobs.publish(
                    func=HANDLER,
                    payload={"marker": "delayed-explicit-runner"},
                    channel=CH_EXPLICIT_RUNNER,
                    delay=1,
                )
            except ValueError:
                pass
            else:
                raise AssertionError(
                    "a live heartbeat must not authorize delayed direct work"
                )
        assert job_id in _queued_ids(opts, CH_EXPLICIT_RUNNER), (
            f"a live explicit runner must accept direct work on its exact id, "
            f"queue holds {_queued_ids(opts, CH_EXPLICIT_RUNNER)}"
        )
    finally:
        engine.executor.shutdown(wait=True)
        opts.redis.delete(opts.keys.runner_hb(CH_EXPLICIT_RUNNER))
        for channel in engine.channels:
            opts.redis.get_client().zrem(
                opts.keys.runner_registry(channel), CH_EXPLICIT_RUNNER)


@th.django_unit_test("enforced: a ScheduledTask cannot be saved with an undeclared channel")
def test_scheduled_task_undeclared_channel_rejected(opts):
    """Owner-editable field, published hourly — reject at write time, not at
    dispatch. Monitor mode allows the save (dispatch then routes and
    reports), so pin enforcement on."""
    _clear(opts)
    from mojo.apps.jobs.models import ScheduledTask

    task = ScheduledTask(name="t936 undeclared channel", task_type="report",
                         run_times=["09:00"], run_days=[0],
                         channel=CH_UNDECLARED)
    with _enforced(CH_DECLARED):
        try:
            task.save()
        except ValueError:
            pass
        else:
            ScheduledTask.objects.filter(name="t936 undeclared channel").delete()
            raise AssertionError(
                "saving a ScheduledTask with an undeclared channel must raise ValueError"
            )


@th.django_unit_test("an engine also consumes a channel named after its runner id")
def test_runner_id_channel(opts):
    """The box-direct channel IS the runner id (default '<hostname>-engine'),
    so the name a publisher targets is the same id already visible in
    heartbeats, pidfiles and logs. Default ids use the statically allowed
    '-engine' suffix; other explicit ids use their exact live heartbeat."""
    _clear(opts)
    from unittest import mock
    from mojo.apps.jobs import job_engine as je

    engine = je.JobEngine(channels=["default"], runner_id="t906-host-on")
    assert "t906-host-on" in engine.channels, (
        f"the engine should consume its own runner-id channel so a publisher "
        f"can target it, engine has {engine.channels}"
    )

    default_engine = je.JobEngine(channels=["default"])
    expected = f"{je.host_channel()}{je.ENGINE_CHANNEL_SUFFIX}"
    assert default_engine.runner_id == expected, (
        f"the default runner id should be the host channel plus "
        f"{je.ENGINE_CHANNEL_SUFFIX!r}, got {default_engine.runner_id!r}"
    )
    assert expected in default_engine.channels, (
        f"the default box-direct channel {expected!r} should be consumed, "
        f"engine has {default_engine.channels}"
    )

    with mock.patch.object(je, "JOBS_HOSTNAME_CHANNEL", False):
        opted_out = je.JobEngine(channels=["default"], runner_id="t906-host-off")
    assert opted_out.channels == ["default"], (
        f"JOBS_HOSTNAME_CHANNEL=False must leave the channel list alone, "
        f"got {opted_out.channels}"
    )


@th.django_unit_test("a failed registry read leaves the scheduler's channels alone")
def test_scheduler_keeps_channels_when_registry_unreadable(opts):
    """A transient Redis error must not shrink the scheduler back to this box's
    own channels — that would stall every other box's delayed jobs until the
    next successful refresh."""
    _clear(opts)
    from unittest import mock
    from mojo.apps import jobs
    from mojo.apps.jobs.scheduler import Scheduler

    jobs.register_sched_channel(CH_SCHED)
    scheduler = Scheduler(channels=None, scheduler_id="t906-resilient")
    scheduler._refresh_channels()
    assert CH_SCHED in scheduler.channels, "precondition: the channel should be picked up"

    serving = list(scheduler.channels)
    scheduler._last_channel_refresh = 0  # allow an immediate re-refresh
    with mock.patch("mojo.apps.jobs.scheduler.get_sched_channels",
                    side_effect=RuntimeError("redis down")):
        scheduler._refresh_channels()

    assert scheduler.channels == serving, (
        f"a failed registry read must leave the channel list untouched, "
        f"it became {scheduler.channels}"
    )


@th.django_unit_test("the scheduler CLI leaves auto mode on when --channels is absent")
def test_cli_scheduler_defaults_to_auto_mode(opts):
    """Without this the whole scheduler half of the fix is dead code: passing
    the settings list would make every scheduler look explicitly pinned."""
    from unittest import mock
    from mojo.apps.jobs import cli

    for start in ("start_scheduler_daemon", "start_scheduler_foreground"):
        with mock.patch("mojo.apps.jobs.scheduler.Scheduler") as fake, \
             mock.patch("mojo.apps.jobs.daemon.DaemonRunner"), \
             mock.patch.object(cli, "setup_signal_handlers"), \
             mock.patch.object(cli, "is_scheduler_running", return_value=False):
            getattr(cli, start)(verbose=False)

        assert fake.call_args is not None, f"{start} should construct a Scheduler"
        passed = fake.call_args.kwargs.get("channels", "MISSING")
        assert passed is None, (
            f"{start} must pass channels=None so auto mode engages, passed {passed!r}"
        )
