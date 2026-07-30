"""Channel routing: publish puts a job on the channel it was given.

The bug this module pins down (django-mojo <= 1.2.61): `publish()` validated the
requested channel against the *publisher's own* `JOBS_CHANNELS` and silently
rerouted anything unlisted to "default" — returning success. That made
cross-box routing impossible: a box could not publish to a channel it does not
itself consume, because the one setting drove both.

Channels here are prefixed `t906_` so they cannot collide with a real channel
or with another module's queues.
"""

from testit import helpers as th


# Module-level sinks/handlers: the real publish path stores a dotted path and
# re-imports it at execution time, so handlers must be resolvable by name.
CALLS = []


def record_call(job):
    CALLS.append(job.payload.get("marker"))
    return "ok"


HANDLER = f"{__name__}.record_call"

CH_FIREWALL = "t906_firewall"
CH_A = "t906_a"
CH_B = "t906_b"
CH_SCHED = "t906_sched"
CH_SEED = "t906_seed"
CH_ORPHAN = "t906_orphan"

TEST_CHANNELS = [CH_FIREWALL, CH_A, CH_B, CH_SCHED, CH_SEED, CH_ORPHAN]


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
    Job.objects.filter(channel__in=TEST_CHANNELS).delete()
    Event.objects.filter(category="jobs:unconsumed_channel",
                         title__in=[_alert_title(ch) for ch in TEST_CHANNELS]).delete()


def _alert_title(channel):
    """The title check_unconsumed_channels gives its incident."""
    return f"Unconsumed job channel: {channel}"


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


@th.django_unit_test("publish routes to the channel it was given, configured or not")
def test_publish_routes_as_named(opts):
    """THE regression. Fails on <=1.2.61: the job lands on 'default' instead."""
    _clear(opts)
    from mojo.apps import jobs
    from mojo.apps.jobs.models import Job

    before_default = opts.redis.llen(opts.keys.queue("default"))

    job_id = jobs.publish(func=HANDLER, payload={"marker": "fw"}, channel=CH_FIREWALL)

    job = Job.objects.get(id=job_id)
    assert job.channel == CH_FIREWALL, (
        f"publish must record the channel it was given, not reroute it; "
        f"Job.channel is {job.channel!r}"
    )
    assert job_id in _queued_ids(opts, CH_FIREWALL), (
        f"job {job_id} should be queued on {CH_FIREWALL!r}, "
        f"queue holds {_queued_ids(opts, CH_FIREWALL)}"
    )
    assert opts.redis.llen(opts.keys.queue("default")) == before_default, (
        "publishing to an unconfigured channel must not touch the default queue"
    )


@th.django_unit_test("publish rejects an empty channel instead of minting a bad queue key")
def test_publish_empty_channel_raises(opts):
    _clear(opts)
    from mojo.apps import jobs
    from mojo.apps.jobs.models import Job

    for bad in ("", None):
        try:
            jobs.publish(func=HANDLER, payload={}, channel=bad)
        except ValueError:
            pass
        else:
            raise AssertionError(
                f"publish(channel={bad!r}) must raise ValueError, it returned normally"
            )

    assert not Job.objects.filter(func=HANDLER, channel__in=("", None)).exists(), (
        "a rejected publish must not leave a Job row behind"
    )


@th.django_unit_test("two engines with disjoint channels each claim only their own jobs")
def test_disjoint_engines_claim_only_own_channels(opts):
    """The acceptance shape: one Redis, two engines, no cross-claiming.

    Drains via BRPOP + execute_job in THIS process — the same thing
    `th.run_jobs()` does. The engine's isolation IS its BRPOP key list, so
    deriving the keys from `engine.channels` (never hardcoded) is what makes
    this a faithful stand-in for the daemon's main loop.
    """
    _clear(opts)
    from mojo.apps import jobs
    from mojo.apps.jobs.job_engine import JobEngine

    id_a = jobs.publish(func=HANDLER, payload={"marker": "a"}, channel=CH_A)
    id_b = jobs.publish(func=HANDLER, payload={"marker": "b"}, channel=CH_B)

    engine_a = JobEngine(channels=[CH_A], runner_id="t906-engine-a")
    drained = _drain(opts, engine_a)

    assert drained == [id_a], (
        f"the {CH_A!r} engine should claim only its own job, drained {drained}"
    )
    assert CALLS == ["a"], f"only the {CH_A!r} job should have executed, got {CALLS}"
    assert id_b in _queued_ids(opts, CH_B), (
        f"job {id_b} must still be waiting on {CH_B!r} for its own engine, "
        f"queue holds {_queued_ids(opts, CH_B)}"
    )

    engine_b = JobEngine(channels=[CH_B], runner_id="t906-engine-b")
    drained_b = _drain(opts, engine_b)

    assert drained_b == [id_b], (
        f"the {CH_B!r} engine should then claim its own job, drained {drained_b}"
    )
    assert CALLS == ["a", "b"], f"both jobs should have run by now, got {CALLS}"


