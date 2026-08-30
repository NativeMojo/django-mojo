"""
Channel health: the /api/jobs/health endpoints and JobManager.get_channel_health().

The bug this module pins down: `get_channel_health()` read `state['stream_length']`
and `state['pending_count']` from `get_queue_state()`, which has returned
`queued_count` / `inflight_count` / `scheduled_count` since the Plan B (list +
ZSET) rewrite. Every call raised KeyError, so both
`GET /api/jobs/health/<channel>` and `GET /api/jobs/health` answered HTTP 400 —
the health surface has never returned a 200.

It also pins the degraded signal: every Redis read on this path swallows its own
exception, so with the counters fixed a total Redis outage would report all-zero
counters, no alerts and `status: 'healthy'` — green while the data source is
down. `get_queue_state()` only reaches `state['metrics']` when its reads
survived, so a missing `metrics` key means exactly "the Redis reads blew up".

The channel used here is deliberately NOT in the testproject's JOBS_CHANNELS, so
no engine, scheduler or reaper touches it and every count is this module's alone.
"""

TESTIT_TIER = "bug"
from testit import helpers as th
import time


ADMIN_USER = "jobs_health_admin"
ADMIN_PWORD = "testit##mojo"

TEST_CHANNEL = "testit_jobs_health"

QUEUED_JOB_ID = "testit-health-queued"
INFLIGHT_JOB_ID = "testit-health-inflight"
STUCK_JOB_ID = "testit-health-stuck"

# _find_stuck_jobs() cuts at now_ms - 60000, and job_engine claims with a
# millisecond score. 120s back is unambiguously past the cutoff.
STUCK_AGE_MS = 120000

MESSAGE_KEYS = ('total', 'unclaimed', 'pending', 'scheduled', 'stuck')


def _clear_channel(opts):
    """Drop every Redis structure the health counters read for TEST_CHANNEL."""
    opts.redis.delete(
        opts.keys.queue(TEST_CHANNEL),
        opts.keys.processing(TEST_CHANNEL),
        opts.keys.sched(TEST_CHANNEL),
        opts.keys.sched_broadcast(TEST_CHANNEL),
    )


@th.django_unit_setup()
def setup_channel_health(opts):
    """Create the admin caller and seed exactly one queued + one in-flight entry."""
    from mojo.apps.account.models import User
    from mojo.apps.jobs.adapters import get_adapter
    from mojo.apps.jobs.keys import JobKeys

    admin = User.objects.filter(username=ADMIN_USER).last()
    if admin is None:
        admin = User(
            username=ADMIN_USER,
            display_name=ADMIN_USER,
            email=f"{ADMIN_USER}@example.com"
        )
        admin.save()
    admin.remove_all_permissions()
    admin.add_permission(["manage_jobs", "view_jobs"])
    admin.is_email_verified = True
    admin.save_password(ADMIN_PWORD)

    opts.redis = get_adapter()
    opts.keys = JobKeys()

    # Delete before creating — this database and Redis index are long-lived.
    _clear_channel(opts)

    opts.redis.rpush(opts.keys.queue(TEST_CHANNEL), QUEUED_JOB_ID)
    # Milliseconds, exactly as job_engine.py claims: a seconds-valued score is
    # ~6 orders of magnitude below the stuck cutoff and would read as stuck.
    opts.redis.zadd(
        opts.keys.processing(TEST_CHANNEL),
        {INFLIGHT_JOB_ID: int(time.time() * 1000)}
    )


@th.django_unit_test("GET /api/jobs/health/<channel> returns the Plan B counters")
def test_channel_health_endpoint_returns_counts(opts):
    """THE regression. Before the fix this endpoint answers 400 for every channel."""
    opts.client.login(ADMIN_USER, ADMIN_PWORD)
    resp = opts.client.get(f"/api/jobs/health/{TEST_CHANNEL}")

    assert resp.status_code == 200, (
        f"Expected 200 from /api/jobs/health/{TEST_CHANNEL}, got "
        f"{resp.status_code}: {resp.json}"
    )

    data = resp.json
    assert data.get('status') is True, \
        f"Expected status true in health response, got {data}"

    health = data['data']
    messages = health['messages']
    expected = {'total': 2, 'unclaimed': 1, 'pending': 1, 'scheduled': 0, 'stuck': 0}
    for key, want in expected.items():
        assert messages.get(key) == want, (
            f"messages[{key!r}] should be {want} for one queued + one in-flight "
            f"entry, got {messages.get(key)!r} (messages={messages})"
        )

    assert health['runners']['active'] == 0, (
        f"{TEST_CHANNEL} is not in JOBS_CHANNELS so no runner serves it; "
        f"got runners={health['runners']}"
    )
    assert health['status'] == 'critical', (
        f"messages present with zero active runners must be critical, got "
        f"{health['status']!r} (alerts={health['alerts']})"
    )
    assert any("No active runners" in alert for alert in health['alerts']), \
        f"Expected a 'No active runners' alert, got {health['alerts']}"


