"""A broadcast that cannot prove its roster says so (item #2729).

Default tier on purpose. The real `get_runners_bounded` can be driven to both
outcomes with nothing but test-owned Redis keys — no module patching, no
process-global mutation — and this is a shared-framework fleet-safety contract,
which is exactly what the default tier is for.

The defect: `get_runners` swallows every error into `[]`, which `publish()`
cannot tell apart from "no runners", so a Redis blip silently turned fleet-wide
work into a unicast. The two cases must now be distinguishable.

Channels are uuid-suffixed and end in '-engine' so they are unique per run and
implicitly allowed by `is_channel_allowed` (the test project runs with
JOBS_ALLOWED_CHANNELS set, i.e. enforcement on).
"""
import time
import uuid

from testit import helpers as th

GHOST = "t2729-ghost-engine"


def noop(job):
    """Resolvable by dotted path — nothing here executes it, but a handler
    that cannot be imported is a trap for the next test that tries."""
    return "ok"


FUNC = "tests.test_jobs.test_broadcast_roster.noop"


def _channel():
    return f"t2729-{uuid.uuid4().hex[:10]}-engine"


def _degraded_events():
    from mojo.apps.incident.models import Event
    from mojo.apps.jobs import DEGRADED_BROADCAST_CATEGORY
    return Event.objects.filter(category=DEGRADED_BROADCAST_CATEGORY)


def _cleanup(channel):
    """Long-lived DB and Redis: delete everything this test will create.

    The suppression key matters most — it has an hour TTL, so a suite re-run
    inside the hour would file no second Event and the assertion would read
    that as a bug rather than as leftover state.
    """
    from mojo.apps import incident
    from mojo.apps.jobs import DEGRADED_BROADCAST_CATEGORY
    from mojo.apps.jobs.keys import JobKeys
    from mojo.apps.jobs.models import Job
    from mojo.helpers.redis import get_connection

    keys = JobKeys()
    client = get_connection()
    client.delete(keys.runner_registry(channel))
    client.delete(keys.queue(channel))
    client.delete(incident.notice_key(DEGRADED_BROADCAST_CATEGORY, channel))
    Job.objects.filter(channel=channel).delete()
    _degraded_events().filter(metadata__channel=channel).delete()


@th.django_unit_test("an unprovable roster is reported, not swallowed")
def test_unreadable_roster_files_an_incident(opts):
    """A registry entry whose heartbeat row is gone — what a SIGKILLed engine
    leaves behind — makes the exact reader raise. The publish must still
    happen, and the degradation must be visible."""
    from mojo.apps import jobs
    from mojo.apps.jobs.keys import JobKeys
    from mojo.apps.jobs.models import Job
    from mojo.helpers.redis import get_connection

    channel = _channel()
    _cleanup(channel)
    try:
        # Declared in the channel index, with no matching heartbeat row.
        get_connection().zadd(
            JobKeys().runner_registry(channel), {GHOST: time.time()})

        result = jobs.publish(FUNC, {"n": 1}, channel=channel, broadcast=True)

        assert isinstance(result, str), (
            f"with no provable roster the job waits on the shared queue, so "
            f"one id is expected, got {type(result).__name__}: {result!r}")
        assert Job.objects.filter(channel=channel).count() == 1, (
            "the work must still be queued — dropping it is worse than "
            "delivering it to one node")
        assert _degraded_events().count() >= 1, (
            "an unprovable roster filed no incident, which is the whole "
            "defect: the fleet silently under-delivers and nobody is told")
    finally:
        _cleanup(channel)


@th.django_unit_test("a genuinely empty roster is not reported as a fault")
def test_empty_roster_is_not_an_incident(opts):
    """The regression guard on the distinction. If a later change collapses
    'no runners' back into 'could not read', this fails."""
    from mojo.apps import jobs
    from mojo.apps.jobs.models import Job

    channel = _channel()
    _cleanup(channel)
    before = _degraded_events().count()
    try:
        # No registry key at all: zrangebyscore returns [], zcount returns 0,
        # and the exact reader returns an empty roster WITHOUT raising.
        result = jobs.publish(FUNC, {"n": 2}, channel=channel, broadcast=True)

        assert isinstance(result, str), (
            f"a broadcast with no runners queues once, got "
            f"{type(result).__name__}: {result!r}")
        assert Job.objects.filter(channel=channel).count() == 1, (
            "an empty roster must behave exactly as before: one queued job")
        assert _degraded_events().count() == before, (
            "an empty fleet is not a fault and must file no incident — "
            "otherwise every idle deployment reports one every cron cycle")
    finally:
        _cleanup(channel)


@th.django_unit_test("broadcast_roster reports whether the roster was proven")
def test_broadcast_roster_reports_exactness(opts):
    """The `exact` flag is what lets publish() log the two cases differently."""
    from mojo.apps import jobs
    from mojo.apps.jobs.keys import JobKeys
    from mojo.helpers.redis import get_connection

    clean = _channel()
    broken = _channel()
    _cleanup(clean)
    _cleanup(broken)
    try:
        rows, exact = jobs.broadcast_roster(clean, FUNC)
        assert rows == [] and exact is True, (
            f"a readable, empty roster must report exact=True, got "
            f"rows={rows!r} exact={exact!r}")

        get_connection().zadd(
            JobKeys().runner_registry(broken), {GHOST: time.time()})
        rows, exact = jobs.broadcast_roster(broken, FUNC)
        assert exact is False, (
            "a roster that could not be proven must report exact=False; "
            "reporting True is how the caller ends up logging the wrong thing")
    finally:
        _cleanup(clean)
        _cleanup(broken)