@th.django_unit_test("an engine also consumes a channel named after its host")
def test_hostname_channel(opts):
    _clear(opts)
    from unittest import mock
    from mojo.apps.jobs import job_engine as je

    engine = je.JobEngine(channels=["default"], runner_id="t906-host-on")
    assert je.host_channel() in engine.channels, (
        f"the host channel {je.host_channel()!r} should be consumed so a publisher "
        f"can target this box, engine has {engine.channels}"
    )

    with mock.patch.object(je, "JOBS_HOSTNAME_CHANNEL", False):
        opted_out = je.JobEngine(channels=["default"], runner_id="t906-host-off")
    assert opted_out.channels == ["default"], (
        f"JOBS_HOSTNAME_CHANNEL=False must leave the channel list alone, "
        f"got {opted_out.channels}"
    )


@th.django_unit_test("an explicit channel list is not mutated by the host channel")
def test_channels_argument_not_mutated(opts):
    """The host channel is appended to a COPY — a shared list must survive."""
    _clear(opts)
    from mojo.apps.jobs.job_engine import JobEngine

    caller_list = ["default"]
    JobEngine(channels=caller_list, runner_id="t906-nomutate")
    assert caller_list == ["default"], (
        f"the caller's channel list must not be mutated, it became {caller_list}"
    )


@th.django_unit_test("the scheduler promotes a channel this box does not consume")
def test_scheduler_registry_covers_foreign_channel(opts):
    """Auto mode: a delayed job on a foreign channel still gets promoted.

    This is the second half of the bug — the cluster runs ONE scheduler, so if
    it only served its own box's JOBS_CHANNELS, every other box's delayed jobs
    and retries would stall forever.
    """
    _clear(opts)
    import time
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

    time.sleep(1.2)  # let the job come due
    scheduler._process_scheduled_jobs()

    assert job_id in _queued_ids(opts, CH_SCHED), (
        f"the due job should have been promoted onto the {CH_SCHED!r} queue, "
        f"queue holds {_queued_ids(opts, CH_SCHED)}"
    )


