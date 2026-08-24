"""Channel routing: publish puts a job on the channel it was given — if the
channel is declared.

The bug this module pins down (django-mojo <= 1.2.61): `publish()` validated the
requested channel against the *publisher's own* `JOBS_CHANNELS` and silently
rerouted anything unlisted to "default" — returning success. That made
cross-box routing impossible: a box could not publish to a channel it does not
itself consume, because the one setting drove both.

The fix is two-layered (items 906 + 936, then 2860): an allowed channel is
routed verbatim, consumed here or not; an UNDECLARED channel — outside
DEFAULT_CHANNELS ∪ JOBS_CHANNELS ∪ JOBS_ALLOWED_CHANNELS, not ending in
'-engine', and not the exact current runner id for immediate work — is refused
with ValueError and a suppressed jobs:rejected_channel incident, instead of
queueing work nothing consumes.

Channels here are prefixed `t906_`/`t936` so they cannot collide with a real
channel or another module's queues. The t906_* names are declared in the
testproject's JOBS_ALLOWED_CHANNELS (allowed-but-not-consumed — the cross-box
shape); the t936_undeclared name is deliberately NOT.
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
CH_UNDECLARED = "t936_undeclared"    # in NO list — publish must refuse it
CH_DECLARED = "t936_allowed"         # in JOBS_ALLOWED_CHANNELS only
CH_BOX_DIRECT = "t936-box-engine"    # implicitly allowed by the suffix
CH_EXPLICIT_RUNNER = "t2860-explicit-runner"

TEST_CHANNELS = [CH_FIREWALL, CH_A, CH_B, CH_SCHED, CH_SEED, CH_ORPHAN,
                 CH_UNDECLARED, CH_DECLARED, CH_BOX_DIRECT]


class _HeartbeatPipeline:
    def __init__(self, raw, ttl, error=None):
        self.raw = raw
        self.heartbeat_ttl = ttl
        self.error = error
        self.commands = []
        self.reset_called = False

    def get(self, key):
        self.commands.append(("get", key))
        return self

    def ttl(self, key):
        self.commands.append(("ttl", key))
        return self

    def execute(self):
        if self.error:
            raise self.error
        return [self.raw, self.heartbeat_ttl]

    def reset(self):
        self.reset_called = True


class _HeartbeatClient:
    def __init__(self, raw, ttl, error=None):
        self.pipe = _HeartbeatPipeline(raw, ttl, error=error)
        self.transactions = []
        self.entered = False
        self.exited = False

    def pipeline(self, transaction=True):
        self.transactions.append(transaction)
        return self.pipe

    def __enter__(self):
        self.entered = True
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.exited = True


def _heartbeat_document(channel, observed, channels=None, runner_id=None):
    import json
    return json.dumps({
        "runner_id": runner_id or channel,
        "channels": channels if channels is not None else [channel],
        "last_heartbeat": observed.isoformat(),
    })


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


def _isolate_orphan_ranking(opts):
    """Drain queued jobs on channels this module does not own.

    `check_unconsumed_channels` files incidents for only the deepest
    `UNCONSUMED_REPORT_LIMIT` (5) channels. Other modules finish leaving jobs
    queued on shared channels — a full-suite run reaches 81 on `default`, 44 on
    `renditions` — which pushes this module's one-job channel out of the
    reported set and fails the assertions below for a reason that has nothing to
    do with the behaviour under test.

    Only unconsumed channels are touched: `find_unconsumed_channels` returns
    channels that no live runner heartbeat claims, so these are inert leftovers
    by definition and draining them cannot strand work a later test is waiting
    on. Modules run serially, so anything seen here belongs to a module that has
    already finished.
    """
    from mojo.apps.jobs import cronjobs
    for channel, _depth in cronjobs.find_unconsumed_channels():
        if channel not in TEST_CHANNELS:
            opts.redis.delete(opts.keys.queue(channel))


def _orphan_ranking():
    """Ranked orphan channels with the report cutoff marked.

    `check_unconsumed_channels` files incidents for only the DEEPEST
    `UNCONSUMED_REPORT_LIMIT` channels and summarises the rest, so a one-job
    test channel is silently dropped whenever the box is carrying enough other
    orphan backlogs — which is the difference between this module passing alone
    and failing in a full-suite run. Naming the competitors turns that from a
    bare "got 0" into something diagnosable.
    """
    from mojo.apps.jobs import cronjobs
    ranked = sorted(cronjobs.find_unconsumed_channels(),
                    key=lambda item: item[1], reverse=True)
    limit = cronjobs.UNCONSUMED_REPORT_LIMIT
    return (f"{len(ranked)} orphan channel(s), report limit {limit} — "
            f"reported={ranked[:limit]} truncated={ranked[limit:]}")


# The tests that pin JOBS_ALLOWED_CHANNELS / JOBS_HOSTNAME_CHANNEL or patch
# scheduler/CLI internals (and their _enforced/_monitor helpers) moved to
# tests/test_jobs_extended_serial/test_channel_routing.py: patching shared
# mojo.apps.jobs module attributes is unsafe under the parallel default tier
# (maestro item #1839).


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


@th.tier("bug")
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


@th.django_unit_test("publish rejects a malformed channel name")
def test_publish_rejects_malformed_channel(opts):
    """A channel becomes a Redis key, a metric slug, a log line and an incident
    title. Nothing clamps it to "default" any more, so the charset is the guard.
    """
    _clear(opts)
    from mojo.apps import jobs
    from mojo.apps.jobs.models import Job

    before = Job.objects.count()
    bad_names = [
        "   ",                       # whitespace-only would mint "queue: "
        "with space",
        "has:colon",                 # engine splits the queue key on ":"
        "line\nbreak",               # would forge log lines
        "glob*star",                 # would match unrelated keys in scans
        "trav/ersal",
        "x" * 101,                   # Job.channel is max_length=100
    ]
    for bad in bad_names:
        try:
            jobs.publish(func=HANDLER, payload={}, channel=bad)
        except ValueError:
            continue
        raise AssertionError(f"publish(channel={bad!r}) must raise ValueError")

    assert Job.objects.count() == before, (
        "a rejected publish must not leave a Job row behind"
    )

    # Everything the framework and the box-direct feature actually use.
    for good in ("default", "webhook_fanout", "t906.dotted", "web-01-engine", "x" * 100):
        jobs.validate_channel_name(good)


@th.django_unit_test("a ScheduledTask cannot be saved with a malformed channel")
def test_scheduled_task_channel_validated(opts):
    """Owners can edit their own task with no jobs permission, and dispatch
    publishes task.channel verbatim — so it must be rejected at write time."""
    _clear(opts)
    from mojo.apps.jobs.models import ScheduledTask

    task = ScheduledTask(name="t906 bad channel", task_type="report",
                         run_times=["09:00"], run_days=[0], channel="has:colon")
    try:
        task.save()
    except ValueError:
        pass
    else:
        ScheduledTask.objects.filter(name="t906 bad channel").delete()
        raise AssertionError(
            "saving a ScheduledTask with a malformed channel must raise ValueError"
        )


@th.django_unit_test("--runner-id is charset-checked before it becomes a path")
def test_parse_runner_id_arg(opts):
    from mojo.apps.jobs.cli import parse_runner_id_arg

    assert parse_runner_id_arg("uploads-engine") == "uploads-engine", \
        "a well-formed runner id should pass through"
    assert parse_runner_id_arg(None) is None, "absent means the engine picks its own"

    # '*' would make the "already running" glob match unrelated engines;
    # '/' would put the pidfile outside /tmp.
    for bad in ("star*", "../../etc/cron.d/x", "has space"):
        try:
            parse_runner_id_arg(bad)
        except ValueError:
            continue
        raise AssertionError(f"parse_runner_id_arg({bad!r}) must raise ValueError")


@th.django_unit_test("live runner proof is one bounded primary-only exact pipeline")
def test_live_runner_channel_lookup_shape(opts):
    from datetime import datetime, timezone as datetime_timezone
    from mojo.apps import jobs
    from mojo.apps.jobs.keys import JobKeys

    now = datetime(2026, 8, 24, 12, 0, tzinfo=datetime_timezone.utc)
    client = _HeartbeatClient(
        _heartbeat_document(CH_EXPLICIT_RUNNER, now), 10)
    connection_args = []

    def connect(**kwargs):
        connection_args.append(kwargs)
        return client

    allowed = jobs._is_live_runner_channel(
        CH_EXPLICIT_RUNNER, connect=connect, now=now)

    assert allowed, (
        "a current positive-TTL heartbeat advertising its exact runner id "
        "must authorize that immediate direct channel"
    )
    assert connection_args == [{
        "timeout": 1.0,
        "max_connections": 1,
        "read_from_replicas": False,
    }], f"live runner proof used the wrong Redis connection: {connection_args!r}"
    assert client.transactions == [False], (
        f"live runner proof must use one nontransactional pipeline, got "
        f"{client.transactions!r}"
    )
    heartbeat_key = JobKeys().runner_hb(CH_EXPLICIT_RUNNER)
    assert client.pipe.commands == [
        ("get", heartbeat_key), ("ttl", heartbeat_key)
    ], f"live runner proof must issue exact GET+TTL, got {client.pipe.commands!r}"
    assert client.pipe.reset_called, "live runner proof did not reset its pipeline"
    assert client.entered and client.exited, (
        "live runner proof did not close its bounded Redis client"
    )


@th.django_unit_test("untrusted runner heartbeat shapes fail closed")
def test_live_runner_channel_lookup_rejects_untrusted_heartbeats(opts):
    import json
    from datetime import datetime, timedelta, timezone as datetime_timezone
    from mojo.apps import jobs

    now = datetime(2026, 8, 24, 12, 0, tzinfo=datetime_timezone.utc)
    current = _heartbeat_document(CH_EXPLICIT_RUNNER, now)
    cases = [
        ("missing", None, -2),
        ("expired", current, 0),
        ("persistent", current, -1),
        ("extended TTL", current, 16),
        ("malformed JSON", "{", 10),
        ("non-object JSON", "[]", 10),
        ("mismatched id", _heartbeat_document(
            CH_EXPLICIT_RUNNER, now, runner_id="some-other-runner"), 10),
        ("malformed channels", json.dumps({
            "runner_id": CH_EXPLICIT_RUNNER,
            "channels": CH_EXPLICIT_RUNNER,
            "last_heartbeat": now.isoformat(),
        }), 10),
        ("direct channel opted out", _heartbeat_document(
            CH_EXPLICIT_RUNNER, now, channels=[CH_DECLARED]), 10),
        ("missing timestamp", json.dumps({
            "runner_id": CH_EXPLICIT_RUNNER,
            "channels": [CH_EXPLICIT_RUNNER],
        }), 10),
        ("stale timestamp", _heartbeat_document(
            CH_EXPLICIT_RUNNER, now - timedelta(seconds=15)), 10),
        ("future-skewed timestamp", _heartbeat_document(
            CH_EXPLICIT_RUNNER, now + timedelta(seconds=15)), 10),
    ]
    for label, raw, ttl in cases:
        client = _HeartbeatClient(raw, ttl)
        allowed = jobs._is_live_runner_channel(
            CH_EXPLICIT_RUNNER, client=client, now=now)
        assert not allowed, f"{label} heartbeat must not authorize direct publish"
        assert client.pipe.reset_called, (
            f"{label} heartbeat did not reset its lookup pipeline"
        )

    unreadable = _HeartbeatClient(
        current, 10, error=RuntimeError("redis unavailable"))
    assert not jobs._is_live_runner_channel(
        CH_EXPLICIT_RUNNER, client=unreadable, now=now
    ), "a Redis read failure must not authorize direct publish"
    assert unreadable.pipe.reset_called, (
        "a failed heartbeat read did not reset its pipeline"
    )


@th.django_unit_test("an unchanged orphan channel is not re-reported every cycle")
def test_unconsumed_alert_is_suppressed_when_unchanged(opts):
    """Nothing drains an orphan queue, so without suppression this cron would
    file an incident every 5 minutes forever."""
    _clear(opts)
    from mojo.apps import jobs
    from mojo.apps.jobs import cronjobs
    from mojo.apps.incident.models import Event

    _isolate_orphan_ranking(opts)
    opts.redis.delete(cronjobs._unconsumed_notice_key(opts.keys, CH_ORPHAN))
    jobs.publish(func=HANDLER, payload={"marker": "orphan"}, channel=CH_ORPHAN)

    cronjobs.check_unconsumed_channels()
    first = Event.objects.filter(category="jobs:unconsumed_channel",
                                 title=_alert_title(CH_ORPHAN)).count()
    assert first == 1, (
        f"the first pass should report the orphan once, got {first}. "
        f"{_orphan_ranking()}")

    cronjobs.check_unconsumed_channels()
    again = Event.objects.filter(category="jobs:unconsumed_channel",
                                 title=_alert_title(CH_ORPHAN)).count()
    assert again == 1, (
        f"an unchanged backlog must not raise a second incident, got {again}"
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

    _isolate_orphan_ranking(opts)
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
        "it is the only thing that surfaces a misrouted publish now. "
        f"{_orphan_ranking()}"
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