@th.django_unit_test("GET /api/jobs/health returns an overview with aggregate totals")
def test_health_overview_returns_totals(opts):
    """Shape only — the overview covers shared channels, so counts are not ours."""
    opts.client.login(ADMIN_USER, ADMIN_PWORD)
    resp = opts.client.get("/api/jobs/health")

    assert resp.status_code == 200, (
        f"Expected 200 from /api/jobs/health, got {resp.status_code}: {resp.json}"
    )

    data = resp.json
    assert data.get('status') is True, \
        f"Expected status true in overview response, got {data}"

    overview = data['data']
    assert overview['overall_status'] in ('healthy', 'warning', 'critical'), \
        f"Unexpected overall_status {overview['overall_status']!r}"

    totals = overview['totals']
    for key in ('unclaimed', 'pending', 'stuck', 'runners'):
        value = totals.get(key)
        assert isinstance(value, int) and not isinstance(value, bool), \
            f"totals[{key!r}] should be an int, got {value!r}"
        assert value >= 0, f"totals[{key!r}] should be non-negative, got {value}"

    channels = overview['channels']
    assert channels, "Overview must report at least one configured channel"
    for name, health in channels.items():
        assert health.get('status') in ('healthy', 'warning', 'critical'), \
            f"Channel {name!r} has unexpected status {health.get('status')!r}"
        messages = health.get('messages', {})
        assert set(messages.keys()) == set(MESSAGE_KEYS), (
            f"Channel {name!r} messages should carry exactly {MESSAGE_KEYS}, "
            f"got {sorted(messages.keys())}"
        )
        runners = health.get('runners', {})
        for key in ('active', 'total'):
            assert key in runners, \
                f"Channel {name!r} runners missing {key!r}, got {runners}"


@th.django_unit_test("manager.get_channel_health() reports the same counters in-process")
def test_manager_helper_returns_health(opts):
    from mojo.apps.jobs.manager import get_channel_health

    try:
        health = get_channel_health(TEST_CHANNEL)

        assert health['channel'] == TEST_CHANNEL, \
            f"Expected channel {TEST_CHANNEL!r}, got {health.get('channel')!r}"

        messages = health['messages']
        expected = {'total': 2, 'unclaimed': 1, 'pending': 1,
                    'scheduled': 0, 'stuck': 0}
        for key, want in expected.items():
            assert messages.get(key) == want, (
                f"messages[{key!r}] should be {want} in-process, got "
                f"{messages.get(key)!r} (messages={messages})"
            )
    finally:
        opts.redis.delete(
            opts.keys.queue(TEST_CHANNEL),
            opts.keys.processing(TEST_CHANNEL),
        )


@th.django_unit_test("an in-flight entry older than the idle threshold is counted and alerted")
def test_stuck_entry_is_counted_and_alerted(opts):
    from mojo.apps.jobs.manager import get_channel_health

    stale_score = int(time.time() * 1000) - STUCK_AGE_MS
    opts.redis.zadd(opts.keys.processing(TEST_CHANNEL), {STUCK_JOB_ID: stale_score})

    try:
        health = get_channel_health(TEST_CHANNEL)

        assert health['messages']['stuck'] == 1, (
            f"one in-flight entry {STUCK_AGE_MS}ms old must count as stuck, got "
            f"{health['messages']}"
        )
        assert health['stuck_jobs'], \
            f"stuck_jobs should list the stale entry, got {health['stuck_jobs']}"
        assert any("Stuck jobs" in alert for alert in health['alerts']), \
            f"Expected a 'Stuck jobs' alert, got {health['alerts']}"
    finally:
        opts.redis.delete(opts.keys.processing(TEST_CHANNEL))


@th.django_unit_test("channel health goes critical when the Redis state read failed")
def test_degraded_when_channel_state_unavailable(opts):
    """A swallowed Redis failure must not read as a healthy, all-zero channel."""
    from mojo.apps.jobs.manager import JobManager

    class _UnavailableStateManager(JobManager):
        """get_queue_state() whose Redis reads blew up: no 'metrics' key."""

        def get_queue_state(self, channel, *, runners=None):
            return {
                'channel': channel,
                'queued_count': 0,
                'inflight_count': 0,
                'scheduled_count': 0,
                'runners': 0,
            }

    _clear_channel(opts)

    health = _UnavailableStateManager().get_channel_health(TEST_CHANNEL)

    assert health['status'] == 'critical', (
        f"all-zero counters from a failed state read must not report healthy; "
        f"got {health['status']!r} (alerts={health['alerts']})"
    )
    assert any("unavailable" in alert.lower() for alert in health['alerts']), \
        f"Expected an alert naming the unavailable channel state, got {health['alerts']}"