@th.django_unit_test("an explicitly pinned scheduler ignores the registry")
def test_scheduler_explicit_channels_are_pinned(opts):
    _clear(opts)
    from mojo.apps import jobs
    from mojo.apps.jobs.scheduler import Scheduler

    jobs.register_sched_channel(CH_SCHED)

    scheduler = Scheduler(channels=["default"], scheduler_id="t906-pinned")
    scheduler._refresh_channels()

    assert scheduler.auto_channels is False, \
        "an explicit channel list must disable auto mode"
    assert scheduler.channels == ["default"], (
        f"a pinned scheduler must serve exactly what it was given, "
        f"got {scheduler.channels}"
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


@th.django_unit_test("the scheduler seeds its registry from pre-existing sched keys")
def test_scheduler_seed_scan(opts):
    """Jobs delayed before this version shipped are not in the registry."""
    _clear(opts)
    from mojo.apps import jobs
    from mojo.apps.jobs.scheduler import Scheduler

    # Simulate pre-upgrade state: a sched ZSET with no registry entry.
    opts.redis.zadd(opts.keys.sched(CH_SEED), {"t906-pre-upgrade-job": 1.0})
    opts.redis.get_client().srem(opts.keys.sched_registry(), CH_SEED)
    assert CH_SEED not in jobs.get_sched_channels(), \
        "precondition: the seed channel must start out unregistered"

    scheduler = Scheduler(channels=None, scheduler_id="t906-seed")
    scheduler._seed_registry()

    assert CH_SEED in jobs.get_sched_channels(), (
        f"the startup scan should have registered {CH_SEED!r}, "
        f"registry holds {jobs.get_sched_channels()}"
    )
    assert opts.keys.sched_registry().split(":")[-1] not in jobs.get_sched_channels(), \
        "the registry key itself must never be mistaken for a channel"


@th.django_unit_test("a queue with no live consumer raises an incident")
def test_unconsumed_channel_alert(opts):
    _clear(opts)
    from mojo.apps import jobs
    from mojo.apps.jobs import cronjobs
    from mojo.apps.incident.models import Event

    jobs.publish(func=HANDLER, payload={"marker": "orphan"}, channel=CH_ORPHAN)

    orphans = dict(cronjobs.find_unconsumed_channels())
    assert CH_ORPHAN in orphans, (
        f"{CH_ORPHAN!r} has a queued job and no consumer, so it should be "
        f"reported; found {sorted(orphans)}"
    )

    cronjobs.check_unconsumed_channels()
    assert Event.objects.filter(category="jobs:unconsumed_channel",
                                title=_alert_title(CH_ORPHAN)).exists(), (
        "an unconsumed channel must raise a jobs:unconsumed_channel incident — "
        "it is the only thing that surfaces a misrouted publish now"
    )


@th.django_unit_test("a channel a live runner consumes is not reported")
def test_consumed_channel_is_not_alerted(opts):
    _clear(opts)
    import json
    from mojo.apps import jobs
    from mojo.apps.jobs import cronjobs

    jobs.publish(func=HANDLER, payload={"marker": "served"}, channel=CH_ORPHAN)

    # Stand in for a live engine: a heartbeat naming the channel.
    hb_key = opts.keys.runner_hb("t906-fake-runner")
    opts.redis.set(hb_key, json.dumps({
        "runner_id": "t906-fake-runner",
        "channels": [CH_ORPHAN],
    }), ex=60)
    try:
        orphans = dict(cronjobs.find_unconsumed_channels())
        assert CH_ORPHAN not in orphans, (
            f"{CH_ORPHAN!r} has a live consumer, so a backlog on it is a capacity "
            f"question, not a routing error; found {sorted(orphans)}"
        )
    finally:
        opts.redis.delete(hb_key)


@th.django_unit_test("--channels parses to a list, absent means the component decides")
def test_parse_channels_arg(opts):
    from mojo.apps.jobs.cli import parse_channels_arg

    assert parse_channels_arg("a, b") == ["a", "b"], \
        f"a comma list should split and strip, got {parse_channels_arg('a, b')}"
    assert parse_channels_arg("solo") == ["solo"], \
        "a single channel should still come back as a list"
    for empty in (None, "", " , , "):
        assert parse_channels_arg(empty) is None, (
            f"parse_channels_arg({empty!r}) must be None so the component keeps "
            f"its own default, got {parse_channels_arg(empty)!r}"
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


@th.django_unit_test("DEFAULT_CHANNELS covers every channel the framework publishes to")
def test_default_channels_cover_framework(opts):
    """Drift guard: publish no longer reroutes, so a framework channel missing
    from the default consume list would strand jobs on an unconsumed queue."""
    from mojo.apps.jobs import DEFAULT_CHANNELS

    for channel in ("default", "priority", "cleanup", "incident_handlers",
                    "renditions", "certs", "webhooks", "webhook_fanout"):
        assert channel in DEFAULT_CHANNELS, (
            f"the framework publishes to {channel!r}, so it must be in "
            f"DEFAULT_CHANNELS or an unconfigured deployment strands those jobs"
        )


def _drain(opts, engine, max_jobs=10):
    """Claim and execute everything on this engine's own channels."""
    queue_keys = [opts.keys.queue(ch) for ch in engine.channels]
    executed = []
    while len(executed) < max_jobs:
        target = next((k for k in queue_keys if opts.redis.llen(k) > 0), None)
        if target is None:
            break
        popped = opts.redis.brpop([target], timeout=1)
        if not popped:
            break
        queue_key, job_id = popped
        engine.execute_job(queue_key.split(":")[-1], job_id)
        executed.append(job_id)
    return executed
