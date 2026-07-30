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

TEST_CHANNELS = [CH_FIREWALL, CH_A, CH_B]


def _clear(opts):
    """Reset our channels. Tests share a long-lived DB and Redis, so anything
    this module will create must be deleted before it is created."""
    from mojo.apps.jobs.models import Job

    CALLS.clear()
    for ch in TEST_CHANNELS:
        opts.redis.delete(opts.keys.queue(ch))
        opts.redis.delete(opts.keys.sched(ch))
        opts.redis.delete(opts.keys.sched_broadcast(ch))
        opts.redis.delete(opts.keys.processing(ch))
    Job.objects.filter(channel__in=TEST_CHANNELS).delete()


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
